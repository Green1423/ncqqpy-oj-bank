"""Markdown → HTML 片段（题目卡片专用，自包含、只用标准库）。

**为什么自己写**：题目卡片的 HTML 会交给无头 Chromium 截图，因此必须自包含、
不引任何外部资源；而题面来自洛谷与模型，属于**不可信文本** —— 原样透传的标签
会变成图里的任意内容（`<img src=file:///…>` 会被 Chromium 读出来画进去）。这里
只认白名单语法，所有文本先转义、再拼标签。

**输出契约**（下游截图器 `render.py` 依赖，改动前先看调用方）：

- 只返回片段，不包 `<html>` / `<body>`；
- 类名固定为 `code` / `link` / `img-note` / `math-inline` / `math-block`；
- 数学节点**不含文本**，公式全在 `data-tex`，由页面脚本交给 KaTeX；
- 链接与图片的 URL **直接丢弃**：截图时不能发请求，也不能跳转；
- 块级元素之间用换行分隔，便于调试。

**处理顺序就是安全边界**：行内代码 → 数学 → 反斜杠转义 → HTML 转义 →
链接/图片 → 强调。代码与数学先摘成占位符，因此它们里面的 `*`、`_`、`<`
不会被后面的规则二次处理。占位符用私用区字符，解析前会先从原文里删掉，
避免用户伪造占位符去引用内部 stash。
"""
from __future__ import annotations

import re

__all__ = ["escape", "markdown_to_html"]

#: HTML 转义表。必须用 `str.translate` 单趟替换：先 `replace("<", "&lt;")`
#: 再 `replace("&", "&amp;")` 会把刚转出来的 `&` 再转一次（变成 `&amp;lt;`）。
_ESCAPE_TABLE = str.maketrans({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
})

#: 代码 / 数学 / 链接的占位符。私用区字符不会出现在正常文本里，也不会被
#: `str.translate` 改写。
_PH_OPEN = "\ue000"
_PH_CLOSE = "\ue001"
_PH_RE = re.compile(_PH_OPEN + r"(\d+)" + _PH_CLOSE)

#: 围栏开头：` ``` ` 或 `~~~`，后面最多一个语言名（不含空白）。
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s*>[ \t]?(.*)$")
_UL_RE = re.compile(r"^( *)([-*+])[ \t]+(.*)$")
_OL_RE = re.compile(r"^( *)(\d{1,9})[.)][ \t]+(.*)$")

#: 表格分隔行的单元格：`:---` / `:---:` / `---:` / `---`。
#: 至少两个连字符（题面表格常写 `:--:`），避免把 `- ` 当分隔。
_ALIGN_CELL_RE = re.compile(r"^:?-{2,}:?$")

#: 单行行内代码。内容不允许换行，也不允许里面再出现反引号。
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

#: Markdown 反斜杠转义。**只认标点**：`\frac`、`\le`、`\nabla` 这类 LaTeX
#: 命令一个字符都不匹配，反斜杠原样保留。`\n` 也不会被改成换行。
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|$&<>])")

#: 行尾反斜杠是 Markdown 的「硬换行」记号。洛谷题面大量使用
#: （`……$n,k$；\` 换行），我们本来就用 `<br>` 换行，所以把它去掉 ——
#: 否则图上会多出一个莫名其妙的 `\`。
_HARD_BREAK_RE = re.compile(r"(?<!\\)\\$")

#: 行内公式 `$...$`。四条约束缺一不可：
#: ① 开头的 `$` 不是 `\$`（反斜杠转义）也不是 `$$`；
#: ② `$` 后面不是空白，否则 `$ 5` 这种会被当公式；
#: ③ 结尾的 `$` 前面不是空白或反斜杠，否则「价格 $5 和 $10」会被配对；
#: ④ 结尾 `$` 后面不是数字，挡住「价格$5和$10」这种无空格货币写法。
#: 不要求开头 `$` 前面非空白：中文里 `求 $a+b$ 的值` 很常见，卡掉会大面积漏。
#: 内容里不能有占位符：说明代码片段夹在 `$…$` 中间（`$a`x`b$`），这不是公式。
_MATH_INLINE_RE = re.compile(
    r"(?<![\\$])\$(?![\s$])"
    r"([^$\n" + _PH_OPEN + _PH_CLOSE + r"]+?)"
    r"(?<![\s\\])\$(?!\d)")

#: 图片必须先于链接替换，否则 `![alt](url)` 会被链接规则当成 `[alt](url)`。
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

_STRIKE_RE = re.compile(r"~~(?!\s)([^~\n]+?)(?<!\s)~~")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
#: `__bold__` 加字母数字边界：`foo__bar__baz` 不算（和 `_斜体_` 同理）。
_BOLD_U_RE = re.compile(
    r"(?<![A-Za-z0-9_])__(?!\s)([^_\n]+?)(?<!\s)__(?![A-Za-z0-9_])")
#: `*斜体*`：开头 `*` 后不能是空白，结尾 `*` 前不能是空白，且不能挨着另一个
#: `*`（把 `**粗体**` 留给粗体规则）。于是 `a * b` 不会被误伤。
_ITALIC_RE = re.compile(r"(?<!\*)\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?!\*)")
#: `_斜体_` 两侧若是字母/数字就是 `snake_case`，不是强调。
_ITALIC_U_RE = re.compile(
    r"(?<![A-Za-z0-9_])_(?![\s_])([^_\n]+?)(?<![\s_])_(?![A-Za-z0-9_])")


def escape(text: str) -> str:
    """HTML 转义 `& < > " '`。

    Args:
        text: 待转义文本。

    Returns:
        转义后的文本；单趟替换，`&amp;` 不会被二次转义。
    """
    return str(text or "").translate(_ESCAPE_TABLE)


def _math_inline_html(tex: str) -> str:
    """行内公式节点：内容为空，公式只放在 `data-tex` 属性里。"""
    return (f'<span class="math-inline" data-tex="{escape(tex)}" '
            f'data-display="0"></span>')


def _math_block_html(tex: str) -> str:
    """块级公式节点；`data-tex` 里的换行压成空格（KaTeX 不依赖换行）。"""
    flat = " ".join(tex.split("\n")).strip()
    return (f'<div class="math-block" data-tex="{escape(flat)}" '
            f'data-display="1"></div>')


def _restore(text: str, stash: list[str]) -> str:
    """把占位符换回本模块生成的 HTML 片段。"""
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return stash[idx] if 0 <= idx < len(stash) else ""

    return _PH_RE.sub(repl, text)


def _emphasis(text: str) -> str:
    """给**已经转义**的文本套行内强调标签（占位符原样穿过）。"""
    out = _STRIKE_RE.sub(r"<del>\1</del>", text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _BOLD_U_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return _ITALIC_U_RE.sub(r"<em>\1</em>", out)


def _inline(text: str) -> str:
    """单行 Markdown → 行内 HTML：代码/公式先摘，纯文本统一转义。"""
    stash: list[str] = []

    def put(fragment: str) -> str:
        """登记一段自己生成、已转义的 HTML，返回占位符。"""
        stash.append(fragment)
        return f"{_PH_OPEN}{len(stash) - 1}{_PH_CLOSE}"

    # ① 行内代码最先摘：代码里的 `$` 不是公式，`**` 也不是粗体。
    out = _INLINE_CODE_RE.sub(
        lambda m: put(f"<code>{escape(m.group(1))}</code>"), text)
    # ② 数学。此时行内代码已是占位符，其中的 `$` 不会再被看到。
    out = _MATH_INLINE_RE.sub(
        lambda m: put(_math_inline_html(m.group(1))), out)
    # ③ 反斜杠转义。只命中 Markdown 标点，`\frac` / `\le` 原样保留；
    #    转义出来的字面字符还要再过一次 HTML 转义（`\&` → `&amp;`）。
    out = _ESCAPE_RE.sub(lambda m: put(escape(m.group(1))), out)
    # ④ 剩下的才是纯文本，统一转义。占位符是私用区字符，不受影响。
    out = escape(out)
    # ⑤ 图片、链接：URL 直接丢弃，结果立刻占位 —— 否则后面的强调规则可能
    #    跨过 `</span>` 配对星号，产出错位的标签。
    out = _IMAGE_RE.sub(
        lambda m: put('<span class="img-note">[图片：'
                      f'{_restore(m.group(1), stash)}]</span>'), out)
    # 链接文字**先套强调、再还原占位符**：反过来的话，代码内容已经变成
    # `<code>**x**</code>`，强调规则会把代码里的星号当真格式。
    out = _LINK_RE.sub(
        lambda m: put('<span class="link">'
                      f'{_restore(_emphasis(m.group(1)), stash)}</span>'), out)
    # ⑥ 强调。全部在已转义文本上做，插入的标签只可能来自本模块。
    out = _emphasis(out)
    # ⑦ 还原。`re.sub` 不会重扫替换串，所以链接文字里的占位符已在 ⑤ 还原。
    return _restore(out, stash)


def _is_fence_close(line: str, fence: str) -> bool:
    """是否是 `fence` 的收尾行（同种字符，长度不短于开头）。"""
    body = line.strip()
    return (len(body) >= len(fence) and body[0] == fence[0]
            and body == fence[0] * len(body))


def _render_code_block(body: list[str], lang: str) -> str:
    """围栏代码块：内容只做 HTML 转义，不做任何行内解析。"""
    attr = f' data-lang="{escape(lang)}"' if lang else ""
    code = escape("\n".join(body))
    return f'<pre class="code"{attr}><code>{code}</code></pre>'


def _math_block_at(lines: list[str], i: int) -> tuple[str, int, str] | None:
    r"""识别块级公式，返回 `(公式内容, 下一行号, 收尾行余下的文字)`。

    洛谷的块级公式**不保证** `$$` 独占一行，实测长这样：

        $$\def\arraystretch{1.2}
        \begin{array}{|c|c|}
        … \end{array}$$

    所以不能只认「单独一行的 `$$`」：开头 `$$` 后面可以直接跟内容，收尾 `$$`
    可以在任意一行的中间/末尾；收尾后面的文字要交回调用方继续当正文解析
    （直接丢掉就是「公式后面的句子整段消失」）。
    """
    stripped = lines[i].strip()
    if not stripped.startswith("$$"):
        return None
    # 同一行就收尾：`$$a+b$$` / `$$a$$ 后面还有字`（trailing 交回调用方）
    if len(stripped) > 2:
        pos = stripped.find("$$", 2)
        if pos >= 0:
            return stripped[2:pos], i + 1, stripped[pos + 2:]
    body: list[str] = [stripped[2:]]
    j = i + 1
    while j < len(lines):
        pos = lines[j].find("$$")
        if pos >= 0:
            body.append(lines[j][:pos])
            return "\n".join(body), j + 1, lines[j][pos + 2:]
        body.append(lines[j])
        j += 1
    return None


def _split_row(line: str) -> list[str]:
    """按未转义的 `|` 切表格行，去掉首尾竖线。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip().replace("\\|", "|")
            for cell in re.split(r"(?<!\\)\|", body)]


def _table_aligns(sep_line: str) -> list[str] | None:
    """解析表格分隔行；每列返回 `left` / `center` / `right` / 空串。"""
    cells = _split_row(sep_line)
    if not cells or not all(_ALIGN_CELL_RE.match(cell) for cell in cells):
        return None
    aligns: list[str] = []
    for cell in cells:
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif left:
            aligns.append("left")
        elif right:
            aligns.append("right")
        else:
            aligns.append("")
    return aligns


def _table_at(
    lines: list[str], i: int,
) -> tuple[list[str], list[str], list[list[str]], int] | None:
    """识别 GFM 表格，返回 `(表头, 对齐, 数据行, 下一行号)`。"""
    if (i + 1 >= len(lines) or "|" not in lines[i]
            or "|" not in lines[i + 1]):
        return None
    header = _split_row(lines[i])
    aligns = _table_aligns(lines[i + 1])
    if not aligns or not header or len(header) != len(aligns):
        return None
    if not any(cell for cell in header):
        return None
    rows: list[list[str]] = []
    j = i + 2
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        cells = _split_row(lines[j])
        rows.append((cells + [""] * len(header))[:len(header)])
        j += 1
    return header, aligns, rows, j


def _render_table(header: list[str], aligns: list[str],
                  rows: list[list[str]]) -> str:
    """渲染表格；对齐方式写进每个 `th` / `td` 的 `style`。"""
    def cell(tag: str, text: str, align: str) -> str:
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{_inline(text)}</{tag}>"

    head = "".join(cell("th", text, align)
                   for text, align in zip(header, aligns))
    body = "".join(
        "<tr>" + "".join(cell("td", text, align)
                         for text, align in zip(row, aligns)) + "</tr>"
        for row in rows)
    return (f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table>")


def _list_entry(line: str) -> tuple[int, bool, str] | None:
    """拆一行列表项，返回 `(缩进, 是否有序, 内容)`。"""
    m = _UL_RE.match(line)
    if m:
        return len(m.group(1)), False, m.group(3)
    m = _OL_RE.match(line)
    if m:
        return len(m.group(1)), True, m.group(3)
    return None


def _render_list(entries: list[tuple[int, bool, str]]) -> str:
    """渲染列表；缩进比基准多 2 空格以上的项算一层嵌套。"""
    base = min(indent for indent, _, _ in entries)
    out: list[str] = []
    top_tag = ""
    nested_tag = ""
    li_open = False
    for indent, ordered, text in entries:
        tag = "ol" if ordered else "ul"
        if indent >= base + 2 and li_open:
            if nested_tag != tag:
                if nested_tag:
                    out.append(f"</{nested_tag}>")
                out.append(f"<{tag}>")
                nested_tag = tag
            out.append(f"<li>{_inline(text)}</li>")
            continue
        if nested_tag:
            out.append(f"</{nested_tag}>")
            nested_tag = ""
        if li_open:
            out.append("</li>")
            li_open = False
        if top_tag != tag:
            if top_tag:
                out.append(f"</{top_tag}>")
            out.append(f"<{tag}>")
            top_tag = tag
        out.append(f"<li>{_inline(text)}")
        li_open = True
    if nested_tag:
        out.append(f"</{nested_tag}>")
    if li_open:
        out.append("</li>")
    if top_tag:
        out.append(f"</{top_tag}>")
    return "".join(out)


def _is_block_start(lines: list[str], i: int) -> bool:
    """这一行是否会打断段落（收集段落时用来提前收尾）。"""
    line = lines[i]
    if _FENCE_RE.match(line) or _HEADING_RE.match(line):
        return True
    if _HR_RE.match(line) or _QUOTE_RE.match(line):
        return True
    if _list_entry(line) is not None:
        return True
    if _table_at(lines, i) is not None:
        return True
    return _math_block_at(lines, i) is not None


def markdown_to_html(text: str) -> str:
    """把 Markdown 子集转成 HTML 片段（不包 `<html>` / `<body>`）。

    Args:
        text: 不可信 Markdown 文本；其中的 HTML 一律当普通文字处理。

    Returns:
        HTML 片段，块级元素以换行分隔；空输入返回空串。
    """
    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 占位符是私用区字符：分两步从原文里删掉，防止用户伪造占位符去引用内部
    # stash（stash 里虽然也只有本模块生成的转义 HTML）。先删完整的
    # 「开标记 + 数字 + 闭标记」，再删零散的标记字符，避免留下一个孤零零的数字。
    body = _PH_RE.sub("", body)
    body = body.replace(_PH_OPEN, "").replace(_PH_CLOSE, "")
    lines = body.split("\n")

    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker, lang = fence.group(1), fence.group(2)
            i += 1
            code_lines: list[str] = []
            while i < n and not _is_fence_close(lines[i], marker):
                code_lines.append(lines[i])
                i += 1
            i += 1                  # 跳过收尾围栏；没写收尾就吃到文件尾
            out.append(_render_code_block(code_lines, lang))
            continue

        math_block = _math_block_at(lines, i)
        if math_block is not None:
            tex, nxt, trailing = math_block
            out.append(_math_block_html(tex))
            if trailing.strip():
                # 收尾 `$$` 后面还有正文 → 原样插回去继续解析，别吞掉
                lines.insert(nxt, trailing)
                n += 1
            i = nxt
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            inner = _inline(heading.group(2).strip())
            out.append(f"<h{level}>{inner}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        table = _table_at(lines, i)
        if table is not None:
            header, aligns, rows, i = table
            out.append(_render_table(header, aligns, rows))
            continue

        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < n:
                qm = _QUOTE_RE.match(lines[i])
                if qm is None:
                    break
                quoted.append(qm.group(1))
                i += 1
            inner = "<br>".join(_inline(part) for part in quoted)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        if _list_entry(line) is not None:
            entries: list[tuple[int, bool, str]] = []
            while i < n:
                entry = _list_entry(lines[i])
                if entry is None:
                    break
                entries.append(entry)
                i += 1
            out.append(_render_list(entries))
            continue

        paragraph = [line]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines, i):
            paragraph.append(lines[i])
            i += 1
        inner = "<br>".join(
            _inline(_HARD_BREAK_RE.sub("", part)) for part in paragraph)
        out.append(f"<p>{inner}</p>")

    return "\n".join(out)

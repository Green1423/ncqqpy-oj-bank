"""题目出图：Markdown → 自包含 HTML → PNG（无头 Chromium，两趟渲染）。

为什么用 Chromium 而不是画图库：这台机器上没有 PIL/Pillow，但装了 Chromium
和 Noto Sans CJK SC。把排版交给真正的排版引擎，中文换行、标点挤压、代码块、
表格、KaTeX 公式都不用自己写。**代价是必须两趟**：

1. 第一趟 `--dump-dom`：页面脚本量出真实高度，写进 `document.title`（`H=1234`）；
2. 第二趟 `--screenshot --window-size=W,H`：按量到的高度截图。

一趟做不到自适应高度：`--window-size` 就是最终图像尺寸，页面里的
`window.resizeTo()` 在无头模式下**被忽略**（实测：要 600×900 就给 600×900，
底部一大片空白）。

安全：题面来自洛谷/AI，属于不可信文本。`markdown.py` 已经把所有 HTML 转义，
这里**不往页面里放任何外部资源**（KaTeX 走插件自带的那份），并且 Chromium
带 `--disable-background-networking` —— 截图时不会去联网，也就没有 SSRF。
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .markdown import markdown_to_html

from napcat.persist import prune_keep

# ⚠️ Chromium 的定位/启动/超时统一由框架提供（`napcat.render`）。老进程里
# 还没有这个模块，直接 import 会把插件打挂 → 拿不到就按路径加载同一份实现。
try:
    from napcat.render import (RenderError, find_chromium, file_uri)
    from napcat.render import run_chromium as _run_chromium
except ImportError:
    import importlib.util as _ilu
    import sys as _sys

    _spec = _ilu.spec_from_file_location(
        "napcat_render_boot_oj",
        Path(__file__).resolve().parents[2] / "napcat" / "render.py")
    _mod = _ilu.module_from_spec(_spec)
    _sys.modules["napcat_render_boot_oj"] = _mod
    _spec.loader.exec_module(_mod)
    RenderError = _mod.RenderError
    find_chromium = _mod.find_chromium
    file_uri = _mod.file_uri
    _run_chromium = _mod.run_chromium

_file_uri = file_uri

_HEIGHT_RE = re.compile(r"H=(\d+)")


@dataclass
class RenderOptions:
    """出图参数（`config.json` → `plugins.oj_bank.render`）。"""

    #: 图片宽度（像素）
    width: int = 760
    #: 单次渲染超时（秒）
    timeout: float = 45.0
    #: 图片最大高度（超长题面会被截断，防止出 2 万像素的图）
    max_height: int = 8000
    #: 目录里最多留几张图（旧的自动删，防止写满磁盘）
    keep_files: int = 120
    #: Chromium 路径；空 = 自动探测
    chromium: str = ""
    #: 虚拟时间预算（毫秒）：等字体/KaTeX 就绪，给太小会截断
    virtual_time_budget: int = 4000


_KATEX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "katex")


def katex_assets() -> tuple[str, str]:
    """返回 `(css_uri, js_uri)`；插件里没带 KaTeX 时返回空串（公式退化成源码）。"""
    css = os.path.join(_KATEX_DIR, "katex.min.css")
    js = os.path.join(_KATEX_DIR, "katex.min.js")
    if os.path.exists(css) and os.path.exists(js):
        return _file_uri(css), _file_uri(js)
    return "", ""


def _css(width: int) -> str:
    """整页样式。只描述结构，不做花哨设计（题面要的是清楚，不是好看）。"""
    return f"""
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #f5f6f8; }}
    body {{
      font-family: "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
                   sans-serif;
      -webkit-font-smoothing: antialiased;
      color: #1f2328;
    }}
    .wrap {{
      width: {int(width)}px; margin: 0 auto; padding: 22px 26px 18px;
      background: #ffffff;
    }}
    .head {{ border-bottom: 2px solid #e5e7eb; padding-bottom: 10px; }}
    .title {{ font-size: 26px; font-weight: 700; line-height: 1.3; }}
    .title .no {{ color: #6b7280; font-weight: 600; margin-right: 8px; }}
    .meta {{ margin-top: 8px; font-size: 14px; color: #4b5563; }}
    .badge {{
      display: inline-block; padding: 1px 8px; margin-right: 8px;
      border-radius: 10px; background: #eef2ff; color: #4338ca; font-size: 13px;
    }}
    h2.sec {{
      font-size: 19px; font-weight: 700; margin: 20px 0 8px;
      padding-left: 9px; border-left: 4px solid #4f46e5;
    }}
    .body {{ font-size: 16px; line-height: 1.75; word-wrap: break-word; }}
    .body p {{ margin: 8px 0; }}
    .body h1, .body h2, .body h3, .body h4 {{
      font-size: 17px; margin: 14px 0 6px;
    }}
    .body ul, .body ol {{ margin: 6px 0; padding-left: 24px; }}
    .body li {{ margin: 2px 0; }}
    .body blockquote {{
      margin: 8px 0; padding: 6px 12px; color: #4b5563;
      border-left: 3px solid #d1d5db; background: #f9fafb;
    }}
    code, pre.code, pre.sample {{
      font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Liberation Mono",
                   monospace;
      font-size: 14px;
    }}
    /* ⚠️ 行内代码的浅底色**只能给「不在 pre 里」的 code**：
       `.body code` 会连代码块里的 <code> 一起命中，于是每行都铺一条浅色横条，
       浅字叠在浅底上 → 代码块看起来是一堆空白条（实测踩过）。 */
    .body :not(pre) > code {{ background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }}
    pre.code {{
      background: #0f172a; color: #e2e8f0; padding: 10px 14px; border-radius: 8px;
      overflow-x: auto; line-height: 1.55; margin: 8px 0;
    }}
    pre.code code {{ background: transparent; color: inherit; padding: 0; }}
    pre.sample {{
      background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px;
      padding: 10px 14px; margin: 6px 0; white-space: pre-wrap;
      word-break: break-all; line-height: 1.55;
    }}
    table {{ border-collapse: collapse; margin: 10px 0; font-size: 15px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 5px 10px; }}
    th {{ background: #f3f4f6; }}
    .math-block {{ margin: 10px 0; text-align: center; overflow-x: auto; }}
    .math-inline {{ display: inline-block; vertical-align: baseline; }}
    .foot {{ margin-top: 18px; font-size: 12px; color: #9ca3af; }}
    hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 14px 0; }}
    """


#: 页面脚本：先渲染公式，再量高度写进 title。
#: ⚠️ 量高度必须等 `document.fonts.ready` —— 字体没到位时量出来的高度偏小，
#: 截图就会把最后几行截掉（实测）。
_MEASURE_JS = """
(function () {
  function renderMath() {
    if (typeof katex === 'undefined') { return; }
    var nodes = document.querySelectorAll('[data-tex]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var tex = el.getAttribute('data-tex') || '';
      var display = el.getAttribute('data-display') === '1'
                    || el.classList.contains('math-block');
      try {
        katex.render(tex, el, {displayMode: display, throwOnError: false,
                               strict: false});
      } catch (err) {
        el.textContent = tex;      // 渲染失败就显示源码，别让公式整段消失
      }
    }
  }
  function measure() {
    var h = Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0
    );
    document.title = 'H=' + Math.ceil(h + 6);
  }
  function run() {
    renderMath();
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
      document.fonts.ready.then(function () { setTimeout(measure, 30); })
                        .catch(function () { setTimeout(measure, 30); });
      // 字体接口挂住时的兜底
      setTimeout(function () {
        if (document.title.indexOf('H=') !== 0) { measure(); }
      }, 2500);
    } else {
      setTimeout(measure, 30);
    }
  }
  if (document.readyState === 'complete') { run(); }
  else { window.addEventListener('load', run); }
})();
"""


def _esc(text: str) -> str:
    """短文本转义（长文本走 `markdown_to_html`，那里自己转义）。"""
    return html.escape(str(text or ""), quote=True)


def _section(title: str, body_html: str) -> str:
    """一个带标题的区块（body_html 已经是可信 HTML）。"""
    return f'<h2 class="sec">{_esc(title)}</h2>\n<div class="body">{body_html}</div>\n'


def build_problem_page(problem, *, cases: int = 0, width: int = 760) -> str:
    """把一道题拼成整页 HTML（纯函数，方便单测断言）。"""
    from .repo import source_label
    meta_bits = [_esc(source_label(problem))]
    if problem.difficulty:
        from .repo import difficulty_label
        meta_bits.append(_esc(difficulty_label(problem.difficulty)))
    meta_bits.append(f"{int(problem.time_limit_ms or 0)} ms")
    meta_bits.append(f"{int(problem.memory_limit_mb or 0)} MB")
    if cases:
        meta_bits.append(f"{int(cases)} 组测试点")

    parts = [
        '<div class="head">',
        f'<div class="title"><span class="no">#{int(problem.id)}</span>'
        f'{_esc(problem.title)}</div>',
        '<div class="meta">' + "".join(
            f'<span class="badge">{bit}</span>' for bit in meta_bits) + "</div>",
        "</div>",
    ]
    if (problem.statement or "").strip():
        parts.append(_section("题目描述", markdown_to_html(problem.statement)))
    if (problem.input_format or "").strip():
        parts.append(_section("输入格式", markdown_to_html(problem.input_format)))
    if (problem.output_format or "").strip():
        parts.append(_section("输出格式", markdown_to_html(problem.output_format)))
    if (problem.sample_input or "").strip():
        parts.append('<h2 class="sec">输入样例</h2>\n<pre class="sample">'
                     f"{_esc(problem.sample_input)}</pre>")
    if (problem.sample_output or "").strip():
        parts.append('<h2 class="sec">输出样例</h2>\n<pre class="sample">'
                     f"{_esc(problem.sample_output)}</pre>")
    if (problem.note or "").strip():
        parts.append(_section("注意事项", markdown_to_html(problem.note)))
    parts.append('<div class="foot">ncqqpy · oj_bank · '
                 f'{_esc(time.strftime("%Y-%m-%d %H:%M"))}</div>')
    return _page("".join(parts), width=width)


def build_set_page(group, rows: list[tuple[int, object]],
                   *, width: int = 760) -> str:
    """题组页：标题 + 描述 + 题目清单（`rows` 是 `(序号, Problem|None)`）。"""
    lines = []
    for ordinal, problem in rows:
        if problem is None:
            lines.append(f"{ordinal}. （已删除的题目）")
        else:
            from .repo import difficulty_label
            bits = [f"{ordinal}. #{problem.id} {problem.title}"]
            if problem.difficulty:
                bits.append(difficulty_label(problem.difficulty))
            lines.append(" · ".join(bits))
    body = markdown_to_html("\n".join(lines) if lines else "（题组还是空的）")
    description = (markdown_to_html(group.description)
                   if (group.description or "").strip() else "")
    parts = [
        '<div class="head">',
        f'<div class="title">题组 · {_esc(group.title or group.name)}</div>',
        f'<div class="meta"><span class="badge">{len(rows)} 题</span>'
        f'<span class="badge">{_esc(group.name)}</span></div>',
        "</div>",
    ]
    if description:
        parts.append(_section("说明", description))
    parts.append(_section("题目清单", body))
    parts.append('<div class="foot">ncqqpy · oj_bank · '
                 f'{_esc(time.strftime("%Y-%m-%d %H:%M"))}</div>')
    return _page("".join(parts), width=width)


def _page(inner: str, *, width: int) -> str:
    """页面外壳（KaTeX 资源、样式、量高脚本）。"""
    css_uri, js_uri = katex_assets()
    head = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
            f"<style>{_css(width)}</style>"]
    if css_uri:
        head.append(f'<link rel="stylesheet" href="{css_uri}">')
    if js_uri:
        head.append(f'<script src="{js_uri}"></script>')
    head.append("</head><body>")
    return ("".join(head) + f'<div class="wrap">{inner}</div>'
            f"<script>{_MEASURE_JS}</script></body></html>")


class ProblemRenderer:
    """把题目渲染成 PNG。同一时刻只跑一次 Chromium（连开几个会互相抢内存）。"""

    def __init__(self, options: Optional[RenderOptions] = None, *,
                 out_dir: str, logger=None) -> None:
        self.opt = options or RenderOptions()
        # 图片要按 `file://` 发给 QQ，只有**绝对路径**才能转成 URI
        # （`Path.as_uri()` 对相对路径直接抛 ValueError，出图链路全挂）。
        self.out_dir = os.path.abspath(out_dir)
        self.logger = logger
        self._lock = asyncio.Semaphore(1)

    # ---- 对外 ----
    async def render_problem(self, problem, *, cases: int = 0) -> str:
        """渲染一道题，返回 PNG 路径。"""
        doc = build_problem_page(problem, cases=cases, width=self.opt.width)
        return await self.render_html(doc, name=f"p{int(problem.id)}")

    async def render_set(self, group, rows) -> str:
        """渲染题组页，返回 PNG 路径。"""
        doc = build_set_page(group, rows, width=self.opt.width)
        return await self.render_html(doc, name=f"set-{group.name}")


    async def render_html(self, doc: str, *, name: str = "page") -> str:
        """整页 HTML → PNG（两趟截图）。"""
        chrome = find_chromium(self.opt.chromium)
        if not chrome:
            raise RenderError(
                "找不到 Chromium（Ubuntu：apt install chromium-browser；"
                "或在 config.json 的 render.chromium 里写绝对路径）")
        os.makedirs(self.out_dir, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name))[:40] or "page"
        stamp = int(time.time() * 1000)
        html_path = os.path.join(self.out_dir, f"{stem}-{stamp}.html")
        png_path = os.path.join(self.out_dir, f"{stem}-{stamp}.png")
        profile = tempfile.mkdtemp(prefix="ojprof-", dir=self.out_dir)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(doc)
        try:
            async with self._lock:
                height = await self._measure(chrome, html_path, profile)
                await self._shoot(chrome, html_path, png_path, profile, height)
        finally:
            for path in (html_path,):
                try:
                    os.remove(path)
                except OSError:
                    pass
            shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
            raise RenderError("Chromium 没有产出图片（可能被沙箱限制）")
        self.prune()
        return png_path

    # ---- Chromium ----
    async def _measure(self, chrome: str, html_path: str, profile: str) -> int:
        """第一趟：让页面把真实高度写进 title，再取回来。"""
        out, err = await self._run_chrome(
            chrome, [f"--virtual-time-budget={int(self.opt.virtual_time_budget)}",
                     "--dump-dom", _file_uri(html_path)], profile)
        match = _HEIGHT_RE.search(out or "")
        if not match:
            # 量不到就用保守高度：图能出，只是可能留白/截断，比直接失败好
            if self.logger:
                self.logger.warning("[oj_bank] 出图高度探测失败：%s",
                                    (err or out or "")[-200:])
            return min(int(self.opt.max_height), 1400)
        return max(80, min(int(match.group(1)), int(self.opt.max_height)))

    async def _shoot(self, chrome: str, html_path: str, png_path: str,
                     profile: str, height: int) -> None:
        """第二趟：按量到的高度截图。"""
        await self._run_chrome(
            chrome, [f"--virtual-time-budget={int(self.opt.virtual_time_budget)}",
                     f"--screenshot={png_path}", "--hide-scrollbars",
                     f"--window-size={int(self.opt.width)},{int(height)}",
                     _file_uri(html_path)], profile)

    async def _run_chrome(self, chrome: str, args: list[str],
                          profile: str) -> tuple[str, str]:
        """跑一次 Chromium（参数走 argv，不拼 shell，带超时）。"""
        _rc, out, err = await _run_chromium(
            chrome, args, profile=profile, timeout=float(self.opt.timeout))
        return out, err

    # ---- 清理 ----
    def prune(self) -> None:
        """目录里只留最近 `keep_files` 张图（防止出图把磁盘写满）。"""
        prune_keep(self.out_dir, keep=int(self.opt.keep_files or 1),
                   skip_prefix=("ojprof-",))


__all__ = ["RenderError", "RenderOptions", "ProblemRenderer", "build_problem_page",
           "build_set_page", "find_chromium", "katex_assets"]

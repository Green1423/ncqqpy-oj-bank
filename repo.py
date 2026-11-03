"""oj_bank 的数据访问层：命令与工具都只走这里，不直接摸模型。

为什么要单独一层：

* **编号解析只有一处**：用户可能写 `12`（题库编号）、`P1001`（洛谷题号）、
  `luogu:P1001`（去重键）—— 三种口径如果散落在各命令里，迟早出现
  「/oj show 12 能看、/oj submit 12 说没有」这种事；
* **写操作的约束只有一处**：测试点最多 10 组、题组名唯一、题组成员有序，
  这些校验放在命令里就会漏（AI 工具也写库）；
* 命令与工具都能被单测直接调用，不需要起客户端。

模型字段的语义见 `models.py`。所有函数都是**同步**的（sqlite 单次读写是毫秒级，
与框架 `napcat.store` 的口径一致），异步调用方直接调即可。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

from .models import (MAX_HACK_CASES, Problem, ProblemSet, SetItem, Submission,
                     TestCase, now)

#: 题组名：字母数字、下划线、连字符、汉字（长度 1..32）
SET_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,32}$")
#: 题库编号（纯数字，1 起）
_ID_RE = re.compile(r"^\d{1,9}$")


class OJError(Exception):
    """题库操作失败（人话说明原因，命令层直接回给用户）。"""


# --------------------------------------------------------------------------
# 编号
# --------------------------------------------------------------------------
def luogu_key(pid: str) -> str:
    """洛谷题号的去重键：`P1001` → `luogu:P1001`。"""
    return f"luogu:{str(pid or '').strip().upper()}"


def title_key(title: str) -> str:
    """AI/手写题目的去重键：标题哈希。

    用哈希而不是 `slugify(title)`：中文标题没法 slug，转拼音又要引依赖；
    哈希的代价是「改标题 = 一道新题」，但 AI 重试保存时标题不变，正好去重。
    """
    digest = hashlib.sha1(str(title or "").strip().encode("utf-8")).hexdigest()
    return f"ai:{digest[:10]}"


def parse_ident(raw: Any) -> tuple[str, Any]:
    """把用户/模型给的编号解析成 `("id", 12)` 或 `("key", "luogu:P1001")`。

    Raises:
        OJError: 既不是题库编号也不是去重键。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise OJError("没给题目编号（题库编号或洛谷题号都行）")
    if _ID_RE.match(text):
        return "id", int(text)
    if text.startswith(("luogu:", "ai:")):
        return "key", text
    if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", text):
        return "key", luogu_key(text)
    raise OJError(f"看不懂的题目编号：{text}（用题库编号如 12，或洛谷题号如 P1001）")


def get_problem(raw: Any) -> Optional[Problem]:
    """按题库编号 / 去重键取题目；取不到返回 None（不抛异常）。"""
    kind, value = parse_ident(raw)
    if kind == "id":
        return Problem.objects.filter(pk=value).first()
    return Problem.objects.filter(key=value).first()


def require_problem(raw: Any) -> Problem:
    """同 `get_problem`，取不到抛 `OJError`（命令层直接回给用户）。"""
    found = get_problem(raw)
    if found is None:
        raise OJError(f"题库里没有这道题：{raw}（用 /oj list 看编号）")
    return found


# --------------------------------------------------------------------------
# 题目
# --------------------------------------------------------------------------
_PROBLEM_FIELDS = (
    "key", "title", "source", "source_id", "url", "difficulty", "tags",
    "statement", "input_format", "output_format", "sample_input",
    "sample_output", "note", "time_limit_ms", "memory_limit_mb",
)


#: 「数据范围」段落的开头（标题 / 对于…数据 / 保证… / 约束…）
_RANGE_LEAD = re.compile(
    r"^\s*(?:#{1,6}\s*|[>\-*]\s*|\*\*)*"
    r"(?:【[^】]{0,12}数据[^】]{0,12}】|数据(?:范围|规模|约定|约束|限制)\s*[:：]?|"
    r"对于|保证|其中|约束|限制)", re.I)
_RANGE_HEAD = re.compile(r"(数据范围|数据规模|数据约定|数据约束|数据限制)")
#: 没有标题的「裸约束」（`$n \le 10^5$`）：AI 存题时常这么写
_RANGE_COMPARE = re.compile(r"(≤|≥|<=|>=|\\le|\\ge|\\leq|\\geq)")
#: 明显是「提示/题解/广告/样例程序」的段落：题库里只留数据范围
_HINT_MARK = re.compile(
    r"(提示|思路|解法|算法|复杂度|贪心|证明|实现|建议|做法|题解|广告|"
    r"样例程序|样例代码|判定核心)")


def data_range_only(text: str) -> str:
    """只保留标题里的**数据范围/约束**，丢掉提示、题解、广告、样例代码。

    洛谷的「提示」页会混着广告、多语言样例程序、做法讲解；存进题库只会让
    题目卡片变长、把关键数据范围淹掉。规则：

    - 按空行切段（围栏代码块直接整段丢掉）；
    - 段落必须以「数据范围 / 对于…数据 / 保证 / 约束」之类开头；
    - 段落里带「提示 / 思路 / 贪心 / 复杂度 / 广告 …」且较长 → 丢；
    - 没有数据范围的题返回空串（宁缺毋滥）。

    Args:
        text: 原始「注意事项 / 提示」文本。

    Returns:
        只剩数据范围的文本（可能为空），最长 2000 字。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""
    paragraphs: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in raw.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            continue
        if in_fence:
            continue
        if not line.strip():
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        paragraphs.append("\n".join(buf))

    kept: list[str] = []
    for para in paragraphs:
        text = para.strip()
        if not text or "```" in text or len(text) > 600:
            continue
        if _HINT_MARK.search(text) and len(text) > 80:
            continue
        head = text.lstrip("*_ ")
        if not _RANGE_LEAD.match(head):
            if not (len(text) <= 120 and _RANGE_COMPARE.search(text)
                    and re.search(r"\d", text)):
                continue
        if not re.search(r"\d", text) and not _RANGE_HEAD.search(head):
            continue
        kept.append(text)
    return "\n".join(kept)[:2000].strip()


def sanitize_notes() -> int:
    """把库里题目的「注意事项」收敛成「只剩数据范围」，返回改动题数。"""
    changed = 0
    for problem in Problem.objects.all():
        clean = data_range_only(problem.note)
        if clean != problem.note:
            problem.note = clean
            problem.save()
            changed += 1
    return changed


def _clean_problem_fields(data: dict) -> dict:
    """只取模型认识的字段，并做长度/类型收敛（模型给的东西不可全信）。"""
    out: dict[str, Any] = {}
    for name in _PROBLEM_FIELDS:
        if name not in data:
            continue
        value = data[name]
        if name in ("difficulty", "time_limit_ms", "memory_limit_mb"):
            try:
                out[name] = int(value)
            except (TypeError, ValueError):
                continue
        elif name == "note":
            # 只存数据范围：提示/题解/广告一律不进题库
            out[name] = data_range_only("" if value is None else str(value))
        else:
            out[name] = "" if value is None else str(value)
    return out


def upsert_problem(data: dict, *, cases: Optional[list[dict]] = None,
                   replace_cases: bool = False,
                   hack_limit: int = MAX_HACK_CASES) -> tuple[Problem, bool]:
    """新建或更新一道题。

    去重键取 `data["key"]`，没给就按标题算 `ai:<hash>`。已存在时**覆盖**
    题目字段（题面更新、洛谷改了题面都走这里），并按 `replace_cases` 决定
    测试点是替换还是追加。

    Args:
        data: 题目字段（见 `_PROBLEM_FIELDS`）。
        cases: 测试点列表，每项 `{"input"/"stdin", "output"/"expected",
            "note", "weight", "kind"}`。
        replace_cases: True = 先删掉同 kind 的旧测试点再写。
        hack_limit: 测试点上限。

    Returns:
        `(problem, created)`。

    Raises:
        OJError: 标题为空、或测试点超过上限。
    """
    fields = _clean_problem_fields(dict(data or {}))
    title = str(fields.get("title") or "").strip()
    if not title:
        raise OJError("题目没有标题")
    fields["title"] = title[:200]
    key = str(fields.pop("key", "") or "").strip() or title_key(title)
    fields["key"] = key

    existing = Problem.objects.filter(key=key).first()
    created = existing is None
    stamp = now()
    if created:
        problem = Problem(**{"source": "local", "source_id": "", "url": "",
                             "difficulty": 0, "tags": "", "statement": "",
                             "input_format": "", "output_format": "",
                             "sample_input": "", "sample_output": "",
                             "note": "", "time_limit_ms": 1000,
                             "memory_limit_mb": 256, **fields,
                             "created_at": stamp, "updated_at": stamp})
        problem.save()
    else:
        problem = existing
        for name, value in fields.items():
            setattr(problem, name, value)
        problem.updated_at = stamp
        problem.save()

    if cases is not None:
        add_cases(problem, cases, replace=replace_cases, hack_limit=hack_limit)
    return problem, created


def update_problem(problem: Problem, **fields) -> Problem:
    """只改给定字段（不碰测试点），返回同一个对象。"""
    for name, value in _clean_problem_fields(fields).items():
        setattr(problem, name, value)
    problem.updated_at = now()
    problem.save()
    return problem


def delete_problem(problem: Problem) -> int:
    """删题 + 它的测试点；返回删掉的测试点数量。

    提交记录**不删**：它们是榜单的历史，删题后再建同编号的题只是新题，
    老提交不该被悄悄抹掉（题组里按 `problem_id` 引用，编号不会再复用）。
    """
    removed = TestCase.objects.filter(problem_id=problem.id).count()
    for case in list(TestCase.objects.filter(problem_id=problem.id)):
        case.delete()
    problem.delete()
    return removed


def list_problems(keyword: str = "", *, limit: int = 10,
                  offset: int = 0) -> tuple[list[Problem], int]:
    """按关键词（标题/来源/去重键）模糊查题，返回 `(本页, 总数)`。"""
    qs = Problem.objects.all()
    text = str(keyword or "").strip()
    if text:
        qs = qs.filter(title__icontains=text)
        rows = list(qs.order_by("-id"))
        # 标题没命中就再按来源/题号找一遍（SQL 的 OR 在 ORM 里没抽象，两次查更直白）
        if not rows:
            rows = [p for p in Problem.objects.order_by("-id")
                    if text.lower() in f"{p.source} {p.source_id} {p.key}".lower()]
        total = len(rows)
        page = rows[offset:offset + limit]
        return page, total
    total = Problem.objects.count()
    page = list(Problem.objects.order_by("-id")[offset:offset + limit])
    return page, total


# --------------------------------------------------------------------------
# 测试点
# --------------------------------------------------------------------------
def _case_row(problem_id: int, ordinal: int, case: dict, kind: str) -> dict:
    stdin = case.get("input", case.get("stdin", ""))
    expected = case.get("output", case.get("expected", ""))
    try:
        weight = max(1, int(case.get("weight") or 1))
    except (TypeError, ValueError):
        weight = 1
    return {
        "problem_id": problem_id,
        "ordinal": ordinal,
        "kind": kind if kind in ("hack", "sample") else "hack",
        "stdin": "" if stdin is None else str(stdin),
        "expected": "" if expected is None else str(expected),
        "note": str(case.get("note") or "")[:200],
        "weight": weight,
    }


def add_cases(problem: Problem, cases: Iterable[dict], *,
              kind: str = "hack", replace: bool = False,
              hack_limit: int = MAX_HACK_CASES) -> int:
    """给题目追加测试点，返回写入数量。

    Args:
        problem: 目标题目。
        cases: 每项 `{"input"/"stdin", "output"/"expected", "note", "weight"}`；
            也接受 `kind` 覆盖本次的 kind。
        kind: 默认种类（hack / sample）。
        replace: True = 先删掉同 kind 的旧测试点（重新生成一组时用）。
        hack_limit: 测试点上限（默认 10，题面要求）。

    Raises:
        OJError: 追加后会超过上限（**整批不写**，不做「写一半」）。
    """
    rows = [c for c in (cases or []) if isinstance(c, dict)]
    if not rows:
        return 0
    kind = kind if kind in ("hack", "sample") else "hack"

    # 先把每项的实际种类算出来，再判上限：**整批不写**比「写一半再报错」好，
    # 否则模型会以为保存成功了（它只看得到异常文本，看不到库里已经有 7 组）。
    kinds = [str(c.get("kind") or kind) for c in rows]
    kinds = [k if k in ("hack", "sample") else kind for k in kinds]

    if replace:
        for old in list(TestCase.objects.filter(problem_id=problem.id, kind=kind)):
            old.delete()

    existing = TestCase.objects.filter(problem_id=problem.id, kind="hack").count()
    hack_new = sum(1 for k in kinds if k == "hack")
    if existing + hack_new > hack_limit:
        raise OJError(
            f"测试点最多 {hack_limit} 组（现在 {existing} 组，"
            f"这次要加 {hack_new} 组）—— 先 /oj cases {problem.id} clear 再重加")

    ordinal = TestCase.objects.filter(problem_id=problem.id).count()
    for case, row_kind in zip(rows, kinds):
        ordinal += 1
        TestCase(**_case_row(problem.id, ordinal, case, row_kind)).save()
    _reindex_tail(problem.id)
    return len(rows)


def _reindex_tail(problem_id: int) -> None:
    """按创建顺序重排序号（1..N）—— 删除后不留空档，用户看到的「第几组」连续。"""
    rows = list(TestCase.objects.filter(problem_id=problem_id).order_by("id"))
    for index, row in enumerate(rows, start=1):
        if row.ordinal != index:
            row.ordinal = index
            row.save()


def cases_of(problem_id: int, *, kind: str = "") -> list[TestCase]:
    """取题目的测试点（按序号）；`kind` 给了就只取该种类。"""
    qs = TestCase.objects.filter(problem_id=problem_id)
    if kind:
        qs = qs.filter(kind=kind)
    return list(qs.order_by("ordinal"))


def update_case(case: TestCase, *, stdin: str, expected: str,
                note: str = "") -> None:
    """就地改一组测试点（`/oj case` 重做单组用）。"""
    case.stdin = str(stdin or "")
    case.expected = str(expected or "")
    case.note = str(note or "")[:200]
    case.save()


def clear_cases(problem: Problem, *, kind: str = "") -> int:
    """清空测试点（`kind` 给了只清该种类），返回删除数量。"""
    qs = TestCase.objects.filter(problem_id=problem.id)
    if kind:
        qs = qs.filter(kind=kind)
    rows = list(qs)
    for row in rows:
        row.delete()
    return len(rows)


# --------------------------------------------------------------------------
# 文本展示（命令 / AI 工具共用）
# --------------------------------------------------------------------------
DIFFICULTY_LABEL = {
    0: "未知", 1: "入门", 2: "普及-", 3: "普及/提高-", 4: "普及+/提高",
    5: "提高+/省选-", 6: "省选/NOI-", 7: "NOI/NOI+/CTSC",
}


def difficulty_label(level: int) -> str:
    """难度数字 → 人话标签。"""
    try:
        return DIFFICULTY_LABEL.get(int(level), "未知")
    except (TypeError, ValueError):
        return "未知"


def source_label(problem: Problem) -> str:
    """来源的一行文字：洛谷题显示「洛谷 P1001」，其它来源用 `source_id` 原文。

    AI 搜来的题不一定来自洛谷（Codeforces / AtCoder / 教材 / 自拟），`source_id`
    存的就是那段自由文本，展示时直接用；只有完全没标出处时才回落到种类名。
    """
    sid = str(problem.source_id or "").strip()
    if problem.source == "luogu":
        return f"洛谷 {sid}" if sid else "洛谷"
    if sid:
        return sid
    return {"ai": "AI 搜题", "local": "本地"}.get(problem.source,
                                                  problem.source or "本地")


def one_line(problem: Problem, *, cases: int = -1) -> str:
    """列表里的一行：`#12 P1001 A+B Problem · 洛谷 · 入门 · 10 组测试点`。"""
    if cases < 0:
        cases = TestCase.objects.filter(problem_id=problem.id).count()
    bits = [f"#{problem.id}", problem.title, source_label(problem)]
    if problem.difficulty:
        bits.append(difficulty_label(problem.difficulty))
    bits.append(f"{cases} 组测试点")
    return " · ".join(bits)


def summary_text(problem: Problem) -> str:
    """给 AI 工具看的题目摘要（自包含，模型不需要再查库）。"""
    cases = cases_of(problem.id)
    hack = sum(1 for c in cases if c.kind == "hack")
    lines = [
        f"#{problem.id} {problem.title}",
        f"来源：{source_label(problem)}"
        f" · 难度：{difficulty_label(problem.difficulty)}"
        f" · 时限：{problem.time_limit_ms}ms · 内存：{problem.memory_limit_mb}MB"
        f" · 测试点：{len(cases)} 组",
    ]
    if problem.note:
        lines.append(f"注意事项：{problem.note[:300]}")
    return "\n".join(lines)


def full_text(problem: Problem) -> str:
    """题目全文（文本形态；出图用 `render.py`，这里给 AI 和排查用）。"""
    cases = cases_of(problem.id)
    blocks = [summary_text(problem), "", "## 题干", problem.statement or "（空）"]
    if problem.input_format:
        blocks += ["", "## 输入格式", problem.input_format]
    if problem.output_format:
        blocks += ["", "## 输出格式", problem.output_format]
    if problem.sample_input or problem.sample_output:
        blocks += ["", "## 输入样例", "```", problem.sample_input, "```",
                   "## 输出样例", "```", problem.sample_output, "```"]
    if cases:
        blocks += ["", f"## 测试点（{len(cases)} 组）"]
        for case in cases:
            blocks.append(f"{case.ordinal}. [{case.kind}] {case.note or '（无说明）'}"
                          f" · 输入 {len(case.stdin)} 字 · 期望输出 {len(case.expected)} 字")
    return "\n".join(blocks)


# --------------------------------------------------------------------------
# 题组
# --------------------------------------------------------------------------
def normalize_set_name(raw: Any) -> str:
    """题组名规范化 + 校验（唯一）。

    Raises:
        OJError: 名字非法。
    """
    name = str(raw if raw is not None else "").strip()
    if not SET_NAME_RE.match(name):
        raise OJError("题组名只能用字母、数字、下划线、连字符、汉字（1..32 字）")
    return name


def create_set(name: str, *, title: str = "", description: str = "",
               owner: str = "") -> tuple[ProblemSet, bool]:
    """建题组；已存在则只更新标题/描述。返回 `(set, created)`。"""
    name = normalize_set_name(name)
    existing = ProblemSet.objects.filter(name=name).first()
    if existing is not None:
        changed = False
        if title:
            existing.title = title[:120]
            changed = True
        if description:
            existing.description = description
            changed = True
        if changed:
            existing.save()
        return existing, False
    group = ProblemSet(name=name, title=(title or name)[:120],
                       description=description or "", owner=str(owner or ""),
                       created_at=now())
    group.save()
    return group, True


def get_set(name: str) -> Optional[ProblemSet]:
    """按名字取题组；没有返回 None。"""
    text = str(name or "").strip()
    if not text:
        return None
    return ProblemSet.objects.filter(name=text).first()


def require_set(name: str) -> ProblemSet:
    """按名字取题组，没有就抛 `OJError`。"""
    found = get_set(name)
    if found is None:
        raise OJError(f"没有这个题组：{name}（/oj set list 看全部）")
    return found


def list_sets() -> list[tuple[ProblemSet, int]]:
    """所有题组 + 每题组里的题目数，按名字排序。"""
    out = []
    for group in ProblemSet.objects.order_by("name"):
        count = SetItem.objects.filter(set_name=group.name).count()
        out.append((group, count))
    return out


def set_problems(name: str) -> list[tuple[SetItem, Optional[Problem]]]:
    """题组成员（有序）——题目被删了也给 `None` 占位，选题时能看出「有个洞」。"""
    items = list(SetItem.objects.filter(set_name=name).order_by("ordinal"))
    out = []
    for item in items:
        problem = Problem.objects.filter(pk=item.problem_id).first()
        out.append((item, problem))
    return out


def add_to_set(name: str, problems: Iterable[Problem]) -> tuple[int, int]:
    """把题目追加进题组（已存在就跳过），返回 `(新加, 跳过)`。"""
    require_set(name)
    current = {item.problem_id for item in
               SetItem.objects.filter(set_name=name)}
    ordinal = SetItem.objects.filter(set_name=name).count()
    added = skipped = 0
    for problem in problems:
        if problem.id in current:
            skipped += 1
            continue
        ordinal += 1
        SetItem(set_name=name, problem_id=problem.id, ordinal=ordinal).save()
        current.add(problem.id)
        added += 1
    return added, skipped


def remove_from_set(name: str, problems: Iterable[Problem]) -> int:
    """把题目移出题组，返回移除数量；之后重排序号。"""
    require_set(name)
    removed = 0
    for problem in problems:
        for item in list(SetItem.objects.filter(set_name=name,
                                                problem_id=problem.id)):
            item.delete()
            removed += 1
    items = list(SetItem.objects.filter(set_name=name).order_by("ordinal"))
    for index, item in enumerate(items, start=1):
        if item.ordinal != index:
            item.ordinal = index
            item.save()
    return removed


def delete_set(name: str) -> int:
    """删题组（连同成员），返回删掉的成员数。"""
    group = require_set(name)
    items = list(SetItem.objects.filter(set_name=group.name))
    for item in items:
        item.delete()
    group.delete()
    return len(items)


# --------------------------------------------------------------------------
# 提交与榜单
# --------------------------------------------------------------------------
def record_submission(*, problem: Problem, user_id: int, group_id: int,
                      language: str, code: str, status: str, score: int,
                      detail: list) -> Submission:
    """记一次判题结果。"""
    row = Submission(problem_id=problem.id, user_id=int(user_id or 0),
                     group_id=int(group_id or 0), language=language,
                     code=code or "", status=status, score=int(score),
                     detail=detail or [], created_at=now())
    row.save()
    return row


def submissions_of(problem: Problem, *, user_id: int = 0,
                   limit: int = 10) -> list[Submission]:
    """某题最近的提交（可按用户过滤），新的在前。"""
    qs = Submission.objects.filter(problem_id=problem.id)
    if user_id:
        qs = qs.filter(user_id=int(user_id))
    return list(qs.order_by("-id")[:limit])


def best_scores(problem_ids: Iterable[int]) -> dict[int, dict[int, int]]:
    """`{user_id: {problem_id: 历史最高分}}` —— 榜单的唯一数据源。"""
    ids = [int(x) for x in problem_ids]
    if not ids:
        return {}
    out: dict[int, dict[int, int]] = {}
    for row in Submission.objects.filter(problem_id__in=ids):
        per_user = out.setdefault(int(row.user_id), {})
        old = per_user.get(int(row.problem_id), -1)
        if int(row.score) > old:
            per_user[int(row.problem_id)] = int(row.score)
    return out


def rank_set(name: str, *, limit: int = 10) -> list[dict]:
    """题组榜单：每人每题取最高分，按总分降序（同分按 AC 数、再按最后提交时间）。

    Returns:
        `[{"user_id", "score", "total", "solved", "details": {pid: score}}]`。
        没交过的题目按 0 分计入总分（分母固定，避免「只交简单题」刷榜）。
    """
    require_set(name)
    problems = [p for _item, p in set_problems(name) if p is not None]
    total = 100 * len(problems)          # 每题满分 100，没交按 0 计（分母固定）
    scores = best_scores([p.id for p in problems])
    rows = []
    for user_id, per in scores.items():
        got = sum(per.values())
        solved = sum(1 for value in per.values() if value >= 100)
        rows.append({"user_id": user_id, "score": got, "total": total,
                     "solved": solved, "details": dict(per)})
    rows.sort(key=lambda r: (-r["score"], -r["solved"], r["user_id"]))
    return rows[:limit]


__all__ = [
    "OJError", "add_cases", "add_to_set", "best_scores", "cases_of",
    "clear_cases", "create_set", "delete_problem", "delete_set",
    "difficulty_label", "full_text", "get_problem", "get_set", "list_problems",
    "list_sets", "luogu_key", "one_line", "parse_ident", "rank_set",
    "record_submission", "remove_from_set", "require_problem", "require_set",
    "set_problems", "source_label", "submissions_of", "summary_text", "title_key",
    "update_problem", "upsert_problem",
]

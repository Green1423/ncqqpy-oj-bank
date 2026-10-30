"""给 AI 用的工具（`register_tool`）—— **结果回给模型**，由模型组织答复。

为什么是工具而不是技能（`register_skill`）：技能的本质是「命令映射」，参数只能
走**命令行**（`--statement='…'`），会经过命令解析器的引号/转义处理 —— 题面里的
`\\frac`、`\\n`、代码块里的反斜杠会被改坏，10 组测试点这种结构化数据更是没法塞。
工具直接收 **JSON Schema 参数**，`statement` 原样进库，`testcases` 是数组。

工具清单：

| 工具 | 干什么 |
|------|--------|
| `oj_bank.save_problem` | 把 AI 搜到/整理好的题目 + 10 组测试点入库 |
| `oj_bank.cases`        | 给已有题目补/换测试点（洛谷抓来的题只有样例） |
| `oj_bank.luogu`        | 按题号抓洛谷题目 |
| `oj_bank.get_problem`  | 按编号取题目全文（模型回答「题库里那道题」时用） |
| `oj_bank.list`         | 列出题库 |

所有工具都先过**服务范围白名单**（`ctx` 里的群/用户）：`permission`/`scope` 只能
表达「超管/群聊」这种粗粒度，业务白名单只有插件自己知道。
"""
from __future__ import annotations

import json
from typing import Any

from .repo import (OJError, add_cases, cases_of, full_text, list_problems,
                   one_line, require_problem, upsert_problem)

#: 测试点 JSON Schema（工具参数里复用）
_CASE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {"type": "string",
                  "description": "这一组的输入（原样喂给程序；空串表示无输入）"},
        "output": {"type": "string",
                   "description": "这一组的标准输出（必须准确，评分逐字节比对，"
                                  "忽略行尾空格与末尾空行）"},
        "note": {"type": "string",
                 "description": "这一组的用途（常规 / 边界 / 特殊结构 / 大规模），如「n=10^9」「负数」「空输入」「链退化成菊花」"},
        "weight": {"type": "integer", "minimum": 1, "maximum": 100,
                   "description": "权重（默认 1；想加重某组就调大）"},
    },
    "required": ["input", "output"],
}

#: 测试点上限（与题面要求一致：10 组）
MAX_CASES = 10


def register(plugin) -> None:
    """把 AI 工具挂到 `plugin` 上。"""
    reg = plugin.register_tool

    # ------------------------------------------------------------------
    @reg(
        "save_problem",
        description=(
            "把一道题保存进本地题库。用户让你搜题 / 找题，或你自己整理出了一道题时"
            "都要调用它。题目可以来自任何地方（洛谷、Codeforces、AtCoder、LeetCode、"
            "教材、面试题、你自己出的题），不是洛谷题也能存 —— 不要因为「不是洛谷题」"
            "就不存，也不要去找等价的洛谷题。\n"
            "最少给 title（题名）+ statement（题干）；建议同时给："
            "input_format、output_format、sample_input、sample_output、"
            "note（注意事项 / 数据范围）、source（出处，如「Codeforces 1234E」）。\n"
            f"测试点用 testcases 传（每组 input/output，最多 {MAX_CASES} 组）；"
            "一次凑不齐也没关系，先存题，之后用 oj_bank_cases 补。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "题名（必填）"},
                "statement": {"type": "string",
                              "description": "题干（必填；Markdown，数学用 $...$）"},
                "input_format": {"type": "string", "description": "输入格式"},
                "output_format": {"type": "string", "description": "输出格式"},
                "sample_input": {"type": "string", "description": "输入样例（原样文本）"},
                "sample_output": {"type": "string", "description": "输出样例（原样文本）"},
                "note": {"type": "string",
                         "description": "**只写数据范围**（如 `1≤n≤10^5`、`|a_i|≤10^9`）。"
                                        "不要写提示/题解/样例代码/广告 —— 入库时会自动"
                                        "过滤掉，只保留数据范围段"},
                "difficulty": {"type": "integer", "minimum": 0, "maximum": 7,
                               "description": "难度：1 入门 … 7 NOI/CTSC，0 未知"},
                "tags": {"type": "string", "description": "标签，逗号分隔"},
                "source": {"type": "string",
                           "description": "出处，如「洛谷 P1001」「Codeforces 1234E」「AI 自拟」"},
                "url": {"type": "string", "description": "原题链接（没有就留空）"},
                "time_limit_ms": {"type": "integer", "minimum": 100, "maximum": 20000,
                                  "description": "时限（毫秒），默认 1000"},
                "memory_limit_mb": {"type": "integer", "minimum": 16, "maximum": 1024,
                                    "description": "内存上限（MB），默认 256"},
                "testcases": {
                    # 不要求：入库后会自动让 AI 生成一套（更可靠）。这里只在
                    # 用户已经明确给出测试数据时才填。
                    "type": "array", "maxItems": MAX_CASES,
                    "items": _CASE_ITEM_SCHEMA,
                    "description": "已有的测试数据（可留空；留空时入库后自动生成）",
                },
            },
            "required": ["title", "statement"],
        },
        scope="all",
        max_chars=3000,
        prompt_hint=(
            "用户让你搜题、找题、出题，或说「存进题库」「记下来」「收藏这道题」时：找到题目后先调 oj_bank_save_problem 存进题库，再回答（来源不限：洛谷 / Codeforces / LeetCode / 教材 / 你自己出的题都行）。"
        ),
    )
    async def save_problem(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        data = _as_dict(args)
        title = str(data.get("title") or "").strip()
        statement = str(data.get("statement") or "").strip()
        if not title or not statement:
            return ("❌ 入库失败：至少要有 title（题名）和 statement（题干）。"
                    "其余建议一起给：input_format、output_format、sample_input、"
                    "sample_output、note、source；测试点用 testcases 或之后再补。")

        cases = _clean_cases(data.get("testcases"))
        # 必填字段无条件写；**可选字段只在给了值时才写** —— 模型第二次保存时
        # 常常省略 note/tags，直接把空串写进去会把上一次的内容抹掉（实测：
        # 重存一道题之后「注意事项」整段消失）。
        fields: dict[str, Any] = {
            "key": "", "title": title,
            "source": "ai", "source_id": str(data.get("source") or "")[:64],
            "statement": statement,
            "input_format": data.get("input_format") or "",
            "output_format": data.get("output_format") or "",
            "sample_input": data.get("sample_input") or "",
            "sample_output": data.get("sample_output") or "",
            "time_limit_ms": _clamp(data.get("time_limit_ms"), 100, 20000, 1000),
            "memory_limit_mb": _clamp(data.get("memory_limit_mb"), 16, 1024, 256),
        }
        for key in ("url", "tags", "note"):
            value = str(data.get(key) or "").strip()
            if value:
                fields[key] = value
        if data.get("difficulty") is not None:
            fields["difficulty"] = _clamp(data.get("difficulty"), 0, 7, 0)
        try:
            problem, created = upsert_problem(
                fields, cases=cases or None, replace_cases=bool(cases),
                hack_limit=plugin.max_cases)
        except OJError as e:
            return f"❌ 入库失败：{e}"

        # 把「还缺什么」点名告诉模型 —— 它就靠这句决定下一步补什么
        missing: list[str] = []
        if not (problem.input_format or "").strip():
            missing.append("输入格式 input_format")
        if not (problem.output_format or "").strip():
            missing.append("输出格式 output_format")
        if not (problem.sample_input or "").strip():
            missing.append("输入样例 sample_input")
        if not (problem.sample_output or "").strip():
            missing.append("输出样例 sample_output")
        if not (problem.note or "").strip():
            missing.append("注意事项 note")
        if not (problem.source_id or "").strip():
            missing.append("出处 source")

        scheduled = plugin.schedule_generation(problem, ctx=ctx)
        hack = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        origin = problem.source_id or "未标出处"
        lines = [f"{'已入库' if created else '已更新'}：#{problem.id} {problem.title}",
                 f"来源：{origin} · 题干 {len(problem.statement)} 字 · "
                 f"测试点 {hack}/{plugin.max_cases} 组"]
        if missing:
            lines.append("还缺：" + "、".join(missing))
        if hack < plugin.max_cases:
            if scheduled:
                lines.append(f"🧠 正在用 AI 生成 {plugin.max_cases} 组测试点，"
                             "完成后会发到本聊天（不用再手写 testcases）")
            else:
                lines.append(f"测试点还差 {plugin.max_cases - hack} 组："
                             f"调 oj_bank_generate_cases（problem_id={problem.id}）"
                             "让 AI 自动生成，或自己用 oj_bank_cases 写")
        lines.append(f"出图：用户发 /oj show {problem.id}；"
                     f"交题：/oj submit {problem.id} cpp（或 c / py）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    @reg(
        "cases",
        description=(
            "给题库里已有的题目补 / 换测试点（任何来源的题都能补：AI 搜的题、"
            "洛谷抓的题、手写题）。每组要有 input 与精确的 output；评分逐字节比对。"
            f"最多 {MAX_CASES} 组，一次补几组也行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "problem_id": {"type": "integer",
                               "description": "题库编号（题目入库时返回的 #编号）"},
                "cases": {"type": "array", "maxItems": MAX_CASES,
                          "items": _CASE_ITEM_SCHEMA,
                          "description": f"测试点（每题最多 {MAX_CASES} 组测试点）"},
                "replace": {"type": "boolean",
                            "description": "true = 先清掉旧测试点再写（默认 false 追加）"},
            },
            "required": ["problem_id", "cases"],
        },
        scope="all",
        max_chars=2000,
        prompt_hint=(
            "题库里的题缺测试点时用 oj_bank_cases 补测试点（最多 10 组，可以分几次补）。"
        ),
    )
    async def add_test_cases(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        data = _as_dict(args)
        try:
            problem = require_problem(data.get("problem_id"))
        except OJError as e:
            return f"❌ {e}"
        cases = _clean_cases(data.get("cases"))
        if not cases:
            return ("❌ 没给测试点：cases 至少要有 1 组，每组 `input` 与 `output` "
                    "（output 要精确，评分逐字节比对）")
        try:
            written = add_cases(problem, cases, kind="hack",
                                replace=bool(data.get("replace")),
                                hack_limit=plugin.max_cases)
        except OJError as e:
            return f"❌ {e}"
        hack = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        tail = ("" if hack >= plugin.max_cases
                else f"；还差 {plugin.max_cases - hack} 组才算齐")
        return (f"✅ #{problem.id} {problem.title}：写入 {written} 组，"
                f"现在测试点 {hack}/{plugin.max_cases} 组{tail}")

    # ------------------------------------------------------------------
    @reg(
        "luogu",
        description=("**只在用户明确给出洛谷题号**（P1001 / CF1A / 题目链接）时用它，"
                     "按题号抓题入库（题干/格式/样例/时限内存）。"
                     "其它来源（其它 OJ、教材、AI 自拟）一律用 oj_bank_save_problem。"
                     "抓完只有官方样例，要评分还需要用 oj_bank_cases 补测试点。"),
        parameters={
            "type": "object",
            "properties": {
                "pid": {"type": "string",
                        "description": "洛谷题号，如 P1001 / CF1A；也可以给题目链接"},
                "overwrite": {"type": "boolean",
                              "description": "题库里已有同题时是否按洛谷题面覆盖"},
            },
            "required": ["pid"],
        },
        scope="all",
        max_chars=2000,
        prompt_hint=(
            "用户明确给出洛谷题号（如 P1001、题目链接）时用 oj_bank_luogu 抓题；其它来源的题一律用 oj_bank_save_problem。"
        ),
    )
    async def luogu(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        data = _as_dict(args)
        return await plugin.import_luogu(str(data.get("pid") or ""),
                                         overwrite=bool(data.get("overwrite")),
                                         ctx=ctx)

    # ------------------------------------------------------------------
    @reg(
        "generate_cases",
        description=(
            "让 AI 给题库里的题**自动出测试点**（推荐先用这个，而不是自己手写）。"
            "插件会**一次只要一组**（1 份输入 + 1 份输出），重复到凑满 N 组；"
            "重复的输入会去重，某一组失败只补那一组；"
            "如果 AI 判断这题是**固定输出**（无输入），不会生成额外测试点。"
            "生成在后台跑：工具立刻返回，完成后结果会发到本聊天。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "problem_id": {"type": "integer",
                               "description": "题库编号（#编号）"},
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_CASES,
                          "description": f"本次最多补几组（默认补满缺口，最多 {MAX_CASES}）"},
                "replace": {"type": "boolean",
                            "description": "已废弃：现在只补缺口，不会覆盖已有测试点"},
            },
            "required": ["problem_id"],
        },
        scope="all",
        max_chars=2000,
        prompt_hint=(
            "题库里的题没有测试点、或做不了评分时，用 oj_bank_generate_cases "
            "让 AI 逐组生成（一次一组、含输入+输出；后台跑，完成后会发消息）。"
        ),
    )
    async def generate_cases(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        data = _as_dict(args)
        try:
            problem = require_problem(data.get("problem_id"))
        except OJError as e:
            return f"❌ {e}"
        # 只补缺口：已有几组就只补到 10 组，不覆盖已有的
        existing = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        need = plugin.max_cases - existing
        if need <= 0:
            return (f"✅ #{problem.id} 已有 {existing} 组测试点，不用再补。"
                    "要重做某一组用 /oj case <编号> <组号>；"
                    "整题重来先 /oj cases <编号> clear")
        want = _clamp(data.get("count"), 1, MAX_CASES, need)
        want = max(1, min(need, want))
        if not plugin.schedule_generation(problem, ctx=ctx, force=True,
                                          count=want, replace=False,
                                          base=existing):
            return ("⚠️ 没配置用于出题的模型（config：gen / coach / ai_chat.llm）")
        return (f"⏳ 已有 {existing}/{plugin.max_cases} 组，开始补齐 {want} 组"
                "（一次一组、只补缺的），完成后会发到本聊天。")

    # ------------------------------------------------------------------
    @reg(
        "get_problem",
        description=("按编号取题库里一道题的全文（题干/格式/样例/注意事项/测试点概况）。"
                     "用户问「题库里那道题」时先用它拿准确内容，不要凭记忆复述。"),
        parameters={
            "type": "object",
            "properties": {
                "ident": {"type": "string",
                          "description": "题库编号（如 12）或洛谷题号（如 P1001）"},
            },
            "required": ["ident"],
        },
        scope="all",
        max_chars=3500,
        prompt_hint=(
            "用户问「题库里那道题」时，先用 oj_bank_get_problem 取原文再回答，不要凭记忆复述。"
        ),
    )
    async def get_one(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        try:
            problem = require_problem(_as_dict(args).get("ident"))
        except OJError as e:
            return f"❌ {e}"
        return full_text(problem)

    # ------------------------------------------------------------------
    @reg(
        "list",
        description="列出题库里的题目（可按关键词过滤）。用户问「题库里有什么题」时用。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词（标题/题号），可空"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "最多几条，默认 8"},
            },
        },
        scope="all",
        max_chars=2000,
        prompt_hint=(
            "用户问题库里有哪道题时用 oj_bank_list。"
        ),
    )
    async def list_all(args: dict, ctx=None):
        if not plugin.tool_access_ok(ctx):
            return _denied(ctx)
        data = _as_dict(args)
        rows, total = list_problems(str(data.get("keyword") or ""),
                                    limit=_clamp(data.get("limit"), 1, 20, 8))
        if not rows:
            return "题库里没有匹配的题目"
        lines = [f"题库共 {total} 题，这里列出 {len(rows)} 题："]
        lines.extend(one_line(p) for p in rows)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 参数收敛
# --------------------------------------------------------------------------
_DENIED = "⛔ 当前聊天没有开通题库服务。"


def _denied(ctx=None) -> str:
    """服务范围外时回给模型的话 —— 要**直接能照做**（带群号/QQ 与命令）。"""
    if ctx is None:
        return _DENIED
    if getattr(ctx, "in_group", False):
        gid = int(getattr(ctx, "group_id", 0) or getattr(ctx, "chat_id", 0) or 0)
        return (f"⛔ 本群（{gid}）没有开通题库服务：让管理员在私聊发 "
                f"/oj bind add group {gid} 开通后重试。")
    uid = int(getattr(ctx, "user_id", 0) or 0)
    return (f"⛔ 你（{uid}）还没有开通题库服务：让管理员在私聊发 "
            f"/oj bind add user {uid} 开通后重试。")


def _as_dict(args: Any) -> dict:
    """工具参数可能是 dict / JSON 字符串 / None，统一成 dict。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip().startswith("{"):
        try:
            data = json.loads(args)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _clamp(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clean_cases(raw: Any) -> list[dict]:
    """把模型给的测试点收敛成 repo 认识的结构。

    模型偶尔会把 `output` 写成 `expected`、或把整组当成字符串，这里全部纠正，
    免得「保存成功了但一组测试点都没写进去」。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            out.append({"input": item, "output": "", "note": "", "kind": "hack"})
            continue
        if not isinstance(item, dict):
            continue
        stdin = item.get("input", item.get("stdin", item.get("in", "")))
        expected = item.get("output", item.get("expected", item.get("out", "")))
        out.append({
            "input": "" if stdin is None else str(stdin),
            "output": "" if expected is None else str(expected),
            "note": str(item.get("note") or item.get("hint") or "")[:200],
            "weight": _clamp(item.get("weight"), 1, 100, 1),
            "kind": "hack",
        })
    return out


__all__ = ["register"]

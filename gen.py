"""让 AI 出测试点：**一次一组**（1 份输入 + 1 份输出），重复 N 次。

为什么一组一组要
----------------
一次要 10 组会让模型生成一整篇 JSON —— 慢、容易超时、也更容易被 `max_tokens`
截断成半截。拆成「每次一组」之后，单次请求很小，失败了也只丢这一组，
重试只补缺的那几组。

每组由模型直接给出 **input 与 output**（以及这一组用途的说明），插件只做
格式/长度校验与去重，不再跑参考解 —— 少一次编译、少一份参考解的输出 token，
整轮生成快很多。

⚠️ 代价：期望输出是模型算的，可能算错（判题逐字节比对）。重要用途建议自己
用一份正确解 `/oj submit` 复核；发现某组不对可以 `/oj cases <编号> clear`
后重新生成。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import resource
import signal
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .ai_client import ChatClient

#: 默认出几组（与题库上限一致）
DEFAULT_COUNT = 10
#: 单组输入的长度上限（超过就当这组无效；防模型吐出几十 KB 的「测试数据」）
MAX_INPUT_CHARS = 20000


class GenError(Exception):
    """生成失败（模型没给可用结果 / 输出不是合格 JSON）—— 人话说明原因。"""


@dataclass
class GenOptions:
    """生成配置（`config.json` → `plugins.oj_bank.gen`）。"""

    enabled: bool = True
    #: 连接三项留空时依次借用 coach → ai_chat.llm
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    count: int = DEFAULT_COUNT
    #: 单次请求超时（秒）—— 一次只要一组，这个值很宽裕
    timeout: float = 120.0
    #: 整次生成的总预算（秒）：超了就用手上已有的，别无限耗
    max_seconds: float = 3600.0
    #: 单次输出只有一组数据，但留足余量；4096 在长题面/思考模型上偶有截断
    max_tokens: int = 65536
    #: 题面送审的截断长度（太长既费 token 也容易超出上下文）
    max_statement_chars: int = 6000
    max_input_chars: int = MAX_INPUT_CHARS
    #: 生成前先问一次「本题常见误区」，再针对误区出卡人点
    pitfalls: bool = True
    #: 允许模型给「生成脚本」（在沙箱里跑出输入/输出），大规模/随机数据更省事
    allow_scripts: bool = True
    #: 生成脚本的超时（秒）
    script_timeout: float = 15.0
    reasoning_effort: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and self.api_key)


SYSTEM_PROMPT = (
    "你是算法竞赛出题人。用户会给你一道题的题面、输入输出格式、样例与数据范围。"
    "先判断题目类型，再决定输出什么（不要 Markdown、不要解释）：\n"
    "A. 如果这道题是**固定输出**（没有输入，或输出与输入无关，"
    "例如「输出 42」「按样例打印一个图形」），**不要生成测试数据**，只输出：\n"
    '{"fixed_output": true, "reason": "为什么不需要测试点（20 字内）"}\n'
    "B. 否则这一轮**只出一组**测试数据：\n"
    '{"input": "这一组的输入", "output": "这一组的正确输出", '
    '"note": "卡的是哪种错误设计（20 字内）"}\n'
    "   大规模 / 随机数据也可以给**生成脚本**："
    '{"script": "Python 源码", "note": "这组用途"}——脚本只能用标准库，用 print '
    "输出一行 `<<<INPUT>>>`、输入正文、一行 `<<<OUTPUT>>>`、输出正文；"
    "不许读写文件、联网、起子进程；\n"
    "要求：\n"
    "1) input 必须严格符合输入格式；output 必须是**你算出来的正确答案**"
    "（判题逐字节比对，忽略行尾空格与末尾空行）；\n"
    "2) **输入必须严格落在题面给出的数据范围内**（长度、数值上下界、"
    "子任务/特殊限制）；题面写了 n ≤ 10^5 就不要给 10^9，写了 |a_i| ≤ 10^9 "
    "就不要给更大的值，也不能小于最小约束；范围没写时按这类题的常规规模保守取；\n"
    "3) 测试点是**混合**的，而且**卡人的组不超过 40%**（10 组里最多 4 组）："
    "多数组是**常规数据**（典型输入、不设陷阱，验证正确解能过）；"
    "卡人的组才是打在**「代码逻辑考虑不周」**的地方——不是把数据取到范围边界，"
    "而是构造会让「看起来对、其实漏了某种情况」的程序出错的输入，例如："
    "漏掉无解 / 空 / 重复 / 全相同 / 并列答案等场景、多组数据之间没清空、"
    "删除或弹出后为空、队列空时还查询、int 溢出 / 负数 / 0、"
    "假设图一定连通或没有重边、贪心或 DP 漏状态、搜索没剪枝；"
    "卡人组里**至少有一组**把数据取到范围内尽量大，让没有剪枝 / 复杂度选错的解法超时；"
    "卡人的组不要只是把数据取到最大 / 最小，要用**具体的构造**暴露漏洞"
    "（全相同、重复、无解、非连通、大规模但结构简单等）；\n"
    "4) 生成后**自己再核对一遍**：输入是否满足每一条数据范围、输出是否与这组输入一致；\n"
    "5) **不要重复**已经生成过的输入；每组只给一组，不要多给。"
)


class CaseGenerator(ChatClient):
    """一次调用生成**一组**测试数据（输入 + 输出）。"""

    ERROR = GenError
    SYSTEM_PROMPT = SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 65536
    CONFIG_SECTION = "gen"

    def __init__(self, options: Optional[GenOptions] = None, *, logger=None,
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        super().__init__(options or GenOptions(), logger=logger, session=session)

    async def propose_one(self, *, problem, index: int = 1, total: int = 10,
                          avoid: Optional[list[str]] = None,
                          pitfalls: Optional[list[str]] = None) -> dict:
        """向模型要**一组**测试数据（input + output + note）。

        Args:
            problem: 题目。
            index: 这是第几组（1 起），写进提示词让模型知道还剩几组。
            total: 一共要几组。
            avoid: 已经生成过的输入，避免模型重复给同一组。
            pitfalls: 先一步分析出的「本题常见误区」；卡点组按它出，空的则全出常规组。

        Returns:
            `{"input": str, "output": str, "note": str}`，或
            `{"script": str, "note": str}`（生成脚本）/ `{"fixed_output": True, ...}`。

        Raises:
            GenError: 没配置模型 / 超时 / 输出不是合格的 JSON。
        """
        if not self.opt.ready:
            raise GenError("没有配置用于生成测试点的模型（见 config 的 gen）")
        prompt = self._user_prompt(problem, index, total, avoid=avoid,
                                   pitfalls=pitfalls)
        try:
            raw = await asyncio.wait_for(
                self._ask(prompt), timeout=max(10.0, float(self.opt.timeout)))
        except asyncio.TimeoutError:
            raise GenError("生成超时（模型没在限定时间内返回）") from None
        return parse_single_case(raw, max_input_chars=self.opt.max_input_chars)

    async def analyze_pitfalls(self, *, problem) -> list[str]:
        """先问一次「本题常见误区」，返回最多 4 条（空列表 = 没有值得卡的误区）。"""
        if not self.opt.ready:
            raise GenError("没有配置用于生成测试点的模型（见 config 的 gen）")
        try:
            raw = await asyncio.wait_for(
                self._ask(self._pitfall_prompt(problem), system=PITFALL_SYSTEM),
                timeout=max(10.0, float(self.opt.timeout)))
        except asyncio.TimeoutError:
            raise GenError("误区分析超时") from None
        return parse_pitfalls(raw)

    def _pitfall_prompt(self, problem) -> str:
        statement = str(getattr(problem, "statement", "") or "")
        limit = max(500, int(self.opt.max_statement_chars or 6000))
        if len(statement) > limit:
            statement = statement[:limit] + "\n…（题面过长已截断）"
        parts = [
            f"题名：{getattr(problem, 'title', '') or '（未命名）'}",
            "【题面】", statement or "（题面为空）",
        ]
        note = str(getattr(problem, "note", "") or "").strip()
        if note:
            parts += ["【数据范围】", note[:1500]]
        input_format = str(getattr(problem, "input_format", "") or "").strip()
        if input_format:
            parts += ["【输入格式】", input_format]
        sample_in = str(getattr(problem, "sample_input", "") or "")
        sample_out = str(getattr(problem, "sample_output", "") or "")
        if sample_in or sample_out:
            parts += ["【样例输入】", sample_in, "【样例输出】", sample_out]
        parts.append("请列出这道题最常见的错误做法/思维误区（最多 4 条），只输出 JSON。")
        return "\n".join(parts)

    # ---- 内部 ----
    def _user_prompt(self, problem, index: int, total: int,
                     avoid: Optional[list[str]] = None,
                     pitfalls: Optional[list[str]] = None) -> str:
        statement = str(getattr(problem, "statement", "") or "")
        limit = max(500, int(self.opt.max_statement_chars or 6000))
        if len(statement) > limit:
            statement = statement[:limit] + "\n…（题面过长已截断）"
        parts = [
            f"题名：{getattr(problem, 'title', '') or '（未命名）'}",
            f"这是第 {index}/{total} 组，只出这一组。",
            "【题面】",
            statement or "（题面为空）",
        ]
        note = str(getattr(problem, "note", "") or "").strip()
        if note:
            if len(note) > 2000:
                note = note[:2000] + "\n…（过长已截断）"
            # 洛谷的「数据范围」就在提示/注意事项里；不给模型看，生成的点必然越界
            parts += ["【数据范围 / 注意事项（输入的每一组都必须满足）】", note]
        limits = []
        if getattr(problem, "time_limit_ms", 0):
            limits.append(f"时限 {int(problem.time_limit_ms)} ms")
        if getattr(problem, "memory_limit_mb", 0):
            limits.append(f"内存 {int(problem.memory_limit_mb)} MB")
        if limits:
            parts += ["【限制】", "，".join(limits) + "（据此选择数据规模）"]
        input_format = str(getattr(problem, "input_format", "") or "").strip()
        if input_format:
            parts += ["【输入格式】", input_format]
        output_format = str(getattr(problem, "output_format", "") or "").strip()
        if output_format:
            parts += ["【输出格式】", output_format]
        sample_in = str(getattr(problem, "sample_input", "") or "")
        sample_out = str(getattr(problem, "sample_output", "") or "")
        if sample_in or sample_out:
            parts += ["【样例输入】", sample_in, "【样例输出】", sample_out]
        # 每组轮换一个侧重点：只写「不要重复」模型容易反复给同一个边界
        # 卡人点不超过 40%：每 5 组 = 3 组常规 + 2 组卡点（10 组 = 6 常规 + 4 卡点）
        regulars = (
            "常规数据：样例规模附近的典型输入，验证基本正确性",
            "常规数据：中等规模随机输入，不刻意构造陷阱",
            "常规数据：另一种普通形态（如多组数据 / 普通序列），不刻意卡人",
        )
        traps = (
            "卡点·场景遗漏：漏了无解 / 空 / 重复 / 全相同 / 只有一个取值 / 多个并列答案",
            "卡点·流程与结构：多组数据没清空、删空后还操作、队列空时查询、"
            "假设图一定连通 / 没有重边 / 是二叉树",
            "卡点·数值与算法：int 溢出、负数、0、浮点精度；贪心反例、DP 漏状态、"
            "搜索漏标记",
            "卡点·性能与剪枝：数据取范围内尽量大，让没剪枝 / 复杂度选错的解法超时",
        )
        pitfalls = [str(x)[:80] for x in (pitfalls or []) if str(x).strip()][:4]
        slot = (index - 1) % 5
        if pitfalls and slot >= 3:
            if slot == 4:
                # 每组 5 个里固定留一个「规模卡点」：卡没剪枝 / 复杂度选错的实现
                focus = ("卡点·性能与剪枝：数据取范围内尽量大（n、容量、P 都拉满），"
                         "让没剪枝 / 复杂度选错的解法超时")
            else:
                offset = (index - 1) // 5
                pitfall = pitfalls[offset % len(pitfalls)]
                focus = (f"卡点（针对本题误区）：{pitfall}"
                         "——构造能让这种错误做法出错的输入，别只取极值")
        else:
            # 没有误区 / 常规组：全出常规数据（卡点占比因此自然不超过 40%）
            offset = (index - 1) // 5 * 3 + (slot if slot < 3 else 0)
            focus = regulars[offset % len(regulars)]
        parts += ["【这一组的重点】", focus]
        if pitfalls:
            parts += ["【本题常见误区（卡点从这里出，同一个误区只卡一次）】",
                      "\n".join(f"- {t}" for t in pitfalls)]
        if avoid:
            shown = "；".join(str(x)[:40] for x in avoid[-8:])
            parts += ["【已经生成过的（不要重复）】", shown]
        parts.append(
            '按题目类型只输出一段 JSON：固定输出的题给 '
            '{"fixed_output": true, "reason": "…"}；否则给 '
            '{"input": "…", "output": "…", "note": "…"}，'
            '大规模 / 随机数据也可以给 {"script": "Python 源码", "note": "…"}'
            '（脚本用 print 输出 <<<INPUT>>> 输入正文 <<<OUTPUT>>> 输出正文，'
            "只用标准库，不读写文件、不联网）")
        return "\n".join(parts)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I | re.M)


def _first_json_object(raw: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍围栏与前后废话）。"""
    text = _FENCE_RE.sub("", str(raw or "")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise GenError(f"模型没有输出 JSON（前 120 字：{text[:120]!r}）")
    try:
        data: Any = json.loads(text[start:end + 1])
    except ValueError as e:
        raise GenError(f"模型输出的 JSON 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise GenError("模型输出的不是 JSON 对象")
    return data


def parse_single_case(raw: str, *,
                      max_input_chars: int = MAX_INPUT_CHARS) -> dict:
    """模型输出 → 一组测试数据，或「这题固定输出」的判定。

    返回两种形状：

    - `{"input", "output", "note"}`：一组测试数据；
    - `{"fixed_output": True, "reason": "..."}`：模型判断这题输出固定
      （没有输入 / 输出与输入无关），不需要额外测试点。

    字段名放宽（`input`/`stdin`/`in`、`output`/`expected`/`answer`/`out`），
    但 **input 与 output 都必须给**：缺 output 就没法评分，缺 input 说明模型
    没按格式来 —— 这两种都直接报错，让调用方重试这一组。

    Raises:
        GenError: 没有 JSON、缺 input/output、或输入超长。
    """
    data = _first_json_object(raw)
    script = data.get("script") or data.get("generator")
    if isinstance(script, str) and script.strip():
        note = str(data.get("note") or data.get("hint") or "")
        return {"script": script.strip(), "note": note.strip()[:120]}
    if _is_fixed_output(data):
        reason = str(data.get("reason") or data.get("note")
                     or data.get("why") or "输出固定").strip()
        return {"fixed_output": True, "reason": reason[:120]}
    # 有的模型会包一层 {"case": {...}} / {"test": {...}}
    for wrapper in ("case", "test", "data"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and ("input" in inner or "stdin" in inner):
            data = inner
            break

    stdin = data.get("input", data.get("stdin", data.get("in", None)))
    if stdin is None:
        raise GenError("模型没给输入（input）")
    stdin = "" if stdin is None else str(stdin)
    if len(stdin) > max(1, int(max_input_chars)):
        raise GenError(f"这一组的输入太长（{len(stdin)} > {max_input_chars} 字）")

    output = data.get("output", data.get("expected",
                          data.get("answer", data.get("ans", data.get("out", None)))))
    if output is None:
        raise GenError("模型没给输出（output）——一组测试数据必须同时有输入和输出")
    note = str(data.get("note") or data.get("hint") or data.get("why") or "")
    return {"input": stdin, "output": str(output),
            "note": note.strip()[:120]}


def _is_fixed_output(data: dict) -> bool:
    """模型是否判定「这题固定输出，不需要测试数据」。"""
    for key in ("fixed_output", "fixed", "no_input", "constant_output",
                "no_testcases", "no_test_cases"):
        value = data.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in (
                "true", "yes", "1"):
            return True
    kind = str(data.get("type") or data.get("kind") or "").strip().lower()
    return kind in ("fixed_output", "fixed", "constant", "no_input")


#: 误区分析的专用系统提示
PITFALL_SYSTEM = (
    "你是算法竞赛出题人。给你一道题，请列出选手在这道题上**常见的错误做法 / "
    "思维误区**（不是数据范围）。只输出一段 JSON：\n"
    '{"pitfalls": [{"name": "简短名称（10 字内）", "why": "为什么会错（20 字内）"}]}\n'
    "要求：最多 4 条，按重要性排序；每条要具体（例如「只按相邻去重」"
    "「P=0 当成 0 次」「没做二进制拆分/复杂度选错会超时」），"
    "**包含复杂度 / 没剪枝导致超时的误区**；不要写「数据范围没考虑」这类空话；"
    "如果这题没有值得卡的误区"
    "（太简单 / 固定输出），输出 {\"pitfalls\": []}。"
)


def parse_pitfalls(raw: str) -> list[str]:
    """模型输出 → 误区列表（最多 4 条，每条 ≤80 字）。"""
    data = _first_json_object(raw)
    items = (data.get("pitfalls") or data.get("mistakes")
             or data.get("risks") or [])
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
            why = str(item.get("why") or item.get("reason") or "").strip()
            text = f"{name}：{why}" if name and why else (name or why)
        else:
            continue
        text = " ".join(text.split()).strip()
        if text:
            out.append(text[:80])
    return out[:4]


#: 脚本生成测试点：静态筛查（运行仍受 rlimit 限制）
_SCRIPT_MARK_IN = "<<<INPUT>>>"
_SCRIPT_MARK_OUT = "<<<OUTPUT>>>"
_SCRIPT_FORBIDDEN = re.compile(
    r"\b(socket|ssl|urllib|requests|httpx|aiohttp|ftplib|smtplib|subprocess|"
    r"popen|fork|exec|eval|compile|__import__|importlib|ctypes|multiprocessing|"
    r"threading|shutil|pathlib|tempfile|glob|pty|signal|open)\b")


def _script_limits() -> None:
    """子进程资源上限（CPU / 内存 / 可写文件 / 打开文件数）。"""
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


async def run_script(source: str, *, timeout: float = 15.0,
                     max_input: int = MAX_INPUT_CHARS,
                     max_output: int = MAX_INPUT_CHARS) -> tuple[str, str]:
    """跑模型给的「测试数据生成脚本」，返回 `(输入, 输出)`。

    脚本约定：print 一行 `<<<INPUT>>>`，接着输入正文；一行 `<<<OUTPUT>>>`，
    接着输出正文。

    安全措施：
    - 源码**静态筛查**：联网 / 起进程 / 读写文件 / 动态执行一律拒；
    - 子进程：`-I -S`（隔离环境、不加载 site-packages）、清空环境变量、
      独立进程组、CPU/内存/文件 rlimit，超时整组 kill；
    - 只收 stdout（有上限），不喂 stdin。

    Raises:
        GenError: 脚本不合规 / 超时 / 没按约定输出。
    """
    text = str(source or "")
    if not text.strip():
        raise GenError("生成脚本是空的")
    if len(text) > 12000:
        raise GenError("生成脚本太长（超过 12000 字）")
    bad = _SCRIPT_FORBIDDEN.search(text)
    if bad:
        raise GenError(f"生成脚本里有不允许的用法（{bad.group(0)}）")
    with tempfile.TemporaryDirectory(prefix="oj-gen-") as tmp:
        path = os.path.join(tmp, "gen.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp,
            env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8",
                 "HOME": tmp, "TMPDIR": tmp},
            preexec_fn=_script_limits,
            start_new_session=True)
        try:
            out, _err = await asyncio.wait_for(proc.communicate(),
                                               timeout=max(1.0, float(timeout)))
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            raise GenError("生成脚本超时") from None
    raw = out.decode("utf-8", "replace")
    if len(raw) > max(max_input, max_output) * 4:
        raise GenError("生成脚本输出太大")
    if _SCRIPT_MARK_IN not in raw or _SCRIPT_MARK_OUT not in raw:
        raise GenError("生成脚本没有按约定打印 <<<INPUT>>> / <<<OUTPUT>>>")
    stdin, rest = raw.split(_SCRIPT_MARK_IN, 1)
    stdin, output = rest.split(_SCRIPT_MARK_OUT, 1)
    stdin = stdin.strip("\n")
    output = output.strip("\n")
    if len(stdin) > max_input:
        raise GenError(f"脚本生成的输入太长（{len(stdin)} 字）")
    if len(output) > max_output:
        raise GenError(f"脚本生成的输出太长（{len(output)} 字）")
    return stdin, output


__all__ = ["DEFAULT_COUNT", "CaseGenerator", "GenError", "GenOptions",
           "parse_single_case"]

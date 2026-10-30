"""判完之后的 AI 点评：总结解法、点出问题、给可执行的改进建议。

为什么单独一个模块
------------------
判题（`judge.py`）是**确定性**的：编译、跑测试点、比对。点评是**锦上添花**的
一次模型调用，随时可能超时、限流、返回一坨 Markdown。把两者分开，判题结果就不会
被模型的脾气拖住：点评失败就什么都不发，用户至少已经拿到分数。

调用时机
--------
评分**发出去之后**再由后台任务调用（`main.schedule_coach`）。判题本身要
编译 + 跑 10 组，已经几秒；再同步等一次模型（可能十几秒）会让「交题」看起来卡死。
所以先发分数，点评作为第二条消息补上。

配置复用
--------
`plugins.oj_bank.coach` 留空时**借用 `plugins.ai_chat.llm`** 的连接配置 ——
同一个网关、同一把 key，省得为了一个点评再配一遍。想换模型/关掉点评，单独写
这三个字段即可。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .ai_client import ChatClient

#: 点评最多给多少字（QQ 一条消息太长会被折叠；模型也爱写长文）
DEFAULT_MAX_CHARS = 700

SYSTEM_PROMPT = (
    "你是算法竞赛教练，带过很多刚入门的选手。"
    "下面是一份刚被判题机评过分的提交，请用**中文纯文本**点评，"
    "不要 Markdown（不要 **、#、```、表格），不要重复贴代码。"
    "严格按三段写，总长不超过 200 字：\n"
    "思路：一句话说清这份代码在做什么；\n"
    "复杂度：时间 / 空间；\n"
    "改进：最多 3 条，要具体可执行（换算法、补边界、写法问题），"
    "不要泛泛说「多练习」。\n"
    "如果代码已经很好，就直说「没有明显可改进的地方」，不要硬凑。"
)

#: 模型爱加的围栏/前缀，回给用户前去掉
_FENCE_RE = re.compile(r"```[a-zA-Z0-9+#.\-]*\n?|\n?```")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


class CoachError(Exception):
    """点评服务不可用（网络/接口/解析失败）—— 调用方吞掉即可。"""


@dataclass
class CoachOptions:
    """点评配置（`config.json` → `plugins.oj_bank.coach`）。"""

    enabled: bool = True
    #: 以下三项留空时借用 `plugins.ai_chat.llm`
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 60.0
    max_tokens: int = 32768
    #: 送审代码的截断长度（超长代码只送前面这段）
    max_code_chars: int = 12000
    #: 点评回给用户前的截断
    max_chars: int = DEFAULT_MAX_CHARS
    #: 推理预算；空 = 不传（普通模型不认这个字段）
    reasoning_effort: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and self.api_key)


class CodeCoach(ChatClient):
    """一次判题 = 一次点评调用。失败返回空串，绝不抛到调用方。"""

    ERROR = CoachError
    SYSTEM_PROMPT = SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 900
    CONFIG_SECTION = "coach"

    def __init__(self, options: Optional[CoachOptions] = None, *, logger=None,
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        super().__init__(options or CoachOptions(), logger=logger, session=session)

    async def advise(self, *, code: str, language: str, problem_title: str,
                     verdict: str, detail: str = "") -> str:
        """让模型点评一份代码。

        Args:
            code: 选手代码（会按 `max_code_chars` 截断）。
            language: 语言标签（`C++17` / `Python 3` …）。
            problem_title: 题名（帮模型判断题目类型）。
            verdict: 判题结论（`AC` / `WA` …）。
            detail: 失败组说明（第几组、期望与实际预览）。

        Returns:
            点评文本（已清理 Markdown、按 `max_chars` 截断）；失败返回空串。
        """
        if not self.opt.ready:
            return ""
        source = str(code or "")[: max(1, int(self.opt.max_code_chars))]
        prompt = self._user_prompt(source, language, problem_title, verdict, detail)
        try:
            raw = await asyncio.wait_for(
                self._ask(prompt), timeout=max(5.0, float(self.opt.timeout)))
        except asyncio.TimeoutError:
            raise CoachError("点评超时") from None
        text = clean_reply(raw)
        if not text:
            return ""
        limit = max(80, int(self.opt.max_chars or DEFAULT_MAX_CHARS))
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    # ---- 内部 ----
    @staticmethod
    def _user_prompt(code: str, language: str, title: str, verdict: str,
                     detail: str) -> str:
        head = [f"题目：{title or '（未命名）'}", f"语言：{language}",
                f"判题结果：{verdict}"]
        if detail:
            head.append(f"细节：{detail[:300]}")
        return ("\n".join(head) + "\n【代码开始】\n" + code + "\n【代码结束】\n"
                "请按三段式点评。")


def clean_reply(raw: str) -> str:
    """模型输出 → 能直接发进 QQ 的纯文本。

    QQ 不渲染 Markdown：保留 `**`/`#`/围栏只会变成乱码，所以统一剥掉；
    连续空行压成一个。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


__all__ = ["CodeCoach", "CoachError", "CoachOptions", "clean_reply"]

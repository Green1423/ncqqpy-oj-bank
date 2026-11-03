"""代码审查：**在把别人的代码放上服务器跑之前**先过两道闸。

判题是本插件唯一的高风险动作 —— 用户提交的是一段**会被执行的程序**。
本机没有可用的隔离手段（`unshare -n` 被禁用，见 `judge.py` 的说明），所以
第一道闸只能是「看代码像不像恶意的」：

1. **静态规则**（零成本、必然执行）：`system()`、`fork()`、`rm -rf`、读
   `/etc/passwd`、`curl | sh` 这类**竞赛代码里不可能出现**的东西。宁可漏，不可误杀：
   一条误判会让正常题解永远交不上去，而漏掉的一条还有第二道闸和资源限制兜底；
2. **模型审查**（可选，参考 `bupt_news` 的摘要客户端写法）：把代码和题目名交给
   OpenAI 兼容接口，问一句「这像不像恶意程序」。**失败时的口径由配置决定**
   （`on_error: deny` 默认拒绝 —— 审查服务挂了不等于可以随便跑）。

⚠️ 两道闸都是**尽力而为**，不是安全边界。真正的安全边界是「谁能提交」
（`binding` 白名单）+ 资源限制。文档里不要把它说成沙箱。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .ai_client import ChatClient

#: 规则命中时给人的解释（也是拒绝理由）
RULES: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[a-zA-Z]*r", "删除文件/目录"),
    (r"/etc/(?:passwd|shadow|sudoers)", "读取系统账号文件"),
    (r"\bchmod\s+[0-7]{3,4}\b", "修改文件权限"),
    (r"\b(?:curl|wget)\s+(?:-\S+\s+)*https?://", "联网下载"),
    (r"\bbase64\s+(?:-d|--decode)", "解码后执行"),
    (r"\bfork\s*\(", "创建子进程"),
    (r"\b(?:popen|execl|execv|execve|execvp)\s*\(", "执行外部程序"),
    (r"\bsystem\s*\(", "执行 shell 命令"),
    (r"#include\s*<\s*sys/socket\.h\s*>", "使用套接字"),
    (r"\bimport\s+(?:socket|subprocess|ctypes|multiprocessing|pty)\b",
     "导入高危模块"),
    (r"\bos\.(?:system|popen|fork|execv|remove|unlink|rmdir)\b", "危险系统调用"),
    (r"\bshutil\.rmtree\b", "删除目录"),
    (r"\b__import__\s*\(", "动态导入模块"),
    (r"\bopen\s*\(\s*['\"]/(?:etc|root|home|proc|sys|var|bin|sbin|usr)\b",
     "访问系统路径"),
    (r"/bin/(?:sh|bash)\b", "调用 shell"),
    (r":\s*\(\s*\)\s*\{", "疑似 fork 炸弹"),
    (r"\bwhile\s*\(\s*(?:1|true)\s*\)\s*\{?\s*(?:fork|system)\b",
     "疑似无限派生进程"),
)

_RULES = tuple((re.compile(pattern, re.I), label) for pattern, label in RULES)

#: 模型输出的判定词 → 内部口径
_UNSAFE_WORDS = {"unsafe", "malicious", "dangerous", "block", "blocked",
                 "deny", "unsafe_code", "恶意", "危险", "拒绝"}
_SAFE_WORDS = {"safe", "ok", "allow", "allowed", "safe_code", "安全", "通过"}

SYSTEM_PROMPT = (
    "你是服务器上的代码安全审查员。下面这段代码是用户提交的竞赛题解，"
    "会被编译并在服务器上以普通用户身份运行。"
    "判断它是否包含**恶意或破坏性**行为：读写/删除服务器文件、联网下载或外传数据、"
    "启动子进程或 shell、提权、挖矿、fork 炸弹、读取系统机密、无限刷屏。"
    "正常的算法代码（标准输入输出、STL、递归、循环、大量计算、开大数组）"
    "必须判安全 —— 不要因为「效率低」「可能超时」就拦截。"
    '只输出 JSON：{"verdict":"safe" 或 "unsafe","reason":"不超过40字的中文理由"}，'
    "不要输出任何其他文字。"
)


class ReviewError(Exception):
    """审查服务不可用（网络/接口/解析失败），不是「代码不安全」。"""


@dataclass
class ReviewOptions:
    """审查配置（`config.json` → `plugins.oj_bank.review`）。"""

    #: 静态规则开关
    rules: bool = True
    #: 模型审查开关（没配 base_url/model/api_key 时自动视为关闭）
    ai_enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30.0
    max_tokens: int = 8192
    #: 推理模型的思考预算；`none` 表示别思考（审查不需要长推理）
    reasoning_effort: str = "none"
    #: 审查服务失败时的口径：deny（默认，宁可不判）/ allow
    on_error: str = "deny"
    #: 缓存多少份已审代码（同一份代码反复提交时不重复花钱）
    cache_size: int = 256
    #: 送审代码的截断长度（超长代码只送前面这段；再长也不该跑）
    max_code_chars: int = 60000

    @property
    def ai_ready(self) -> bool:
        return bool(self.ai_enabled and self.base_url and self.model)


@dataclass
class Verdict:
    """审查结论。"""

    #: 能不能跑
    allowed: bool
    reason: str = ""
    #: 结论来源：rule / ai / cache / off
    source: str = ""
    #: safe / blocked
    verdict: str = "safe"

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "source": self.source, "verdict": self.verdict}


def scan_rules(code: str) -> list[str]:
    """静态规则扫描，返回命中的中文标签（空列表 = 没命中）。"""
    text = code or ""
    hits: list[str] = []
    for pattern, label in _RULES:
        if pattern.search(text) and label not in hits:
            hits.append(label)
    return hits


def parse_verdict(raw: str) -> tuple[bool, str]:
    """解析模型输出，返回 `(is_safe, reason)`。

    模型爱加 ```json 围栏或前后废话，所以先剥围栏、再找第一个 JSON 对象；
    都失败就按关键词兜底（`unsafe` / `safe`），最后才抛 `ReviewError`。

    Raises:
        ReviewError: 完全看不出结论。
    """
    text = (raw or "").strip()
    if not text:
        raise ReviewError("审查服务返回空内容")
    fenced = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text,
                    flags=re.I | re.M).strip()
    match = re.search(r"\{.*\}", fenced, re.S)
    candidate = match.group(0) if match else fenced
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        verdict = str(data.get("verdict") or data.get("safe") or "").strip().lower()
        reason = str(data.get("reason") or data.get("why") or "").strip()
        if verdict in _UNSAFE_WORDS:
            return False, reason or "模型判定为不安全"
        if verdict in _SAFE_WORDS:
            return True, reason
    lowered = fenced.lower()
    for word in _UNSAFE_WORDS:
        if word in lowered:
            return False, "模型判定为不安全"
    for word in _SAFE_WORDS:
        if word in lowered:
            return True, ""
    raise ReviewError(f"审查结果无法解析：{fenced[:120]!r}")


class CodeReviewer(ChatClient):
    """静态规则 + 模型审查。`check()` 永远返回 `Verdict`，不向上抛。"""

    ERROR = ReviewError
    SYSTEM_PROMPT = SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 1024
    CONFIG_SECTION = "review"

    def __init__(self, options: Optional[ReviewOptions] = None, *, logger=None,
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        super().__init__(options or ReviewOptions(), logger=logger, session=session)
        self._cache: dict[str, Verdict] = {}
        self.last_error = ""

    # ---- 对外 ----
    async def check(self, code: str, *, language: str = "cpp",
                    context: Optional[dict] = None) -> Verdict:
        """审查一段代码。

        Args:
            code: 源码。
            language: 语言（只影响提示词与缓存键）。
            context: 题目名 / 提交者等，写进提示词帮模型判断。

        Returns:
            `Verdict`；`allowed=False` 时 `reason` 是可直接回给提交者的话。
        """
        text = str(code or "")
        if not self.opt.rules and not self.opt.ai_ready:
            return Verdict(True, source="off")

        if self.opt.rules:
            hits = scan_rules(text)
            if hits:
                return Verdict(False, reason="代码命中了禁用规则：" + "、".join(hits),
                               source="rule", verdict="blocked")

        if not self.opt.ai_ready:
            return Verdict(True, source="rule" if self.opt.rules else "off")

        key = hashlib.sha1(f"{language}\0{text}".encode("utf-8")).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            return Verdict(cached.allowed, cached.reason, "cache", cached.verdict)

        try:
            allowed, reason = await self._ask_model(text, language, context or {})
        except Exception as e:  # noqa: BLE001 - 审查失败不向上抛
            self.last_error = _err_text(e)
            if self.logger:
                self.logger.warning("[oj_bank] 代码审查失败：%s", self.last_error)
            if str(self.opt.on_error or "deny").lower() == "allow":
                return Verdict(True, reason=f"审查服务不可用（{self.last_error}），按配置放行",
                               source="ai", verdict="safe")
            return Verdict(False, reason=f"代码审查服务不可用，暂不判题（{self.last_error}）",
                           source="ai", verdict="blocked")

        verdict = Verdict(allowed, reason if not allowed else "",
                          "ai", "safe" if allowed else "blocked")
        self._remember(key, verdict)
        return verdict

    # ---- 内部 ----
    def _remember(self, key: str, verdict: Verdict) -> None:
        limit = max(1, int(self.opt.cache_size or 1))
        if len(self._cache) >= limit:
            # 最简单的淘汰：丢掉最早插入的一半（够用，不必上 LRU）
            for old in list(self._cache)[: max(1, limit // 2)]:
                self._cache.pop(old, None)
        self._cache[key] = verdict

    async def _ask_model(self, code: str, language: str,
                         context: dict) -> tuple[bool, str]:
        """问模型要一个结论。失败抛 `ReviewError`（由 `check` 兜住）。"""
        title = str(context.get("title") or "").strip()
        who = str(context.get("user_id") or "").strip()
        head = []
        if title:
            head.append(f"题目：{title}")
        if who:
            head.append(f"提交者：QQ {who}")
        user_prompt = "\n".join(head + [
            f"语言：{language}",
            "代码：",
            "```",
            code[: max(1, int(self.opt.max_code_chars or 60000))],
            "```",
        ])
        return parse_verdict(await self._ask(user_prompt))


def _err_text(e: BaseException) -> str:
    """异常的可读描述 —— **绝不抛异常**（aiohttp 的 DNS 异常 `__str__` 会自己崩）。"""
    name = type(e).__name__
    try:
        msg = str(e).strip()
    except Exception:  # noqa: BLE001
        msg = ""
    return f"{name}: {msg}" if msg else name


__all__ = ["RULES", "CodeReviewer", "ReviewError", "ReviewOptions", "Verdict",
           "parse_verdict", "scan_rules"]

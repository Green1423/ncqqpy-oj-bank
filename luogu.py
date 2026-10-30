"""洛谷题目抓取：WAF cookie 挑战 + lentille-context 解析。

为什么需要这个模块
==================

洛谷的题目页对**无 cookie 的 GET** 会先返回 302 到同一 URL，并下发
``Set-Cookie: C3VK=...``；带上这个 cookie 再请求一次才会返回真正的 200 页面。
这是 nginx 层的 WAF 挑战（无 cookie 的 HEAD 反而是 200，所以不是按 IP 封禁），
不是题目不存在。因此抓取时必须：

* 用带 ``CookieJar(unsafe=True)`` 的同一个 session，让挑战 cookie 被保存下来；
* 请求允许自动跳转，并把「仍然拿到 302」当作挑战，最多再重试 2 次；
* 一次抓取内复用 session，因为 cookie 只在这段会话里有意义。

为什么只信 lentille-context
===========================

页面里同时有服务端渲染的 HTML 文本、``contenu`` 镜像字段等来源，但它们可能
不同步，且 HTML 文本经过转义/KaTeX 渲染。真正被前端当作数据源的是：

    <script id="lentille-context" type="application/json">{...}</script>

其中 ``data.problem.content`` 的 markdown 是**原样**的题面，``$...$`` LaTeX
与围栏代码块都保留。因此解析只认这一个 script 标签，其余 HTML 一律忽略。本
模块对 markdown 不做转义，也不做反转义，交给下游渲染器处理。

另外注意洛谷的 ``limits.memory`` 单位是 **KB**，对外暴露时换算成 MB；
``limits.time`` 单位是毫秒。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

__all__ = [
    "LuoguError",
    "ProblemNotFound",
    "LuoguProblem",
    "normalize_pid",
    "parse_problem_page",
    "fetch_problem",
]

SITE = "https://www.luogu.com.cn"
PROBLEM_URL = f"{SITE}/problem"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 响应体上限：异常响应（如被中间设备塞入巨大页面）不能打爆机器人内存。
MAX_HTML_BYTES = 8 * 1024 * 1024

#: 非 200 响应只把前 200 个字符放进异常消息，避免日志被整页 HTML 淹没。
MAX_ERROR_CHARS = 200

#: 首次请求 + 2 次 WAF cookie 挑战重试。
MAX_ATTEMPTS = 3

#: 洛谷题号：字母开头、至少含一个数字、只含字母/数字/下划线。
#: 覆盖 P1001、CF1A、AT_abc123_a、SP1234、U123456 等形态。
_PID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*$")

#: 从题目页 URL 中截出题号；允许带 query / fragment / 尾部斜杠。
_URL_PROBLEM_RE = re.compile(r"/problem/([^/?#\s]+)", re.I)

#: 唯一可信的数据源。属性顺序可能变化，所以 id 前后的属性用 [^>]* 吞掉；
#: JSON 内的字符串可能含换行，必须 re.S，且用非贪婪避免吞掉后续 script。
_CONTEXT_RE = re.compile(
    r"""<script[^>]*\bid\s*=\s*["']lentille-context["'][^>]*>(.*?)</script>""",
    re.S | re.I,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class LuoguError(Exception):
    """洛谷题目抓取失败的基类。"""


class ProblemNotFound(LuoguError):
    """题号不存在（404 / 题号格式不对 / 页面里没有数据）。"""


@dataclass
class LuoguProblem:
    """一道洛谷题目的结构化数据。

    Attributes:
        pid: 规范化的题号，如 ``P1001``。
        name: 题名。
        difficulty: 难度 1..7，0 表示未知。
        statement: 题面 markdown（``content.description``）。
        background: 题面背景（``content.background``）。
        input_format: 输入格式（``content.formatI``）。
        output_format: 输出格式（``content.formatO``）。
        hint: 提示/注意事项（``content.hint``）。
        samples: 样例对 ``[(输入, 输出), ...]``。
        time_limit_ms: 时间限制，毫秒。
        memory_limit_mb: 内存限制，MB（洛谷原始值是 KB，已换算）。
        url: 题目页 URL。
    """

    pid: str
    name: str
    difficulty: int = 0
    statement: str = ""
    background: str = ""
    input_format: str = ""
    output_format: str = ""
    hint: str = ""
    samples: list[tuple[str, str]] = field(default_factory=list)
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    url: str = ""


def normalize_pid(raw: str) -> str:
    """把用户输入规范化成洛谷题号。

    支持裸题号与题目页 URL 两种输入；后者会截取 ``/problem/<pid>`` 一段。
    不含下划线的纯「字母 + 数字」题号统一转大写（``p1001`` -> ``P1001``，
    ``sp1234`` -> ``SP1234``），而 ``AT_abc123_a`` 这类洛谷本身就小写的题号
    保持原样，不能整串大写。

    Args:
        raw: 用户输入，例如 ``" p1001 "`` 或
            ``"https://www.luogu.com.cn/problem/P1001"``。

    Returns:
        规范化后的题号，如 ``"P1001"``。

    Raises:
        ProblemNotFound: 输入为空、URL 里没有题号，或题号格式非法。
    """
    text = str(raw or "").strip()
    if not text:
        raise ProblemNotFound("洛谷题号不能为空")

    url_match = _URL_PROBLEM_RE.search(text)
    if url_match:
        text = url_match.group(1)

    if not _PID_RE.match(text):
        raise ProblemNotFound(f"不是合法的洛谷题号：{raw!r}")

    # 洛谷的 AT_abc123_a 等题号大小写敏感，必须原样保留；其余纯字母数字
    # 题号（P1001 / CF1A / SP1234 / U123456）大小写不敏感，统一转大写。
    return text if "_" in text else text.upper()


def parse_problem_page(html: str, *, pid: str = "") -> LuoguProblem:
    """从洛谷题目页 HTML 中解析出结构化题目。

    只读取 ``<script id="lentille-context">`` 里的 JSON；页面其余部分一律
    不参与解析。markdown 字段原样返回，不做转义或反转义。

    Args:
        html: 题目页 HTML 全文。
        pid: 调用方已知的题号，仅在页面 JSON 缺少 ``pid`` 时兜底，
            也用于错误信息。

    Returns:
        解析好的 :class:`LuoguProblem`。

    Raises:
        ProblemNotFound: 页面里没有 lentille-context、JSON 损坏，或
            ``data.problem`` 不存在。
    """
    label = pid or "未知"
    if not isinstance(html, str) or not html:
        raise ProblemNotFound(f"洛谷页面为空，没有题目数据（题号 {label}）")

    context_match = _CONTEXT_RE.search(html)
    if context_match is None:
        raise ProblemNotFound(
            f"洛谷页面里没有 lentille-context，可能被 WAF 拦截（题号 {label}）"
        )

    try:
        payload = json.loads(context_match.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProblemNotFound(f"洛谷页面数据不是合法 JSON（题号 {label}）") from exc

    if not isinstance(payload, dict):
        raise ProblemNotFound(f"洛谷页面数据结构异常（题号 {label}）")

    # 严格按 data.problem 取，不做多层兜底：结构变了应该显式失败，而不是
    # 悄悄拿到另一份可能不同步的数据。
    data = payload.get("data")
    problem = data.get("problem") if isinstance(data, dict) else None
    if not isinstance(problem, dict):
        raise ProblemNotFound(f"洛谷页面里没有题目数据（题号 {label}）")

    content = problem.get("content")
    if not isinstance(content, dict):
        content = {}

    background = _text(content.get("background"))
    description = _text(content.get("description"))
    # 少数题没有 description，此时用 background 充当题面，保证 statement 不空。
    statement = description if description.strip() else background

    samples: list[tuple[str, str]] = []
    raw_samples = problem.get("samples")
    if isinstance(raw_samples, list):
        for item in raw_samples:
            # 每项正常是 [输入, 输出]；长度不足的脏数据直接跳过。
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            samples.append((_sample_text(item[0]), _sample_text(item[1])))

    limits = problem.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    time_limit_ms = _first_positive_int(limits.get("time"), 1000)
    # 洛谷给的是 KB；空列表时用 256 MB 兜底，再走统一换算。
    memory_kb = _first_positive_int(limits.get("memory"), 256 * 1024)
    memory_limit_mb = max(1, memory_kb // 1024)

    canonical_pid = _text(problem.get("pid")).strip() or (pid or "").strip()

    return LuoguProblem(
        pid=canonical_pid,
        name=_text(problem.get("name")),
        difficulty=_as_int(problem.get("difficulty"), 0),
        statement=statement,
        background=background,
        input_format=_text(content.get("formatI")),
        output_format=_text(content.get("formatO")),
        hint=_text(content.get("hint")),
        samples=samples,
        time_limit_ms=time_limit_ms,
        memory_limit_mb=memory_limit_mb,
        url=f"{PROBLEM_URL}/{canonical_pid}" if canonical_pid else "",
    )


async def fetch_problem(
    pid: str,
    *,
    timeout: float = 20.0,
    user_agent: str = "",
    cookie: str = "",
    session: aiohttp.ClientSession | None = None,
) -> LuoguProblem:
    """抓取并解析一道洛谷题目。

    没有传入 ``session`` 时会临时创建一个带 ``CookieJar(unsafe=True)`` 的
    session，并在结束时关闭；传入的 session 由调用方负责生命周期（便于测试
    桩替换，也便于复用连接池）。

    Args:
        pid: 题号或题目页 URL，内部会先 :func:`normalize_pid`。
        timeout: 总超时（秒），同时用于 session 与单次请求。
        user_agent: 自定义 User-Agent；为空时使用浏览器 UA。
        cookie: 额外的 ``Cookie`` 请求头，例如已登录/已有挑战 cookie 时。
        session: 外部 ``aiohttp.ClientSession``；为 ``None`` 时自建临时 session。

    Returns:
        解析好的 :class:`LuoguProblem`。

    Raises:
        ProblemNotFound: 题号非法、404，或页面里没有题目数据。
        LuoguError: 超时、网络错误、非 200 响应或页面超过 8 MB。
    """
    canonical_pid = normalize_pid(pid)
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    headers = {
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie

    owned_session = session is None
    if owned_session:
        # CookieJar 在 aiohttp 3.14 需要 running loop，必须在 async 函数内创建。
        # unsafe=True 让 cookie 在非严格域名场景下也能存住，WAF 挑战需要它。
        session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=timeout_obj,
        )

    try:
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with session.get(
                    f"{PROBLEM_URL}/{canonical_pid}",
                    headers=headers,
                    allow_redirects=True,
                    timeout=timeout_obj,
                ) as resp:
                    status = resp.status

                    if status == 200:
                        html = await _read_text(resp, MAX_HTML_BYTES)
                        return parse_problem_page(html, pid=canonical_pid)

                    if status == 404:
                        raise ProblemNotFound(f"洛谷上没有题号 {canonical_pid}")

                    if status in _REDIRECT_STATUSES and attempt < MAX_ATTEMPTS - 1:
                        # 典型场景：WAF 302 到同一地址。没跟随时重试，此时
                        # session 的 CookieJar 里已经拿到 C3VK 挑战 cookie。
                        continue

                    # 非 200 且不再重试：只带前 200 字，避免异常消息过长。
                    body = (await _read_text(resp, MAX_HTML_BYTES)).strip()
                    detail = body[:MAX_ERROR_CHARS]
                    message = f"HTTP {status}"
                    if detail:
                        message = f"{message}: {detail}"
                    raise LuoguError(message)
            except asyncio.TimeoutError as exc:
                raise LuoguError(
                    f"请求洛谷超时（超过 {timeout:g} 秒）：{canonical_pid}"
                ) from exc
            except aiohttp.TooManyRedirects as exc:
                raise LuoguError(
                    f"洛谷重定向次数过多，可能被 WAF 拦截：{canonical_pid}"
                ) from exc
            except aiohttp.ClientError as exc:
                raise LuoguError(f"访问洛谷失败：{exc}") from exc

        # 理论上循环内一定 return/raise；保留显式兜底方便静态检查。
        raise LuoguError(f"洛谷请求失败：{canonical_pid}")
    finally:
        if owned_session:
            await session.close()


# ------------------------------------------------------------------ 内部工具


def _text(value: Any) -> str:
    """把 JSON 字段转成字符串；缺失或类型不对时用空串。"""
    return value if isinstance(value, str) else ""


def _sample_text(value: Any) -> str:
    """样例元素转字符串；None 视为空串，避免出现字面量 ``"None"``。"""
    return "" if value is None else str(value)


def _as_int(value: Any, default: int = 0) -> int:
    """尽量转 int，失败时返回默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_positive_int(values: Any, default: int) -> int:
    """取列表首项的正整数；空列表或异常值返回默认值。"""
    if isinstance(values, (list, tuple)) and values:
        number = _as_int(values[0], 0)
        if number > 0:
            return number
    return default


async def _read_text(resp: Any, limit: int) -> str:
    """读取响应文本，并在读取阶段就施加大小上限。

    注意不能只调用一次 ``resp.content.read(limit + 1)``：aiohttp 的
    ``StreamReader.read(n)`` 在拿到**任意**一块数据后就会返回，并不保证读到
    EOF，直接拿来读整页会得到截断的 HTML（JSON script 没有闭合标签，解析
    必然失败）。这里改用 ``iter_chunked`` 一直读到 EOF，同时累计大小、超限
    立即中止，避免异常响应打爆内存。对只实现 ``read()``/``text()`` 的测试桩
    做兼容。
    """
    content = getattr(resp, "content", None)
    if content is not None and hasattr(content, "iter_chunked"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise LuoguError(f"洛谷响应超过 {limit} 字节上限，已放弃解析")
            chunks.append(chunk)
        raw: Any = b"".join(chunks)
    elif content is not None and hasattr(content, "read"):
        raw = await content.read()
    elif callable(getattr(resp, "read", None)):
        raw = await resp.read()
    else:
        text = await resp.text()
        if len(text.encode("utf-8", "replace")) > limit:
            raise LuoguError(f"洛谷响应超过 {limit} 字节上限，已放弃解析")
        return text

    if len(raw) > limit:
        raise LuoguError(f"洛谷响应超过 {limit} 字节上限，已放弃解析")
    if isinstance(raw, str):
        return raw

    # aiohttp 已按 Content-Type 探测 charset（洛谷是 utf-8）；探测不到时兜底。
    charset = getattr(resp, "charset", None) or "utf-8"
    return raw.decode(charset, errors="replace")

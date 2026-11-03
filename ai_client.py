"""OpenAI 兼容接口的最小客户端 —— 点评 / 出题 / 代码审查共用一份。

三个调用方（`coach.py`、`gen.py`、`review.py`）问的是同一个
`/chat/completions` 接口，带同一套请求头、同一套「非 200 / 网络错误 /
空内容」的报错口径；连 `reasoning_effort` 怎么塞进 payload 都各写了一遍。
**协议只有一份**，提示词、响应解析、异常类型才属于各自的业务。

子类只需要声明四个类属性：

```python
class XxxClient(ChatClient):
    ERROR = XxxError              # 抛什么异常
    SYSTEM_PROMPT = "..."          # 系统提示词
    DEFAULT_MAX_TOKENS = 1024      # `opt.max_tokens` 缺省时用
    CONFIG_SECTION = "xxx"         # 空响应提示里的配置段名（"可调大 xxx.max_tokens"）
```
"""
from __future__ import annotations

import asyncio
from typing import Optional

import aiohttp


class ChatClient:
    """带懒建 session 的对话客户端基类（`opt` 是各业务的配置对象）。

    子类通过类属性描述差异；网络与协议部分在这里只有一份实现。
    """

    #: 网络/协议/空响应失败时抛的异常类型
    ERROR: type = RuntimeError
    #: 系统提示词
    SYSTEM_PROMPT = ""
    #: `opt.max_tokens` 缺省（或写 0）时的上限
    DEFAULT_MAX_TOKENS = 1024
    #: 空响应提示里的配置段名，如 `coach` → 「可调大 coach.max_tokens」
    CONFIG_SECTION = "llm"

    def __init__(self, options, *, logger=None,
                 session: Optional[aiohttp.ClientSession] = None) -> None:
        self.opt = options
        self.logger = logger
        self._session = session
        self._own_session = session is None

    async def close(self) -> None:
        """关闭自己建的 session（外部传入的不动）。"""
        if self._own_session and self._session is not None:
            if not self._session.closed:
                await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """懒建 session（关掉过的重建；外部的 session 不接管所有权）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.opt.timeout))
            self._own_session = True
        return self._session

    async def _ask(self, prompt: str, system: Optional[str] = None) -> str:
        """发一次对话请求，返回模型输出的原文（解析交给调用方）。

        Args:
            prompt: 用户消息。
            system: 系统提示词（默认用类属性 `SYSTEM_PROMPT`；「先问误区」这类
                不同任务的调用可以单独传）。

        Returns:
            模型的 `content` 原文。

        Raises:
            ERROR: 非 200、网络失败、或返回空内容（都带可读原因）。
        """
        payload = {
            "model": self.opt.model,
            "messages": [
                {"role": "system", "content": system or self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": int(self.opt.max_tokens or self.DEFAULT_MAX_TOKENS),
            "stream": False,
        }
        effort = str(getattr(self.opt, "reasoning_effort", "") or "").strip().lower()
        if effort and effort not in ("off", "inherit", "default"):
            payload["reasoning_effort"] = effort
        url = self.opt.base_url.rstrip("/") + "/chat/completions"
        data = None
        # 网关偶尔会掐断长连接（ServerDisconnectedError）：换一条连接重试一次，
        # 不要因为一次网络抖动就让点评/出题白跑。
        for attempt in range(2):
            session = await self._ensure_session()
            try:
                async with session.post(
                        url, json=payload,
                        headers={"Authorization": f"Bearer {self.opt.api_key}",
                                 "Content-Type": "application/json"}) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200]
                        raise self.ERROR(f"HTTP {resp.status}: {body}")
                    data = await resp.json()
                break
            except (aiohttp.ClientError, OSError) as e:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise self.ERROR(f"网络错误：{type(e).__name__}") from e
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not str(content).strip():
            reason = choice.get("finish_reason")
            hint = ("；可调大 %s.max_tokens" % self.CONFIG_SECTION
                    if reason == "length" else "")
            raise self.ERROR(f"模型返回空内容（finish_reason={reason}）{hint}")
        return str(content)

"""取「用户发上来的代码文件」：私聊文件段 / 群文件上传 / 引用里的文件。

为什么交题走文件而不是贴文本
----------------------------
QQ 消息里的代码会被命令解析器处理引号与 `\\n`、`\\t` 转义（`printf("\\n")`
里的反斜杠会被吃掉），长代码与宏很容易改坏。文件是**字节流**，一个字符都不动，
而且语言可以从后缀判断（`main.cpp` / `sol.py`）。

三条来源，归一成同一个 `CodeFile`：

1. **消息里的 `file` 段**（私聊发文件、`/oj submit 1` 时一起带上的附件）；
2. **群文件上传通知**（`group_upload`）：QQ 群里的「发文件」是 notice 而不是
   普通消息，所以要在事件层接；
3. **被引用的那条消息**（`/oj submit 1` 引用一个代码文件）。

每一路都可能拿不到 URL（平台差异大），所以按 **URL → 落盘路径 → 拒收** 的顺序
降级；不管哪条路，都先过**大小上限**与**是不是文本**两道闸再解码。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import aiohttp

#: 允许直接读取的本机目录（NapCat/QQ 的文件缓存）。**不是**任意路径都读：
#: `get_file`/消息段里的 `url` 可能是用户可控的字符串，放开等于本机任意文件读取。
DEFAULT_LOCAL_ROOTS = (
    "~/.config/QQ",
    "~/.local/share/QQ",
    "/opt/ncqq",
    "/tmp",
    "/var/tmp",
)

#: 代码后缀 → 判题语言（与 `judge.LANGUAGES` 对应）
CODE_EXTS = {
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp", ".hpp": "",
    ".c": "c", ".h": "",
    ".py": "py", ".py3": "py", ".pyw": "py",
    ".txt": "", ".md": "", ".in": "", ".out": "",
}

#: 首块里出现这些字节就大概率不是文本（NUL / 常见二进制魔数）
_BINARY_MAGIC = (
    b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"PK\x03\x04", b"%PDF",
    b"\x7fELF", b"MZ", b"RIFF", b"\x1f\x8b",
)


class CodeFileError(Exception):
    """代码文件不可用（太大 / 不是文本 / 下载失败）—— 人话说明原因。"""


@dataclass
class CodeFile:
    """一份代码文件。"""

    #: 原始文件名（可能为空）
    name: str
    #: 字节数
    size: int
    #: 解码后的文本
    content: str
    #: 由后缀推断的语言；推断不出时为空串（用调用方给的语言）
    language: str

    def describe(self) -> str:
        return f"{self.name or '代码'}（{self.size} 字节）"


# --------------------------------------------------------------------------
# 消息段
# --------------------------------------------------------------------------
def file_segments(message: Any) -> list[dict]:
    """消息里的 `file` 段（兼容 list 与纯字符串两种 `message`）。"""
    if not isinstance(message, list):
        return []
    return [seg for seg in message
            if isinstance(seg, dict) and seg.get("type") == "file"]


def _seg_data(seg: dict) -> dict:
    return seg.get("data") if isinstance(seg.get("data"), dict) else {}


def _name_of(data: dict) -> str:
    """段/通知里的文件名（平台字段名不统一，挨个认）。"""
    for key in ("name", "file_name", "filename", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            # 有些实现把完整路径塞在 `file` 里
            return os.path.basename(value.replace("\\", "/")) or value
    return ""


def _size_of(data: dict) -> int:
    for key in ("file_size", "size"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _id_of(data: dict) -> str:
    for key in ("file_id", "id", "file_uuid", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def language_of(name: str, default: str = "") -> str:
    """按后缀推断语言；推断不出用 `default`。"""
    suffix = os.path.splitext(str(name or ""))[1].lower()
    return CODE_EXTS.get(suffix) or default


def message_text(message: Any) -> str:
    """消息段（或纯文本）→ 文本：只取 `text` 段，图片/文件忽略。"""
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    parts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            parts.append(str((seg.get("data") or {}).get("text") or ""))
    return "".join(parts)


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------
def decode_text(raw: bytes, *, limit: int) -> str:
    """字节 → 文本（UTF-8 优先，退 GB18030，最后替换非法字节）。

    Raises:
        CodeFileError: 空文件、超过上限、或明显是二进制。
    """
    if not raw:
        raise CodeFileError("文件是空的")
    if len(raw) > limit:
        raise CodeFileError(
            f"文件太大（{len(raw)} 字节 > {limit} 字节）；"
            "交题文件请控制在几十 KB 以内")
    head = raw[:4096]
    if b"\x00" in head or any(head.startswith(magic) for magic in _BINARY_MAGIC):
        raise CodeFileError("这看起来不是文本文件（像是图片/压缩包/可执行文件）")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def make_code_file(name: str, raw: bytes, *, limit: int,
                  default_language: str = "") -> CodeFile:
    """字节 + 文件名 → `CodeFile`（解码与语言推断都在这里）。"""
    text = decode_text(raw, limit=limit)
    return CodeFile(name=name, size=len(raw), content=text,
                    language=language_of(name, default_language))


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def local_path_of(value: str) -> str:
    """`file://` URI 或普通路径 → 本机绝对路径（不校验存在性）。"""
    text = str(value or "").strip()
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        text = path
    return os.path.abspath(os.path.expanduser(text)) if text else ""


def within_roots(path: str, roots) -> bool:
    """`path` 是否在 `roots` 里（按 realpath 比，挡住软链接跳出去）。"""
    if not roots:
        return True
    real = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(os.path.expanduser(str(root)))
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:            # 不同盘符 / 非法路径
            continue
    return False


async def download(url: str, *, limit: int, timeout: float = 30.0,
                   roots=None) -> bytes:
    """取一份文件：http(s) 走网络，**本机路径 / `file://` 直接读**。

    Args:
        roots: 允许读取的本机目录；`None`/空 = 不限制（测试用），
            生产由 `submit.local_roots` 传入。
    """
    target = str(url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return await _read_local(target, limit, roots=roots)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(target) as resp:
                if resp.status != 200:
                    raise CodeFileError(f"下载文件失败：HTTP {resp.status}")
                declared = resp.content_length or 0
                if declared and declared > limit:
                    raise CodeFileError(
                        f"文件太大（{declared} 字节 > {limit} 字节）")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise CodeFileError(
                            f"文件太大（超过 {limit} 字节）")
                    chunks.append(chunk)
                return b"".join(chunks)
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise CodeFileError("下载文件超时") from e
    except (aiohttp.ClientError, OSError) as e:
        raise CodeFileError(f"下载文件失败：{type(e).__name__}") from e


# --------------------------------------------------------------------------
# 取文件（三条来源）
# --------------------------------------------------------------------------
async def _read_local(path: str, limit: int, roots=None) -> bytes:
    """读本机路径（机器人 & NapCat 同机时可用；只允许 `roots` 下的目录）。"""
    target = local_path_of(path)
    if not target:
        raise CodeFileError("拿不到文件内容（既没有下载地址，也不是本机文件）")
    if not within_roots(target, roots):
        raise CodeFileError(
            f"本机文件不在允许读取的目录里（{target}）；管理员可以在 "
            "config 的 submit.local_roots 里加上这个目录")
    path = target
    if not os.path.isfile(path):
        raise CodeFileError("拿不到文件内容（既没有下载地址，也不是本机文件）")
    try:
        size = os.path.getsize(path)
        if size > limit:
            raise CodeFileError(f"文件太大（{size} 字节 > {limit} 字节）")
        with open(path, "rb") as f:
            return f.read(limit + 1)
    except OSError as e:
        raise CodeFileError(f"读文件失败：{e}") from e


async def _fetch_bytes(file_manager, api, *, file=None, file_id=None,
                       limit: int, timeout: float = 30.0,
                       roots=None) -> bytes:
    """取文件字节：**优先走框架的文件 API**（`FileManager` / `ApiClient`）。

    平台的 `download_file_stream` 能直接读 NapCat 侧的文件（本机路径、URL、
    FileNapCat UUID 都认），比插件自己发 HTTP / 读磁盘可靠；两条 API 都不可用
    （旧版 NapCat / 单测）时才退回「http 直连 / 本机路径」。
    """
    def _bounded(chunks: list[bytes], chunk: bytes) -> bytes:
        chunks.append(chunk)
        if sum(len(x) for x in chunks) > limit:
            raise CodeFileError(f"文件太大（超过 {limit} 字节）")
        return chunk

    if file_manager is not None:
        try:
            chunks: list[bytes] = []
            async for chunk in file_manager.download_stream(
                    file or "", file_id=file_id, timeout=timeout):
                _bounded(chunks, chunk)
            if chunks:
                return b"".join(chunks)
        except CodeFileError:
            raise
        except Exception:             # noqa: BLE001 - 平台差异大，退下一档
            pass
    if api is not None:
        try:
            chunks = []
            async for chunk in api.download_file_stream(
                    file=file, file_id=file_id, timeout=timeout):
                _bounded(chunks, chunk)
            if chunks:
                return b"".join(chunks)
        except CodeFileError:
            raise
        except Exception:             # noqa: BLE001
            pass
    return await download(file, limit=limit, timeout=timeout, roots=roots)


def _info_url(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("url") or "")
    return str(getattr(info, "url", "") or "")


def _info_path(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("file") or "")
    return str(getattr(info, "file", "") or "")


def _info_name(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("name") or "")
    return str(getattr(info, "name", "") or "")


async def from_segment(segment: dict, api: Any, *, limit: int,
                       timeout: float = 30.0,
                       default_language: str = "",
                       roots=None, file_manager=None) -> CodeFile:
    """消息 `file` 段 → CodeFile（走框架文件 API，失败再退回直连/本机路径）。"""
    data = _seg_data(segment)
    name = _name_of(data)
    declared = _size_of(data)
    if declared and declared > limit:
        raise CodeFileError(f"文件太大（{declared} 字节 > {limit} 字节）")
    url = str(data.get("url") or "")
    file_id = _id_of(data)
    raw: Optional[bytes] = None
    last_error: Optional[CodeFileError] = None

    candidates: list[tuple[Optional[str], Optional[str]]] = []
    if url:
        candidates.append((url, None))
    if file_id:
        candidates.append((None, file_id))
    for file_arg, fid in candidates:
        try:
            raw = await _fetch_bytes(file_manager, api, file=file_arg, file_id=fid,
                                     limit=limit, timeout=timeout, roots=roots)
            break
        except CodeFileError as e:
            last_error = e

    if raw is None and file_id:
        # 退路：有些实现只有 `get_file` 里才有可用的 url / 落盘路径
        try:
            info = await api.get_file(file_id=file_id)
        except Exception as e:  # noqa: BLE001 - 平台差异大，统一转成人话
            info = None
            last_error = CodeFileError(f"读取文件信息失败：{type(e).__name__}")
        if info is not None:
            name = name or _info_name(info)
            source = _info_url(info) or _info_path(info)
            if source:
                try:
                    raw = await _fetch_bytes(file_manager, api, file=source,
                                             limit=limit, timeout=timeout,
                                             roots=roots)
                except CodeFileError as e:
                    last_error = e
    if raw is None:
        if last_error is not None:
            raise last_error
        raise CodeFileError("这条消息里没有可读取的文件")
    return make_code_file(name, raw, limit=limit,
                          default_language=default_language)


async def from_group_upload(event: Any, api: Any, *, limit: int,
                            timeout: float = 30.0,
                            default_language: str = "",
                            roots=None, file_manager=None) -> CodeFile:
    """群文件上传通知（`group_upload`）→ CodeFile。

    群文件不在消息里：先 `get_group_file_url` 换地址，再按文件 ID 让框架取；
    都拿不到时退到 `get_file` / 本机路径。
    """
    info = getattr(event, "file", None) or {}
    if not isinstance(info, dict):
        info = {}
    name = _name_of(info)
    declared = _size_of(info)
    if declared and declared > limit:
        raise CodeFileError(f"文件太大（{declared} 字节 > {limit} 字节）")
    file_id = _id_of(info)
    url = ""
    if file_id:
        try:
            got = await api.get_group_file_url(str(getattr(event, "group_id", "")),
                                               file_id)
        except Exception:  # noqa: BLE001 - 有些实现没有这个 action
            got = None
        if isinstance(got, dict):
            url = str(got.get("url") or "")
        elif isinstance(got, str):
            url = got

    last_error: Optional[CodeFileError] = None
    for file_arg, fid in ((url or None, None), (None, file_id or None)):
        if not file_arg and not fid:
            continue
        try:
            raw = await _fetch_bytes(file_manager, api, file=file_arg, file_id=fid,
                                     limit=limit, timeout=timeout, roots=roots)
            return make_code_file(name, raw, limit=limit,
                                  default_language=default_language)
        except CodeFileError as e:
            last_error = e

    # 退路：当普通文件查一次（拿 url / 落盘路径）
    if file_id:
        try:
            got = await api.get_file(file_id=file_id)
        except Exception:  # noqa: BLE001
            got = None
        if got is not None:
            name = name or _info_name(got)
            source = _info_url(got) or _info_path(got)
            if source:
                try:
                    raw = await _fetch_bytes(file_manager, api, file=source,
                                             limit=limit, timeout=timeout,
                                             roots=roots)
                    return make_code_file(name, raw, limit=limit,
                                          default_language=default_language)
                except CodeFileError as e:
                    last_error = e
    if last_error is not None:
        raise last_error
    raise CodeFileError("拿不到群文件的下载地址")


__all__ = ["CODE_EXTS", "CodeFile", "CodeFileError", "decode_text", "download",
           "file_segments", "from_group_upload", "from_segment", "language_of",
           "make_code_file", "message_text"]

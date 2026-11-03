"""本地判题引擎：编译 → 逐组运行 → 比对 → 给分。

为什么不是「跑一下看看输出」那么简单
--------------------------------------
判的是**别人的程序**，所以每个失败面都要有确定的判据，而不是靠运气：

* **不能死等**：CPU 用 `RLIMIT_CPU` 兜底（忙等），墙上时间再有 `asyncio.wait_for`
  兜一层（`sleep(1e9)` 这类不吃 CPU 的死等）。超时**杀整个进程组**——
  只杀父进程的话，选手 `fork()` 出来的孙子进程会继续跑，把机器拖死；
* **不能刷屏**：stdout 直接重定向到文件，并设 `RLIMIT_FSIZE`。用管道
  `communicate()` 会把「输出 10GB 的程序」整个读进内存；文件 + 大小上限时
  超限的程序会被 `SIGXFSZ` 杀掉，判成 OLE；
* **不能写爆磁盘**：`RLIMIT_FSIZE` 同时限制它写的任何文件；工作目录是一次性的
  临时目录，判完就删；
* **不能借判题提权**：`env` 只留 PATH/LANG/HOME/TMPDIR 这类死值，把选手自己
  塞进来的环境变量（`LD_PRELOAD`、`PYTHONPATH`）全部剥掉。

比对口径对齐洛谷：**忽略每行行尾空格、忽略末尾多余空行**，其余逐字节比较。

⚠️ 这不是安全沙箱：本机禁用 `unshare`（`unshare -n` 直接 Operation not permitted），
所以**没有网络隔离**，也没法降权（机器人以普通用户运行）。真正的第一道闸是
`review.py` 的代码审查，第二道是这里的资源限制。部署时把判题白名单收紧。
"""
from __future__ import annotations

import asyncio
import os
import re
import resource
import sys
import tempfile
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


class JudgeError(Exception):
    """判题环境层面的失败（编译器缺失、工作目录不可写……），不是「选手做错了」。"""


class LanguageError(JudgeError):
    """不支持的语言 / 代码太长。"""


#: 判题状态（存进 Submission.status，也是给人看的那几个字）
AC = "AC"
WA = "WA"
TLE = "TLE"
RE = "RE"
MLE = "MLE"
OLE = "OLE"
CE = "CE"
#: 被判题环境自己判死（超时之外的内部错误）
IE = "IE"

STATUS_LABEL = {
    AC: "通过", WA: "答案错误", TLE: "超时", RE: "运行错误",
    MLE: "超内存", OLE: "输出超限", CE: "编译错误", IE: "判题失败",
}


# --------------------------------------------------------------------------
# 语言
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Language:
    """一种可判题的语言。"""

    key: str
    label: str
    #: 源文件名
    source: str
    #: 是否用 RLIMIT_AS 限内存
    limit_address_space: bool = True
    #: 地址空间上限在「题目内存」之外额外留的余量（MB）。
    #: C/C++ 给 0（题目内存就是程序能用的）；Python 必须留 —— 解释器自己就要
    #: 几十 MB 虚拟地址，卡在 256MB 会在 `import` 阶段就 MemoryError（假阳性 MLE）。
    vm_headroom_mb: int = 0
    #: 语言要求的额外说明（给人看）
    note: str = ""


LANGUAGES: dict[str, Language] = {
    "cpp": Language("cpp", "C++17", "main.cpp"),
    "c": Language("c", "C11", "main.c"),
    # ⚠️ Python 也要设地址空间上限，否则一段 `[0] * 10**10` 能把整台机器拖进
    # OOM（判题机与机器人同机）。留 512MB 余量给解释器本身，只挡「离谱的分配」。
    "py": Language("py", "Python 3", "main.py", vm_headroom_mb=512,
                   note="Python 的内存上限含 512MB 解释器余量，超了表现为 MLE/RE"),
}

#: 用户/模型可能写的别名 → 规范语言名
LANGUAGE_ALIASES = {
    "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "g++": "cpp", "cc": "cpp",
    "c++17": "cpp", "cpp17": "cpp",
    "c": "c", "c11": "c", "gcc": "c",
    "py": "py", "py3": "py", "python": "py", "python3": "py",
}


def resolve_language(raw: str) -> Language:
    """把 `cpp` / `C++` / `py` 这类写法规范化。

    Raises:
        LanguageError: 不支持的语言（错误信息里列出支持哪些）。
    """
    key = str(raw or "").strip().lower()
    if not key:
        raise LanguageError(
            "没给语言（支持：" + " / ".join(LANGUAGES) + "）")
    canonical = LANGUAGE_ALIASES.get(key)
    if canonical is None:
        raise LanguageError(
            f"不支持的语言：{raw}（支持：" + " / ".join(LANGUAGES) + "）")
    return LANGUAGES[canonical]


# --------------------------------------------------------------------------
# 选项 / 结果
# --------------------------------------------------------------------------
@dataclass
class JudgeOptions:
    """判题参数（来自 config.json → plugins.oj_bank.judge）。"""

    #: 工作目录根（每次判题在下面开一个 uuid 子目录，判完删）
    work_root: str = ""
    compile_timeout_ms: int = 20000
    #: 墙上时间的额外余量（毫秒）：题面时限之外再给这么多，避免卡在边界上
    wall_margin_ms: int = 500
    default_time_limit_ms: int = 1000
    default_memory_limit_mb: int = 256
    #: 单组输出上限（KB）
    output_limit_kb: int = 1024
    #: 代码长度上限（字符）
    max_code_chars: int = 64000
    #: 同时判几份（多了会把机器压满；QQ 机器人场景 1 就够）
    max_concurrent: int = 1
    cpp_compiler: str = "g++"
    c_compiler: str = "gcc"
    python: str = ""            # 空 = 用当前解释器（sys.executable）


@dataclass
class CaseResult:
    """一组测试点的结果。"""

    ordinal: int
    status: str
    ms: int = 0
    weight: int = 1
    note: str = ""
    #: 失败时的实际输出预览（截断，给提交者看「差在哪」）
    got: str = ""
    #: 附加说明（退出码、信号、命中哪条限制）
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "status": self.status, "ms": self.ms,
                "weight": self.weight, "note": self.note, "got": self.got,
                "detail": self.detail}


@dataclass
class JudgeResult:
    """一次判题的完整结果。"""

    status: str
    score: int
    passed: int
    total: int
    total_ms: int = 0
    detail: list[CaseResult] = field(default_factory=list)
    #: 编译错误 / 判题环境错误的可读文本
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == AC

    def to_dict(self) -> dict:
        return {"status": self.status, "score": self.score,
                "passed": self.passed, "total": self.total,
                "total_ms": self.total_ms, "message": self.message,
                "detail": [c.to_dict() for c in self.detail]}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.M)


def normalize_output(text: str) -> str:
    """洛谷口径的输出归一化：统一换行、去掉每行行尾空白、去掉末尾空行。

    注意**不去中间空行**（`1\\n\\n2` 与 `1\\n2` 是两种答案），也不折叠行内空格。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _TRAILING_WS_RE.sub("", normalized)
    return normalized.rstrip("\n")


def outputs_match(expected: str, got: str) -> bool:
    """期望输出与实际输出是否一致（按 `normalize_output` 的口径）。"""
    return normalize_output(expected) == normalize_output(got)


def _preview(text: str, limit: int = 160) -> str:
    """单行预览：换行压成 `⏎`，超长截断。"""
    flat = (text or "").replace("\n", "⏎")
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _kill_group(pid: int) -> None:
    """杀掉整个进程组（只杀父进程的话 fork 出来的孙子会漏网）。"""
    try:
        os.killpg(os.getpgid(pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _apply_limits(*, cpu_seconds: int, address_space: int,
                  file_size: int) -> None:
    """在子进程 exec 前设资源上限（`preexec_fn`，只在 fork 后的子进程里跑）。

    ⚠️ 这个函数里**绝不能抛异常**：异常发生在 fork 之后的子进程里，父进程只会
    看到一个莫名其妙的 "Exception occurred in preexec_fn"，真实原因全丢。
    每一条都单独 try 住：某个平台不支持某项限制时，判题应该降级而不是崩溃。
    """
    def _set(what: int, value: int) -> None:
        try:
            resource.setrlimit(what, (value, value))
        except (ValueError, OSError):
            pass

    _set(resource.RLIMIT_CORE, 0)
    _set(resource.RLIMIT_CPU, max(1, int(cpu_seconds)))
    _set(resource.RLIMIT_FSIZE, max(1024, int(file_size)))
    _set(resource.RLIMIT_NOFILE, 64)
    if address_space > 0:
        _set(resource.RLIMIT_AS, int(address_space))


# --------------------------------------------------------------------------
# 判题器
# --------------------------------------------------------------------------
class Judge:
    """把一份代码对着 N 组测试点跑一遍。

    用法::

        judge = Judge(JudgeOptions(work_root="plugins/oj_bank/data/judge"))
        result = await judge.run(language="cpp", code=src, cases=cases,
                                 time_limit_ms=1000, memory_limit_mb=256)
    """

    def __init__(self, options: Optional[JudgeOptions] = None, *, logger=None,
                 code_reviewer=None) -> None:
        self.opt = options or JudgeOptions()
        self.logger = logger
        #: 可选的代码审查器（`review.CodeReviewer`）：给时在**编译前**先审代码，
        #: 审不过直接返回 BLOCKED（不落盘、不编译、不运行）
        self.reviewer = code_reviewer
        self._semaphore: Optional[asyncio.Semaphore] = None

    # ---- 生命周期 ----
    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(
                max(1, int(self.opt.max_concurrent or 1)))
        return self._semaphore

    @property
    def work_root(self) -> Path:
        root = Path(self.opt.work_root or
                    os.path.join(tempfile.gettempdir(), "oj_bank_judge"))
        root.mkdir(parents=True, exist_ok=True)
        return root

    # ---- 主流程 ----
    async def run(self, *, language: str, code: str,
                  cases: Iterable, time_limit_ms: int = 0,
                  memory_limit_mb: int = 0,
                  context: Optional[dict] = None) -> JudgeResult:
        """判一次。

        Args:
            language: 语言（`cpp` / `c` / `py`，别名见 `LANGUAGE_ALIASES`）。
            code: 源码。
            cases: 测试点，每项要有 `.stdin` / `.expected` / `.ordinal` /
                `.weight` / `.note`（`models.TestCase` 正好；也接受同名字典）。
            time_limit_ms: 时限，0 = 用默认。
            memory_limit_mb: 内存上限，0 = 用默认。
            context: 给代码审查器的上下文（题目名、提交者等）。

        Returns:
            `JudgeResult`。**判题环境出错也返回结果**（status=IE），不向调用方抛
            —— 用户需要看到「判题机出问题了」，而不是一个栈。
        """
        try:
            lang = resolve_language(language)
        except LanguageError as e:
            return JudgeResult(status=CE, score=0, passed=0, total=0,
                               message=str(e))
        src = str(code or "")
        if not src.strip():
            return JudgeResult(status=CE, score=0, passed=0, total=0,
                               message="代码是空的")
        if len(src) > self.opt.max_code_chars:
            return JudgeResult(
                status=CE, score=0, passed=0, total=0,
                message=f"代码太长（{len(src)} > {self.opt.max_code_chars} 字符）")

        case_list = [_case_view(c) for c in (cases or [])]
        if not case_list:
            return JudgeResult(status=IE, score=0, passed=0, total=0,
                               message="这道题还没有测试点，没法判分")

        # 代码审查：不通过就不落盘
        verdict = None
        if self.reviewer is not None:
            verdict = await self.reviewer.check(src, language=lang.key,
                                                context=context or {})
            if not verdict.allowed:
                return JudgeResult(
                    status="BLOCKED", score=0, passed=0, total=len(case_list),
                    message=verdict.reason or "代码没有通过安全审查")

        time_ms = int(time_limit_ms or self.opt.default_time_limit_ms or 1000)
        mem_mb = int(memory_limit_mb or self.opt.default_memory_limit_mb or 256)

        async with self._sem():
            return await self._run_locked(lang, src, case_list, time_ms, mem_mb)

    async def _run_locked(self, lang: Language, code: str, cases: list[dict],
                          time_ms: int, mem_mb: int) -> JudgeResult:
        workdir = Path(self.work_root) / f"run-{uuid.uuid4().hex[:12]}"
        try:
            workdir.mkdir(parents=True, exist_ok=False)
        except OSError as e:
            return JudgeResult(status=IE, score=0, passed=0, total=len(cases),
                               message=f"判题目录不可写：{e}")

        started = time.perf_counter()
        try:
            source = workdir / lang.source
            source.write_text(code, encoding="utf-8", errors="replace")

            compile_error = await self._compile(lang, workdir)
            if compile_error is not None:
                return JudgeResult(
                    status=CE, score=0, passed=0, total=len(cases),
                    message=compile_error,
                    total_ms=int((time.perf_counter() - started) * 1000))

            results: list[CaseResult] = []
            for case in cases:
                results.append(await self._run_case(
                    lang, workdir, case, time_ms, mem_mb))
        except JudgeError as e:
            return JudgeResult(status=IE, score=0, passed=0, total=len(cases),
                               message=str(e),
                               total_ms=int((time.perf_counter() - started) * 1000))
        except Exception as e:  # noqa: BLE001 - 判题机的问题不能让命令崩掉
            if self.logger:
                self.logger.warning("[oj_bank] 判题异常", exc_info=True)
            return JudgeResult(status=IE, score=0, passed=0, total=len(cases),
                               message=f"判题机内部错误：{type(e).__name__}: {e}",
                               total_ms=int((time.perf_counter() - started) * 1000))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        return self._score(results, int((time.perf_counter() - started) * 1000))

    # ---- 编译 ----
    async def _compile(self, lang: Language, workdir: Path) -> Optional[str]:
        """编译；成功返回 None，失败返回可读的编译错误（截断）。"""
        if lang.key == "py":
            # 先做一次语法检查：把「缩进错了」判成 CE（和 C++ 的编译错误对齐），
            # 而不是让它在第一组数据上炸成 RE。
            cmd = [self._python(), "-I", "-m", "py_compile", lang.source]
            ok, out = await self._run_compile_cmd(cmd, workdir)
            return None if ok else (out or "Python 语法检查失败")
        if lang.key == "cpp":
            cmd = [self.opt.cpp_compiler or "g++", "-O2", "-std=c++17", "-pipe",
                   "-o", "main", lang.source]
        else:
            cmd = [self.opt.c_compiler or "gcc", "-O2", "-std=c11", "-pipe",
                   "-o", "main", lang.source]
        ok, out = await self._run_compile_cmd(cmd, workdir)
        if ok:
            return None
        if "No such file or directory" in out and (
                "g++" in out or "gcc" in out):
            raise JudgeError("服务器上没有 g++/gcc，C/C++ 判题不可用")
        return out or "编译失败"

    async def _run_compile_cmd(self, cmd: list[str],
                               workdir: Path) -> tuple[bool, str]:
        env = self._env(workdir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env,
                start_new_session=True)
        except OSError as e:
            raise JudgeError(f"启动编译器失败：{e}") from e
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.opt.compile_timeout_ms / 1000)
        except (asyncio.TimeoutError, TimeoutError):
            _kill_group(proc.pid)
            await proc.wait()
            return False, f"编译超时（>{self.opt.compile_timeout_ms}ms）"
        text = (out or b"").decode("utf-8", "replace")
        # 编译器输出可能几千行；只留头尾，够定位就行
        if len(text) > 4000:
            text = text[:2500] + "\n…\n" + text[-1200:]
        return proc.returncode == 0, text

    # ---- 单组运行 ----
    async def _run_case(self, lang: Language, workdir: Path, case: dict,
                        time_ms: int, mem_mb: int) -> CaseResult:
        ordinal = int(case["ordinal"])
        weight = int(case.get("weight") or 1)
        note = str(case.get("note") or "")
        expected = str(case.get("expected") or "")

        out_limit = max(16, int(self.opt.output_limit_kb)) * 1024
        in_path = workdir / f"in-{ordinal}.txt"
        out_path = workdir / f"out-{ordinal}.txt"
        err_path = workdir / f"err-{ordinal}.txt"
        in_path.write_text(str(case.get("stdin") or ""), encoding="utf-8",
                           errors="replace")

        cmd = self._run_cmd(lang, workdir)
        wall = max(0.05, (time_ms + max(0, self.opt.wall_margin_ms)) / 1000)
        cpu_seconds = max(1, int((time_ms + 999) // 1000) + 1)
        address_space = ((mem_mb + int(lang.vm_headroom_mb)) * 1024 * 1024
                         if lang.limit_address_space else 0)
        env = self._env(workdir)

        def _limits() -> None:
            _apply_limits(cpu_seconds=cpu_seconds, address_space=address_space,
                          file_size=out_limit)

        started = time.perf_counter()
        timed_out = False
        with open(in_path, "rb") as fin, open(out_path, "wb") as fout, \
                open(err_path, "wb") as ferr:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(workdir), stdin=fin, stdout=fout, stderr=ferr,
                    env=env, preexec_fn=_limits, start_new_session=True)
            except OSError as e:
                raise JudgeError(f"启动选手程序失败：{e}") from e
            try:
                await asyncio.wait_for(proc.wait(), timeout=wall)
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                _kill_group(proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        rc = proc.returncode if proc.returncode is not None else -9
        out_bytes = _read_head(out_path, out_limit + 1)
        out_text = out_bytes[:out_limit].decode("utf-8", "replace")
        err_text = _read_head(err_path, 4096).decode("utf-8", "replace")

        if timed_out:
            return CaseResult(ordinal, TLE, elapsed_ms, weight, note,
                              detail=f"超过 {time_ms}ms")
        # 输出超限有两条路：C 系程序吃到 SIGXFSZ（returncode -25）；Python 把
        # EFBIG 变成 `OSError: [Errno 27] File too large` 然后非零退出 ——
        # 只看信号会把后者误判成 RE（实测踩过）。
        if (rc == -25 or len(out_bytes) > out_limit
                or _looks_like_ole(err_text)):
            return CaseResult(ordinal, OLE, elapsed_ms, weight, note,
                              detail=f"输出超过 {out_limit // 1024}KB")
        if rc == -24:                                     # SIGXCPU
            return CaseResult(ordinal, TLE, elapsed_ms, weight, note,
                              detail="CPU 时间超限")
        if rc != 0:
            status = MLE if _looks_like_mle(err_text) else RE
            detail = f"退出码 {rc}" if rc > 0 else f"被信号 {-rc} 终止"
            preview = _preview(err_text) if err_text.strip() else ""
            return CaseResult(ordinal, status, elapsed_ms, weight, note,
                              got=preview, detail=detail)
        if outputs_match(expected, out_text):
            return CaseResult(ordinal, AC, elapsed_ms, weight, note)
        return CaseResult(ordinal, WA, elapsed_ms, weight, note,
                          got=_preview(out_text),
                          detail=f"期望 {_preview(expected, 80)}")

    # ---- 命令 / 环境 ----
    def _python(self) -> str:
        return self.opt.python or sys.executable or "python3"

    def _run_cmd(self, lang: Language, workdir: Path) -> list[str]:
        if lang.key == "py":
            return [self._python(), "-I", "-u", lang.source]
        return ["./main"]

    def _env(self, workdir: Path) -> dict:
        """选手进程的环境：**只留死值**，不继承机器人的环境变量。"""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }

    # ---- 计分 ----
    def _score(self, results: list[CaseResult], total_ms: int) -> JudgeResult:
        """按权重算分；整题状态取**第一组没过的状态**（最接近人话）。"""
        total_weight = sum(max(1, r.weight) for r in results) or 1
        passed_weight = sum(max(1, r.weight) for r in results
                            if r.status == AC)
        score = int(round(100 * passed_weight / total_weight))
        if passed_weight == total_weight:
            status = AC
        else:
            status = next(r.status for r in results if r.status != AC)
        return JudgeResult(status=status, score=score,
                           passed=sum(1 for r in results if r.status == AC),
                           total=len(results), total_ms=total_ms,
                           detail=results)


# --------------------------------------------------------------------------
# 内部小工具
# --------------------------------------------------------------------------
def _case_view(case) -> dict:
    """把 `TestCase` 模型或 dict 收敛成判题引擎用的最小结构。"""
    if isinstance(case, dict):
        return {
            "ordinal": int(case.get("ordinal") or 0),
            "stdin": str(case.get("stdin", case.get("input", "")) or ""),
            "expected": str(case.get("expected", case.get("output", "")) or ""),
            "weight": int(case.get("weight") or 1),
            "note": str(case.get("note") or ""),
            "kind": str(case.get("kind") or "hack"),
        }
    return {
        "ordinal": int(getattr(case, "ordinal", 0) or 0),
        "stdin": str(getattr(case, "stdin", "") or ""),
        "expected": str(getattr(case, "expected", "") or ""),
        "weight": int(getattr(case, "weight", 1) or 1),
        "note": str(getattr(case, "note", "") or ""),
        "kind": str(getattr(case, "kind", "hack") or "hack"),
    }


def _read_head(path: Path, limit: int) -> bytes:
    """只读文件开头 `limit` 字节（输出文件可能几 MB，别整读）。"""
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


_OLE_HINTS = ("File too large", "Errno 27", "EFBIG")


def _looks_like_ole(stderr: str) -> bool:
    """stderr 里出现「文件太大」的典型字样 → OLE（Python 不接 SIGXFSZ）。"""
    text = stderr or ""
    return any(hint in text for hint in _OLE_HINTS)


_MLE_HINTS = ("bad_alloc", "MemoryError", "Cannot allocate memory",
              "std::bad_alloc", "out of memory", "OutOfMemory")


def _looks_like_mle(stderr: str) -> bool:
    """stderr 里出现内存分配失败的典型字样 → 判 MLE 而不是 RE。"""
    text = stderr or ""
    return any(hint in text for hint in _MLE_HINTS)


__all__ = ["AC", "CE", "IE", "LANGUAGES", "MLE", "OLE", "RE", "STATUS_LABEL",
           "TLE", "WA", "CaseResult", "Judge", "JudgeError", "JudgeOptions",
           "JudgeResult", "Language", "LanguageError", "normalize_output",
           "outputs_match", "resolve_language"]

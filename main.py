"""oj_bank —— 本地题库 + 判题（洛谷抓题 / AI 搜题入库 / 出图 / 题组）。

职责边界
--------
* **题库**（`repo.py`）：题目、测试点、题组、提交记录；
* **抓题**（`luogu.py`）：洛谷题目页 → `Problem`；
* **出图**（`render.py` + `markdown.py`）：题面渲染成 PNG（QQ 里看图比读长文舒服）；
* **判题**（`judge.py`）：编译/运行/比对/评分，是唯一的高风险动作；
* **审查**（`review.py`）：判题前的代码审查（静态规则 + 可选模型审查）；
* **对 AI**（`tools.py`）：`register_tool`，**结果回给模型**（不是命令映射）；
* **对人**（`commands.py`）：`/oj ...` 一组命令。

这个文件只做**装配 + 共用逻辑**（配置读取、服务白名单、判题编排、回复），
命令与工具各自在 `commands.py` / `tools.py` 里（注册与逻辑分离，见
`docs/plugin-dev/14-layout.md`）。

服务范围（白名单）
------------------
绑定命令与 `ai_chat` 同一套（`plugins.oj_bank.binding`），但群名单**独立**：

* 群：**只看 `binding.groups`**（不回落框架的 `bound_groups` —— 「监听哪些群」
  与「允许哪些群用」是两件事），为空 = 一个群都不许；
* 私聊：`binding.users` 是唯一闸门（框架不管私聊），为空 = 一个都不许；
* **超级用户豁免**（与其它插件同口径，见 `napcat.subscribe`）—— 否则名单配错时
  连第一条都开不出来。

判题是执行别人的代码，白名单是这里的**第一道**安全边界（第二道是 `review.py`）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from napcat.models import GroupUploadEvent, MessageEvent
from napcat.plugin import PluginBase

from .coach import CodeCoach, CoachError, CoachOptions
from .gen import CaseGenerator, GenError, GenOptions, run_script
from .codefile import (DEFAULT_LOCAL_ROOTS, CodeFileError, file_segments,
                       from_group_upload, from_segment)
from .judge import (AC, STATUS_LABEL, Judge, JudgeOptions, JudgeResult,
                    resolve_language, LanguageError)
from .luogu import (DEFAULT_USER_AGENT, LuoguError, ProblemNotFound,
                    fetch_problem, normalize_pid)
from .models import (MAX_HACK_CASES, Problem, ProblemSet, SetItem, Submission,
                     TestCase)
from .render import ProblemRenderer, RenderOptions
from .repo import (OJError, add_cases, cases_of, get_problem, list_problems,
                   luogu_key, record_submission, sanitize_notes, update_case,
                   upsert_problem)
from .review import CodeReviewer, ReviewOptions

from napcat.subscribe import Subscription, parse_ids

#: 状态图标（判题结果里的每一组）
STATUS_ICON = {
    AC: "✅", "WA": "❌", "TLE": "⏱", "RE": "💥", "MLE": "🧠",
    "OLE": "📤", "CE": "🛠", "BLOCKED": "🚫", "IE": "⚠️",
}

#: 消息开头连续的 `[@123]`（群里 @ 机器人时 plain_text 会带上它）
_AT_PREFIX_RE = re.compile(r"^(?:\[@\d+\]\s*)+")


@dataclass
class PendingSubmit:
    """「下一步把这句消息当代码」的待办（用户发 `/oj submit 12 cpp` 后建立）。"""

    problem_id: int
    language: str
    #: 聊天标识：只在这个聊天里收代码（同一用户在别的群说的不算）
    chat_key: tuple
    expires_at: float


class OJBankPlugin(PluginBase):
    """本地题库 / 判题 / 题组插件。"""

    name = "oj_bank"
    version = "0.1.0"
    description = "本地题库与判题（洛谷抓题 / AI 搜题入库 / 题目出图 / 测试点 / 题组）"
    author = "ncqqpy"

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self):
        self._data_dir = self._resolve_data_dir()
        os.makedirs(self._data_dir, exist_ok=True)
        # 库文件放在 `data_dir` 下（默认 `plugins/oj_bank/data/`）。**必须显式给
        # path**：`self.database()` 的默认落点是 `plugin_dir/data/<插件名>.db`，
        # 配置里的 data_dir 就管不到它了，测试/迁移时会被写进仓库目录。
        self.database(path=Path(self._data_dir) / f"{self.name}.db",
                      ensure=[Problem, TestCase, ProblemSet, SetItem, Submission])
        # 老数据里的「提示/题解/广告」清掉，只留数据范围（新数据在入库时过滤）
        try:
            cleaned = sanitize_notes()
        except OJError:
            cleaned = 0
        if cleaned:
            self.logger.info("[oj_bank] 清理了 %d 道题的提示，只保留数据范围", cleaned)
        #: 用户 → 待提交的代码（`/oj submit` 两步走）
        self._pending: dict[int, PendingSubmit] = {}
        #: 本插件注册过的命令名（`on_disable` 要逐条撤，框架不会自动收）
        self._command_names: list[str] = []
        #: 用户 → 上次判题时间（冷却）
        self._last_submit: dict[int, float] = {}
        self._reviewer: Optional[CodeReviewer] = None
        self._coach: Optional[CodeCoach] = None
        self._gen: Optional[CaseGenerator] = None
        self._gen_lock: Optional[asyncio.Semaphore] = None
        #: 入库后自动生成测试点的后台任务（关插件时取消）
        self._gen_tasks: set = set()
        #: 启动补旧题的后台任务
        self._backfill_task: Optional[asyncio.Task] = None
        #: 正在生成测试点的题目 id（同一题不并发生成，避免重复写）
        self._gen_active: set[int] = set()
        #: 点评是后台任务：判题结果先发出去，点评后到（见 `schedule_coach`）
        self._coach_tasks: set = set()
        self._coach_lock: Optional[asyncio.Semaphore] = None
        self._visibility_filter = None
        self._judge: Optional[Judge] = None
        self._renderer: Optional[ProblemRenderer] = None
        self.logger.info("[oj_bank] 已加载（数据目录 %s）", self._data_dir)

    async def on_enable(self):
        self._build_services()
        from . import commands, tools
        commands.register(self)
        tools.register(self)
        self._install_visibility_filter()
        # 老题（在本功能之前入库的）没有测试点：启动时补上，
        # 否则「所有题都有 10 组」只对新入库的题成立。
        self._schedule_backfill()
        self.on_message()(self._on_code_message)
        # 群文件上传是 notice（不是消息），交 .cpp 到群文件时靠这条路径
        self.on_event(GroupUploadEvent, description="群文件交题")(
            self._on_group_upload)
        self.logger.info("[oj_bank] 已启用（%d 条命令）", len(self._command_names))
        # 服务范围为空时最容易「看起来装好了、其实哪都用不了」：启动时点一句，
        # 免得用户以为是 AI 没把题存进去（实际是工具调用被服务范围挡了）。
        if not self.bound_groups():
            self.logger.warning(
                "[oj_bank] 群名单为空：群里用不了（超管私聊发 "
                "/oj bind add group <群号> 开通）")
        if not self.binding_ids("users"):
            self.logger.warning(
                "[oj_bank] 私聊名单为空：私聊用不了（超管私聊发 "
                "/oj bind add user 开通；省略号 = 自己）")

    async def on_disable(self):
        await super().on_disable()
        self._pending.clear()
        for name in list(self._command_names):
            try:
                self.unregister_command(name)
            except Exception:  # noqa: BLE001 - 停用阶段尽力而为
                pass
        self._command_names = []
        # 工具跟命令一样绑在这个实例上：不同步撤掉的话，禁用后模型仍然能在
        # 工具表里看到它，调用进来是一段「已停用插件」的 handler（框架的
        # `disable()` 不负责收工具表，卸载/热重载才收）。
        try:
            self.plugin_tools.unregister_plugin(self.name)
        except Exception:  # noqa: BLE001
            pass
        for task in list(getattr(self, "_gen_tasks", ()) or ()):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._gen_tasks = set()
        for task in list(getattr(self, "_coach_tasks", ()) or ()):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._coach_tasks = set()
        if self._coach is not None:
            await self._coach.close()
            self._coach = None
        self._gen_active = set()
        if self._gen is not None:
            await self._gen.close()
            self._gen = None
        self._coach_lock = None
        self._gen_lock = None
        if self._visibility_filter is not None:
            try:
                self.cmds.remove_visibility_filter(self._visibility_filter)
            except Exception:  # noqa: BLE001
                pass
            self._visibility_filter = None
        if self._reviewer is not None:
            await self._reviewer.close()
            self._reviewer = None
        self._judge = None
        self._renderer = None
        self._last_submit.clear()
        self.logger.info("[oj_bank] 已停用")

    async def on_unload(self):
        if self._reviewer is not None:
            await self._reviewer.close()
            self._reviewer = None

    # ------------------------------------------------------------------ 装配
    def _build_services(self) -> None:
        """按当前配置建好判题 / 审查 / 出图服务（配置改了重新启用即生效）。"""
        judge_cfg = self.config.get("judge") or {}
        review_cfg = self.config.get("review") or {}
        ai_cfg = review_cfg.get("ai") or {}
        render_cfg = self.config.get("render") or {}

        self._reviewer = CodeReviewer(
            ReviewOptions(
                rules=bool(review_cfg.get("rules", True)),
                ai_enabled=bool(ai_cfg.get("enabled", False)),
                base_url=str(ai_cfg.get("base_url") or ""),
                model=str(ai_cfg.get("model") or ""),
                # 密钥优先环境变量（别把明文密钥发进仓库/日志）
                api_key=(os.environ.get("OJ_REVIEW_API_KEY")
                         or str(ai_cfg.get("api_key") or "")),
                timeout=float(ai_cfg.get("timeout") or 30),
                max_tokens=int(ai_cfg.get("max_tokens") or 1024),
                reasoning_effort=str(ai_cfg.get("reasoning_effort") or "none"),
                on_error=str(ai_cfg.get("on_error") or "deny"),
                cache_size=int(ai_cfg.get("cache_size") or 256),
                max_code_chars=int(judge_cfg.get("max_code_chars") or 60000),
            ),
            logger=self.logger,
        )
        self._judge = Judge(
            JudgeOptions(
                work_root=os.path.join(self._data_dir, "judge"),
                compile_timeout_ms=int(judge_cfg.get("compile_timeout_ms") or 20000),
                wall_margin_ms=int(judge_cfg.get("wall_margin_ms") or 500),
                default_time_limit_ms=int(
                    judge_cfg.get("default_time_limit_ms") or 1000),
                default_memory_limit_mb=int(
                    judge_cfg.get("default_memory_limit_mb") or 256),
                output_limit_kb=int(judge_cfg.get("output_limit_kb") or 1024),
                max_code_chars=int(judge_cfg.get("max_code_chars") or 64000),
                max_concurrent=int(judge_cfg.get("max_concurrent") or 1),
                cpp_compiler=str(judge_cfg.get("cpp_compiler") or "g++"),
                c_compiler=str(judge_cfg.get("c_compiler") or "gcc"),
            ),
            logger=self.logger,
            code_reviewer=self._reviewer,
        )
        coach_cfg = self.config.get("coach") or {}
        gen_cfg = self.config.get("gen") or {}
        coach_base, coach_model, coach_key = self._llm_connection(coach_cfg)
        gen_base, gen_model, gen_key = self._llm_connection(
            gen_cfg, key_env="OJ_GEN_API_KEY")

        self._gen = CaseGenerator(
            GenOptions(
                enabled=bool(gen_cfg.get("enabled", True)),
                base_url=gen_base, model=gen_model, api_key=gen_key,
                count=int(gen_cfg.get("count") or 10),
                timeout=float(gen_cfg.get("timeout") or 600),
                max_seconds=float(gen_cfg.get("max_seconds") or 3600),
                max_tokens=int(gen_cfg.get("max_tokens") or 65536),
                max_statement_chars=int(gen_cfg.get("max_statement_chars") or 6000),
                max_input_chars=int(gen_cfg.get("max_input_chars") or 20000),
                pitfalls=bool(gen_cfg.get("pitfalls", True)),
                allow_scripts=bool(gen_cfg.get("allow_scripts", True)),
                script_timeout=float(gen_cfg.get("script_timeout") or 15),
                reasoning_effort=str(gen_cfg.get("reasoning_effort") or ""),
            ),
            logger=self.logger,
        )
        self._gen_lock = asyncio.Semaphore(1)
        self._coach = CodeCoach(
            CoachOptions(
                enabled=bool(coach_cfg.get("enabled", True)),
                base_url=coach_base, model=coach_model, api_key=coach_key,
                timeout=float(coach_cfg.get("timeout") or 300),
                max_tokens=int(coach_cfg.get("max_tokens") or 32768),
                max_code_chars=int(coach_cfg.get("max_code_chars") or 12000),
                max_chars=int(coach_cfg.get("max_chars") or 700),
                reasoning_effort=str(coach_cfg.get("reasoning_effort") or ""),
            ),
            logger=self.logger,
        )
        self._coach_lock = asyncio.Semaphore(1)
        self._renderer = ProblemRenderer(
            RenderOptions(
                width=int(render_cfg.get("width") or 760),
                timeout=float(render_cfg.get("timeout") or 45),
                max_height=int(render_cfg.get("max_height") or 8000),
                keep_files=int(render_cfg.get("keep_files") or 120),
                chromium=str(render_cfg.get("chromium") or ""),
                virtual_time_budget=int(
                    render_cfg.get("virtual_time_budget") or 4000),
            ),
            out_dir=os.path.join(self._data_dir, "renders"),
            logger=self.logger,
        )

    def config_template(self) -> dict:
        return {
            "data_dir": "",
            # 服务范围：**本插件独立的一份名单**，不读框架的 bound_groups。
            # 空 = 谁都不用（要开就 `/oj bind add` 显式加，口径见 napcat.subscribe）。
            "binding": {"groups": [], "users": []},
            "max_cases": MAX_HACK_CASES,
            "list_page_size": 8,
            "submit": {
                # 允许直接读的本机目录（NapCat/QQ 的文件缓存）。不要配成 "/"：
                # 文件路径来自平台返回，放开等于本机任意文件读取。
                "local_roots": ["~/.config/QQ", "~/.local/share/QQ",
                                "/opt/ncqq", "/tmp", "/var/tmp"],
            },
            "judge": {
                "enabled": True,
                "max_concurrent": 1,
                "cooldown_seconds": 20,
                "submit_window_seconds": 600,
                "compile_timeout_ms": 20000,
                "wall_margin_ms": 500,
                "default_time_limit_ms": 1000,
                "default_memory_limit_mb": 256,
                "output_limit_kb": 1024,
                "max_code_chars": 64000,
                # 交题文件的大小上限（KB）：代码文件一般几十 KB，给大了只是给它
                # 一个把内存撑爆的机会
                "max_file_kb": 64,
                "cpp_compiler": "g++",
                "c_compiler": "gcc",
            },
            "review": {
                "rules": True,
                "ai": {
                    "enabled": False,
                    "base_url": "https://myai.bupt.edu.cn/llm-gw/v1",
                    "model": "deepseek-v4-flash",
                    "api_key": "",
                    "timeout": 30,
                    "max_tokens": 1024,
                    "reasoning_effort": "none",
                    "on_error": "deny",
                    "cache_size": 256,
                },
            },
            "gen": {
                # AI 出测试点：一次一组（1 份输入 + 1 份输出），重复到凑满
                "enabled": True,
                # 先问「本题常见误区」，再针对误区出卡点；没有误区则全出常规点
                "pitfalls": True,
                # 允许模型用 Python 脚本生成输入/输出（沙箱里跑）
                "allow_scripts": True,
                "script_timeout": 15,
                # **所有入库的题目都自动生成 10 组**（命令/工具/AI 存题都一样）
                "auto_insert": True,
                # 允许多请求几次补缺口（一次失败只影响那一组）
                "retry_missing": 3,
                # 启动时把库里**没有测试点**的旧题补上（默认开，最多补几道）
                "backfill_on_start": True,
                "backfill_limit": 5,
                "count": 10,
                # **少量多次**：每批只要几组输入，一次要 10 组既慢又容易截断
                "batch_size": 3,
                # 整次生成的总预算（秒），超了就用已有结果收工
                "max_seconds": 900,
                "timeout": 180,
                # 单次输出很小；留余量防长题面/思考模型截断
                "max_tokens": 65536,
                "max_statement_chars": 6000,
                "max_input_chars": 20000,
                "base_url": "",
                "model": "",
                "api_key": "",
                "reasoning_effort": "",
            },
            "coach": {
                # 判完分之后的 AI 点评：总结解法 / 复杂度 / 改进建议。
                # enabled=false 关掉；连接三项留空时借用 plugins.ai_chat.llm
                "enabled": True,
                "base_url": "",
                "model": "",
                "api_key": "",
                "timeout": 300,
                # qwen 是思考模型：光「想」就可能上万 token，给足空间
                "max_tokens": 32768,
                "max_code_chars": 12000,
                "max_chars": 700,
                "reasoning_effort": "",
            },
            "render": {
                "width": 760,
                "timeout": 45,
                "max_height": 8000,
                "keep_files": 120,
                "chromium": "",
                "virtual_time_budget": 4000,
            },
            "luogu": {
                "timeout": 20,
                "user_agent": "",
                "cookie": "",
                # 题库里已有同一道洛谷题时，是否按洛谷题面覆盖
                "overwrite": False,
            },
        }

    # ------------------------------------------------------------------ 路径 / 配置
    def _resolve_data_dir(self) -> str:
        """数据目录**必须是绝对路径**。

        `plugin_dir` 来自配置里的 `plugin_dir`（默认 `./plugins`），是相对路径；
        直接拼出来的 `plugins/oj_bank/data` 交给 `Path.as_uri()` 会抛
        `ValueError: relative paths can't be expressed as file URIs` ——
        出图、发图全挂（实测就是这样）。所以这里统一 `abspath`。
        """
        raw = str(self.config.get("data_dir", "") or "").strip()
        if raw:
            return os.path.abspath(raw)
        base = Path(self.plugin_dir) if self.plugin_dir else Path("plugins") / self.name
        return os.path.abspath(str(Path(base) / "data"))

    @property
    def data_dir(self) -> str:
        return getattr(self, "_data_dir", self._resolve_data_dir())

    @property
    def judge(self) -> Optional[Judge]:
        return self._judge

    @property
    def renderer(self) -> Optional[ProblemRenderer]:
        return self._renderer

    @property
    def reviewer(self) -> Optional[CodeReviewer]:
        return self._reviewer

    def _bool_cfg(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")

    @property
    def judge_enabled(self) -> bool:
        return self._bool_cfg("judge.enabled", True)

    @property
    def max_cases(self) -> int:
        try:
            return max(1, int(self.config.get("max_cases", MAX_HACK_CASES)))
        except (TypeError, ValueError):
            return MAX_HACK_CASES

    @property
    def file_roots(self) -> list[str]:
        """允许直接读取的本机目录（NapCat 文件缓存）。

        默认值覆盖 Linux 上 QQ/NapCat 的常见落盘位置；文件在别处时，
        管理员把它加进 `submit.local_roots`。
        """
        raw = self.config.get("submit.local_roots")
        if raw in (None, ""):
            raw = list(DEFAULT_LOCAL_ROOTS)
        if isinstance(raw, str):
            raw = [raw]
        roots = []
        for item in raw or []:
            text = str(item or "").strip()
            if text:
                roots.append(os.path.abspath(os.path.expanduser(text)))
        return roots

    @property
    def max_file_bytes(self) -> int:
        """交题文件的大小上限（字节）。"""
        try:
            kb = int(self.config.get("judge.max_file_kb", 64) or 64)
        except (TypeError, ValueError):
            kb = 64
        return max(1, min(1024, kb)) * 1024

    @property
    def list_page_size(self) -> int:
        try:
            return max(1, min(20, int(self.config.get("list_page_size", 8))))
        except (TypeError, ValueError):
            return 8

    # ------------------------------------------------------------------ 服务范围
    @staticmethod
    def _to_ids(raw) -> set[int]:
        """配置里的列表 → 正整数集合（解析口径见 `napcat.subscribe.parse_ids`）。"""
        return parse_ids(raw)

    def _subscription(self) -> Subscription:
        """服务范围（口径见 `napcat.subscribe`）：只看 `binding`，空 = 谁都不用。"""
        return Subscription.of(self.config.get("binding"))

    def binding_ids(self, key: str) -> set[int]:
        """`binding.groups` / `binding.users` 的当前值。"""
        return parse_ids(self.config.get(f"binding.{key}") or [])

    def bound_groups(self) -> set[int]:
        """群白名单：**只看本插件的 `binding.groups`**，不回落框架。

        `bound_groups` 是「机器人监听哪些群」（不在里面的群消息根本不会到达
        插件），与「允许哪些群用本插件」是两件事 —— 混在一起会让「加一个群给
        别的插件用」顺带把题库也开了。要开就 `/oj bind add group` 显式写。
        """
        return self.binding_ids("groups")

    @staticmethod
    def chat_target(event) -> tuple[str, int]:
        """事件 → `(群/私聊, 目标号)`。

        群聊看 `group_id`；**群临时会话（小窗）按来源群判**（与 byrdocs 同口径）；
        其余私聊看发消息的人。
        """
        group_id = int(getattr(event, "group_id", 0) or 0)
        if _is_group_event(event):
            return "group", group_id
        if getattr(event, "is_temp_session", False) and group_id:
            return "group", group_id
        return "private", int(getattr(event, "user_id", 0) or 0)

    def check_access(self, *, user_id: int, chat_kind: str,
                     chat_id: int) -> tuple[bool, str]:
        """这个聊天是否在服务范围内。返回 `(允许, 回绝原因)`。

        白名单口径与其它插件一致（见 `napcat.subscribe`）：**只看本插件自己的
        `binding`，空 = 谁都不用，不回落框架 `bound_groups`**；群与私聊是两条
        独立通道（群开通 = 全群可用；私聊按人开通），谁也不能替代谁。

        **超级用户豁免**：`/oj bind add` 本身只在私聊由超管执行，不豁免就会
        「名单配错 → 谁都改不了」。
        """
        hint = "config.json → plugins.oj_bank.binding"
        uid = int(user_id)
        if self.perm.is_superuser(uid):
            return True, ""                  # 豁免：防止白名单把自己锁在外面
        subscription = self._subscription()
        groups = self.bound_groups()
        users = self.binding_ids("users")

        if chat_kind == "group":
            if subscription.group_ok(chat_id):
                return True, ""
            if not groups:
                return False, (f"本群没有开通题库服务（{hint}.groups 为空）。"
                               "联系管理员用 /oj bind add group 开通。")
            return False, (f"本群（{chat_id}）没有开通题库服务。"
                           f"联系管理员把它加到 {hint}.groups。")

        if subscription.user_ok(uid):
            return True, ""
        if not users:
            return False, (f"私聊没有开通题库服务（{hint}.users 为空）。"
                           "联系管理员把你加入白名单。")
        return False, (f"你（{uid}）没有开通题库服务。"
                       f"联系管理员把你加到 {hint}.users。")

    def access_ok(self, *, user_id: int, group_id: int = 0,
                  is_group: bool = False) -> bool:
        """按群 / 私聊判服务范围（命令与工具共用的薄封装）。"""
        kind = "group" if is_group else "private"
        chat_id = int(group_id) if is_group else int(user_id)
        return self.check_access(user_id=int(user_id), chat_kind=kind,
                                 chat_id=chat_id)[0]

    def event_access_ok(self, event) -> bool:
        """命令入口的服务范围判定（兼容通知类事件：群文件上传没有 message_type）。"""
        kind, chat_id = self.chat_target(event)
        return self.check_access(user_id=int(getattr(event, "user_id", 0) or 0),
                                 chat_kind=kind, chat_id=chat_id)[0]

    def tool_access_ok(self, ctx) -> bool:
        """AI 工具入口的服务范围判定（`ctx` 是 ai_chat 的 `ToolContext`，鸭子类型）。"""
        if ctx is None:
            return True              # 没有上下文（脚本/单测直接调）→ 不拦
        is_group = bool(getattr(ctx, "in_group", False))
        group_id = int(getattr(ctx, "group_id", 0)
                       or getattr(ctx, "chat_id", 0) or 0)
        return self.access_ok(user_id=int(getattr(ctx, "user_id", 0) or 0),
                              group_id=group_id, is_group=is_group)

    def manage_binding(self, *, action: str, kind: str, target: int,
                       user_id: int) -> str:
        """管理服务范围（`/oj bind ...`）。返回给用户的话。

        名单**只有本插件这一份**（不读框架的 `bound_groups`）：加/删都是对
        `binding.groups` / `binding.users` 的直接增删，没有「先继承再固化」。
        """
        key = "groups" if kind == "group" else "users"
        label = "群" if kind == "group" else "用户"
        groups = self.bound_groups()
        users = self.binding_ids("users")

        if action == "list":
            lines = ["🔐 题库服务范围（独立绑定，不跟框架的 bound_groups 走）"]
            lines.append("群：" + (("、".join(str(g) for g in sorted(groups)))
                                  or "（空）"))
            lines.append("私聊：" + (("、".join(str(u) for u in sorted(users)))
                                    or "（空）"))
            lines.append("名单为空时：拒绝（要开就 /oj bind add 显式加）")
            lines.append("开通：/oj bind add group <群号> · /oj bind add user <QQ号>")
            lines.append("停用：/oj bind del group <群号> · /oj bind del user <QQ号>")
            return "\n".join(lines)

        ids = self.binding_ids(key)
        if action == "add":
            if kind == "user":
                target = int(target or user_id)   # 省略号 = 开通自己
            if not target:
                return f"⚠️ 要给个{label}号：/oj bind add {kind} <号>"
            if int(target) in ids:
                return f"ℹ️ {label} {target} 已经在服务范围内了"
            ids.add(int(target))
        else:
            if kind == "user":
                target = int(target or user_id)
            if not target:
                return f"⚠️ 要给个{label}号：/oj bind del {kind} <号>"
            if int(target) not in ids:
                return (f"⚠️ {label} {target} 不在服务范围内"
                        f"（/oj bind list 查看）")
            ids.discard(int(target))

        self.config.set(f"binding.{key}", sorted(ids))
        verb = "开通" if action == "add" else "停用"
        return (f"✅ 已{verb}{label} {target} 的题库服务（现在 {len(ids)} 个{label}）\n"
                f"写入 config.json → plugins.oj_bank.binding.{key}")

    # ------------------------------------------------------------------ 帮助可见性
    def _install_visibility_filter(self) -> None:
        """让 `/help` 与执行同口径：服务范围外看不到 `/oj` 这一族。

        超级用户例外：部署还没开通时，正是最需要看到 `/oj bind` 用法的时刻，
        把整个命名空间藏起来等于把出路也藏了。过滤只作用于本命名空间。
        """
        cmds = getattr(self, "cmds", None)
        if cmds is None or not hasattr(cmds, "add_visibility_filter"):
            return
        if self._visibility_filter is not None:
            try:
                cmds.remove_visibility_filter(self._visibility_filter)
            except Exception:  # noqa: BLE001
                pass

        def _visible(handler, event) -> bool:
            name = getattr(handler, "name", "")
            parent = getattr(handler, "parent_name", "")
            if not (name == "oj" or parent == "oj"):
                return True
            if event is None:
                return True
            uid = int(getattr(event, "user_id", 0) or 0)
            if self.perm is not None and self.perm.is_superuser(uid):
                return True
            return self.event_access_ok(event)

        self._visibility_filter = _visible
        cmds.add_visibility_filter(_visible)

    # ------------------------------------------------------------------ 回复
    async def reply(self, event, message) -> None:
        """把消息发回**来的地方**（群消息发群、私聊发私聊、小窗带群号）。

        群文件上传通知没有 `message_type`，但有 `group_id` —— 回群聊。
        """
        if _is_group_event(event) and getattr(event, "group_id", None):
            await self.api.send_group_msg(group_id=str(event.group_id),
                                          message=message)
            return
        await self.api.send_private_msg(
            str(getattr(event, "user_id", "")), message,
            group_id=(str(event.group_id)
                      if getattr(event, "is_temp_session", False) else None))

    @staticmethod
    def reply_message_id(event: MessageEvent) -> Optional[int]:
        """消息里 `reply` 段指向的消息 id（没有引用就 None）。"""
        message = getattr(event, "message", None)
        if not isinstance(message, list):
            return None
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                data = seg.get("data") or {}
                raw = data.get("id") or data.get("message_id") or data.get("seq")
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
        return None

    async def quoted_message(self, event: MessageEvent):
        """引用那条消息的内容（原始 `message`：字符串或段列表；没有就 None）。

        `get_msg` 走框架的 API 节流（线上 1~5 秒一次），所以**只在用户真的
        引用了消息时**才调；失败按「没有引用」处理，别让交题卡在一个取不到的
        历史消息上。
        """
        message_id = self.reply_message_id(event)
        if not message_id:
            return None
        try:
            data = await self.api.get_msg(message_id)
        except Exception as e:  # noqa: BLE001 - 取不到引用不是致命错误
            if self.logger:
                self.logger.debug("[oj_bank] 读取引用消息失败：%r", e)
            return None
        return (data or {}).get("message") if isinstance(data, dict) else data

    def _llm_connection(self, cfg: dict, *,
                        key_env: str = "OJ_COACH_API_KEY") -> tuple[str, str, str]:
        """这个 LLM 用哪套连接：本插件配置优先，缺的字段**借用 `ai_chat.llm`**。

        同一个网关、同一把 key，为了一次点评/出题再配一遍没意义；想换模型就单独写
        `base_url/model/api_key`（任一项写了就以它为准）。密钥优先读环境变量
        （`OJ_COACH_API_KEY` / `OJ_GEN_API_KEY`），别把明文写进仓库。
        """
        base = str(cfg.get("base_url") or "").strip()
        model = str(cfg.get("model") or "").strip()
        key = (os.environ.get(key_env) or str(cfg.get("api_key") or "")).strip()
        if base and model and key:
            return base, model, key
        try:
            llm = self._cfg_root.get("plugins.ai_chat.llm") or {}
        except Exception:  # noqa: BLE001 - 读不到兄弟插件配置不算错误
            llm = {}
        if isinstance(llm, dict):
            base = base or str(llm.get("base_url") or "")
            model = model or str(llm.get("model") or "")
            key = key or str(llm.get("api_key") or "")
        return base, model, key

    def schedule_coach(self, event, problem: Problem, record: Submission,
                       result: JudgeResult) -> None:
        """把「AI 点评」排成后台任务（**判题结果先发**，点评后到）。

        点评是一次模型调用，可能十几秒；同步等它会让「交题」看起来卡死。
        失败/超时都只是**少一条点评**，不影响分数与判题记录。
        """
        coach = self._coach
        if coach is None or not coach.opt.ready:
            return
        if result.status in ("BLOCKED", "IE"):
            return                    # 没跑起来的代码，评设计没有意义
        try:
            task = asyncio.create_task(
                self._run_coach(event, problem, record, result))
        except RuntimeError:          # 没有事件循环（脚本直接调）
            return
        self._coach_tasks.add(task)
        task.add_done_callback(self._coach_tasks.discard)

    async def _run_coach(self, event, problem: Problem, record: Submission,
                         result: JudgeResult) -> None:
        try:
            await self._coach_once(event, problem, record, result)
        except asyncio.CancelledError:
            # 插件卸载/热重载会取消后台任务：要点评的那条就悄悄没了，
            # 至少留一条日志，排查时能对上「提交了但没点评」。
            if self.logger:
                self.logger.info("[oj_bank] 代码点评被取消（插件卸载/重载）")
            raise

    async def _coach_once(self, event, problem: Problem, record: Submission,
                          result: JudgeResult) -> None:
        coach = self._coach
        if coach is None:
            return
        detail = _coach_detail(result)
        try:
            async with (self._coach_lock or _NullLock()):
                text = await coach.advise(
                    code=record.code,
                    language=self._language_label(record.language),
                    problem_title=problem.title,
                    verdict=result.status, detail=detail)
        except CoachError as e:
            if self.logger:
                self.logger.warning("[oj_bank] 代码点评失败：%s", e)
            try:
                await self.reply(event, f"⚠️ 代码点评没生成出来：{e}")
            except Exception:  # noqa: BLE001 - 通知失败也不能影响判题
                pass
            return
        except Exception:  # noqa: BLE001 - 点评失败绝不能影响判题
            if self.logger:
                self.logger.warning("[oj_bank] 代码点评异常", exc_info=True)
            return
        if not text:
            return
        try:
            await self.reply(event, f"🧑‍🏫 代码点评（提交 #{record.id}）\n{text}")
        except Exception:  # noqa: BLE001
            if self.logger:
                self.logger.warning("[oj_bank] 发送代码点评失败", exc_info=True)

    async def coach_submission(self, record: Submission,
                               problem: Problem) -> str:
        """对一条**已保存**的提交跑点评（`/oj coach` 与自动路径共用）。

        Raises:
            CoachError: 没配置点评模型 / 模型调用失败。
        """
        coach = self._coach
        if coach is None or not coach.opt.ready:
            raise CoachError("没配置点评模型（config: coach / ai_chat.llm）")
        async with (self._coach_lock or _NullLock()):
            return await coach.advise(
                code=record.code,
                language=self._language_label(record.language),
                problem_title=problem.title,
                verdict=record.status,
                detail=_coach_detail_record(record))

    # ------------------------------------------------------------------ 判题编排
    def cooldown_left(self, user_id: int) -> float:
        """距上次判题还要等几秒（0 = 可以交）。"""
        try:
            seconds = float(self.config.get("judge.cooldown_seconds", 20) or 0)
        except (TypeError, ValueError):
            seconds = 20.0
        if seconds <= 0:
            return 0.0
        last = self._last_submit.get(int(user_id), 0.0)
        return max(0.0, seconds - (time.time() - last))

    async def judge_code(self, event: MessageEvent, problem: Problem,
                         language: str, code: str) -> tuple[Optional[Submission],
                                                            Optional[JudgeResult]]:
        """跑一次判题并落库。

        Returns:
            `(submission, result)`；前置条件不满足时 `(None, None)`，原因由调用方
            先回给用户（这里不重复发消息）。
        """
        if not self.judge_enabled or self._judge is None:
            return None, None
        cases = cases_of(problem.id)
        if not cases:
            return None, None
        result = await self._judge.run(
            language=language, code=code, cases=cases,
            time_limit_ms=problem.time_limit_ms,
            memory_limit_mb=problem.memory_limit_mb,
            context={"title": problem.title, "problem_id": problem.id,
                     "user_id": int(event.user_id or 0)})
        record = record_submission(
            problem=problem, user_id=int(event.user_id or 0),
            group_id=int(event.group_id or 0) if _is_group_event(event) else 0,
            language=language, code=code, status=result.status,
            score=result.score,
            detail=[c.to_dict() for c in result.detail])
        self._last_submit[int(event.user_id or 0)] = time.time()
        return record, result

    def format_judge_result(self, problem: Problem, record: Submission,
                            result: JudgeResult, *, source: str = "") -> str:
        """把判题结果排成一条 QQ 消息（失败组给「期望/实际」预览）。"""
        lang = self._language_label(record.language)
        origin = f" · {source}" if source else ""
        head = (f"🧪 #{problem.id} {problem.title} · {lang}{origin}"
                f" · 提交 #{record.id}")
        if result.status == "BLOCKED":
            return (f"{head}\n🚫 没有通过代码审查：{result.message}\n"
                    "（判题会执行你的代码，所以先审后跑；改完再交）")
        if result.status == "CE":
            return f"{head}\n🛠 编译错误\n```\n{result.message[:800]}\n```"
        if result.status == "IE":
            return f"{head}\n⚠️ 判题失败：{result.message}"

        icon = STATUS_ICON.get(result.status, "•")
        label = STATUS_LABEL.get(result.status, result.status)
        lines = [
            f"{head}",
            f"{icon} {result.status} · {result.score} 分"
            f"（{result.passed}/{result.total} 组通过）· {result.total_ms}ms",
        ]
        # 每组一个图标，一行最多 5 个，避免长消息刷屏
        chips = [f"{c.ordinal}{STATUS_ICON.get(c.status, '•')}"
                 for c in result.detail]
        for i in range(0, len(chips), 5):
            lines.append("  ".join(chips[i:i + 5]))
        failed = [c for c in result.detail if c.status != AC]
        for case in failed[:3]:
            reason = STATUS_LABEL.get(case.status, case.status)
            note = f"（{case.note}）" if case.note else ""
            lines.append(f"❌ 第 {case.ordinal} 组 {reason}{note}")
            if case.status == "WA":
                if case.detail:
                    lines.append(f"   期望：{case.detail.replace('期望 ', '', 1)}")
                if case.got:
                    lines.append(f"   实际：{case.got}")
            elif case.detail:
                lines.append(f"   {case.detail}")
        if len(failed) > 3:
            lines.append(f"…还有 {len(failed) - 3} 组没过（/oj result {record.id} 看全部）")
        if result.status == AC:
            lines.append("🎉 全通过")
        return "\n".join(lines)

    @staticmethod
    def _language_label(raw: str) -> str:
        try:
            return resolve_language(raw).label
        except LanguageError:
            return str(raw or "?")

    # ------------------------------------------------------------------ 洛谷抓题
    async def import_luogu(self, pid_raw: str, *,
                           overwrite: Optional[bool] = None,
                           event=None, ctx=None) -> str:
        """抓一道洛谷题并入库（命令与 AI 工具共用）。

        Args:
            pid_raw: 题号或题目链接。
            overwrite: 题库里已有同题时是否覆盖；None = 用配置里的
                `luogu.overwrite`。

        Returns:
            给用户/模型看的结果文本（成功与失败都用它可以读的句子，不抛异常）。
        """
        cfg = self.config.get("luogu") or {}
        try:
            pid = normalize_pid(pid_raw)
        except LuoguError as e:
            return f"❌ {e}"
        if overwrite is None:
            overwrite = bool(cfg.get("overwrite", False))
        existing = get_problem(luogu_key(pid))
        if existing is not None and not overwrite:
            return (f"ℹ️ {pid} 已经在题库里：#{existing.id} {existing.title}\n"
                    "要按洛谷最新题面覆盖，把 overwrite 设为 true（或改配置 "
                    "luogu.overwrite）再试一次")

        try:
            got = await fetch_problem(
                pid, timeout=float(cfg.get("timeout") or 20),
                user_agent=str(cfg.get("user_agent") or DEFAULT_USER_AGENT),
                cookie=str(cfg.get("cookie") or ""))
        except ProblemNotFound as e:
            return f"❌ {e}"
        except LuoguError as e:
            return f"❌ 抓取洛谷题目失败：{e}"

        sample_in = got.samples[0][0] if got.samples else ""
        sample_out = got.samples[0][1] if got.samples else ""
        try:
            problem, created = upsert_problem(
                {
                    "key": luogu_key(got.pid), "title": got.name,
                    "source": "luogu", "source_id": got.pid, "url": got.url,
                    "difficulty": got.difficulty, "tags": "",
                    "statement": got.statement,
                    "input_format": got.input_format,
                    "output_format": got.output_format,
                    "sample_input": sample_in, "sample_output": sample_out,
                    "note": got.hint, "time_limit_ms": got.time_limit_ms,
                    "memory_limit_mb": got.memory_limit_mb,
                },
                cases=[{"input": i, "output": o, "kind": "sample",
                        "note": f"官方样例 {n}"}
                       for n, (i, o) in enumerate(got.samples, start=1)],
                replace_cases=existing is not None,
                hack_limit=self.max_cases)
        except OJError as e:
            return f"❌ 入库失败：{e}"

        hack = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        scheduled = self.schedule_generation(problem, event=event, ctx=ctx)
        if scheduled:
            tail = (f"\n🧠 正在用 AI 生成 {self.max_cases} 组测试点，"
                    "完成后会发到本聊天。")
        elif hack:
            tail = ""
        else:
            tail = ("\n💡 只有官方样例，还不能评分：让 AI 用工具 "
                    f"oj_bank_generate_cases 生成 {self.max_cases} 组测试点。")
        verb = "入库" if created else "更新"
        return (f"✅ 已{verb} #{problem.id} {problem.title}"
                f"（{self.difficulty_text(problem)}，"
                f"{len(got.samples)} 组官方样例）{tail}")

    @staticmethod
    def difficulty_text(problem: Problem) -> str:
        from .repo import difficulty_label
        return f"难度 {difficulty_label(problem.difficulty)}"

    # ------------------------------------------------------------------ AI 出测试点
    async def generate_cases(self, problem: Problem, *, count: int = 0,
                             replace: bool = True, on_case=None,
                             base: int = 0) -> str:
        """`_generate_cases` 的并发闸：同一道题同一时刻只跑一轮生成。"""
        pid = int(problem.id)
        if pid in self._gen_active:
            return (f"⏳ #{pid} 正在生成测试点，等这一轮结束再试"
                    "（重复触发不会叠加）")
        self._gen_active.add(pid)
        try:
            return await self._generate_cases(
                problem, count=count, replace=replace, on_case=on_case,
                base=base)
        finally:
            self._gen_active.discard(pid)

    async def _generate_cases(self, problem: Problem, *, count: int = 0,
                              replace: bool = True, on_case=None,
                              base: int = 0) -> str:
        """让 AI **一次只出一组**（1 份输入 + 1 份输出），重复到凑满 N 组。

        为什么一组一次：一次要 10 组会让模型写一整篇 JSON —— 慢、容易超时、
        也更容易被 `max_tokens` 截断。拆开之后单次请求很小，某一组失败只补那一组。

        每组由模型直接给 input/output；插件只做格式与去重，不跑参考解
        （省一次编译，也省掉写参考解的 token）。

        Args:
            on_case: 每生成一组就回调一次（`await on_case(序号, 目标, 这一组)`），
                用来把进度发回聊天；回调抛异常不影响生成。
            base: 这轮之前已经有几组（补缺口时用）：进度按**整题累计**序号报，
                例如已有 3 组、补第 4 组时报「第 4/10 组」。

        Returns:
            给用户/模型看的结果文本（成功与失败都用它可以读的句子）。
        """
        gen = self._gen
        if gen is None or not gen.opt.ready:
            return ("⚠️ 没配置用于出题的模型（config：gen.base_url/model/api_key，"
                    "留空会借用 plugins.ai_chat.llm）")

        cfg = self.config.get("gen") or {}
        target = max(1, min(self.max_cases, int(count or gen.opt.count
                                                or self.max_cases)))
        try:
            retries = max(0, int(cfg.get("retry_missing", 1) or 0))
        except (TypeError, ValueError):
            retries = 1
        try:
            budget = float(cfg.get("max_seconds") or gen.opt.max_seconds or 600)
        except (TypeError, ValueError):
            budget = 600.0
        deadline = time.monotonic() + max(30.0, budget)
        # 允许失败几次：要 N 组，最多请求 N+retries+2 次（重复/报错都算失败）
        max_attempts = target + retries + 2

        pitfalls: list[str] = []
        if gen.opt.pitfalls and self._bool_cfg("gen.pitfalls", True):
            try:
                async with (self._gen_lock or _NullLock()):
                    pitfalls = await gen.analyze_pitfalls(problem=problem)
            except GenError as e:
                pitfalls = []
                if self.logger:
                    self.logger.warning("[oj_bank] 误区分析失败（按无误区处理）：%s", e)

        good: list[dict] = []
        seen: set[str] = set()
        dropped: list[str] = []
        attempts = 0
        last_error = ""
        fixed_reason = ""
        # **拿到一组就存一组**：进程被重载/中断时，已生成的不白跑
        written = 0
        first_write = True
        db_error = ""

        while len(good) < target and attempts < max_attempts:
            if time.monotonic() > deadline:
                dropped.append("时间到点")
                break
            attempts += 1
            try:
                async with (self._gen_lock or _NullLock()):
                    case = await gen.propose_one(
                        problem=problem, index=len(good) + 1, total=target,
                        avoid=[item["input"][:40] for item in good][-8:],
                        pitfalls=pitfalls)
            except GenError as e:
                last_error = str(e)
                dropped.append(f"第 {attempts} 次请求失败")
                continue
            if case.get("fixed_output"):
                # 固定输出的题（无输入 / 输出与输入无关）不需要测试点
                fixed_reason = str(case.get("reason") or "输出固定")
                break
            if case.get("script"):
                if not gen.opt.allow_scripts:
                    dropped.append(f"第 {attempts} 次给了脚本（已禁用）")
                    continue
                try:
                    stdin, expected = await run_script(
                        case["script"],
                        timeout=float(gen.opt.script_timeout or 15),
                        max_input=gen.opt.max_input_chars or 20000)
                except GenError as e:
                    last_error = f"生成脚本失败：{e}"
                    dropped.append(f"第 {attempts} 次脚本失败")
                    continue
                case = {"input": stdin, "output": expected,
                        "note": case.get("note", "")}
            stdin = case["input"]
            if stdin in seen:
                dropped.append(f"第 {attempts} 次给了重复输入")
                continue
            seen.add(stdin)
            item = {"input": stdin, "output": case["output"],
                    "note": case["note"], "kind": "hack"}
            try:
                added = add_cases(problem, [item], kind="hack",
                                  replace=(replace and first_write),
                                  hack_limit=self.max_cases)
            except OJError as e:
                db_error = str(e)
                break
            first_write = False
            written += added
            good.append(item)
            if on_case is not None:
                try:
                    await on_case(base + len(good), base + target, item)
                except Exception:  # noqa: BLE001 - 进度发不出去不能拖垮生成
                    if self.logger:
                        self.logger.warning("[oj_bank] 发送生成进度失败",
                                            exc_info=True)

        if fixed_reason:
            return self._fixed_output_reply(problem, fixed_reason, replace=replace)

        if not good:
            if db_error:
                return f"❌ 测试点没入库：{db_error}"
            if time.monotonic() > deadline:
                return (f"❌ 生成超时（{int(budget)} 秒内一组都没成功）；"
                        "可以调大 gen.max_seconds 或减小 gen.max_steps 再试")
            return f"❌ 生成失败：{last_error or '模型没有给出可用的测试数据'}"

        hack = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        pitfall_note = (f"，针对 {len(pitfalls)} 个常见误区" if pitfalls
                        else "，本题没有明显误区，全部为常规点")
        lines = [f"✅ 已生成 {written} 组测试点（#{problem.id} {problem.title}，"
                 f"逐组生成 {attempts} 次{pitfall_note}，边生成边入库）",
                 f"现在测试点 {hack}/{self.max_cases} 组"]
        if hack < target:
            lines.append(f"⚠️ 只凑到 {hack} 组（请求失败 "
                         f"{len([d for d in dropped if '失败' in d])} 次、"
                         f"重复 {len([d for d in dropped if '重复' in d])} 次）")
        if last_error:
            lines.append(f"最后一次错误：{last_error[:120]}")
        lines.append(f"看：/oj cases {problem.id} · 交题：/oj submit {problem.id} cpp")
        return "\n".join(lines)

    async def regenerate_case(self, problem: Problem, ordinal: int) -> str:
        """重做**某一组**测试点（就地替换，不动其它组）。

        Returns:
            给用户看的结果文本。
        """
        gen = self._gen
        if gen is None or not gen.opt.ready:
            return "⚠️ 没配置用于出题的模型（config：gen.base_url/model/api_key）"
        rows = cases_of(problem.id)
        target = next((c for c in rows
                       if int(c.ordinal) == int(ordinal) and c.kind == "hack"),
                      None)
        if target is None:
            return f"❌ #{problem.id} 没有第 {ordinal} 组（用 /oj cases {problem.id} 看编号）"
        try:
            async with (self._gen_lock or _NullLock()):
                case = await gen.propose_one(
                    problem=problem, index=int(ordinal), total=len(rows),
                    avoid=[c.stdin[:40] for c in rows if c.id != target.id][-8:])
        except GenError as e:
            return f"❌ 重做失败：{e}"
        if case.get("fixed_output"):
            return ("ℹ️ 模型判断这题是固定输出，不需要测试点；"
                    "要改就整题重新生成（/oj gen）")
        try:
            update_case(target, stdin=case["input"], expected=case["output"],
                        note=case["note"])
        except OJError as e:
            return f"❌ 没写进库：{e}"
        return (f"✅ 第 {ordinal} 组已替换（#{problem.id} {problem.title}）\n"
                f"输入：{case['input'][:80]!r}\n"
                f"输出：{case['output'][:80]!r}\n"
                f"用途：{case['note'] or '（未写）'}")

    def case_progress_text(self, problem: Problem, index: int, total: int,
                          case: dict) -> str:
        """单组测试点的进度消息（一行，QQ 上不刷屏）。"""
        def brief(value, limit: int = 36) -> str:
            text = " ".join(str(value or "").split())
            return text if len(text) <= limit else text[:limit] + "…"

        note = brief(case.get("note") or "", 16)
        line = (f"🧪 #{problem.id} 第 {index}/{total} 组："
                f"输入「{brief(case.get('input'))}」"
                f"→ 输出「{brief(case.get('output'))}」")
        return line + (f"（{note}）" if note else "")

    def _fixed_output_reply(self, problem: Problem, reason: str, *,
                            replace: bool = True) -> str:
        """固定输出的题：不生成额外测试点，能评分就够。

        判定来自模型（它读题面）。题库里已经有测试点（洛谷导入会存官方样例）
        就直接能用；一组都没有时，用样例造一组（输入为空）——否则 `/oj submit`
        会因为没有测试点而拒绝评分。
        """
        head = f"ℹ️ AI 判断这题输出固定（{reason}），不需要额外测试点。"
        existing = cases_of(problem.id)
        if existing:
            return (f"{head}题库已有 {len(existing)} 组测试点，可直接评分："
                    f"/oj submit {problem.id} cpp")
        sample_out = str(problem.sample_output or "")
        if sample_out.strip():
            case = {"input": str(problem.sample_input or ""), "output": sample_out,
                    "note": "固定输出（无输入）", "kind": "hack"}
            try:
                add_cases(problem, [case], kind="hack", replace=replace,
                          hack_limit=self.max_cases)
            except OJError as e:
                return f"{head}但样例入库失败：{e}"
            return (f"{head}已把样例作为唯一测试点（1 组，输入为空），"
                    f"可直接评分：/oj submit {problem.id} cpp")
        return (f"{head}但题库里没有样例输出，暂时无法评分；"
                "可以手写一组用 oj_bank_cases 补上。")

    # ---- 入库后自动生成（所有插入路径都走这里） ----
    def schedule_generation(self, problem: Problem, *, event=None, ctx=None,
                            force: bool = False, count: int = 0,
                            progress: bool = False, replace: bool = True,
                            base: int = 0) -> bool:
        """给刚入库的题排一次「AI 生成 10 组测试点」。

        需求是「所有插入的题目都要有 AI 生成的 10 组测试点」，所以三个入口
        （`/oj luogu`、工具 `oj_bank_save_problem`、工具 `oj_bank_luogu`）都会调它。
        生成放后台：一次要一两分钟，塞在命令/工具里会让对话看起来卡死。

        Returns:
            是否真的排上了（没开 `gen.auto_insert` 或没配置模型时 False）。
        """
        gen = self._gen
        if gen is None or not gen.opt.ready:
            return False
        # force=True：用户显式要求（/oj gen 或工具），不受 auto_insert 开关限制
        if not force and not self._bool_cfg("gen.auto_insert", True):
            return False
        try:
            task = asyncio.create_task(self._auto_generate(
                problem, event, ctx, count, progress, replace, base))
        except RuntimeError:              # 没有事件循环（脚本直接调）
            return False
        self._gen_tasks.add(task)
        task.add_done_callback(self._gen_tasks.discard)
        return True

    async def _auto_generate(self, problem: Problem, event, ctx,
                             count: int = 0, progress: bool = False,
                             replace: bool = True, base: int = 0) -> None:
        """后台生成 + 把结果发回来源聊天。

        Args:
            progress: 只有 `/oj gen` 传 True —— 用户显式要看的逐组进度；
                入库自动生成与启动补齐只发最后一条汇总，免得刷屏。
        """
        on_case = None
        if progress:
            async def on_case(index, total, case):
                await self.notify(
                    self.case_progress_text(problem, index, total, case),
                    event=event, ctx=ctx)

        try:
            text = await self.generate_cases(
                problem, count=int(count or self.max_cases), replace=replace,
                on_case=on_case, base=base)
        except asyncio.CancelledError:
            # 插件被卸载/热重载：任务取消是正常路径，但要留痕，否则用户只看到
            # 「开始了却没结果」，排查时无从下手。
            if self.logger:
                self.logger.info("[oj_bank] 测试点生成被取消（插件卸载/重载）")
            raise
        except Exception:  # noqa: BLE001 - 后台任务不能把异常丢给事件循环
            if self.logger:
                self.logger.warning("[oj_bank] 自动生成测试点异常", exc_info=True)
            return
        ok = text.startswith(("✅", "ℹ️"))
        header = f"🧪 #{problem.id} {problem.title}\n"
        if ok:
            body = text
            body += f"\n交题：/oj submit {problem.id} cpp（或 c / py）"
        else:
            body = f"{text}\n可以私聊/群里发 /oj gen {problem.id} 重试"
        try:
            await self.notify(header + body, event=event, ctx=ctx)
        except Exception:  # noqa: BLE001
            if self.logger:
                self.logger.warning("[oj_bank] 发送测试点生成结果失败", exc_info=True)

    def _schedule_backfill(self) -> None:
        """启动时把「还没有测试点」的旧题补上（后台，不挡启用）。"""
        if not self._bool_cfg("gen.backfill_on_start", True):
            return
        if self._gen is None or not self._gen.opt.ready:
            return
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        try:
            task = asyncio.create_task(self._backfill_missing())
        except RuntimeError:              # 没有事件循环（脚本直接调）
            return
        self._backfill_task = task
        self._gen_tasks.add(task)
        task.add_done_callback(self._gen_tasks.discard)

    async def _backfill_missing(self) -> None:
        """按题号顺序补前 `gen.backfill_limit` 道没有测试点的题。"""
        try:
            limit = max(1, int(self.config.get("gen.backfill_limit") or 5))
        except (TypeError, ValueError):
            limit = 5
        try:
            rows, _total = list_problems("", limit=500, offset=0)
        except OJError:
            return
        pending = []
        for problem in rows:
            try:
                has_hack = any(c.kind == "hack" for c in cases_of(problem.id))
            except OJError:
                continue
            if not has_hack:
                pending.append(problem)
            if len(pending) >= limit:
                break
        for problem in pending:
            if self._gen is None or not self._gen.opt.ready:
                return
            try:
                text = await self.generate_cases(problem, count=self.max_cases)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单题失败不影响其它题
                if self.logger:
                    self.logger.warning("[oj_bank] 补测试点失败 #%s",
                                        problem.id, exc_info=True)
                continue
            await self.notify_admin(f"🧪 #{problem.id} {problem.title}\n{text}")

    async def notify_admin(self, message: str) -> None:
        """给超管发私聊（启动补齐没有来源聊天可用）。"""
        targets = sorted(self.perm.superusers)
        if not targets:
            if self.logger:
                self.logger.info("[oj_bank] %s",
                                 str(message).replace("\n", " | ")[:300])
            return
        try:
            await self.api.send_private_msg(str(targets[0]), message)
        except Exception:  # noqa: BLE001 - 通知失败不能影响生成
            if self.logger:
                self.logger.warning("[oj_bank] 通知超管失败", exc_info=True)

    async def notify(self, message, *, event=None, ctx=None) -> None:
        """把消息发回「触发这次操作的地方」。

        命令路径有 `event`（群/私聊/小窗都认）；工具路径只有 ai_chat 的 `ctx`
        （鸭子类型），按 `in_group` 分派。
        """
        if event is not None:
            await self.reply(event, message)
            return
        if ctx is None:
            return
        if getattr(ctx, "in_group", False):
            chat_id = int(getattr(ctx, "chat_id", 0)
                          or getattr(ctx, "group_id", 0) or 0)
            if chat_id:
                await self.api.send_group_msg(group_id=str(chat_id),
                                              message=message)
            return
        uid = int(getattr(ctx, "user_id", 0) or 0)
        if uid:
            await self.api.send_private_msg(str(uid), message)

    # ------------------------------------------------------------------ 待提交代码
    def start_pending(self, event: MessageEvent, problem: Problem,
                      language: str) -> str:
        """登记「下一条消息是代码」，返回要回给用户的话。"""
        try:
            window = float(self.config.get("judge.submit_window_seconds", 600) or 600)
        except (TypeError, ValueError):
            window = 600.0
        self._pending[int(event.user_id or 0)] = PendingSubmit(
            problem_id=problem.id, language=language,
            chat_key=self._chat_key(event), expires_at=time.time() + window)
        lang = resolve_language(language).label if language else "C++17"
        return (f"📥 把代码文件发上来（{lang}，{int(window // 60)} 分钟内有效）：\n"
                "· 私聊：直接发文件（.cpp / .c / .py）；\n"
                "· 群聊：传到群文件（群里发文件走群文件上传）；\n"
                "· 也可以直接发代码文本，或引用一条代码消息再发 `/oj submit`；\n"
                "· 发别的 `/oj` 命令或 `/oj cancel` 取消。")

    @staticmethod
    def _chat_key(event: MessageEvent) -> tuple:
        if event.message_type == "group":
            return ("group", int(event.group_id or 0))
        return ("private", int(event.group_id or 0), int(event.user_id or 0))

    def cancel_pending(self, user_id: int) -> bool:
        return self._pending.pop(int(user_id or 0), None) is not None

    async def _on_code_message(self, event: MessageEvent) -> None:
        """消息监听：这个用户的**下一条普通消息**是代码（文本或文件）。"""
        if not isinstance(event, MessageEvent):
            return
        uid = int(event.user_id or 0)
        pending = self._pending.get(uid)
        if pending is None:
            return
        if time.time() > pending.expires_at:
            self._pending.pop(uid, None)
            return
        if self._chat_key(event) != pending.chat_key:
            return

        prefixes = tuple(getattr(self.cmds.parser, "prefixes", ()) or ())
        text = _strip_at(event.plain_text or "")
        if prefixes and text.lstrip().startswith(prefixes):
            return                      # 用户又发了命令：不吞，交给命令系统

        problem = Problem.objects.filter(pk=pending.problem_id).first()
        if problem is None:
            self._pending.pop(uid, None)
            return
        if not self.event_access_ok(event):
            await self.reply(event, "⛔️ 这个聊天没有开通题库服务（联系管理员配置 binding）")
            return

        # 带文件的消息优先按文件交（文件里的代码不会被转义改写）
        segments = file_segments(getattr(event, "message", None))
        if segments:
            await self._take_code_file(event, problem, pending, segments[0])
            return
        if not text.strip() or "[图片]" in text:
            return
        self._pending.pop(uid, None)
        await self._judge_and_reply(event, problem, pending.language, text)

    async def _take_code_file(self, event, problem: Problem,
                              pending: "PendingSubmit", segment: dict) -> None:
        """消息里的文件段 → 判题（失败时**保留** pending，用户可重传）。"""
        try:
            code_file = await from_segment(
                segment, self.api, limit=self.max_file_bytes,
                default_language=pending.language, roots=self.file_roots,
                file_manager=self.files)
        except CodeFileError as e:
            await self.reply(event, f"❌ {e}\n（再发一次代码文件就行，命令不用重发）")
            return
        self._pending.pop(int(getattr(event, "user_id", 0) or 0), None)
        await self._judge_and_reply(
            event, problem, code_file.language or pending.language,
            code_file.content, source=code_file.name)

    async def _on_group_upload(self, event: GroupUploadEvent) -> None:
        """群文件上传（notice）：待提交状态下的代码文件。

        QQ 群里「发文件」是 `group_upload` 通知而不是聊天消息，所以这条路径要
        单独接 —— 否则群里交 .cpp 会没反应。
        """
        uid = int(getattr(event, "user_id", 0) or 0)
        gid = int(getattr(event, "group_id", 0) or 0)
        pending = self._pending.get(uid)
        if pending is None or pending.chat_key != ("group", gid):
            return
        if time.time() > pending.expires_at:
            self._pending.pop(uid, None)
            return
        problem = Problem.objects.filter(pk=pending.problem_id).first()
        if problem is None:
            self._pending.pop(uid, None)
            return
        if not self.event_access_ok(event):
            return                       # 服务范围外：静默
        try:
            code_file = await from_group_upload(
                event, self.api, limit=self.max_file_bytes,
                default_language=pending.language, roots=self.file_roots,
                file_manager=self.files)
        except CodeFileError as e:
            await self.reply(event, f"❌ {e}\n（再传一次代码文件就行）")
            return
        self._pending.pop(uid, None)
        await self._judge_and_reply(
            event, problem, code_file.language or pending.language,
            code_file.content, source=code_file.name)

    async def _judge_and_reply(self, event, problem: Problem,
                               language: str, code: str, *,
                               source: str = "") -> None:
        """判题 + 把结果发回去（命令 / 下一条消息 / 文件三条路共用）。"""
        left = self.cooldown_left(int(event.user_id or 0))
        if left > 0:
            await self.reply(event, f"⏳ 先歇 {left:.0f} 秒再交（判题机一次只跑一份）")
            return
        record, result = await self.judge_code(event, problem, language, code)
        if record is None or result is None:
            await self.reply(event, "⚠️ 现在不能判题（题目没有测试点，或判题被关闭）")
            return
        await self.reply(event, self.format_judge_result(
            problem, record, result, source=source))
        self.schedule_coach(event, problem, record, result)


def _coach_detail(result: JudgeResult) -> str:
    """给点评模型看的「哪一组没过」摘要。"""
    failed = [c for c in result.detail if c.status != AC]
    if not failed:
        return (f"{result.passed}/{result.total} 组通过，"
                f"耗时 {result.total_ms}ms")
    case = failed[0]
    note = f"（{case.note}）" if case.note else ""
    return (f"第 {case.ordinal} 组 {case.status}{note} {case.detail}"
            f"；共 {len(failed)} 组没过")


def _coach_detail_record(record: Submission) -> str:
    """从落库的 `detail` JSON 还原「哪一组没过」（给点评用）。"""
    try:
        cases = json.loads(record.detail or "[]")
    except (TypeError, ValueError):
        cases = []
    failed = [c for c in cases
              if isinstance(c, dict) and c.get("status") not in (None, AC)]
    score = record.score
    if not failed:
        return f"{len(cases)} 组通过，得分 {score}"
    case = failed[0]
    note = f"（{case.get('note')}）" if case.get("note") else ""
    return (f"第 {case.get('ordinal')} 组 {case.get('status')}{note} "
            f"{case.get('detail') or ''}；共 {len(failed)} 组没过")


class _NullLock:
    """没有事件循环/没建锁时的空上下文（让点评逻辑照常走）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _is_group_event(event) -> bool:
    """这个事件从群里来吗（消息事件看 `message_type`，通知事件看 `group_id`）。"""
    message_type = getattr(event, "message_type", "")
    if message_type:
        return message_type == "group"
    return bool(getattr(event, "group_id", None))


def _strip_at(text: str) -> str:
    """去掉消息开头的 `[@123]`（群里 @ 机器人时 `plain_text` 会带上）。"""
    return _AT_PREFIX_RE.sub("", str(text or "")).strip()


__all__ = ["OJBankPlugin", "PendingSubmit", "STATUS_ICON"]

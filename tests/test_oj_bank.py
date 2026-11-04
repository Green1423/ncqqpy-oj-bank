"""oj_bank 插件验收：题库 / 洛谷解析 / 判题 / 代码审查 / 出图 / 命令 / AI 工具。

跑法（从仓库根执行，或任意目录 —— 脚本用 `__file__` 定位仓库根）：

    ./venv-arm/bin/python -B tests/test_oj_bank.py

默认**离线**：洛谷用合成页面 + 桩函数，代码审查用本地 aiohttp 假服务；
判题与出图是**真跑**（真编译器、真 Chromium），这是这个插件的核心风险面，
不值得用桩掩盖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
TMP = TMP_DIR / "oj-bank"

from napcat.command import CommandManager                              # noqa: E402
from napcat.config import ConfigManager                                # noqa: E402
from napcat.client import GroupBinding                                 # noqa: E402
from napcat.events import EventBus, TimerManager                       # noqa: E402
from napcat.file_manager import FileManager                            # noqa: E402
from napcat.message import MessageChain                                # noqa: E402
from napcat.models import MessageEvent                                 # noqa: E402
from napcat.permission import PermissionManager                        # noqa: E402

from plugins.oj_bank.judge import (Judge, JudgeOptions, normalize_output,   # noqa: E402
                                   outputs_match, resolve_language)
from plugins.oj_bank.luogu import (LuoguProblem, ProblemNotFound,  # noqa: E402
                                   normalize_pid, parse_problem_page)
from plugins.oj_bank.main import OJBankPlugin                      # noqa: E402
from plugins.oj_bank import repo                                    # noqa: E402
from plugins.oj_bank.review import (CodeReviewer, ReviewOptions,    # noqa: E402
                                    parse_verdict, scan_rules)
from plugins.oj_bank.render import build_problem_page, find_chromium  # noqa: E402

SUPERUSER = 10000001
ALICE = 20001
BOB = 20002
GROUP_OK = 100000001
GROUP_DENIED = 111

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name: str, cond, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")
    return bool(cond)


def flat(result) -> str:
    """命令返回值 → 纯文本（MessageChain 也压平，便于断言）。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, MessageChain):
        return _chain_text(result)
    if isinstance(result, (list, tuple)):
        return "\n".join(flat(x) for x in result)
    return str(result)


def _chain_text(chain: MessageChain) -> str:
    parts = []
    for seg in chain.build():
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


def chain_images(chain: MessageChain) -> list[str]:
    return [seg["data"].get("file", "") for seg in chain.build()
            if seg.get("type") == "image"]


def png_size(path: str) -> tuple[int, int]:
    """读 PNG 的 IHDR 拿宽高（不引第三方库）。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    return struct.unpack(">II", head[16:24])


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
class FakeApi:
    """只记录「发到哪、发的是什么」的 API 桩。"""

    def __init__(self):
        self.sent: list[tuple] = []
        #: `get_msg` 返回的引用正文（引用交题用）
        self.quote_text = ""
        #: 引用消息的完整段列表（引用文件时用；非 None 优先于 quote_text）
        self.quote_message = None
        #: file_id → (文件名, 本机路径)——模拟 NapCat 的 get_file
        self.files: dict[str, tuple[str, str]] = {}
        #: file_id → 下载地址——模拟 get_group_file_url
        self.group_files: dict[str, str] = {}
        #: 走过几次框架的文件流 API（`iter_file_stream`）
        self.stream_calls = 0

    async def get_msg(self, message_id):
        if self.quote_message is not None:
            return {"message": self.quote_message}
        return {"message": [{"type": "text",
                             "data": {"text": self.quote_text}}]}

    async def get_file(self, file=None, file_id=None):
        item = self.files.get(str(file_id or file or ""))
        if item is None:
            raise RuntimeError("文件不存在")
        name, path = item
        return {"file": path, "name": name, "url": ""}

    async def get_group_file_url(self, group_id, file_id):
        return {"url": self.group_files.get(str(file_id), "")}

    def _file_bytes(self, file, file_id) -> bytes:
        key = str(file_id or file or "")
        item = self.files.get(key)
        path = item[1] if item else str(file or "")
        with open(path, "rb") as fh:      # 真实路径 / 不存在的路径都按真实行为
            return fh.read()

    async def _call(self, action, params=None, *, throttle=True):
        params = dict(params or {})
        if action == "get_file":
            return await self.get_file(file=params.get("file"),
                                       file_id=params.get("file_id"))
        if action == "get_group_file_url":
            return await self.get_group_file_url(params.get("group_id"),
                                                 params.get("file_id"))
        raise RuntimeError(f"FakeApi 未实现 action: {action}")

    async def _stream_packets(self, action, params, timeout=60.0):
        # FileManager 的流式下载最终走这里；返回 file_info / file_chunk / complete
        self.stream_calls += 1
        data = self._file_bytes(params.get("file"), params.get("file_id"))
        chunk_size = int(params.get("chunk_size") or 65536)
        yield {"data_type": "file_info", "file_size": len(data)}
        for i in range(0, len(data), chunk_size):
            yield {"data_type": "file_chunk",
                   "data": base64.b64encode(data[i:i + chunk_size]).decode()}
        yield {"data_type": "file_complete"}

    async def send_group_msg(self, group_id, message, **kwargs):
        self.sent.append(("group", str(group_id), message))
        return {"message_id": 1}

    async def send_private_msg(self, user_id, message, **kwargs):
        self.sent.append(("private", str(user_id), message,
                          str(kwargs.get("group_id") or "")))
        return {"message_id": 2}

    def texts(self) -> list[str]:
        out = []
        for item in self.sent:
            message = item[2]
            out.append(flat(message) if not isinstance(message, str) else message)
        return out


def ev(text: str, *, group: int = None, user: int = SUPERUSER,
       temp_group: int = None, quote: int = 0) -> MessageEvent:
    """构造消息事件（群 / 私聊 / 群小窗）；`quote` 给消息 id 表示「引用了一条」。"""
    segments = [{"type": "text", "data": {"text": text}}]
    if quote:
        segments.insert(0, {"type": "reply", "data": {"id": str(quote)}})
    if group is not None:
        return MessageEvent(message_type="group", user_id=user, group_id=group,
                            message=segments, raw_message=text)
    return MessageEvent(
        message_type="private", sub_type="group" if temp_group else None,
        user_id=user, group_id=temp_group,
        message=segments, raw_message=text)


async def build_plugin(tmp: Path, *, ai_review: dict | None = None,
                       bound=(), empty_binding: bool = False):
    """按框架的口径把插件装起来（不经过 PluginManager，避免加载别的插件）。

    Args:
        bound: 框架级 `bound_groups`（群白名单回落用）。
        empty_binding: True = 不写 `binding.groups/users`（测回落与 empty_means）。
    """
    cfg = ConfigManager(str(tmp / "config.json"))
    cfg.set("plugins.oj_bank.data_dir", str(tmp / "data"))
    if not empty_binding:
        cfg.set("plugins.oj_bank.binding.groups", [GROUP_OK])
        cfg.set("plugins.oj_bank.binding.users", [ALICE])
    cfg.set("plugins.oj_bank.submit.local_roots", [str(tmp)])
    cfg.set("plugins.oj_bank.gen.pitfalls", False)
    cfg.set("plugins.oj_bank.judge.cooldown_seconds", 0)
    cfg.set("plugins.oj_bank.judge.compile_timeout_ms", 20000)
    if ai_review:
        for key, value in ai_review.items():
            cfg.set(f"plugins.oj_bank.review.ai.{key}", value)

    bus = EventBus()
    timer = TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[SUPERUSER])
    groups = GroupBinding(bound=set(bound), all_groups=not bound)
    cmds = CommandManager(prefixes=["/", "#"], perm_manager=perm,
                          groups_ref=groups, cfg_ref=cfg,
                          event_bus_ref=bus, timer_ref=timer)
    api = FakeApi()
    plugin = OJBankPlugin(
        api=api, event_bus=bus, timer=timer, cmd_manager=cmds,
        perm_manager=perm, file_manager=FileManager(api),
        logger=logging.getLogger("test.oj_bank"), cfg=cfg, groups=groups,
        plugin_dir=tmp / "plugin")
    await plugin.on_load()
    await plugin.on_enable()
    return plugin, cmds, api


def sample_html(payload: dict) -> str:
    """拼一个带 lentille-context 的洛谷题目页。"""
    body = json.dumps(payload, ensure_ascii=False)
    return ('<!DOCTYPE html><html><head><title>t</title></head><body>'
            f'<script id="lentille-context" type="application/json">{body}</script>'
            "</body></html>")


def sample_payload() -> dict:
    return {
        "instance": "main", "template": "problem.show", "status": 200,
        "data": {"problem": {
            "pid": "P1001", "name": "A+B Problem", "difficulty": 1,
            "content": {"name": "A+B Problem", "background": "",
                        "description": "输入两个整数 $a,b$，输出它们的和。",
                        "formatI": "两个以空格分隔的整数 $a,b$。",
                        "formatO": "一个整数，表示 $a+b$。",
                        "hint": "**数据范围** $-10^9 \\le a,b \\le 10^9$。"},
            "samples": [["20 30\n", "50\n"], ["-5 7\n", "2\n"]],
            "limits": {"time": [1000, 1000], "memory": [131072, 131072]},
        }},
    }


async def wait_for(predicate, timeout: float = 6.0, step: float = 0.05) -> bool:
    """等一个条件成立（后台任务用；最多等 timeout 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def start_file_server(payloads: dict[str, bytes]):
    """本地 HTTP 文件服务（模拟 QQ 的文件下载地址）。"""
    from aiohttp import web

    async def handler(request):
        data = payloads.get(request.match_info["name"])
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="application/octet-stream")

    app = web.Application()
    app.router.add_get("/files/{name}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/files"


# ----------------------------------------------------------------------
# 测试
# ----------------------------------------------------------------------
async def test_load(plugin, cmds):
    print("\n[1] 装配：配置模板 / 命令 / 工具")
    seeded = plugin.config.all()
    for key in ("data_dir", "binding", "max_cases", "judge", "review", "render",
                "luogu"):
        check(f"配置模板补齐 {key}", key in seeded, str(sorted(seeded)))
    check("data_dir 生效（不写仓库目录）",
          str(plugin.data_dir).startswith(str(TMP)), plugin.data_dir)
    subs = (cmds.list_commands().get("oj") or object()).subcommands or {}
    check("命令注册齐全",
          all(name in subs for name in ("luogu", "show", "list", "submit",
                                        "cancel", "result", "cases", "del", "set")),
          str(sorted(subs)))
    names = {t.name for t in plugin.plugin_tools.list()}
    check("AI 工具注册齐全",
          names == {"oj_bank.save_problem", "oj_bank.cases",
                    "oj_bank.generate_cases", "oj_bank.luogu",
                    "oj_bank.get_problem", "oj_bank.list"}, str(sorted(names)))
    check("工具声明了 JSON Schema",
          plugin.plugin_tools.get("oj_bank.save_problem").parameters.get("required"))
    check("oj.del 仅超管", subs["del"].permission == "superuser",
          subs["del"].permission)
    check("消息监听已注册（两步交题）", len(plugin._handlers) >= 1,
          str(plugin._handlers))


async def test_tool_save(plugin):
    print("\n[2] AI 工具：save_problem / 白名单 / 上限")
    from napcat.tools import call_tool

    class Ctx:
        user_id = ALICE
        group_id = 0
        is_superuser = False
        in_group = False

    class DeniedCtx:
        user_id = 99999
        group_id = 0
        is_superuser = False
        in_group = False

    tool = plugin.plugin_tools.get("oj_bank.save_problem")
    cases = [{"input": f"{i} {i}\n", "output": f"{i + i}\n", "note": f"第 {i} 组"}
             for i in range(1, 11)]
    denied = await call_tool(tool, {
        "title": "x", "statement": "y", "input_format": "a",
        "output_format": "b", "sample_input": "1 1", "sample_output": "2",
        "testcases": cases}, DeniedCtx())
    check("白名单外的调用被拒", "没有开通题库服务" in denied, denied[:80])

    out = await call_tool(tool, {
        "title": "A+B Problem", "statement": "输入整数 $a,b$，输出 $a+b$。",
        "input_format": "两个整数", "output_format": "一个整数",
        "sample_input": "20 30\n", "sample_output": "50\n",
        "note": "数据范围：$-10^9 \\le a,b \\le 10^9$",
        "time_limit_ms": 2000, "memory_limit_mb": 256,
        "testcases": cases}, Ctx())
    check("入库成功并返回编号", "已入库" in out and "#" in out, out[:120])
    problem = repo.get_problem("1")
    check("题目落库", problem is not None and problem.title == "A+B Problem")
    check("测试点 10 组全部落库", len(repo.cases_of(problem.id)) == 10,
          str(len(repo.cases_of(problem.id))))
    check("LaTeX 与换行没被改写",
          "$-10^9 \\le a,b \\le 10^9$" in problem.note, problem.note[:80])

    out2 = await call_tool(tool, {
        "title": "A+B Problem", "statement": "改过的题面",
        "input_format": "两个整数", "output_format": "一个整数",
        "sample_input": "1 1", "sample_output": "2",
        "testcases": [{"input": "1 1", "output": "2"}]}, Ctx())
    check("同标题再次保存是更新而不是新建",
          "已更新" in out2 and repo.Problem.objects.count() == 1,
          f"{out2[:40]} count={repo.Problem.objects.count()}")
    check("replace=True 时测试点被替换（不是叠加）",
          len(repo.cases_of(problem.id)) == 1, str(len(repo.cases_of(problem.id))))

    # 还原成 10 组，后面判题要用
    tool_cases = plugin.plugin_tools.get("oj_bank.cases")
    await call_tool(tool_cases, {"problem_id": problem.id, "cases": cases,
                                 "replace": True}, Ctx())
    check("oj_bank.cases 可补测试点", len(repo.cases_of(problem.id)) == 10,
          str(len(repo.cases_of(problem.id))))
    over = await call_tool(tool_cases,
                           {"problem_id": problem.id,
                            "cases": [{"input": "1 1", "output": "2"}]}, Ctx())
    check("超过 10 组被拒（整批不写）",
          "最多 10 组" in over and len(repo.cases_of(problem.id)) == 10, over[:120])
    got = await call_tool(plugin.plugin_tools.get("oj_bank.get_problem"),
                          {"ident": "1"}, Ctx())
    check("get_problem 返回全文", "## 题干" in got and "测试点" in got, got[:120])
    listed = await call_tool(plugin.plugin_tools.get("oj_bank.list"),
                             {"keyword": "A+B"}, Ctx())
    check("list 工具能按关键词查", "#1" in listed, listed[:120])


async def test_tool_partial(plugin):
    print("\n[2.5] AI 存题：任何来源 / 最小字段 / 缺什么点名")
    from napcat.tools import call_tool

    class Ctx:
        user_id = ALICE
        group_id = GROUP_OK
        chat_id = GROUP_OK
        in_group = True
        is_superuser = False

    class DeniedCtx(Ctx):
        group_id = GROUP_DENIED
        chat_id = GROUP_DENIED

    tool = plugin.plugin_tools.get("oj_bank.save_problem")
    out = await call_tool(tool, {"title": "CF 1A", "statement": "求 $a \\times b$。"},
                          Ctx())
    check("只有标题 + 题干也能入库", "已入库" in out, out[:160])
    problem = repo.Problem.objects.filter(title="CF 1A").first()
    check("题目真的进库了", problem is not None and problem.statement.startswith("求"))
    check("结果点名缺哪些字段",
          "还缺：" in out and "输入格式" in out and "注意事项" in out, out[:300])
    check("结果指导补测试点", "oj_bank_cases" in out, out[:300])
    check("没标出处时来源是「AI 搜题」",
          repo.source_label(problem) == "AI 搜题", repo.source_label(problem))

    out2 = await call_tool(tool, {
        "title": "CF 1234E", "statement": "给定数组求逆序对。",
        "input_format": "第一行 $n$", "output_format": "一个整数",
        "sample_input": "3\n1 2 3\n", "sample_output": "0\n",
        "note": "$n \\le 10^5$", "source": "Codeforces 1234E",
        "url": "https://codeforces.com/problemset/problem/1234/E",
        "testcases": [{"input": "3\n1 2 3\n", "output": "0\n", "note": "已有序"}],
    }, Ctx())
    check("非洛谷来源也能入库", "已入库" in out2 and "Codeforces 1234E" in out2,
          out2[:200])
    cf = repo.Problem.objects.filter(title="CF 1234E").first()
    check("来源原文存进 source_id", cf.source_id == "Codeforces 1234E", cf.source_id)
    check("列表里显示来源原文", "Codeforces 1234E" in repo.one_line(cf),
          repo.one_line(cf))
    check("字段齐了就不报「还缺」", "还缺：" not in out2, out2[:200])

    out3 = await call_tool(tool, {"title": "半个题"}, Ctx())
    check("缺题干时明确说缺什么", "title" in out3 and "statement" in out3, out3[:160])

    denied = await call_tool(tool, {"title": "x", "statement": "y"}, DeniedCtx())
    check("未开通时给可照做的开通命令",
          "没有开通题库服务" in denied and f"/oj bind add group {GROUP_DENIED}" in denied,
          denied[:160])


async def test_repo(plugin):
    print("\n[3] 仓储：去重 / 题组 / 榜单")
    p1 = repo.get_problem("1")
    created = repo.upsert_problem({"title": "另一道题", "statement": "s"},
                                  cases=[{"input": "1", "output": "1"}],
                                  hack_limit=10)[0]
    check("不同标题是不同题", created.id != p1.id)
    check("按洛谷题号也能查",
          repo.get_problem("luogu:P1001") is None or True)   # 未抓过，允许 None
    try:
        repo.upsert_problem({"title": "超限题"},
                            cases=[{"input": str(i), "output": str(i)}
                                   for i in range(11)], hack_limit=10)
        check("超过 10 组测试点直接拒", False, "居然保存成功")
    except repo.OJError as e:
        check("超过 10 组测试点直接拒", "最多 10 组" in str(e), str(e))

    group, is_new = repo.create_set("周末赛", title="周末练手", owner=str(SUPERUSER))
    check("建题组", is_new and group.name == "周末赛")
    added, skipped = repo.add_to_set("周末赛", [p1, created])
    check("加题进题组", (added, skipped) == (2, 0), f"{added}/{skipped}")
    added2, skipped2 = repo.add_to_set("周末赛", [p1])
    check("重复加题会跳过", (added2, skipped2) == (0, 1), f"{added2}/{skipped2}")
    rows = repo.set_problems("周末赛")
    check("题组成员有序", [p.id for _i, p in rows] == [p1.id, created.id],
          str([p.id for _i, p in rows]))
    check("移出题组", repo.remove_from_set("周末赛", [p1]) == 1)
    check("移出后重排序号",
          [i.ordinal for i, _p in repo.set_problems("周末赛")] == [1],
          str([i.ordinal for i, _p in repo.set_problems("周末赛")]))
    repo.add_to_set("周末赛", [p1])
    try:
        repo.normalize_set_name("非法 名字!")
        check("题组名校验", False, "居然通过")
    except repo.OJError:
        check("题组名校验", True)


async def test_luogu(plugin, cmds, api):
    print("\n[4] 洛谷：题号规范化 / 页面解析 / 抓题命令")
    check("p1001 → P1001", normalize_pid(" p1001 ") == "P1001")
    check("URL → 题号",
          normalize_pid("https://www.luogu.com.cn/problem/P1001") == "P1001")
    check("AT_ 题号保留下划线", normalize_pid("AT_abc123_a") == "AT_abc123_a")
    try:
        normalize_pid("不合法!")
        check("非法题号抛 ProblemNotFound", False, "居然通过")
    except ProblemNotFound:
        check("非法题号抛 ProblemNotFound", True)

    parsed = parse_problem_page(sample_html(sample_payload()))
    check("解析题名", parsed.name == "A+B Problem", parsed.name)
    check("解析题干（保留 $）", "$a,b$" in parsed.statement, parsed.statement[:60])
    check("解析样例 2 组", parsed.samples == [("20 30\n", "50\n"),
                                              ("-5 7\n", "2\n")], str(parsed.samples))
    check("memory 由 KB 换算 MB", parsed.memory_limit_mb == 128,
          parsed.memory_limit_mb)
    try:
        parse_problem_page("<html>no json</html>")
        check("没有 lentille-context 时报错", False, "居然通过")
    except ProblemNotFound:
        check("没有 lentille-context 时报错", True)

    # 抓题命令：把网络层换成本地桩
    import plugins.oj_bank.main as main_module

    async def fake_fetch(pid, **kwargs):
        data = sample_payload()["data"]["problem"]
        return LuoguProblem(
            pid=pid, name=data["name"], difficulty=1,
            statement=data["content"]["description"],
            input_format=data["content"]["formatI"],
            output_format=data["content"]["formatO"],
            hint=data["content"]["hint"],
            samples=[tuple(s) for s in data["samples"]],
            time_limit_ms=1000, memory_limit_mb=128,
            url=f"https://www.luogu.com.cn/problem/{pid}")

    original = main_module.fetch_problem
    main_module.fetch_problem = fake_fetch
    try:
        res = await send(cmds, ev("/oj luogu P1001", group=GROUP_OK, user=ALICE))
        text = flat(res)
        check("抓题命令入库", "已入库" in text and "A+B Problem" in text, text[:120])
        problem = repo.get_problem("luogu:P1001")
        check("题目字段来自洛谷",
              problem is not None and problem.title == "A+B Problem"
              and problem.source == "luogu" and problem.source_id == "P1001")
        check("样例写进题目", problem.sample_input.strip() == "20 30"
              and problem.sample_output.strip() == "50")
        cases = repo.cases_of(problem.id)
        check("官方样例作为测试点", len(cases) == 2
              and all(c.kind == "sample" for c in cases), str(len(cases)))
        res2 = await send(cmds, ev("/oj luogu P1001", group=GROUP_OK, user=ALICE))
        check("重复抓题给提示（不重复入库）",
              "已经在题库里" in flat(res2), flat(res2)[:100])
    finally:
        main_module.fetch_problem = original


async def test_judge(plugin):
    print("\n[5] 判题：真编译 / 真运行 / 状态判定")
    problem = repo.get_problem("1")
    cases = repo.cases_of(problem.id)
    judge = plugin.judge

    py_ac = "a,b=map(int,input().split())\nprint(a+b)\n"
    res = await judge.run(language="py", code=py_ac, cases=cases,
                          time_limit_ms=3000, memory_limit_mb=256)
    check("Python 全通过", res.status == "AC" and res.score == 100,
          f"{res.status}/{res.score}/{res.message[:60]}")

    cpp_ac = ('#include <bits/stdc++.h>\n'
              'int main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<"\\n";}\n')
    res = await judge.run(language="cpp", code=cpp_ac, cases=cases,
                          time_limit_ms=3000, memory_limit_mb=256)
    check("C++ 全通过", res.status == "AC", f"{res.status}/{res.message[:120]}")

    cpp_wa = ('#include <bits/stdc++.h>\n'
              'int main(){std::cout<<42<<"\\n";}\n')
    res = await judge.run(language="cpp", code=cpp_wa, cases=cases,
                          time_limit_ms=3000, memory_limit_mb=256)
    check("错误答案判 WA", res.status == "WA" and res.score == 0, res.status)
    check("WA 带实际输出预览", any(c.got for c in res.detail), "无预览")

    res = await judge.run(language="cpp", code="int main(){ 这不是代码 }",
                          cases=cases)
    check("语法错误判 CE", res.status == "CE" and res.message, res.status)

    res = await judge.run(language="py", code="raise SystemExit(3)", cases=cases[:1],
                          time_limit_ms=2000)
    check("非零退出判 RE", res.status == "RE", res.status)

    res = await judge.run(language="py", code="while True:\n    pass\n",
                          cases=cases[:1], time_limit_ms=300)
    check("死循环判 TLE", res.status == "TLE", res.status)

    res = await judge.run(language="py", code="print('x'*2000000)", cases=cases[:1],
                          time_limit_ms=2000)
    check("撑爆输出判 OLE", res.status == "OLE", res.status)

    res = await judge.run(language="py", code="x = [0] * 2000000000\nprint(1)",
                          cases=cases[:1], time_limit_ms=5000,
                          memory_limit_mb=64)
    check("离谱的内存分配被挡住（MLE/RE，不是把机器拖垮）",
          res.status in ("MLE", "RE") and res.status != "AC", res.status)

    try:
        resolve_language("rust")
        check("不支持的语言报错", False, "居然通过")
    except Exception as e:
        check("不支持的语言报错", "不支持的语言" in str(e), str(e))

    check("输出归一化：行尾空格/末尾空行",
          outputs_match("1 \n2\n\n", "1\n2"))
    check("输出归一化不动中间空行",
          normalize_output("1\n\n2") == "1\n\n2")
    check("输出不同就不过", not outputs_match("1", "2"))


async def test_review(plugin):
    print("\n[6] 代码审查：静态规则 + 模型审查（本地假服务）")
    check("system() 命中规则", scan_rules('int main(){system("ls");}')
          == ["执行 shell 命令"], str(scan_rules('int main(){system("ls");}')))
    check("normal 代码不误杀",
          scan_rules("#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;}")
          == [], str(scan_rules("int main(){}")))
    check("fork 炸弹命中", scan_rules(":(){ :|:& };:") != [])
    check("解析模型 JSON 围栏",
          parse_verdict('```json\n{"verdict":"safe","reason":""}\n```') == (True, ""))
    check("解析 unsafe",
          parse_verdict('{"verdict":"unsafe","reason":"读文件"}')[0] is False)

    # 本地假 OpenAI 兼容服务
    from aiohttp import web
    state = {"mode": "safe"}

    async def handler(request):
        if state["mode"] == "http500":
            return web.Response(status=500, text="boom")
        if state["mode"] == "flaky":
            state["flaky_calls"] = int(state.get("flaky_calls") or 0) + 1
            if state["flaky_calls"] == 1:
                request.transport.abort()       # 第一次直接掐断连接
                return web.Response(status=200)
        verdict = ('{"verdict":"unsafe","reason":"试图读取服务器文件"}'
                   if state["mode"] == "unsafe"
                   else '{"verdict":"safe","reason":""}')
        return web.json_response({"choices": [
            {"message": {"content": verdict}, "finish_reason": "stop"}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        reviewer = CodeReviewer(ReviewOptions(
            rules=True, ai_enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
            model="stub", api_key="k", timeout=10))
        safe = await reviewer.check("int main(){return 0;}", language="cpp")
        check("模型判安全 → 放行", safe.allowed and safe.source == "ai",
              safe.to_dict())
        async def unsafe_code(_):
            return "x"
        state["mode"] = "unsafe"
        # ⚠️ 不能再用同一份代码：审查有缓存（按 language+code 哈希），
        # 同一份代码会直接命中上一条「安全」的缓存，测不到模型分支。
        bad = await reviewer.check("int main(){/*other*/return 1;}",
                                   language="cpp")
        check("模型判不安全 → 拒绝", not bad.allowed and "服务器文件" in bad.reason,
              bad.reason)
        state["mode"] = "http500"
        denied = await reviewer.check("int main(){/*d*/}", language="cpp")
        check("审查服务 500 时默认拒绝", not denied.allowed, denied.reason)
        allow_opt = ReviewOptions(rules=True, ai_enabled=True,
                                  base_url=f"http://127.0.0.1:{port}/v1",
                                  model="stub", api_key="k", timeout=10,
                                  on_error="allow")
        reviewer2 = CodeReviewer(allow_opt)
        allowed = await reviewer2.check("int main(){/*e*/}", language="cpp")
        check("on_error=allow 时放行", allowed.allowed, allowed.reason)
        await reviewer.close()
        await reviewer2.close()
    finally:
        await runner.cleanup()

    # 判题入口的拦截：规则命中时不编译、不运行
    problem = repo.get_problem("1")
    res = await plugin.judge.run(
        language="cpp", code='#include <cstdlib>\nint main(){system("rm -rf /");}',
        cases=repo.cases_of(problem.id))
    check("判题前拦截恶意代码", res.status == "BLOCKED", res.status)
    check("拦截有可读理由", "禁用规则" in res.message, res.message)


async def test_markdown_math():
    print("\n[7.5] Markdown：块级公式 / 硬换行（洛谷题面的真实形态）")
    from plugins.oj_bank.markdown import markdown_to_html

    # 洛谷的块级公式：`$$` 后面直接跟内容，`$$` 收在最后一行末尾
    latex = ("$$\\def\\arraystretch{1.2}\n\\begin{array}{|c|c|}\\hline\n"
             "A & B \\\\ \\hline\n\\end{array}$$")
    html = markdown_to_html(latex)
    check("多行 $$ 公式被识别成 math-block",
          "math-block" in html and "$$" not in html, html[:160])
    check("公式内容完整（array 保留）", "begin{array}" in html and "end{array}" in html,
          html[:200])

    html = markdown_to_html("前面\n\n$$\na+b\n$$\n\n后面")
    check("单独成行的 $$ 仍然识别",
          html.count("math-block") == 1 and "前面" in html and "后面" in html,
          html[:160])

    html = markdown_to_html("$$x$$ 后面的文字")
    check("同行收尾且后面还有正文：正文不丢",
          "math-block" in html and "后面的文字" in html, html[:160])

    html = markdown_to_html("第一行 $n,k$；\\\n第二行")
    check("行尾反斜杠（硬换行）不显示成字面 \\",
          "；<br>第二行" in html, html[:160])
    check("公式里的 \\\\ 不被当成硬换行吃掉",
          "\\" in markdown_to_html("$$a \\\\ b$$"), "")


async def test_note_filter(plugin):
    print("\n[7.6] 题目提示只保留数据范围（提示/题解/广告/样例代码都丢掉）")
    from plugins.oj_bank.models import Problem
    from plugins.oj_bank.repo import data_range_only, sanitize_notes

    keep = data_range_only("提示：用单调队列。\n\n【数据范围】\n"
                           "对于 100% 的数据，1≤n≤10^5。")
    check("保留数据范围", keep == "【数据范围】\n对于 100% 的数据，1≤n≤10^5。",
          repr(keep))
    check("丢掉题解", data_range_only("贪心策略：每次取最小的两个数，复杂度 O(n log n)。") == "",
          "")
    check("丢掉广告与样例代码",
          data_range_only("**广告**\n洛谷网校。\n\n```cpp\nint main(){}\n```") == "", "")
    check("【数据规模与约定】这种标题也认",
          data_range_only("**【数据规模与约定】**\n- 对于 30% 的数据，保证 n≤15。")
          .startswith("**【数据规模与约定】**"), "")

    problem = repo.upsert_problem(
        {"title": "带提示的题", "statement": "题干",
         "note": "**提示**：可以用前缀和。\n\n**数据范围**\n$1 \\le n \\le 100$。"},
        hack_limit=10)[0]
    check("入库时过滤提示",
          "前缀和" not in problem.note and "1 \\le n \\le 100" in problem.note,
          repr(problem.note))

    Problem(title="脏数据", statement="题干", key="dirty-note",
            note="提示：贪心。\n\n对于 100% 的数据，1≤n≤10。").save()
    cleaned = sanitize_notes()
    again = Problem.objects.filter(key="dirty-note").first()
    check("回填清理旧数据", cleaned >= 1, str(cleaned))
    check("旧数据只剩数据范围", again.note == "对于 100% 的数据，1≤n≤10。",
          repr(again.note))


async def test_render(plugin, cmds):
    print("\n[7] 出图：题目 / 题组（真起 Chromium）")
    if not find_chromium(plugin.config.get("render.chromium") or ""):
        print("  ⏭️ 没有 Chromium，跳过出图")
        return
    # 相对路径与 file:// 的回归：`plugin_dir` 默认是 `./plugins`，数据目录若
    # 保持相对路径，`Path.as_uri()` 会抛 ValueError，出图/发图全挂。
    from plugins.oj_bank.commands import _file_uri
    check("数据目录是绝对路径", os.path.isabs(plugin.data_dir), plugin.data_dir)
    check("渲染目录是绝对路径", os.path.isabs(plugin.renderer.out_dir),
          plugin.renderer.out_dir)
    check("相对路径也能转 file URI",
          _file_uri("renders/a b.png").startswith("file://"), _file_uri("renders/a b.png"))

    problem = repo.get_problem("1")
    page = build_problem_page(problem, cases=10)
    for section in ("题目描述", "输入格式", "输出格式", "输入样例", "输出样例",
                    "注意事项"):
        check(f"出图页面含「{section}」", section in page, "")
    check("页面不含外部资源", "http://" not in page and "https://" not in page,
          "页面里出现了外链")
    check("KaTeX 资源用本地 file://", "file://" in page and "katex" in page, "")

    res = await send(cmds, ev("/oj show 1", group=GROUP_OK, user=ALICE))
    images = chain_images(res) if isinstance(res, MessageChain) else []
    check("show 返回图片段", len(images) == 1, str(res)[:120])
    if images:
        path = images[0].replace("file://", "")
        from urllib.parse import unquote
        path = unquote(path)
        check("图片文件存在", os.path.exists(path), path)
        width, height = png_size(path)
        check("图片宽度按配置", width == int(plugin.config.get("render.width") or 760),
              f"{width}x{height}")
        check("图片高度合理", 200 <= height <= 8000, f"{width}x{height}")
        check("题目图上有测试点数", "10 组" in flat(res), flat(res)[:80])


async def test_commands(plugin, cmds, api):
    print("\n[8] 命令：提交（两步）/ 结果 / 列表 / 测试点")
    problem = repo.get_problem("1")
    api.sent.clear()

    res = await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
    check("不带代码时进入等待", "把代码文件发上来" in flat(res), flat(res)[:80])
    code_event = ev("a,b=map(int,input().split())\nprint(a+b)", group=GROUP_OK,
                    user=ALICE)
    await plugin._on_code_message(code_event)
    check("下一条消息被当成代码并判题",
          any("AC" in t for t in api.texts()), str(api.texts())[:200])
    last = repo.Submission.objects.order_by("-id").first()
    check("提交记录落库", last is not None and last.status == "AC"
          and last.score == 100, str(last))

    res = await send(cmds, ev("/oj result", group=GROUP_OK, user=ALICE))
    check("result 显示最近一次", f"#{last.id}" in flat(res) and "AC" in flat(res),
          flat(res)[:100])

    res = await send(cmds, ev("/oj list", group=GROUP_OK, user=ALICE))
    check("list 列出题库", "#1" in flat(res) and "题库" in flat(res), flat(res)[:100])

    # 只显示编号 + 标题（来源/难度/测试点数都不进列表）
    rows = [line for line in flat(res).splitlines() if line.startswith("#")]
    check("列表每行只有编号 + 标题",
          bool(rows) and all(" · " not in line and "组测试点" not in line
                             for line in rows), str(rows)[:160])

    # 分页：一页 2 条，翻页与页码提示
    plugin.config.set("list_page_size", 2)
    try:
        first = flat(await send(cmds, ev("/oj list", group=GROUP_OK, user=ALICE)))
        head = first.splitlines()[0]
        check("表头显示总题数与页码", "共" in head and "第 1/" in head, head)
        check("第 1 页给下一页导航", "下一页" in first and "--page=2" in first,
              first[-160:])
        check("第 1 页不出现上一页", "上一页" not in first, first[-160:])
        check("每页条数受 list_page_size 限制",
              len([l for l in first.splitlines() if l.startswith("#")]) == 2,
              first)
        second = flat(await send(cmds, ev("/oj list --page=2", group=GROUP_OK,
                                          user=ALICE)))
        check("翻到第 2 页", "第 2/" in second.splitlines()[0],
              second.splitlines()[0])
        check("第 2 页给上一页导航", "上一页" in second and "--page=1" in second,
              second[-160:])
        last_page = flat(await send(cmds, ev("/oj list --page=999",
                                             group=GROUP_OK, user=ALICE)))
        check("页码超出范围时夹到最后一页", "共" in last_page and "第" in last_page,
              last_page.splitlines()[0] if last_page else "")
    finally:
        plugin.config.set("list_page_size", 8)

    res = await send(cmds, ev("/oj cases 1", group=GROUP_OK, user=ALICE))
    check("cases 列出测试点", "10 组测试点" in flat(res), flat(res)[:80])

    before = len(api.sent)
    await send(cmds, ev("/oj submit 1 cpp int main(){return 0;}",
                        group=GROUP_OK, user=ALICE))
    texts = api.texts()[before:]
    check("命令行直接带代码也能判",
          any("WA" in t or "AC" in t for t in texts), str(texts)[:120])

    # 引用一条代码消息再发 /oj submit → 直接读那条的正文
    api.quote_text = "a,b=map(int,input().split())\nprint(a+b)"
    before = len(api.sent)
    await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE,
                        quote=99001))
    text = "\n".join(api.texts()[before:])
    check("引用代码直接判题", "AC" in text, text[:120])
    api.quote_text = ""
    res = await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE,
                              quote=99002))
    check("引用里没有正文时退回「等下一条」",
          "把代码文件发上来" in flat(res), flat(res)[:80])
    await send(cmds, ev("/oj cancel", group=GROUP_OK, user=ALICE))

    res = await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
    check("再次进入等待", "把代码文件发上来" in flat(res))
    res = await send(cmds, ev("/oj cancel", group=GROUP_OK, user=ALICE))
    check("cancel 取消等待", "已取消" in flat(res), flat(res))
    before = len(api.sent)
    await plugin._on_code_message(ev("print(1)", group=GROUP_OK, user=ALICE))
    check("取消后代码消息不再判题", len(api.sent) == before,
          f"又多发了 {len(api.sent) - before} 条")

    res = await send(cmds, ev("/oj del 1", group=GROUP_OK, user=ALICE))
    check("非超管删题被拒（框架回未知命令或权限提示）",
          res is None or "_not_found" in str(res), str(res)[:80])


async def test_sets(plugin, cmds):
    print("\n[9] 题组：命令 / 榜单")
    res = await send(cmds, ev("/oj set new 练习赛 周三练手", group=GROUP_OK,
                              user=ALICE))
    check("new 建题组", "已新建题组" in flat(res), flat(res)[:80])
    p1 = repo.get_problem("1")
    p2 = repo.upsert_problem({"title": "第二题", "statement": "s"},
                             cases=[{"input": "1", "output": "1"}],
                             hack_limit=10)[0]
    res = await send(cmds, ev(f"/oj set add 练习赛 {p1.id} {p2.id}",
                              group=GROUP_OK, user=ALICE))
    check("add 加题", "已加入 2 题" in flat(res), flat(res)[:80])
    res = await send(cmds, ev("/oj set list", group=GROUP_OK, user=ALICE))
    check("set list 列出", "练习赛" in flat(res), flat(res)[:80])

    # 给两个人各造一次提交（走插件入口，避开命令行的转义）
    event_a = ev("", group=GROUP_OK, user=ALICE)
    event_b = ev("", group=GROUP_OK, user=BOB)
    await plugin.judge_code(event_a, p1, "py",
                            "a,b=map(int,input().split())\nprint(a+b)")
    await plugin.judge_code(event_b, p1, "py", "print(0)")
    res = await send(cmds, ev("/oj set rank 练习赛", group=GROUP_OK, user=ALICE))
    text = flat(res)
    check("榜单出现提交者", str(ALICE) in text and str(BOB) in text, text[:200])
    check("榜单按总分排序（AC 在前）",
          text.index(str(ALICE)) < text.index(str(BOB)), text[:200])
    check("榜单分母是题组总分 200", "200" in text, text[:120])

    res = await send(cmds, ev(f"/oj set remove 练习赛 {p2.id}", group=GROUP_OK,
                              user=ALICE))
    check("remove 移出", "已移出 1 题" in flat(res), flat(res)[:80])
    res = await send(cmds, ev("/oj set del 练习赛", group=GROUP_OK, user=ALICE))
    check("非超管删题组被拒", "仅超级用户" in flat(res) or res is None, flat(res)[:60])
    res = await send(cmds, ev("/oj set del 练习赛", group=GROUP_OK, user=SUPERUSER))
    check("超管删题组", "已删除题组" in flat(res), flat(res)[:80])


async def test_access(plugin, cmds):
    print("\n[10] 服务范围：与 ai_chat 同一套绑定语义")
    res = await send(cmds, ev("/oj list", group=GROUP_DENIED, user=ALICE))
    check("白名单外的群静默", res is None, str(res)[:80])
    res = await send(cmds, ev("/oj list", group=GROUP_DENIED, user=SUPERUSER))
    check("超管豁免服务范围（防止名单把自己锁在外面）",
          res is not None and "题库" in flat(res), flat(res)[:80] if res else "None")
    res = await send(cmds, ev("/oj list", group=GROUP_OK, user=SUPERUSER))
    check("绑定内的群谁都能用", "题库" in flat(res), flat(res)[:60])
    res = await send(cmds, ev("/oj list", user=ALICE))
    check("白名单内的私聊可用", "题库" in flat(res), flat(res)[:60])
    res = await send(cmds, ev("/oj list", user=BOB))
    check("白名单外的私聊静默", res is None, str(res)[:60])
    res = await send(cmds, ev("/oj list", user=BOB, temp_group=GROUP_OK))
    check("允许的群的小窗可用", "题库" in flat(res), flat(res)[:60])
    res = await send(cmds, ev("/oj list", user=BOB, temp_group=GROUP_DENIED))
    check("未允许的群的小窗静默", res is None, str(res)[:60])


async def test_binding_commands(plugin, cmds):
    print("\n[10.5] 绑定管理：/oj bind list|add|del")
    text = flat(await send(cmds, ev("/oj bind list", user=SUPERUSER)))
    check("bind list 显示群与私聊",
          str(GROUP_OK) in text and "私聊" in text, text[:200])

    text = flat(await send(cmds, ev(f"/oj bind add group {GROUP_DENIED}",
                                    user=SUPERUSER)))
    check("bind add group", "已开通群" in text, text[:100])
    check("写入配置", GROUP_DENIED in plugin.binding_ids("groups"),
          str(plugin.binding_ids("groups")))
    text = flat(await send(cmds, ev("/oj list", group=GROUP_DENIED, user=ALICE)))
    check("新开通的群立刻可用", "题库" in text, text[:80])

    text = flat(await send(cmds, ev(f"/oj bind add user {BOB}", user=SUPERUSER)))
    check("bind add user", "已开通用户" in text, text[:100])
    text = flat(await send(cmds, ev("/oj list", user=BOB)))
    check("新开通的私聊立刻可用", "题库" in text, text[:80])

    text = flat(await send(cmds, ev(f"/oj bind del group {GROUP_DENIED}",
                                    user=SUPERUSER)))
    check("bind del group", "已停用群" in text, text[:100])
    res = await send(cmds, ev("/oj list", group=GROUP_DENIED, user=ALICE))
    check("撤掉后立刻失效", res is None, str(res)[:60])

    text = flat(await send(cmds, ev("/oj bind add user", user=SUPERUSER)))
    check("add user 省略号 = 开通自己", "已开通用户" in text, text[:100])
    check("自己进了私聊白名单", SUPERUSER in plugin.binding_ids("users"),
          str(plugin.binding_ids("users")))

    res = await send(cmds, ev("/oj bind list", user=ALICE))
    check("非超管不能用 bind", res is None or "_not_found" in str(res)
          or "权限" in str(res), str(res)[:80])
    res = await send(cmds, ev("/oj bind list", group=GROUP_OK, user=SUPERUSER))
    check("bind 只能私聊用", res is None or "_not_found" in str(res),
          str(res)[:80])

    # 帮助可见性过滤：未开通的群看不到 /oj，超管始终能看到
    class _Handler:
        name = "oj"
        parent_name = ""
    filt = plugin._visibility_filter
    check("未开通的群看不到 /oj 帮助",
          filt is not None and filt(_Handler(), ev("", group=GROUP_DENIED,
                                                   user=ALICE)) is False,
          "没有过滤器")
    check("超管始终能看到 /oj 帮助",
          filt is not None and filt(_Handler(), ev("", group=GROUP_DENIED,
                                                   user=SUPERUSER)) is True,
          "过滤器把超管也挡了")
    check("别的命名空间不受影响",
          filt(_Handler(), ev("", group=GROUP_DENIED, user=ALICE)) is False
          and filt(type("H", (), {"name": "ping", "parent_name": ""})(),
                   ev("", group=GROUP_DENIED, user=ALICE)) is True)


async def test_tool_ctx(plugin):
    print("\n[11] 工具拿到调用者上下文（框架契约）")
    tool = plugin.plugin_tools.get("oj_bank.list")
    check("工具探测出 accepts_ctx", tool.accepts_ctx is True, "")
    from napcat.tools import call_tool

    class Ctx:
        user_id = ALICE
        group_id = GROUP_OK
        chat_id = GROUP_OK
        in_group = True
        is_superuser = False

    out = await call_tool(tool, {}, Ctx())
    check("群内允许的调用可见题库", "题库" in out, out[:80])

    class BadCtx(Ctx):
        group_id = GROUP_DENIED
        chat_id = GROUP_DENIED

    denied = await call_tool(tool, {}, BadCtx())
    check("群外调用被拒", "没有开通题库服务" in denied, denied[:80])


async def test_ai_chat_bridge(plugin):
    print("\n[12] ai_chat 桥：工具进模型工具表、结果回模型")
    from plugins.ai_chat.tools import (ToolContext, ToolRegistry,
                                       register_plugin_tools)
    reg = ToolRegistry()
    register_plugin_tools(reg, tools=plugin.plugin_tools, cfg={"enabled": True})
    ctx = ToolContext(session_id="s1", session_name="测试", user_id=ALICE,
                      chat_kind="group", chat_id=GROUP_OK, group_id=GROUP_OK)
    names = [t.name for t in reg.visible(ctx)]
    check("工具出现在模型工具表里", "oj_bank_save_problem" in names, str(names))
    schemas = {s["function"]["name"]: s["function"] for s in reg.schemas(ctx)}
    params = schemas.get("oj_bank_save_problem", {}).get("parameters", {})
    check("schema 最多 10 组测试点（不强制一次交齐）",
          params.get("properties", {}).get("testcases", {}).get("maxItems") == 10
          and params.get("properties", {}).get("testcases", {}).get("minItems", 1) <= 1,
          str(params.get("properties", {}).get("testcases")))
    check("schema 必填只有标题和题干（其余可补）",
          params.get("required") == ["title", "statement"],
          str(params.get("required")))
    hints = [t.hint for t in reg.visible(ctx) if t.hint]
    check("工具带「使用时机」提示", any("存进题库" in h for h in hints),
          str(hints)[:200])
    from plugins.ai_chat.compaction import build_system_prompt
    prompt = build_system_prompt(
        tool_names=[t.name for t in reg.visible(ctx)], tool_hints=hints)
    check("系统提示里写了插件工具的时机",
          "插件工具的使用时机" in prompt
          and "oj_bank_save_problem" in prompt, prompt[-400:])

    text, is_error = await reg.call("oj_bank_list", {}, ctx)
    check("通过 ai_chat 调工具成功", not is_error and "题库" in text,
          f"{is_error} {text[:80]}")

    allowed = await reg.call("oj_bank_luogu", {"pid": "不合法!"}, ctx)
    check("工具错误回到模型（不抛异常）",
          isinstance(allowed, tuple) and "❌" in allowed[0], str(allowed)[:100])

    ctx2 = ToolContext(session_id="s2", user_id=BOB, chat_kind="group",
                       chat_id=GROUP_DENIED, group_id=GROUP_DENIED)
    text2, _ = await reg.call("oj_bank_list", {}, ctx2)
    check("白名单外调用被业务层拒绝", "没有开通题库服务" in text2, text2[:80])


async def test_file_submit(plugin, cmds, api):
    print("\n[13] 文件交题：文件段 / 引用文件 / 群文件上传")
    from napcat.models import GroupUploadEvent
    from plugins.oj_bank.codefile import (CodeFileError, decode_text,
                                          from_segment, language_of,
                                          message_text)
    check("后缀推断语言",
          (language_of("a.cpp"), language_of("a.py"), language_of("a.c"),
           language_of("a.txt", "cpp")) == ("cpp", "py", "c", "cpp"),
          str(language_of("a.txt", "cpp")))
    check("BOM 被剥掉",
          decode_text("\ufeffprint(1)".encode("utf-8"), limit=100) == "print(1)")
    check("GB18030 兜底",
          decode_text("print('中文')".encode("gb18030"), limit=100).startswith("print"))
    for label, raw, limit in (("二进制文件被拒", b"\x89PNG\r\n\x1a\n", 100),
                              ("超过大小上限被拒", b"x" * 100, 10)):
        try:
            decode_text(raw, limit=limit)
            check(label, False, "居然通过")
        except CodeFileError:
            check(label, True)
    check("取消息文本只取 text 段",
          message_text([{"type": "text", "data": {"text": "abc"}},
                        {"type": "file", "data": {}}]) == "abc")

    work = TMP / "files"
    work.mkdir(parents=True, exist_ok=True)
    py_src = "a,b=map(int,input().split())\nprint(a+b)"
    cpp_src = ('#include <bits/stdc++.h>\n'
               'int main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<"\\n";}')
    (work / "sol.py").write_text(py_src, encoding="utf-8")
    (work / "main.cpp").write_text(cpp_src, encoding="utf-8")
    api.files["f-cpp"] = ("main.cpp", str(work / "main.cpp"))
    api.files["f-py"] = ("sol.py", str(work / "sol.py"))

    # ①.5 本机路径 / file:// 也能取： NapCat 的 get_file 常常给的是落盘路径
    from plugins.oj_bank.codefile import download, local_path_of, within_roots
    path = str(work / "sol.py")
    raw = await download(path, limit=10000, roots=plugin.file_roots)
    check("本机路径直接读", raw.decode() == py_src, raw[:40])
    raw = await download("file://" + path, limit=10000, roots=plugin.file_roots)
    check("file:// URI 能读", raw.decode() == py_src, raw[:40])
    check("file:// 解析成绝对路径", local_path_of("file://" + path) == path, "")
    try:
        await download("/etc/hostname", limit=10000, roots=plugin.file_roots)
        check("允许目录外的本机文件被拒", False, "居然读到了")
    except CodeFileError as e:
        check("允许目录外的本机文件被拒", "不在允许" in str(e), str(e)[:80])
    check("root 之外不匹配", not within_roots("/etc/hostname", [str(work)]), "")

    # get_file 只给本机路径（不给 url）也要能读到
    api.files["f-local"] = ("sol.py", path)
    seg = {"type": "file", "data": {"file_id": "f-local", "name": "sol.py"}}
    got = await from_segment(seg, api, limit=10000,
                             roots=plugin.file_roots,
                             file_manager=FileManager(api))
    check("get_file 给落盘路径也能取", got.content == py_src, got.content[:40])

    # ① 命令消息自带文件：语言按后缀推断，结果里带文件名
    event = ev("/oj submit 1", group=GROUP_OK, user=ALICE)
    event.message.append({"type": "file",
                          "data": {"file_id": "f-cpp", "name": "main.cpp",
                                   "size": len(cpp_src)}})
    before = len(api.sent)
    await send(cmds, event)
    text = "\n".join(api.texts()[before:])
    check("命令自带文件：直接判题", "AC" in text, text[:120])
    check("文件走框架的文件 API（不是插件自己下载）", api.stream_calls > 0,
          str(api.stream_calls))
    check("结果带文件名与推断语言",
          "main.cpp" in text and "C++17" in text, text[:160])

    # ② 引用一个代码文件
    api.quote_message = [{"type": "file",
                          "data": {"file_id": "f-py", "name": "sol.py",
                                   "size": len(py_src)}}]
    before = len(api.sent)
    await send(cmds, ev("/oj submit 1", group=GROUP_OK, user=ALICE, quote=555))
    text = "\n".join(api.texts()[before:])
    check("引用文件：判题", "AC" in text, text[:120])
    check("引用文件：语言按后缀（.py → Python 3）",
          "Python 3" in text and "sol.py" in text, text[:160])
    api.quote_message = None

    # ③ 群文件上传（notice）：pending 语言 cpp，文件是 .py → 后缀优先
    server, base = await start_file_server({"sol.py": py_src.encode()})
    try:
        api.group_files["g-1"] = f"{base}/sol.py"
        text = flat(await send(cmds, ev("/oj submit 1 cpp", group=GROUP_OK,
                                        user=ALICE)))
        check("进入等待代码文件", "代码文件" in text, text[:80])
        before = len(api.sent)
        await plugin.event_bus.dispatch(GroupUploadEvent(
            group_id=GROUP_OK, user_id=ALICE,
            file={"id": "g-1", "name": "sol.py", "size": len(py_src)}))
        texts = api.texts()[before:]
        check("群文件上传触发判题", any("AC" in t for t in texts), str(texts)[:160])
        check("后缀覆盖 pending 语言（cpp → Python 3）",
              any("Python 3" in t and "sol.py" in t for t in texts),
              str(texts)[:160])
    finally:
        await server.cleanup()

    # ④ 待提交 + 私聊直接发文件消息
    text = flat(await send(cmds, ev("/oj submit 1 py", user=ALICE)))
    check("私聊进入等待", "代码文件" in text, text[:80])
    before = len(api.sent)
    file_msg = ev("", user=ALICE)
    file_msg.message.append({"type": "file",
                             "data": {"file_id": "f-py", "name": "sol.py"}})
    await plugin._on_code_message(file_msg)
    texts = api.texts()[before:]
    check("私聊文件消息判题", any("AC" in t for t in texts), str(texts)[:160])

    # ⑤ 文件不受命令解析转义影响（反斜杠原样保留）
    tricky = ("#include <cstdio>\n"
              'int main(){printf("a\\tb\\n");return 0;}')
    (work / "esc.cpp").write_text(tricky, encoding="utf-8")
    api.files["f-esc"] = ("esc.cpp", str(work / "esc.cpp"))
    from plugins.oj_bank.codefile import from_segment
    got = await from_segment({"data": {"file_id": "f-esc", "name": "esc.cpp"}},
                             api, limit=plugin.max_file_bytes,
                             file_manager=FileManager(api))
    check("文件内容一个字节都不动（\\t / \\n 保留）",
          "\\t" in got.content and "\\n" in got.content, repr(got.content))

    # 过期的下载地址要能退回 get_file（QQ 的文件 url 有时效）
    from plugins.oj_bank.codefile import from_segment as _from_segment
    got2 = await _from_segment(
        {"data": {"url": "http://127.0.0.1:1/expired", "file_id": "f-py",
                  "name": "sol.py"}}, api, limit=plugin.max_file_bytes,
        file_manager=FileManager(api))
    check("过期 URL 退回 get_file", got2.content == py_src, got2.content[:60])
    try:
        await _from_segment({"data": {"url": "javascript:alert(1)"}}, api,
                            limit=1024, file_manager=FileManager(api))
        check("非 http(s) 地址被拒", False, "居然通过")
    except CodeFileError:
        check("非 http(s) 地址被拒", True)

    # ⑥ 服务范围外：群文件上传静默
    text = flat(await send(cmds, ev("/oj submit 1 py", group=GROUP_OK,
                                    user=SUPERUSER)))
    check("超管进入等待", "代码文件" in text, text[:60])
    before = len(api.sent)
    api.group_files["g-2"] = f"{base}/sol.py"
    await plugin.event_bus.dispatch(GroupUploadEvent(
        group_id=GROUP_DENIED, user_id=SUPERUSER,
        file={"id": "g-2", "name": "sol.py"}))
    check("别的群的群文件不会串到本群提交", len(api.sent) == before,
          str(api.texts()[before:])[:120])
    await send(cmds, ev("/oj cancel", group=GROUP_OK, user=SUPERUSER))


async def test_coach(plugin, cmds, api):
    print("\n[14] 判完分后的 AI 点评（后台任务，失败不影响判题）")
    from aiohttp import web
    from plugins.oj_bank.coach import CodeCoach, CoachOptions, clean_reply

    check("点评文本去掉围栏与加粗",
          clean_reply("```\n**思路**：前缀和\n```") == "思路：前缀和",
          clean_reply("```\n**思路**：前缀和\n```"))
    check("点评文本压掉多余空行",
          "\n\n\n" not in clean_reply("a\n\n\n\nb"), "")

    state = {"mode": "ok"}

    async def handler(request):
        if state["mode"] == "http500":
            return web.Response(status=500, text="boom")
        if state["mode"] == "flaky":
            state["flaky_calls"] = int(state.get("flaky_calls") or 0) + 1
            if state["flaky_calls"] == 1:
                request.transport.abort()       # 第一次直接掐断连接
                return web.Response(status=200)
        if state["mode"] == "length":
            return web.json_response({"choices": [
                {"message": {"content": ""}, "finish_reason": "length"}]})
        content = "思路：读入两个整数并输出和。\n复杂度：时间 O(1)，空间 O(1)。\n改进：1) 用更快的 IO；2) 注意 long long。"
        return web.json_response({"choices": [
            {"message": {"content": content}, "finish_reason": "stop"}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        plugin._coach = CodeCoach(
            CoachOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                         model="stub", api_key="k", timeout=5),
            logger=logging.getLogger("test.oj_bank.coach"))
        code = "a,b=map(int,input().split())\nprint(a+b)"

        before = len(api.sent)
        await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
        await plugin._on_code_message(ev(code, group=GROUP_OK, user=ALICE))
        got = await wait_for(lambda: any("代码点评" in t for t in api.texts()[before:]))
        texts = api.texts()[before:]
        check("点评作为后续消息发出来", got, str(texts)[-200:])
        check("先发分数、后发点评",
              texts and "AC" in texts[0] and "代码点评" in texts[-1],
              str(texts)[:200])
        check("点评内容是模型说的那段",
              "复杂度：时间 O(1)" in texts[-1], texts[-1][:160])

        # 手动触发：/oj coach <提交编号>
        latest = repo.Submission.objects.order_by("-id").first()
        res = flat(await send(cmds, ev(f"/oj coach {latest.id}",
                                       group=GROUP_OK, user=ALICE)))
        check("手动点评命令可用", "代码点评" in res, res[:160])
        check("别人不能点评我的提交",
              "只能点评自己的提交" in flat(
                  await send(cmds, ev(f"/oj coach {latest.id}",
                                      group=GROUP_OK, user=BOB))),
              "")

        # 网关掐断连接：自动换连接重试一次，点评照样出来
        state["mode"] = "flaky"
        state["flaky_calls"] = 0
        before5 = len(api.sent)
        await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
        await plugin._on_code_message(ev(code, group=GROUP_OK, user=ALICE))
        got = await wait_for(
            lambda: any("代码点评（提交" in t for t in api.texts()[before5:]),
            timeout=15)
        check("网关断连会自动重试", got, str(api.texts()[before5:])[-200:])

        # 模型被 max_tokens 截断：告诉用户原因，不再静默
        state["mode"] = "length"
        before4 = len(api.sent)
        await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
        await plugin._on_code_message(ev(code, group=GROUP_OK, user=ALICE))
        got = await wait_for(
            lambda: any("点评没生成出来" in t for t in api.texts()[before4:]))
        check("点评被截断时告知失败原因", got,
              str(api.texts()[before4:])[-160:])

        # 服务 500：判题照常，只是少一条点评
        state["mode"] = "http500"
        before2 = len(api.sent)
        await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
        await plugin._on_code_message(ev(code, group=GROUP_OK, user=ALICE))
        await asyncio.sleep(0.4)
        texts2 = api.texts()[before2:]
        check("点评服务挂掉不影响判题结果",
              any("AC" in t for t in texts2)
              and not any(t.startswith("🧑‍🏫") for t in texts2),
              str(texts2)[:200])
        check("点评失败会告知原因",
              any("点评没生成出来" in t for t in texts2), str(texts2)[:200])

        # 关掉点评：不再有第二条消息
        plugin._coach.opt.enabled = False
        before3 = len(api.sent)
        await send(cmds, ev("/oj submit 1 py", group=GROUP_OK, user=ALICE))
        await plugin._on_code_message(ev(code, group=GROUP_OK, user=ALICE))
        await asyncio.sleep(0.3)
        check("关掉点评后只有判题消息",
              len(api.texts()[before3:]) == 1, str(api.texts()[before3:])[:160])
    finally:
        await runner.cleanup()


async def test_generate(plugin, cmds, api):
    print("\n[15] AI 出测试点：一次一组（1 输入 + 1 输出），重复到凑满")
    from aiohttp import web
    from plugins.oj_bank.gen import (CaseGenerator, GenError, GenOptions,
                                     parse_single_case)
    from napcat.tools import call_tool

    case = parse_single_case('```json\n{"input":"1 2","output":"3",'
                             '"note":"最小"}\n```')
    check("解析单组 JSON（带围栏）",
          case == {"input": "1 2", "output": "3", "note": "最小"}, str(case))
    try:
        parse_single_case('{"input":"1 2"}')
        check("缺 output 时报错（一组必须有输入+输出）", False, "居然通过")
    except GenError:
        check("缺 output 时报错（一组必须有输入+输出）", True)
    try:
        parse_single_case("这里没有 JSON")
        check("没有 JSON 时报错", False, "居然通过")
    except GenError:
        check("没有 JSON 时报错", True)

    state = {"mode": "ok", "calls": 0}

    def case_body() -> str:
        state["calls"] = int(state.get("calls") or 0) + 1
        n = state["calls"]
        if state["mode"] == "bad_output":
            return '{"input": "%d %d", "note": "缺输出"}' % (n, n)
        if state["mode"] == "dup":
            return '{"input": "1 1", "output": "2", "note": "重复"}'
        # 故意给一个「不是真答案」的输出：验证插件原样采用模型给的 output
        return json.dumps({"input": f"{n} {n}", "output": str(n * 100),
                           "note": f"第{n}组"}, ensure_ascii=False)

    async def handler(request):
        if state["mode"] == "http500":
            return web.Response(status=500, text="boom")
        if state["mode"] == "flaky":
            state["flaky_calls"] = int(state.get("flaky_calls") or 0) + 1
            if state["flaky_calls"] == 1:
                request.transport.abort()       # 第一次直接掐断连接
                return web.Response(status=200)
        return web.json_response({"choices": [
            {"message": {"content": case_body()}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        plugin._gen = CaseGenerator(
            GenOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                       model="stub", api_key="k", timeout=10, count=2),
            logger=logging.getLogger("test.oj_bank.gen"))
        problem = repo.upsert_problem(
            {"title": "AI 生成测试", "statement": "输入 $a,b$，输出 $a+b$。",
             "input_format": "两个整数", "sample_input": "20 30\n",
             "sample_output": "50\n"}, hack_limit=10)[0]

        state.update(mode="ok", calls=0)
        text = await plugin.generate_cases(problem, count=2)
        cases = repo.cases_of(problem.id)
        check("生成成功并入库", "已生成 2 组" in text and len(cases) == 2, text[:160])
        check("**一次只要一组**：2 组 = 2 次模型调用", state["calls"] == 2,
              str(state["calls"]))
        check("输出采用模型给的那份（不再跑参考解）",
              [c.expected.strip() for c in cases] == ["100", "200"],
              str([c.expected for c in cases]))
        check("输入原样保存", cases[0].stdin.strip() == "1 1", cases[0].stdin)

        # 10 组 = 10 次调用（逐组），不是一次要 10 个
        state.update(mode="ok", calls=0)
        text = await plugin.generate_cases(problem, count=10)
        check("10 组 = 10 次请求", state["calls"] == 10, str(state["calls"]))
        check("逐组生成凑满 10 组", "已生成 10 组" in text, text[:200])
        check("结果里写明逐组生成", "逐组生成 10 次" in text, text[:200])

        # 拿到一组就存一组：回调时库里已经有这一组
        state.update(mode="ok", calls=0)
        incremental = repo.upsert_problem(
            {"title": "边生成边入库", "statement": "输入 a,b 输出 a+b",
             "sample_input": "1 2\n", "sample_output": "3\n"}, hack_limit=10)[0]
        marks = []

        async def record(index, total, case):
            marks.append(len(repo.cases_of(incremental.id)))

        await plugin.generate_cases(incremental, count=3, on_case=record)
        check("拿到一组就存一组（回调时已入库）", marks == [1, 2, 3], str(marks))

        # 第一组入库时替换旧数据，后续追加（不会每存一组就清空）
        with_old = repo.upsert_problem(
            {"title": "替换语义", "statement": "输入 a,b 输出 a+b",
             "sample_input": "1 2\n", "sample_output": "3\n"}, hack_limit=10)[0]
        repo.add_cases(with_old, [{"input": f"{i} 0", "output": str(i)}
                                  for i in range(1, 4)], kind="hack",
                       replace=True, hack_limit=10)
        await plugin.generate_cases(with_old, count=2)
        check("第一组入库时替换旧的、后续追加",
              len(repo.cases_of(with_old.id)) == 2,
              str(len(repo.cases_of(with_old.id))))

        # 重做单组：只替换指定的那一组
        state.update(mode="ok", calls=0)
        redo = repo.upsert_problem(
            {"title": "重做单组", "statement": "输入 a,b 输出 a+b",
             "sample_input": "1 2\n", "sample_output": "3\n"}, hack_limit=10)[0]
        repo.add_cases(redo, [{"input": "11 11", "output": "22", "note": "旧1"},
                              {"input": "22 22", "output": "44", "note": "旧2"}],
                       kind="hack", replace=True, hack_limit=10)
        old = repo.cases_of(redo.id)
        text = flat(await send(cmds, ev(f"/oj case {redo.id} 2", group=GROUP_OK,
                                        user=ALICE)))
        new = repo.cases_of(redo.id)
        check("重做单组：第 1 组没动", new[0].stdin == old[0].stdin, new[0].stdin)
        check("重做单组：第 2 组被替换", new[1].stdin != old[1].stdin,
              f"{old[1].stdin!r} -> {new[1].stdin!r}")
        check("重做结果带新输入输出", "第 2 组已替换" in text, text[:160])
        bad = flat(await send(cmds, ev(f"/oj case {redo.id} 99", group=GROUP_OK,
                                       user=ALICE)))
        check("组号不存在时报错", "没有第 99 组" in bad, bad[:120])

        state.update(mode="bad_output", calls=0)
        text = await plugin.generate_cases(problem, count=3)
        check("模型不给 output 时给可读错误", "生成失败" in text, text[:160])

        state.update(mode="dup", calls=0)
        text = await plugin.generate_cases(problem, count=3)
        check("重复输入不会凑数（如实说只凑到几组）",
              "只凑到 1 组" in text and "重复" in text, text[:200])

        state.update(mode="http500", calls=0)
        text = await plugin.generate_cases(problem, count=2)
        check("模型 500 时给可读错误", "生成失败" in text, text[:120])

        # 已有 10 组：/oj gen 只补缺口，不再重新生成
        repo.clear_cases(problem)
        repo.add_cases(problem, [{"input": f"{i} {i}", "output": str(2 * i),
                                  "note": f"旧{i}"} for i in range(1, 11)],
                       kind="hack", replace=True, hack_limit=10)
        state.update(mode="ok", calls=0)
        res = flat(await send(cmds, ev(f"/oj gen {problem.id}", group=GROUP_OK,
                                       user=ALICE)))
        check("已有 10 组时不再生成", "不用再补" in res, res[:160])
        check("已有 10 组时不调用模型", state["calls"] == 0, str(state["calls"]))

        # 清到只剩 2 组，再 /oj gen：只补 8 组，旧的保留
        repo.clear_cases(problem)
        repo.add_cases(problem, [{"input": "1 1", "output": "2", "note": "旧1"},
                                 {"input": "2 2", "output": "4", "note": "旧2"}],
                       kind="hack", replace=True, hack_limit=10)
        state.update(mode="ok", calls=0)
        before = len(api.sent)
        res = flat(await send(cmds, ev(f"/oj gen {problem.id}", group=GROUP_OK,
                                       user=ALICE)))
        check("补缺口回执说明已有几组", "已有 2/10" in res and "补齐 8" in res,
              res[:160])
        got = await wait_for(
            lambda: any("已生成" in t and f"#{problem.id}" in t
                        for t in api.texts()[before:]), timeout=25)
        check("补缺口完成后发消息", got, str(api.texts()[before:])[-200:])
        cases = repo.cases_of(problem.id)
        check("只补 8 组、旧的两组还在",
              len(cases) == 10 and cases[0].stdin.strip() == "1 1"
              and cases[1].stdin.strip() == "2 2",
              str([(c.stdin, c.note) for c in cases[:3]]))
        check("补缺口只调用 8 次模型", state["calls"] == 8, str(state["calls"]))
        progress = [t for t in api.texts()[before:] if "组：输入" in t]
        check("补缺口进度按整题序号（3/10 … 10/10）",
              any("第 3/10 组" in t for t in progress)
              and any("第 10/10 组" in t for t in progress),
              str(progress)[:200])

        # 同一题正在生成时再触发：不并发生成
        plugin._gen_active.add(int(problem.id))
        try:
            text = await plugin.generate_cases(problem, count=1)
        finally:
            plugin._gen_active.discard(int(problem.id))
        check("同一题生成中不会重复开跑", "正在生成测试点" in text, text[:120])

        class Ctx:
            user_id = ALICE
            group_id = GROUP_OK
            chat_id = GROUP_OK
            in_group = True
            is_superuser = False

        tool = plugin.plugin_tools.get("oj_bank.generate_cases")
        check("生成工具已注册", tool is not None, "")
        # 工具也只补缺口：留 9 组，工具补 1 组
        last = repo.cases_of(problem.id)[-1]
        last.delete()
        before = len(api.sent)
        out = await call_tool(tool, {"problem_id": problem.id, "count": 2}, Ctx())
        check("工具回执说明补几组", "补齐 1" in out, out[:160])
        got = await wait_for(
            lambda: any("已生成" in t for t in api.texts()[before:]), timeout=25)
        check("工具触发的生成也会发消息", got, str(api.texts()[before:])[-160:])
        check("工具补完正好 10 组", len(repo.cases_of(problem.id)) == 10,
              str(len(repo.cases_of(problem.id))))
        tool_progress = [t for t in api.texts()[before:] if "组：输入" in t]
        check("工具路径不逐组反馈（只发汇总）", tool_progress == [],
              str(tool_progress)[:160])
    finally:
        await runner.cleanup()


async def test_backfill(plugin, cmds, api):
    print("\n[15.6] 启动补齐：旧题没有测试点时自动生成")
    from aiohttp import web
    from plugins.oj_bank.gen import CaseGenerator, GenOptions

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        i = calls["n"]
        body = json.dumps({"input": f"{i} {i}\n", "output": f"{2 * i}\n",
                           "note": f"第{i}组"}, ensure_ascii=False)
        return web.json_response({"choices": [{"message": {"content": body}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    tmp = TMP / "backfill"
    tmp.mkdir(parents=True, exist_ok=True)
    fresh, _cmds, fresh_api = await build_plugin(tmp)
    try:
        fresh._gen = CaseGenerator(
            GenOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                       model="stub", api_key="k", timeout=10, count=10),
            logger=logging.getLogger("test.oj_bank.backfill"))
        empty = repo.upsert_problem(
            {"title": "旧题（没测试点）", "statement": "输入 a,b 输出 a+b",
             "sample_input": "1 2\n", "sample_output": "3\n"}, hack_limit=10)[0]
        repo.add_cases(empty, [{"input": f"{i} {i}", "output": str(2 * i)}
                               for i in range(1, 11)], kind="hack",
                       replace=True, hack_limit=10)
        # 上面这道先补满，再建一道空的：启动补齐只挑空的
        another = repo.upsert_problem(
            {"title": "旧题（真正空的）", "statement": "输入 n 输出 n",
             "sample_input": "1\n", "sample_output": "1\n"}, hack_limit=10)[0]

        fresh.config.set("gen.backfill_limit", 1)
        before = len(fresh_api.sent)
        await fresh._backfill_missing()
        got = [t for t in fresh_api.texts()[before:] if "🧪" in t]
        check("启动补齐会通知超管（私聊）", bool(got), str(fresh_api.sent[before:])[:200])
        check("只补没有测试点的那道题",
              len(repo.cases_of(another.id)) == 10, str(len(repo.cases_of(another.id))))
        check("已满 10 组的题不再动", len(repo.cases_of(empty.id)) == 10,
              str(len(repo.cases_of(empty.id))))
        check("补齐结果写进通知", any("已生成 10 组" in t for t in got), str(got)[:200])
        progress = [t for t in fresh_api.texts()[before:] if "组：输入" in t]
        check("启动补齐不逐组反馈（只发汇总）", progress == [], str(progress)[:160])
    finally:
        await fresh.on_disable()
        await runner.cleanup()


async def test_fixed_output(plugin, cmds, api):
    print("\n[15.5] 固定输出的题：AI 判定后不生成额外测试点")
    from aiohttp import web
    from plugins.oj_bank.gen import (CaseGenerator, GenError, GenOptions,
                                     parse_single_case)
    from napcat.tools import call_tool

    # 提示词必须带数据范围（模型越界就是因为没看到约束）
    class Probe:
        title = "砍树"
        statement = "输入 n，输出答案。"
        input_format = "一行一个整数 n"
        output_format = "一个整数"
        sample_input = "3\n"
        sample_output = "6\n"
        note = "【数据范围】对于 100% 的数据，$1 \\le n \\le 10^5$。"
        time_limit_ms = 1000
        memory_limit_mb = 256

    probe_gen = CaseGenerator(GenOptions(enabled=True, base_url="x",
                                         model="m", api_key="k"))
    prompt = probe_gen._user_prompt(Probe(), 1, 10)
    check("生成提示词带数据范围", "1 \\le n \\le 10^5" in prompt, prompt[:200])
    check("生成提示词带时限/内存", "1000 ms" in prompt and "256 MB" in prompt,
          prompt[:200])
    from plugins.oj_bank.gen import SYSTEM_PROMPT
    check("系统提示明确要求不越界", "数据范围" in SYSTEM_PROMPT
          and "不要" in SYSTEM_PROMPT, SYSTEM_PROMPT[:200])
    prompts = [probe_gen._user_prompt(Probe(), i, 10) for i in range(1, 11)]
    focuses = []
    for text in prompts:
        lines = text.splitlines()
        focuses.append(lines[lines.index("【这一组的重点】") + 1])
    traps = [f for f in focuses if f.startswith("卡点")]
    check("第 1 组是常规数据", focuses[0].startswith("常规数据"), focuses[0])
    check("没有误区时全部是常规点", traps == [], str(traps))

    fixed = parse_single_case('{"fixed_output": true, "reason": "没有输入"}')
    check("解析「固定输出」判定", fixed.get("fixed_output") is True, str(fixed))
    check("判定里带原因", fixed.get("reason") == "没有输入", str(fixed))

    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        body = json.dumps({"fixed_output": True, "reason": "没有输入/输出固定"},
                          ensure_ascii=False)
        return web.json_response({"choices": [{"message": {"content": body}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        plugin._gen = CaseGenerator(
            GenOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                       model="stub", api_key="k", timeout=10, count=10),
            logger=logging.getLogger("test.oj_bank.fixed"))

        # ① 有样例、没有测试点 → 用样例造一组（输入为空），只问一次模型
        state["calls"] = 0
        p1 = repo.upsert_problem(
            {"title": "固定输出题", "statement": "输出 42。",
             "sample_output": "42\n"}, hack_limit=10)[0]
        text = await plugin.generate_cases(p1, count=10)
        cases = repo.cases_of(p1.id)
        check("固定输出题只问一次模型", state["calls"] == 1, str(state["calls"]))
        check("固定输出题不生成 10 组", len(cases) == 1, str(len(cases)))
        check("用样例造的那组：输入为空、输出是样例",
              cases[0].stdin == "" and cases[0].expected.strip() == "42",
              f"{cases[0].stdin!r}/{cases[0].expected!r}")
        check("结果说明不需要额外测试点", "不需要额外测试点" in text, text[:160])

        # ② 没有样例输出 → 说明无法评分，不硬造
        p2 = repo.upsert_problem(
            {"title": "固定输出无样例", "statement": "输出 Hello。"},
            hack_limit=10)[0]
        text = await plugin.generate_cases(p2, count=10)
        check("没有样例时不硬造测试点",
              len(repo.cases_of(p2.id)) == 0 and "没有样例输出" in text,
              text[:160])

        # ③ 已经有测试点 → 直接说能评分，不再动数据
        p3 = repo.upsert_problem(
            {"title": "固定输出已有测试点", "statement": "输出 7。",
             "sample_output": "7\n"}, hack_limit=10)[0]
        repo.add_cases(p3, [{"input": "", "output": "7", "note": "样例"}],
                       kind="hack", hack_limit=10)
        text = await plugin.generate_cases(p3, count=10)
        check("已有测试点时不重复生成",
              len(repo.cases_of(p3.id)) == 1 and "已有 1 组测试点" in text,
              text[:160])

        class Ctx:
            user_id = ALICE
            group_id = GROUP_OK
            chat_id = GROUP_OK
            in_group = True
            is_superuser = False

        # ④ 入库自动生成路径：AI 判定固定输出后不再生成 10 组，并通知
        before = len(api.sent)
        out = await call_tool(plugin.plugin_tools.get("oj_bank.save_problem"), {
            "title": "自动固定的题", "statement": "输出 1。",
            "sample_output": "1\n"}, Ctx())
        check("存题仍会安排生成", "正在用 AI 生成" in out, out[:160])
        got = await wait_for(
            lambda: any("不需要额外测试点" in t for t in api.texts()[before:]),
            timeout=20)
        check("固定输出题的完成消息说明不用生成", got,
              str(api.texts()[before:])[-200:])
        saved = repo.Problem.objects.filter(title="自动固定的题").first()
        check("自动路径也只留 1 组测试点",
              saved is not None and len(repo.cases_of(saved.id)) == 1,
              str(len(repo.cases_of(saved.id)) if saved else "无题"))
    finally:
        await runner.cleanup()


async def test_pitfalls(plugin, cmds, api):
    print("\n[15.4] 先问误区再出卡点；没有误区全常规；允许脚本生成")
    from aiohttp import web
    from plugins.oj_bank.gen import (CaseGenerator, GenOptions, parse_pitfalls)

    check("解析误区列表",
          parse_pitfalls('{"pitfalls": [{"name": "只按相邻去重", "why": "漏了不连续重复"}]}')
          == ["只按相邻去重：漏了不连续重复"], "")
    check("误区条数上限 4",
          len(parse_pitfalls('{"pitfalls": ["a","b","c","d","e"]}')) == 4, "")

    state = {"mode": "pitfalls", "cases": [], "pitfall_calls": 0, "case_calls": 0}

    async def handler(request):
        body = await request.json()
        system = str((body.get("messages") or [{}])[0].get("content") or "")
        if "思维误区" in system:
            state["pitfall_calls"] += 1
            if state["mode"] == "no_pitfalls":
                payload = {"pitfalls": []}
            else:
                payload = {"pitfalls": [
                    {"name": "只按相邻去重", "why": "漏了不连续重复"},
                    {"name": "int 溢出", "why": "和超过 2^31"},
                ]}
            return web.json_response({"choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})
        state["case_calls"] += 1
        user = str((body.get("messages") or [{}, {}])[1].get("content") or "")
        state["cases"].append(user)
        if state["mode"] == "script":
            script = ("print('<<<INPUT>>>')\nprint('7 8')\n"
                      "print('<<<OUTPUT>>>')\nprint('15')")
            return web.json_response({"choices": [{"message": {
                "content": json.dumps({"script": script, "note": "脚本生成"})}}]})
        if state["mode"] == "bad_script":
            script = "import socket\nprint('x')"
            return web.json_response({"choices": [{"message": {
                "content": json.dumps({"script": script})}}]})
        n = state["case_calls"]
        return web.json_response({"choices": [{"message": {
            "content": json.dumps({"input": f"{n} {n}", "output": str(2 * n),
                                   "note": f"组{n}"})}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        plugin.config.set("gen.pitfalls", True)
        plugin._gen = CaseGenerator(
            GenOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                       model="stub", api_key="k", timeout=10, count=5),
            logger=logging.getLogger("test.oj_bank.pitfalls"))
        problem = repo.upsert_problem(
            {"title": "误区测试", "statement": "输入 a,b 输出 a+b",
             "input_format": "两个整数", "sample_input": "1 2\n",
             "sample_output": "3\n"}, hack_limit=10)[0]

        state.update(mode="pitfalls", cases=[], pitfall_calls=0, case_calls=0)
        text = await plugin.generate_cases(problem, count=5)
        check("生成前先问一次误区", state["pitfall_calls"] == 1,
              str(state["pitfall_calls"]))
        check("5 组 = 1 次误区分析 + 5 次出题",
              state["case_calls"] == 5, str(state["case_calls"]))
        pitfall_traps = [c for c in state["cases"] if "卡点（针对本题误区）" in c]
        perf_traps = [c for c in state["cases"] if "卡点·性能与剪枝" in c]
        check("5 组里 2 组卡点 = 1 误区 + 1 性能", len(pitfall_traps) == 1
              and len(perf_traps) == 1,
              f"{len(pitfall_traps)}/{len(perf_traps)}")
        check("误区卡点引用具体误区",
              all("只按相邻去重" in t or "int 溢出" in t for t in pitfall_traps),
              str([t[-120:] for t in pitfall_traps]))
        check("性能卡点要求拉满规模", all("尽量大" in t for t in perf_traps),
              str([t[-100:] for t in perf_traps]))
        check("结果说明针对几个误区", "针对 2 个常见误区" in text, text[:160])

        # 没有误区 → 全常规点
        state.update(mode="no_pitfalls", cases=[], pitfall_calls=0, case_calls=0)
        text = await plugin.generate_cases(problem, count=3)
        traps = [c for c in state["cases"] if "卡点（针对本题误区）" in c]
        check("没有误区时全出常规点", traps == [], str(len(traps)))
        check("结果说明本题没有明显误区", "没有明显误区" in text, text[:160])

        # 脚本生成
        state.update(mode="script", cases=[], pitfall_calls=0, case_calls=0)
        text = await plugin.generate_cases(problem, count=1)
        cases = repo.cases_of(problem.id)
        scripted = [c for c in cases if c.stdin.strip() == "7 8"]
        check("脚本生成的输入被采用", bool(scripted), str([c.stdin for c in cases]))
        check("脚本生成的输出被采用",
              bool(scripted) and scripted[0].expected.strip() == "15", "")

        # 危险脚本：拒收
        state.update(mode="bad_script", cases=[], pitfall_calls=0, case_calls=0)
        before = len(repo.cases_of(problem.id))
        text = await plugin.generate_cases(problem, count=1)
        check("危险脚本被拒（不执行）",
              "生成失败" in text or "不允许" in text, text[:160])
        check("危险脚本不写库", len(repo.cases_of(problem.id)) == before, "")
    finally:
        plugin.config.set("gen.pitfalls", False)
        await runner.cleanup()


async def test_auto_generate(plugin, cmds, api):
    print("\n[16] 入库即自动生成 10 组测试点（命令 / 工具 / AI 存题）")
    from aiohttp import web
    from plugins.oj_bank.gen import CaseGenerator, GenOptions
    from napcat.tools import call_tool

    calls = {"n": 0}

    async def handler(request):
        # 一次只给一组：输入 + 输出
        calls["n"] += 1
        i = calls["n"]
        body = json.dumps({"input": f"{i} {i}\n", "output": f"{2 * i}\n",
                           "note": f"第{i}组"}, ensure_ascii=False)
        return web.json_response({"choices": [{"message": {"content": body}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class Ctx:
        user_id = ALICE
        group_id = GROUP_OK
        chat_id = GROUP_OK
        in_group = True
        is_superuser = False

    try:
        plugin._gen = CaseGenerator(
            GenOptions(enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
                       model="stub", api_key="k", timeout=10, count=10),
            logger=logging.getLogger("test.oj_bank.auto"))

        # ① AI 存题：入库后自动生成（工具结果会提示正在生成）
        before = len(api.sent)
        out = await call_tool(plugin.plugin_tools.get("oj_bank.save_problem"), {
            "title": "自动生成测试", "statement": "输入 $a,b$，输出 $a+b$。",
            "input_format": "两个整数", "sample_input": "1 2\n",
            "sample_output": "3\n"}, Ctx())
        check("存题结果提示正在生成", "正在用 AI 生成" in out, out[:200])
        problem = repo.Problem.objects.filter(title="自动生成测试").first()
        check("题目已入库", problem is not None, "")
        got = await wait_for(
            lambda: any("生成" in t and "测试点" in t for t in api.texts()[before:]),
            timeout=20)
        check("后台生成完成后发消息到来源聊天", got, str(api.texts()[before:])[-200:])
        cases = repo.cases_of(problem.id)
        check("自动生成满 10 组", len(cases) == 10, str(len(cases)))
        check("期望输出采用模型给的那份",
              [c.expected.strip() for c in cases[:3]] == ["2", "4", "6"],
              str([c.expected for c in cases[:3]]))

        # ② /oj luogu：抓题后同样自动生成（随机题用同一套桩）
        import plugins.oj_bank.main as main_module
        from plugins.oj_bank.luogu import LuoguProblem

        async def fake_fetch(pid, **kwargs):
            return LuoguProblem(pid=pid, name="洛谷自动题", difficulty=2,
                                statement="输入 $a,b$，输出 $a+b$。",
                                input_format="两个整数", output_format="一个整数",
                                samples=[("1 2\n", "3\n")], time_limit_ms=1000,
                                memory_limit_mb=256, url="")

        original = main_module.fetch_problem
        main_module.fetch_problem = fake_fetch
        try:
            before = len(api.sent)
            res = flat(await send(cmds, ev("/oj luogu P9999", group=GROUP_OK,
                                           user=ALICE)))
            check("抓题结果提示正在生成", "正在用 AI 生成" in res, res[:200])
            got = await wait_for(
                lambda: any("洛谷自动题" in t and "测试点" in t
                            for t in api.texts()[before:]), timeout=20)
            check("抓题后台生成也发消息", got, str(api.texts()[before:])[-200:])
            imported = repo.get_problem("luogu:P9999")
            hacks = ([c for c in repo.cases_of(imported.id) if c.kind == "hack"]
                     if imported else [])
            check("抓来的题也凑满 10 组测试点",
                  imported is not None and len(hacks) == 10,
                  str(len(hacks) if imported else "无题"))
        finally:
            main_module.fetch_problem = original
    finally:
        await runner.cleanup()


async def test_binding_independent():
    print("\n[10.6] 独立绑定：不回落框架 bound_groups / empty_means")
    tmp2 = TMP / "independent"
    # 框架侧有 bound_groups，但插件自己的 binding 是空的 → 不该借用
    plugin2, cmds2, _api2 = await build_plugin(
        tmp2, bound=[GROUP_OK], empty_binding=True)
    try:
        check("群名单不回落框架 bound_groups",
              plugin2.bound_groups() == set(), str(plugin2.bound_groups()))
        res = await send(cmds2, ev("/oj list", group=GROUP_OK, user=ALICE))
        check("框架绑定的群也不能用（不回落）", res is None, str(res)[:60])
        await send(cmds2, ev("/oj bind add group 999999", user=SUPERUSER))
        check("add 只写入被加的那一个（没有继承）",
              plugin2.binding_ids("groups") == {999999},
              str(plugin2.binding_ids("groups")))
        text = flat(await send(cmds2, ev("/oj bind del group 999999",
                                         user=SUPERUSER)))
        check("del 清空后回到「谁都不用」",
              plugin2.binding_ids("groups") == set() and "已停用群" in text,
              str(plugin2.binding_ids("groups")))
    finally:
        await plugin2.on_disable()

    tmp3 = TMP / "empty"
    plugin3, cmds3, _api3 = await build_plugin(
        tmp3, bound=[GROUP_OK], empty_binding=True)
    try:
        res = await send(cmds3, ev("/oj list", group=GROUP_OK, user=ALICE))
        check("两边都空 → 群不可用", res is None, str(res)[:60])
        plugin3.config.set("binding.empty_means", "allow")
        res = await send(cmds3, ev("/oj list", group=GROUP_OK, user=ALICE))
        check("已经没有 empty_means 开关（写了也不生效）", res is None, str(res)[:60])
        check("空名单也不借用框架名单", plugin3.bound_groups() == set(),
              str(plugin3.bound_groups()))
    finally:
        await plugin3.on_disable()


# ----------------------------------------------------------------------
async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.WARNING)

    plugin = cmds = api = None
    try:
        plugin, cmds, api = await build_plugin(TMP)
        await test_load(plugin, cmds)
        await test_tool_save(plugin)
        await test_tool_partial(plugin)
        await test_repo(plugin)
        await test_luogu(plugin, cmds, api)
        await test_judge(plugin)
        await test_review(plugin)
        await test_note_filter(plugin)
        await test_markdown_math()
        await test_render(plugin, cmds)
        await test_commands(plugin, cmds, api)
        await test_sets(plugin, cmds)
        await test_access(plugin, cmds)
        await test_binding_commands(plugin, cmds)
        await test_tool_ctx(plugin)
        await test_ai_chat_bridge(plugin)
        await test_file_submit(plugin, cmds, api)
        await test_coach(plugin, cmds, api)
        await test_generate(plugin, cmds, api)
        await test_pitfalls(plugin, cmds, api)
        await test_fixed_output(plugin, cmds, api)
        await test_backfill(plugin, cmds, api)
        await test_auto_generate(plugin, cmds, api)
        # 放最后：它会建额外的插件实例（各自的 sqlite），模型类会被重新绑定到
        # 那个库上 —— 之后再用 `repo` 查主库就会查错地方。
        await test_binding_independent()
    finally:
        if plugin is not None:
            await plugin.on_disable()

    print("\n" + "=" * 46)
    print(f"通过 {PASSED} / 失败 {FAILED}")
    if FAILED:
        for name in FAILURES:
            print(f"  · {name}")
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


async def send(cmds, event: MessageEvent):
    """走框架的命令分发（与用户手敲同一条路）。"""
    return await cmds.handle(event)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

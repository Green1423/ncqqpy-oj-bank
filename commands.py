"""`/oj ...` 命令（给人用的入口）。

约定（框架，见 `docs/plugin-dev/04-commands.md`）：

* **返回值就是回复**：`str` / `MessageChain` 直接发回来源聊天（群、私聊、小窗
  由框架处理），返回 `None` = 不说话 —— 服务范围外的调用者一律静默（连
  「命令不存在」都不给，避免探测）；
* 参数校验交给 Schema（框架自动回「⚠️ 用法不正确 + 帮助页」），这里**不手写**
  用法提示；
* 权限声明在注册处（`permission=` / `scope=`），这里只判**服务范围白名单**
  （那是插件自己的事，框架不知道）。
"""
from __future__ import annotations

import os
from pathlib import Path

from napcat.message import MessageChain

from .codefile import (CodeFileError, file_segments, from_segment,
                       message_text)
from .judge import LanguageError, STATUS_LABEL, resolve_language
from .coach import CoachError
from .models import Problem, Submission
from .repo import (OJError, add_to_set, cases_of, clear_cases, create_set,
                   delete_problem, delete_set, list_problems, list_sets,
                   rank_set, remove_from_set, require_problem,
                   require_set, set_problems)
from .schemas import (BindSchema, CancelSchema, CaseSchema, CasesSchema,
                      CoachSchema,
                      DelSchema,
                      GenSchema, ListSchema, LuoguSchema, ResultSchema,
                      SetSchema, ShowSchema, SubmitSchema)


def _file_uri(path: str) -> str:
    """本地路径 → `file://` URI（相对路径先转绝对；否则 `as_uri()` 会抛异常）。"""
    return Path(os.path.abspath(path)).as_uri()


def register(plugin) -> None:
    """把本插件的全部命令注册到 `plugin` 上（只做注册，逻辑在下面的函数里）。"""

    def reg(name: str, **kwargs):
        plugin._command_names.append(name)
        return plugin.register_command(name, **kwargs)

    # ------------------------------------------------------------------ 根
    @reg("oj", permission="", scope="all",
         description="本地题库与判题（洛谷抓题 / 出图 / 交题评分 / 题组）")
    async def cmd_oj(packet):
        # 命令组本体**不会被执行**（裸调由框架重定向到 `/help oj`）。
        return None

    # ------------------------------------------------------------------ 抓题
    @reg("oj.luogu", permission="", scope="all", schema=LuoguSchema,
         description="从洛谷抓一道题入库（题干/样例/时限内存）")
    async def cmd_luogu(params, event):
        if not plugin.event_access_ok(event):
            return None
        # 抓取逻辑在 `plugin.import_luogu`：AI 工具 `oj_bank.luogu` 走同一条路，
        # 免得「命令能抓、AI 说不行」这种两套口径。
        return await plugin.import_luogu(params.pid, event=event)

    # ------------------------------------------------------------------ 看题
    @reg("oj.show", permission="", scope="all", schema=ShowSchema,
         description="把题目渲染成图片（题干/格式/样例/注意事项）")
    async def cmd_show(params, event):
        if not plugin.event_access_ok(event):
            return None
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        if plugin.renderer is None:
            return "⚠️ 出图服务没起来（插件未启用？）"
        try:
            path = await plugin.renderer.render_problem(
                problem, cases=len(cases_of(problem.id)))
        except Exception as e:  # noqa: BLE001 - 出图失败要回人话，不是栈
            plugin.logger.warning("[oj_bank] 出图失败", exc_info=True)
            return f"❌ 出图失败：{e}"
        chain = MessageChain()
        hack = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        chain.text(f"#{problem.id} {problem.title}（测试点 {hack} 组）\n").image(
            file=_file_uri(path), name=os.path.basename(path))
        return chain

    # ------------------------------------------------------------------ 列表
    @reg("oj.list", permission="", scope="all", schema=ListSchema,
         description="列出题库里的题目（只显示编号 + 标题，带翻页）")
    async def cmd_list(params, event):
        if not plugin.event_access_ok(event):
            return None
        keyword = params.keyword
        if isinstance(keyword, (list, tuple)):
            keyword = " ".join(str(x) for x in keyword)
        keyword = str(keyword or "").strip()
        size = plugin.list_page_size
        # 先拿总数，才能把页码夹到有效范围（直接跳过尾页只会得到空白）
        _first, total = list_problems(keyword, limit=size, offset=0)
        if not total:
            hint = f"（关键词：{keyword}）" if keyword else ""
            return (f"📭 题库里没有题目{hint}\n"
                    "用 /oj luogu <题号> 抓一道，或让 AI 搜题入库")
        total_pages = max(1, (total + size - 1) // size)
        page = min(max(1, int(params.page or 1)), total_pages)
        rows = (list_problems(keyword, limit=size, offset=0)[0] if page == 1
                else list_problems(keyword, limit=size,
                                   offset=(page - 1) * size)[0])

        # 列表**只给编号 + 标题**：来源/难度/测试点数在 /oj show 里看，
        # 列表一屏能多列几条才是它的用处。
        lines = [f"📚 题库（共 {total} 题 · 第 {page}/{total_pages} 页）"]
        lines.extend(f"#{problem.id} {problem.title}" for problem in rows)

        base = f"/oj list {keyword} " if keyword else "/oj list "
        nav = []
        if page > 1:
            nav.append(f"上一页 {base}--page={page - 1}")
        if page < total_pages:
            nav.append(f"下一页 {base}--page={page + 1}")
        if nav:
            lines.append("📄 " + " · ".join(nav))
        lines.append("看题：/oj show <编号> · 交题：/oj submit <编号> <语言>")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 交题
    @reg("oj.submit", permission="", scope="all", schema=SubmitSchema,
         description="提交代码判题（发代码文件；不带代码时下一条消息/文件就是代码）")
    async def cmd_submit(params, event):
        if not plugin.event_access_ok(event):
            return None
        if not plugin.judge_enabled:
            return "⚠️ 判题功能没开（config.json → plugins.oj_bank.judge.enabled）"
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        if not cases_of(problem.id):
            return (f"❌ #{problem.id} 还没有测试点，没法评分。\n"
                    "用工具 `oj_bank_cases` 给它加 10 组测试点（或 /oj luogu 抓的题"
                    "先补测试点）")
        try:
            language = resolve_language(params.language).key
        except LanguageError as e:
            return f"❌ {e}"
        code = params.code
        if isinstance(code, (list, tuple)):
            code = " ".join(str(x) for x in code)
        code = str(code or "").strip()
        source = ""

        # ① 命令消息**自带代码文件**（私聊直接发文件 + 同一句命令）
        segments = file_segments(getattr(event, "message", None))
        if not code and segments:
            try:
                got = await from_segment(segments[0], plugin.api,
                                         limit=plugin.max_file_bytes,
                                         default_language=language,
                                         roots=plugin.file_roots,
                                         file_manager=plugin.files)
            except CodeFileError as e:
                return f"❌ {e}"
            code, language, source = got.content, got.language or language, got.name

        # ② 引用的消息：里面是文件就按文件，是文本就按文本
        if not code:
            quoted = await plugin.quoted_message(event)
            if quoted is not None:
                quoted_files = file_segments(quoted)
                if quoted_files:
                    try:
                        got = await from_segment(
                            quoted_files[0], plugin.api,
                            limit=plugin.max_file_bytes,
                            default_language=language,
                            roots=plugin.file_roots,
                            file_manager=plugin.files)
                    except CodeFileError as e:
                        return f"❌ {e}"
                    code, language, source = (got.content,
                                              got.language or language, got.name)
                else:
                    text = message_text(quoted).strip()
                    if text:
                        code = text

        # ③ 都没有 → 等下一条消息（文本或文件）
        if not code:
            return plugin.start_pending(event, problem, language)

        left = plugin.cooldown_left(int(event.user_id or 0))
        if left > 0:
            return f"⏳ 先歇 {left:.0f} 秒再交（判题机一次只跑一份）"
        record, result = await plugin.judge_code(event, problem, language, code)
        if record is None or result is None:
            return "⚠️ 判题没有执行（题目没有测试点，或判题被关闭）"
        text = plugin.format_judge_result(problem, record, result, source=source)
        # 自己发而不是 return：这样「分数 → 点评」的顺序是确定的。
        # 返回给框架的话，发送发生在 handler 之后，后台点评可能先到。
        await plugin.reply(event, text)
        plugin.schedule_coach(event, problem, record, result)
        return None

    @reg("oj.cancel", permission="", scope="all", schema=CancelSchema,
         description="取消「下一条消息当代码」的等待")
    async def cmd_cancel(params, event):
        if not plugin.event_access_ok(event):
            return None
        return "✅ 已取消" if plugin.cancel_pending(int(event.user_id or 0)) \
            else "ℹ️ 现在没有等待中的代码"

    # ------------------------------------------------------------------ 结果
    @reg("oj.result", permission="", scope="all", schema=ResultSchema,
         description="查看判题详情（默认自己最近一次）")
    async def cmd_result(params, event):
        if not plugin.event_access_ok(event):
            return None
        from .models import Submission
        if int(params.submission or 0):
            record = Submission.objects.filter(pk=int(params.submission)).first()
            if record is None:
                return f"❌ 没有提交 #{int(params.submission)}"
        else:
            rows = list(Submission.objects.filter(
                user_id=int(event.user_id or 0)).order_by("-id")[:1])
            if not rows:
                return "📭 你还没有提交记录（/oj submit <编号> <语言>）"
            record = rows[0]
        problem = Problem.objects.filter(pk=record.problem_id).first()
        title = f"#{problem.id} {problem.title}" if problem else f"题目 #{record.problem_id}"
        lines = [f"🧾 提交 #{record.id} · {title} · {record.language}",
                 f"状态 {record.status}（{STATUS_LABEL.get(record.status, '?')}）"
                 f" · {record.score} 分"]
        detail = record.detail if isinstance(record.detail, list) else []
        for item in detail:
            if not isinstance(item, dict):
                continue
            bits = [f"{item.get('ordinal', '?')}.",
                    f"{item.get('status', '?')}",
                    f"{item.get('ms', 0)}ms"]
            if item.get("note"):
                bits.append(f"[{item['note']}]")
            lines.append(" ".join(str(b) for b in bits))
            if item.get("got"):
                lines.append(f"   实际：{str(item['got'])[:100]}")
            if item.get("detail"):
                lines.append(f"   {str(item['detail'])[:100]}")
        return "\n".join(lines)

    @reg("oj.coach", permission="", scope="all", schema=CoachSchema,
         description="对某次提交重新生成 AI 代码点评（评价怎么触发：交题后自动，也可手动）")
    async def cmd_coach(params, event):
        if not plugin.event_access_ok(event):
            return None
        record = Submission.objects.filter(pk=int(params.submission)).first()
        if record is None:
            return f"❌ 没有提交 #{params.submission}"
        uid = int(event.user_id or 0)
        if int(record.user_id) != uid and not plugin.perm.is_superuser(uid):
            return "❌ 只能点评自己的提交"
        problem = Problem.objects.filter(pk=record.problem_id).first()
        if problem is None:
            return "❌ 这道题已经被删了"
        if plugin._coach is None or not plugin._coach.opt.ready:
            return ("⚠️ 没配置点评模型（config: coach.base_url/model/api_key，"
                    "留空会借用 plugins.ai_chat.llm）")
        await plugin.reply(event, f"⏳ 正在点评提交 #{record.id}…")
        try:
            text = await plugin.coach_submission(record, problem)
        except CoachError as e:
            return f"❌ 点评失败：{e}"
        return f"🧑‍🏫 代码点评（提交 #{record.id}）\n{text}"

    # ------------------------------------------------------------------ 测试点
    @reg("oj.cases", permission="", scope="all", schema=CasesSchema,
         description="查看 / 清空题目的测试点")
    async def cmd_cases(params, event):
        if not plugin.event_access_ok(event):
            return None
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        action = str(params.action or "list").lower()
        if action == "clear":
            if not plugin.perm.is_superuser(int(event.user_id or 0)):
                return "⛔️ 清空测试点仅超级用户可用"
            removed = clear_cases(problem)
            return f"🧹 已清空 #{problem.id} 的 {removed} 组测试点"
        rows = cases_of(problem.id)
        if not rows:
            return f"📭 #{problem.id} 还没有测试点（交不了题）"
        lines = [f"🧪 #{problem.id} {problem.title} · {len(rows)} 组测试点"]
        for case in rows:
            lines.append(
                f"{case.ordinal}. [{case.kind}] {case.note or '（无说明）'}"
                f" · 权重 {case.weight} · 输入 {len(case.stdin)} 字"
                f" · 期望输出 {len(case.expected)} 字")
        return "\n".join(lines)

    @reg("oj.case", permission="", scope="all", schema=CaseSchema,
         description="重做某一组测试点（就地替换，其它组不动）")
    async def cmd_case(params, event):
        if not plugin.event_access_ok(event):
            return None
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        await plugin.reply(event, f"⏳ 正在重做第 {params.ordinal} 组…")
        return await plugin.regenerate_case(problem, int(params.ordinal))

    # ------------------------------------------------------------------ AI 出测试点
    @reg("oj.gen", permission="", scope="all", schema=GenSchema,
         description="让 AI 逐组出一套测试点（一次一组，含输入+输出；后台跑）")
    async def cmd_gen(params, event):
        if not plugin.event_access_ok(event):
            return None
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        # `/oj gen` 只**补缺口**：已有几组就只补到 10 组，不覆盖已有的。
        # （要整题重来：/oj cases <编号> clear 后再 /oj gen；只改一组：/oj case）
        existing = sum(1 for c in cases_of(problem.id) if c.kind == "hack")
        need = plugin.max_cases - existing
        if need <= 0:
            return (f"✅ #{problem.id} 已有 {existing} 组测试点，不用再补。\n"
                    "要重做某一组：/oj case <编号> <组号>；"
                    "要整题重来：/oj cases <编号> clear 后再 /oj gen")
        # 数量可选：只作为**本次最多补几组**，不会超过缺口
        want = max(1, min(need, int(params.count or need)))
        # 生成是**后台任务**：逐组跑，可能要几分钟。立刻回执、结果另发。
        if not plugin.schedule_generation(problem, event=event, force=True,
                                          count=want, progress=True,
                                          replace=False, base=existing):
            return ("⚠️ 没配置用于出题的模型（config：gen.base_url/model/api_key，"
                    "留空会借用 plugins.ai_chat.llm）")
        return (f"⏳ 已有 {existing}/{plugin.max_cases} 组，开始补齐 {need} 组"
                "（只补缺的，不覆盖已有），完成后发到这里。")

    # ------------------------------------------------------------------ 删题
    @reg("oj.del", permission="superuser", scope="all", schema=DelSchema,
         description="删除题目（仅超管）")
    async def cmd_del(params, event):
        try:
            problem = require_problem(params.ident)
        except OJError as e:
            return f"❌ {e}"
        removed = delete_problem(problem)
        return f"🗑 已删除 #{problem.id} {problem.title}（连同 {removed} 组测试点）"

    # ------------------------------------------------------------------ 服务范围
    @reg("oj.bind", permission="superuser", scope="private", schema=BindSchema,
         description="服务范围：bind list / add / del（仅私聊 + 超管，不受范围限制）")
    async def cmd_bind(params, event):
        # ⚠️ 不调用 `event_access_ok`：管理服务范围的命令必须**永远可用**，
        # 否则名单为空时私聊也被挡住，永远开不出第一条 —— 死锁。
        # 权限（超管）与作用域（仅私聊）由框架在路由前校验。
        return plugin.manage_binding(
            action=str(params.action or "").lower(),
            kind=str(params.kind or "group").lower(),
            target=int(params.target or 0),
            user_id=int(event.user_id or 0))

    # ------------------------------------------------------------------ 题组
    @reg("oj.set", permission="", scope="all", schema=SetSchema,
         description="题组（练习题单）：new / add / remove / show / list / del / rank")
    async def cmd_set(params, event):
        if not plugin.event_access_ok(event):
            return None
        action = str(params.action or "").lower()
        name = str(params.name or "").strip()
        extra = params.extra if isinstance(params.extra, (list, tuple)) else []
        extra = [str(x) for x in extra]

        if action == "list":
            rows = list_sets()
            if not rows:
                return "📭 还没有题组：/oj set new <组名> [标题]"
            lines = [f"🗂 题组（{len(rows)} 个）"]
            for group, count in rows:
                lines.append(f"{group.name} · {group.title or ''} · {count} 题")
            lines.append("看题组：/oj set show <组名> · 榜单：/oj set rank <组名>")
            return "\n".join(lines)

        if not name:
            return "❌ 要说清是哪个题组（/oj set list 看全部）"

        if action == "new":
            try:
                group, created = create_set(
                    name, title=" ".join(extra), owner=str(event.user_id or ""))
            except OJError as e:
                return f"❌ {e}"
            return (f"✅ 已新建题组「{group.name}」" if created
                    else f"ℹ️ 题组「{group.name}」已存在，标题已更新")

        if action in ("add", "remove"):
            try:
                require_set(name)
            except OJError as e:
                return f"❌ {e}"
            problems, bad = _resolve_many(extra)
            if not problems:
                return "❌ 没给出有效的题号（/oj list 看编号）"
            if action == "add":
                added, skipped = add_to_set(name, problems)
                text = f"✅ 已加入 {added} 题"
                if skipped:
                    text += f"（{skipped} 题本来就在组里）"
            else:
                removed = remove_from_set(name, problems)
                text = f"✅ 已移出 {removed} 题"
            if bad:
                text += f"\n⚠️ 跳过了认不出的题号：{'、'.join(bad[:5])}"
            return text

        if action == "show":
            try:
                group = require_set(name)
            except OJError as e:
                return f"❌ {e}"
            rows = [(item.ordinal, problem) for item, problem in set_problems(name)]
            if not rows:
                return f"📭 题组「{name}」还是空的：/oj set add {name} <编号...>"
            if plugin.renderer is None:
                return "⚠️ 出图服务没起来"
            try:
                path = await plugin.renderer.render_set(group, rows)
            except Exception as e:  # noqa: BLE001
                plugin.logger.warning("[oj_bank] 题组出图失败", exc_info=True)
                return f"❌ 出图失败：{e}"
            chain = MessageChain().text(
                f"题组「{group.title or group.name}」· {len(rows)} 题\n").image(
                file=_file_uri(path), name=os.path.basename(path))
            return chain

        if action == "rank":
            try:
                rows = rank_set(name)
            except OJError as e:
                return f"❌ {e}"
            group = require_set(name)
            if not rows:
                return f"📭 题组「{group.name}」还没有提交记录"
            lines = [f"🏆 榜单 · {group.title or group.name}"]
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for index, row in enumerate(rows, start=1):
                mark = medals.get(index, f"{index}.")
                lines.append(f"{mark} QQ {row['user_id']} · {row['score']}/"
                             f"{row['total']} 分 · AC {row['solved']} 题")
            return "\n".join(lines)

        if action == "del":
            if not plugin.perm.is_superuser(int(event.user_id or 0)):
                return "⛔️ 删题组仅超级用户可用"
            try:
                removed = delete_set(name)
            except OJError as e:
                return f"❌ {e}"
            return f"🗑 已删除题组「{name}」（{removed} 个成员）"

        return None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _resolve_many(tokens: list[str]) -> tuple[list[Problem], list[str]]:
    """把一串题号解析成题目；认不出的收集起来，由调用方提示。"""
    problems: list[Problem] = []
    bad: list[str] = []
    seen: set[int] = set()
    for raw in tokens:
        try:
            problem = require_problem(raw)
        except OJError:
            bad.append(str(raw))
            continue
        if problem.id in seen:
            continue
        seen.add(problem.id)
        problems.append(problem)
    return problems, bad


__all__ = ["register"]

"""oj_bank 的命令参数 Schema。

用法行**不手写**：框架按 Schema 生成 `/help` 里的用法与参数表（见
`docs/plugin-dev/04-commands.md §4.3.1`），手写会与解析口径漂移。

⚠️ 为什么题组不用三级命令（`/oj set new` 而不是 `oj.set.new`）：
框架的命令解析只认「命令 + 一个子命令」（`/oj set` 的 `set` 是子命令，
再往后的 `new` 是**位置参数**），注册 `oj.set.new` 会变成一条**永远点不到**的命令。
所以题组做成一个带 `action` 字段的命令：`/oj set <动作> <组名> [参数...]`。
"""
from __future__ import annotations

from napcat.command import (CommandSchema, IntField, StrField, VariadicField)

#: `/oj set` 的动作
SET_ACTIONS = ("new", "add", "remove", "show", "list", "del", "rank")
#: `/oj cases` 的动作
CASE_ACTIONS = ("list", "clear")


class LuoguSchema(CommandSchema):
    """`/oj luogu <题号>`"""

    pid = StrField(required=True, max_length=64,
                   description="洛谷题号（P1001 / CF1A），也可以给题目链接")


class ShowSchema(CommandSchema):
    """`/oj show <编号>`"""

    ident = StrField(required=True, max_length=64,
                     description="题目：题库编号（如 12）或洛谷题号（如 P1001）")


class ListSchema(CommandSchema):
    """`/oj list [关键词] [--page=1]`"""

    # 关键词用可变参数：中文标题天然带空格（"A+B Problem"）
    keyword = VariadicField(description="关键词（标题 / 题号），留空 = 全部")
    page = IntField(default=1, min=1, max=999, description="页码")


class SubmitSchema(CommandSchema):
    """`/oj submit <编号> [语言] [代码 ...]`"""

    ident = StrField(required=True, max_length=64,
                     description="题目：题库编号或洛谷题号")
    language = StrField(default="cpp", max_length=12,
                        description="语言：cpp / c / py（别名 c++ / python）")
    #: 代码可以留空（先发 `/oj submit 12 cpp`，再把代码文件发上来），也可以在
    #: 命令里用三引号贴多行原文（`"""…"""` 不解析转义，长代码/宏不会被改坏）。
    #: 文件仍更适合大代码：命令行与 QQ 消息长度都有限。
    code = VariadicField(
        description="代码文本（可留空发文件；多行用 \"\"\"…\"\"\" 包起来）")


class CancelSchema(CommandSchema):
    """`/oj cancel`（不需要参数）"""


class ResultSchema(CommandSchema):
    """`/oj result [提交号]`"""

    submission = IntField(default=0, min=0,
                          description="提交号；留空 = 看自己最近一次")


class CasesSchema(CommandSchema):
    """`/oj cases <编号> [list|clear]`"""

    ident = StrField(required=True, max_length=64,
                     description="题目：题库编号或洛谷题号")
    action = StrField(default="list", max_length=8, choices=list(CASE_ACTIONS),
                      description="list = 看测试点；clear = 清空（仅超管）")


class CaseSchema(CommandSchema):
    """`/oj case <编号> <组号>`（重做单组）"""

    ident = StrField(required=True, max_length=64,
                     description="题目：题库编号或洛谷题号")
    ordinal = IntField(required=True, min=1, max=1000,
                       description="第几组（/oj cases 里的序号）")


class CoachSchema(CommandSchema):
    """`/oj coach <提交编号>`"""

    submission = IntField(required=True, min=1,
                          description="提交编号（判题结果里的 #编号）")


class DelSchema(CommandSchema):
    """`/oj del <编号>`（仅超管）"""

    ident = StrField(required=True, max_length=64, description="要删的题目编号")


class SetSchema(CommandSchema):
    """`/oj set <动作> [组名] [参数 ...]`

    动作用 `choices` 约束：写错动作时框架自动回「无效值，可选: …」+ 该命令的帮助页，
    不用我们自己拼提示。
    """

    action = StrField(required=True, max_length=12, choices=list(SET_ACTIONS),
                      description="动作：new / add / remove / show / list / del / rank")
    name = StrField(default="", max_length=32,
                    description="题组名（list 可省略）")
    extra = VariadicField(
        description="add/remove = 题号列表；new = 题组标题；show/rank/del 不用填")


class GenSchema(CommandSchema):
    """`/oj gen <编号> [数量]`"""

    ident = StrField(required=True, max_length=64,
                     description="题目：题库编号或洛谷题号")
    count = IntField(default=10, min=1, max=10,
                     description="出几组测试点（1..10，默认 10）")


class BindSchema(CommandSchema):
    """`/oj bind <list|add|del> [group|user] [号]`"""

    action = StrField(required=True, max_length=8,
                      choices=["list", "add", "del"],
                      description="list = 看服务范围；add / del = 开通 / 停用")
    kind = StrField(default="group", max_length=8, choices=["group", "user"],
                    description="对象类型：group 群 / user 私聊")
    target = IntField(default=0, min=0,
                      description="群号或 QQ 号；`add user` 可省略（= 自己）")


__all__ = ["CASE_ACTIONS", "SET_ACTIONS", "BindSchema", "CancelSchema",
           "CaseSchema", "CasesSchema", "CoachSchema", "DelSchema", "GenSchema", "ListSchema",
           "LuoguSchema", "ResultSchema", "SetSchema", "ShowSchema",
           "SubmitSchema"]

"""oj_bank 的数据模型（`napcat.store` / sqlite）。

五张表，边界刻意画得很窄：

* `Problem`   题目本体（题干 / 输入格式 / 输出格式 / 样例 / 注意事项）——
              这些字段就是「出图」时展示的全部内容，**不再另存一份渲染用的副本**：
              图是每次现渲染的，改了题面下次出图自然就新了；
* `TestCase`  **测试点**（评分用，既有常规数据也有边界/特殊结构）。`kind` 区分 `hack`（最多 10 组，题目
              作者/AI 专门构造的边界数据）与 `sample`（官方样例，也参与评分）；
* `ProblemSet` / `SetItem`  题组（练习题单）：题组是一组题号的**有序**集合；
* `Submission` 判题记录。存分数与每题结果，榜单只依赖它 —— 题组本身不存分数，
  因为同一道题在不同题组里应该按**同一份提交记录**算（重复提交取最高分）。

关于「为什么不把测试点塞进 Problem 的 JSONField」：一个测试点可能几十 KB
（大输入的边界数据），10 组就是几百 KB。每次列表/出图都把这坨 JSON 读出来
毫无必要，拆表后按 `problem_id` 惰性取即可。
"""
from __future__ import annotations

from datetime import datetime

from napcat.store import (CharField, DateTimeField, IntegerField, JSONField,
                          Model, TextField)

#: 测试点种类：hack = 专门构造的边界数据；sample = 官方样例
CASE_KINDS = ("hack", "sample")
#: 测试点的上限（题面要求 10 组）
MAX_HACK_CASES = 10


def now() -> datetime:
    """当前时间（带时区）—— 用可调用默认值，避免所有行共用同一个建表时间。"""
    return datetime.now().astimezone()


class Problem(Model):
    """一道题。

    `key` 是**去重键**（不是主键）：洛谷题是 `luogu:P1001`，AI 搜来的是
    `ai:<标题哈希>`。主键仍是自增 `id`（用户在 QQ 里说的编号就是它）。
    """

    class Meta:
        table = "oj_problems"

    key = CharField(max_length=96, unique=True, index=True)
    title = CharField(max_length=200)
    #: 来源：luogu / ai / local
    source = CharField(max_length=16, default="local", index=True)
    #: 来源侧的原 ID（洛谷题号等）
    source_id = CharField(max_length=64, default="")
    url = CharField(max_length=300, default="")
    #: 洛谷难度 1..7（1 入门 … 7 NOI/CTSC），0 = 未知
    difficulty = IntegerField(default=0)
    tags = CharField(max_length=200, default="")

    # ---- 题面（出图展示的五段） ----
    statement = TextField(default="")        # 题干（Markdown，可含 $LaTeX$）
    input_format = TextField(default="")     # 输入格式
    output_format = TextField(default="")    # 输出格式
    sample_input = TextField(default="")     # 输入样例
    sample_output = TextField(default="")    # 输出样例
    note = TextField(default="")             # 注意事项（数据范围 / 提示）

    time_limit_ms = IntegerField(default=1000)
    memory_limit_mb = IntegerField(default=256)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)


class TestCase(Model):
    """一个测试点（评分时喂给选手程序的一组输入/期望输出）。"""

    class Meta:
        table = "oj_testcases"

    problem_id = IntegerField(index=True)
    #: 题内序号，从 1 开始（给人看的「第 3 组」）
    ordinal = IntegerField(default=1)
    kind = CharField(max_length=8, default="hack")
    stdin = TextField(default="")
    expected = TextField(default="")
    #: 这一组想用途（`n=10^9`、`负数`、`空输入`…）—— 出题人/AI 写给自己的，
    #: 判题失败时也会显示给提交者，告诉他「你被哪一组卡了」
    note = CharField(max_length=200, default="")
    weight = IntegerField(default=1)


class ProblemSet(Model):
    """题组（练习题单）。"""

    class Meta:
        table = "oj_problem_sets"

    name = CharField(max_length=64, unique=True)
    title = CharField(max_length=120, default="")
    description = TextField(default="")
    #: 创建者 QQ（字符串，0 = 系统/迁移来的）
    owner = CharField(max_length=32, default="")
    created_at = DateTimeField(default=now)


class SetItem(Model):
    """题组成员（有序）。`ordinal` 是给人看的顺序，不是主键。"""

    class Meta:
        table = "oj_problem_set_items"

    set_name = CharField(max_length=64, index=True)
    problem_id = IntegerField(index=True)
    ordinal = IntegerField(default=1)


class Submission(Model):
    """一次判题记录。`detail` 是每个测试点的结果（JSON 数组）。"""

    class Meta:
        table = "oj_submissions"

    problem_id = IntegerField(index=True)
    user_id = IntegerField(index=True)
    group_id = IntegerField(default=0)
    language = CharField(max_length=16, default="cpp")
    code = TextField(default="")
    #: 整次判题的结论：AC / WA / TLE / RE / MLE / OLE / CE / BLOCKED / ERROR
    status = CharField(max_length=16, default="")
    score = IntegerField(default=0)
    detail = JSONField(default=list)
    created_at = DateTimeField(default=now, index=True)

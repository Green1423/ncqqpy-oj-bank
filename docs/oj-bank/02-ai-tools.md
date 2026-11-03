# 二、给 AI 的工具

> 上级：[oj_bank](README.md) · 上一章：[命令](01-commands.md) · 下一章：[判题](03-judge.md)

插件的 AI 入口是**工具**（`register_tool`），不是技能（`register_skill`）。
两者在框架里的语义完全不同：

| | 技能 | 工具 |
|---|---|---|
| 本质 | 命令映射（`byrdocs.search`） | 自己写的 handler |
| 参数 | 从 `CommandSchema` 取，**走命令行** | JSON Schema，直接给 `dict` |
| 结果 | 命令自己发给用户 | **返回给模型**，模型组织答复 |

题库这边必须用工具：题干是 Markdown（含 `\frac`、`\nabla`、代码块），测试点是
10 组结构化数据。走命令行会被引号/转义规则改写（`\n` 变真换行、`\t` 变制表符），
题面里的 LaTeX 会被改坏 —— 这是选工具通道的决定性理由。

## 2.1 工具清单

| 工具名（模型看到的） | 入参 | 作用 |
|---|---|---|
| `oj_bank_save_problem` | **必填** `title` `statement`；建议 `input_format` `output_format` `sample_input` `sample_output` `note` `source` `url` `difficulty` `tags` | 任何来源的题都能入库；**入库后自动生成 10 组测试点**（测试点不用自己写） |
| `oj_bank_generate_cases` | `problem_id` `count` `replace` | **让 AI 逐组出测试点**（一组含输入+输出，推荐） |
| `oj_bank_cases` | `problem_id` `cases[]` `replace` | 手写 / 覆盖测试点（每题最多 10 组，可分批） |
| `oj_bank_luogu` | `pid` `overwrite` | **只在用户明确给了洛谷题号时**用；其它来源走 `save_problem` |
| `oj_bank_get_problem` | `ident` | 取题目全文（模型回答「题库里那道题」时先取，别凭记忆） |
| `oj_bank_list` | `keyword` `limit` | 列题库 |

**存题的硬要求只有两条**：题名 + 题干。其余字段缺了照样入库 —— 工具结果会逐条点名
「还缺：输入样例、注意事项」。`note` **只写数据范围** —— 提示/题解/广告/样例代码
入库时会被自动丢掉（`repo.data_range_only`），老数据在插件加载时回填清理。
**测试点不用模型手写**：入库后插件自动让 AI 生成 10 组
（后台跑，完成后把结果发回来源聊天）；模型只要转述「已入库，正在生成测试点」即可。

每个测试点：`input` / `output` / `note`（这组用途）/ `weight`（默认 1）。
**`output` 必须准确**：评分是逐字节比对（忽略行尾空格与末尾空行）。

### 典型流程

```text
用户：帮我找一道二分答案的题，存进题库
模型：（搜索……）→ oj_bank_save_problem(title=砍树, statement=…, source=Codeforces 1234E)
工具：已入库：#7 砍树；还缺：输入样例、注意事项；测试点还差 10 组…
模型：→ oj_bank_save_problem（补齐样例/注意事项，或先存着）
     → oj_bank_cases(problem_id=7, cases=[…])
工具：✅ #7 砍树：写入 10 组，现在测试点 10/10 组
模型：已经存好了，是 #7《砍树》，发 /oj show 7 可以看题、/oj submit 7 cpp 交题。
```

没有测试点时**优先让插件出**，而不是让模型手写输出：

```text
用户：这道题帮我出点测试数据
模型：→ oj_bank_generate_cases(problem_id=7, count=10)
工具：⏳ 已开始让 AI 逐组生成 10 组测试点，完成后会发到本聊天。
模型：#7 现在有 9 组测试点了，可以发 /oj submit 7 cpp 开始交题。
```

## 2.2 为什么是「结果回模型」

保存题目是**查询式**操作：模型需要知道「拿到编号几点、还差几组测试点」，才好
继续告诉用户或补数据。工具结果进模型上下文后，模型能自己决定：

```text
用户：帮我找一道二分答案的题存进题库
模型：（搜索……）→ 调 oj_bank_save_problem
工具：已入库：#7 砍树（测试点 10/10 组）；出图：/oj show 7
模型：已经存进题库了，是 #7《砍树》，10 组测试点都齐了，发 /oj show 7 可以看题。
```

## 2.3 白名单在工具里也要判

`permission` / `scope` 只能表达「超管 / 群聊」这种粗粒度。业务白名单（这个群
有没有开通题库）只有插件自己知道，所以工具 handler 声明了第二个参数：

```python
async def save_problem(args: dict, ctx=None):
    if not plugin.tool_access_ok(ctx):      # ctx 由 ai_chat 注入
        return "⛔ 当前聊天没有开通题库服务"
```

`ctx` 是鸭子类型（`user_id` / `group_id` / `in_group` / `is_superuser`），
框架不 import 任何插件，别的消费方给等价对象即可。

未开通时返回的是**能照做的一句话**（带当前群号/QQ 与命令），模型可以直接转述：

```text
⛔ 本群（123456789）没有开通题库服务：让管理员在私聊发
   /oj bind add group 123456789 开通后重试。
```

## 2.4 让 AI 主动用它

只靠工具 schema 的 `description` 不够：`ai_chat` 的系统提示默认写着「插件工具只用来
取数据」，模型很容易把「存题库」当成「口头答应一句」而不调用。所以每个工具都带
一句 `prompt_hint`，由 `ai_chat` 拼进系统提示：

```text
插件工具的使用时机：
- 用户让你搜题、找题、出题，或说「存进题库」「记下来」时：找到题目后先调
  oj_bank_save_problem 存进题库，再回答（来源不限……）。
- 题库里的题缺测试点时用 oj_bank_generate_cases 让 AI 生成（推荐），
  或 oj_bank_cases 手写（最多 10 组，可分批）。
- 用户明确给出洛谷题号（如 P1001）时用 oj_bank_luogu 抓题；其它来源用 save_problem。
```

配合「工具结果点名缺什么」，模型的两步都会走：先存题，再按提示补字段/测试点。

## 2.5 与命令的关系

同一个业务动作在两条链上各有一个入口，共用底层实现：

| 动作 | 命令 | 工具 |
|------|------|------|
| 抓洛谷题 | `/oj luogu` | `oj_bank_luogu` → 同一个 `plugin.import_luogu()` |
| 补测试点 | （无） | `oj_bank_cases` |
| 题库查询 | `/oj list` | `oj_bank_list` / `oj_bank_get_problem` |

抓题共用 `plugin.import_luogu()` 是刻意的：两条入口各写一遍，迟早出现
「命令能抓、AI 说不行」的漂移。

# oj_bank 插件：本地题库与判题

> 上级：[插件文档](README.md) · 插件目录：[plugins/oj_bank](../../README.md)
> 配置段：`config.json` → `plugins.oj_bank`
> 自检：`venv-arm/bin/python -B tests/test_oj_bank.py`

把题目存进本地题库，渲染成图片，用 **10 组测试点**判题评分，并把题目编成
**题组**出榜单。题目有两个来源：洛谷题号（`/oj luogu P1001`）和 AI 搜来的题
（工具 `oj_bank_save_problem`）。

> ⚠️ 判题会**执行别人提交的代码**。开判题前先读
> [判题与安全边界](03-judge.md#34-安全边界)：白名单 + 代码审查 + 资源限制三道闸。

## 效果

（插图：题目卡片，不随仓库发布）

## 章节

| 章节 | 小节 |
|------|------|
| [命令一览](01-commands.md) | 搜题 / 看图 / 交题 / 题组 / 榜单 |
| [给 AI 的工具](02-ai-tools.md) | 工具清单、为什么不用技能、白名单怎么判 |
| [判题与安全边界](03-judge.md) | 状态、评分口径、审查、资源限制、两步交题 |
| [洛谷抓题](04-luogu.md) | 抓什么、为什么必需 cookie、样例与测试点的差别 |
| [配置](05-config.md) | 全部字段与默认值 |
| [出图](06-render.md) | 渲染链路、KaTeX、失败时的表现 |
| [代码结构与测试](07-structure.md) | 模块职责、测试覆盖、排查 |
| [测试点从哪来（AI 生成）](08-testcases.md) | 逐组生成、去重与超时、失败排查 |

## 快速开始

```text
/oj luogu P1001                 # 抓一道洛谷题入库（得到 #1）
/oj show 1                      # 看题（图片）
/oj submit 1 cpp                # 准备交题（下一条发代码文件）
                                # 判完分后 AI 会自动补一条「代码点评」
/oj set new 周末赛 周末练手      # 建题组
/oj set add 周末赛 1            # 把题放进题组
/oj set rank 周末赛             # 看榜单
```

服务范围是**插件自己的一份名单**（不跟框架的 `bound_groups` 走）：名单为空时
空 = 谁都不用。用私聊里的 `/oj bind add ...` 开通（仅超管），
或直接改配置：

```json
"plugins": {
  "oj_bank": {
    "binding": {"groups": [], "users": []}
  }
}
```

## 数据

| 东西 | 位置 |
|------|------|
| 题库（sqlite） | `plugins/oj_bank/data/oj_bank.db`（`data_dir` 可改） |
| 判题临时目录 | `<data_dir>/judge/`（每次判完即删） |
| 出图 | `<data_dir>/renders/`（只留最近 `render.keep_files` 张） |

`data/` 不进发布包；备份题库只需拷 `oj_bank.db`。

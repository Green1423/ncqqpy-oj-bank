# 七、代码结构与测试

> 上级：[oj_bank](README.md) · 上一章：[出图](06-render.md) · 下一章：[测试点从哪来（AI 生成）](08-testcases.md)

## 7.1 模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| `main.py` | 插件类：配置模板、生命周期、白名单、判题编排、两步交题 | 全部 |
| `commands.py` | `/oj ...` 命令（返回值即回复） | repo / judge / render |
| `tools.py` | AI 工具：JSON Schema + handler | repo / main |
| `codefile.py` | 取代码文件：文件段 / 群文件上传 / 引用；解码与语言推断 | aiohttp |
| `models.py` | sqlite 模型：题目 / 测试点 / 题组 / 提交 | `napcat.store` |
| `repo.py` | 数据访问：编号解析、去重、上限、榜单 | models |
| `luogu.py` | 洛谷抓取与解析（纯网络 + 解析，不碰库） | aiohttp |
| `judge.py` | 编译 / 运行 / 比对 / 评分 | 标准库 subprocess + resource |
| `review.py` | 代码审查：静态规则 + 模型审查 | aiohttp |
| `coach.py` | 判完分后的 AI 点评（后台任务，失败静默） | aiohttp |
| `gen.py` | AI 出测试点：一次一组（输入 + 输出），重复到凑满 | aiohttp |
| `markdown.py` | Markdown 子集 → HTML（全转义） | 标准库 |
| `render.py` | HTML → PNG（两趟 Chromium） | markdown |

分层原则：**命令与工具都只调 `repo` / 服务，不直接摸 sqlite**；`luogu` / `judge` /
`review` / `markdown` / `render` 不 import 插件类，方便单测直接调。

## 7.2 数据模型

```text
Problem（题目）            key 唯一：luogu:P1001 / ai:<标题哈希>
  └ TestCase（测试点）     problem_id + ordinal；kind = hack / sample；weight
ProblemSet（题组）         name 唯一
  └ SetItem（成员）        set_name + problem_id + ordinal（有序）
Submission（提交）         problem_id + user_id + status + score + detail(JSON)
```

- 题目按 `key` 去重：洛谷题号或标题哈希。同标题再保存是**更新**；
- 测试点上限 `max_cases`（默认 10），超过时**整批不写**（不做「写一半」）；
- 题组不存分数：榜单从 `Submission` 现算（每题取历史最高分），
  所以同一道题在多个题组里口径一致。

## 7.3 测试

```bash
cd /ncqqpy
./venv-arm/bin/python -B tests/test_oj_bank.py
```

覆盖：

| 面 | 检查点 |
|----|--------|
| 装配 | 配置模板补齐、命令 / 工具注册、权限声明 |
| 工具 | save / cases / get / list、最小字段入库、缺字段点名、任何来源、白名单拒绝、`prompt_hint` 进系统提示 |
| 仓储 | 去重、题组增删排序、榜单口径 |
| 洛谷 | 题号规范化、lentille-context 解析、KB→MB、抓题命令（网络用桩） |
| 判题 | Python / C++ 真编译真运行、AC / WA / CE / RE / TLE / OLE |
| 审查 | 静态规则命中、模型审查（本地假服务）、服务 500 时拒绝 |
| 出图 | 真起 Chromium、图片宽度、页面无外链 |
| 命令 | 两步交题、取消、result、list、cases、del 权限 |
| 文件交题 | 后缀判语言、BOM/GB18030、二进制拒收、引用文件、群文件上传 |
| 题目提示 | 只保留数据范围（提示/题解/广告/样例代码过滤，老数据回填） |
| AI 点评 | 先分数后点评、模型 500 不影响判题、关掉后只有判题消息 |
| AI 测试点 | 一次一组、重复输入去重、缺 output 报错、逐组凑满 10 组 |
| 绑定 | 群 / 私聊 / 小窗、独立名单（不回落框架）、空 = 谁都不用、`/oj bind`、工具 ctx |

判题与出图**不打桩**：它们是这个插件的核心风险面，用桩会掩盖真实行为。

## 7.4 加一门语言

`judge.py` 里 `LANGUAGES` 加一项（源文件名、编译命令、运行命令），
`LANGUAGE_ALIASES` 加别名，再在 `_compile` / `_run_cmd` 里各加一个分支。
测试里加一个最小 AC 程序即可（参考现有 Python / C++ 用例）。

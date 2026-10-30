# 五、配置

> 上级：[oj_bank](README.md) · 上一章：[洛谷抓题](04-luogu.md) · 下一章：[出图](06-render.md)

全部配置在 `config.json` → `plugins.oj_bank`。插件首次加载时**自动补齐**全部键
（只补缺、不覆盖已有值、嵌套段逐键补），所以升级后新增的键会自己出现。

## 5.1 全文与默认值

```json
{
  "data_dir": "",
  "binding": {"groups": [], "users": []},
  "max_cases": 10,
  "list_page_size": 8,
  "judge": {
    "enabled": true,
    "max_concurrent": 1,
    "cooldown_seconds": 20,
    "submit_window_seconds": 600,
    "compile_timeout_ms": 20000,
    "wall_margin_ms": 500,
    "default_time_limit_ms": 1000,
    "default_memory_limit_mb": 256,
    "output_limit_kb": 1024,
    "max_code_chars": 64000,
    "max_file_kb": 64,
    "cpp_compiler": "g++",
    "c_compiler": "gcc"
  },
  "review": {
    "rules": true,
    "ai": {
      "enabled": false,
      "base_url": "https://myai.bupt.edu.cn/llm-gw/v1",
      "model": "deepseek-v4-flash",
      "api_key": "",
      "timeout": 30,
      "max_tokens": 1024,
      "reasoning_effort": "none",
      "on_error": "deny",
      "cache_size": 256
    }
  },
  "gen": {
    "enabled": true,
    "auto_insert": true,
    "pitfalls": true,
    "allow_scripts": true,
    "script_timeout": 15,
    "retry_missing": 3,
    "backfill_on_start": true,
    "backfill_limit": 5,
    "count": 10,
    "max_seconds": 3600,
    "timeout": 600,
    "max_tokens": 65536,
    "max_statement_chars": 6000,
    "max_input_chars": 20000,
    "base_url": "",
    "model": "",
    "api_key": "",
    "reasoning_effort": ""
  },
  "coach": {
    "enabled": true,
    "base_url": "",
    "model": "",
    "api_key": "",
    "timeout": 300,
    "max_tokens": 32768,
    "max_code_chars": 12000,
    "max_chars": 700,
    "reasoning_effort": ""
  },
  "render": {
    "width": 760,
    "timeout": 45,
    "max_height": 8000,
    "keep_files": 120,
    "chromium": "",
    "virtual_time_budget": 4000
  },
  "luogu": {
    "timeout": 20,
    "user_agent": "",
    "cookie": "",
    "overwrite": false
  }
}
```

## 5.2 字段说明

### 顶层

| 键 | 说明 |
|----|------|
| `data_dir` | 数据目录；空 = `plugins/oj_bank/data`。sqlite、判题临时目录、出图都在它下面 |
| `binding.groups` | 允许使用的**群号**列表（独立名单，不看框架的 `bound_groups`） |
| `binding.users` | 允许**直接私聊**使用的 QQ 列表（小窗按来源群判） |
| — | —— |
| `max_cases` | 每题最多几组测试点（默认 10，与题面要求一致） |
| `list_page_size` | `/oj list` 每页几条（1..20） |

> 绑定命令的语义跟 `ai_chat` / `byrdocs` 一致（见 `napcat.scope`），**群名单不回落框架的
> `bound_groups`** —— 那是「机器人监听哪些群」，与「允许哪些群用本插件」是两件事。
> **超级用户豁免**（名单配错时仍能进来改；用 `/oj bind` 开通，或改这里）。运行期用
> `/oj bind list|add|del` 管理，会写回本段。

### `judge`

| 键 | 说明 |
|----|------|
| `enabled` | 判题总开关；关掉后题库 / 出图 / 题组照常，`/oj submit` 回「没开」 |
| `max_concurrent` | 同时判几份；QQ 机器人场景 1 就够 |
| `cooldown_seconds` | 同一用户两次交题的间隔（0 = 不限） |
| `submit_window_seconds` | 两步交题的等待窗口（秒） |
| `compile_timeout_ms` | 编译超时 |
| `wall_margin_ms` | 墙上时间在题面时限之外多给的余量 |
| `default_time_limit_ms` / `default_memory_limit_mb` | 题目没设置时用的兜底 |
| `output_limit_kb` | 单组输出上限；超了判 OLE |
| `max_code_chars` | 代码长度上限（字符） |
| `max_file_kb` | 交题文件的大小上限（KB，默认 64；最大 1024） |
| `cpp_compiler` / `c_compiler` | 编译器路径（默认 `g++` / `gcc`） |

### `review`

| 键 | 说明 |
|----|------|
| `rules` | 静态规则开关（`system` / `fork` / `rm -rf` …） |
| `ai.enabled` | 模型审查开关；还要配齐 `base_url` + `model` + `api_key` 才生效 |
| `ai.base_url` | OpenAI 兼容接口根地址（会拼 `/chat/completions`） |
| `ai.model` | 模型名 |
| `ai.api_key` | 密钥；环境变量 `OJ_REVIEW_API_KEY` 优先 |
| `ai.timeout` | 单次审查超时（秒） |
| `ai.on_error` | 审查服务不可用时：`deny`（不判）/ `allow`（放行） |
| `ai.cache_size` | 审查结果缓存条数 |

### `gen`（AI 出测试点）

| 键 | 说明 |
|----|------|
| `enabled` | 开关（默认开；没配模型时自动跳过） |
| `auto_insert` | 所有入库的题**自动**生成 10 组（默认开） |
| `retry_missing` | 不够 10 组时再补几轮（默认 3） |
| `pitfalls` | 生成前先问「本题常见误区」，再针对误区出卡点（默认开） |
| `allow_scripts` | 允许模型用 Python 脚本生成测试数据（默认开，沙箱执行） |
| `script_timeout` | 生成脚本超时（秒，默认 15） |
| `backfill_on_start` | 启动时补「还没有测试点」的旧题（默认开） |
| `backfill_limit` | 一次最多补几道（默认 5） |
| `count` | 默认出几组（1..10） |
| `max_seconds` | 整次生成的总时间预算（秒，默认 3600）；超了就用已有结果收工 |
| `timeout` | 单次请求超时（秒，默认 600） |
| `max_tokens` | 单次请求的输出上限（默认 65536；思考模型的思考过程也算在里面） |
| `max_statement_chars` | 题面送审的截断长度 |
| `max_input_chars` | 单组输入的长度上限 |
| `base_url` / `model` / `api_key` | 留空借用 `plugins.ai_chat.llm`；也可用 `OJ_GEN_API_KEY` |

详见 [测试点从哪来](08-testcases.md)。

### `coach`（判完分之后的 AI 点评）

| 键 | 说明 |
|----|------|
| `enabled` | 点评开关（默认开；没配模型时自动跳过） |
| `base_url` / `model` / `api_key` | 留空时**借用 `plugins.ai_chat.llm`**；任一项写了就以它为准 |
| `timeout` | 单次点评超时（秒） |
| `max_tokens` | 点评输出上限 |
| `max_code_chars` | 送审代码的截断长度 |
| `max_chars` | 点评回给用户前的截断长度 |
| `reasoning_effort` | 推理预算；空 = 不传（普通模型不认这个字段） |

密钥也可以用环境变量 `OJ_COACH_API_KEY`（优先）。

### `render`

| 键 | 说明 |
|----|------|
| `width` | 图片宽度（像素） |
| `timeout` | 单次渲染超时（秒） |
| `max_height` | 图片最大高度（超长题面会被截断在最大高度） |
| `keep_files` | 出图目录最多留几张（旧的自动删） |
| `chromium` | Chromium 路径；空 = 自动探测 |
| `virtual_time_budget` | 等字体/公式就绪的虚拟时间预算（毫秒） |

### `luogu`

| 键 | 说明 |
|----|------|
| `timeout` | 抓取超时（秒） |
| `user_agent` | 自定义 UA；空 = 内置的浏览器 UA |
| `cookie` | 额外的 cookie（一般不用） |
| `overwrite` | 题库已有同题时是否按洛谷题面覆盖 |

## 5.3 改完多久生效

配置在插件**启用时**读取（`on_enable` 建好判题 / 审查 / 出图服务）。改完配置后
`/plugin` 之外的重载方式：

```text
/reload oj_bank     # 热重载本插件（或 /reload 全部）
```

白名单与 `judge.enabled` 每次命令都会读，不必重载；编译器路径、宽度这类
在建服务时读取的，需要重载。

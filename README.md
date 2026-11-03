# oj_bank —— 本地题库与判题

在 QQ 里维护本地题库：洛谷抓题 / AI 搜题入库、题卡出图、10 组测试点判题，以及题组与榜单。

这是 [ncqqpy](https://github.com/Green1423/ncqqpy) 的插件，**独立仓库**：
框架按 `plugin_dir`（默认 `./plugins`）扫描子目录加载插件，所以把它 clone 到
框架的 `plugins/` 下即可。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/Green1423/ncqqpy-oj-bank plugins/oj_bank
# 重新加载（QQ 内，超级用户 · 仅私聊）
#   /reload oj_bank
```

要求：框架 [ncqqpy](https://github.com/Green1423/ncqqpy)（`napcat` 包需可导入）。

## 配置

插件被**构造**时（`on_load()` 之前）会把自己的**全部**配置项写进
`config.json` 的 `plugins.oj_bank` 段（只补缺键、不覆盖已有值、幂等），所以打开
文件就能看到所有开关和默认值。模板如下：

```json
{
  "data_dir": "",
  "binding": {
    "groups": [],
    "users": []
  },
  "max_cases": 10,
  "list_page_size": 8,
  "submit": {
    "local_roots": [
      "~/.config/QQ",
      "~/.local/share/QQ",
      "/opt/ncqq",
      "/tmp",
      "/var/tmp"
    ]
  },
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
    "pitfalls": true,
    "allow_scripts": true,
    "script_timeout": 15,
    "auto_insert": true,
    "retry_missing": 3,
    "backfill_on_start": true,
    "backfill_limit": 5,
    "count": 10,
    "batch_size": 3,
    "max_seconds": 900,
    "timeout": 180,
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

环境变量（优先级高于文件）：

| 变量 | 说明 |
|------|------|
| `OJ_REVIEW_API_KEY` | AI 代码审查的 key（也可写进 `config.json → plugins.oj_bank.review.ai.api_key`；环境变量优先） |


详细文档：[`docs/oj-bank/README.md`](docs/oj-bank/README.md)。
## 测试

```bash
# 独立仓库里跑：框架需可导入（PYTHONPATH 指向框架仓库，或 pip install -e）
PYTHONPATH=/path/to/ncqqpy python -B tests/test_oj_bank.py
```

套件：`tests/test_oj_bank.py`。测试会在 `tests/.test-tmp/` 下建临时目录，不碰真实数据。

## 注意

- 判题会**执行别人提交的代码**：先读 `docs/oj-bank/03-judge.md` 的安全边界，开判题前确认白名单与资源限制。
- 题库（`data/` 下的 sqlite）与洛谷 cookie 都是本地运行期数据，**不要**提交。

## License

MIT，见 [LICENSE](LICENSE)。

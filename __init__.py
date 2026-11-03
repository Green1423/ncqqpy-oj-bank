"""oj_bank —— 本地题库 + 判题（洛谷抓题 / AI 搜题入库 / 出图 / 题组）。

插件入口在 `main.py`；这里只做导出与 `PLUGIN` 声明（见
`docs/plugin-dev/14-layout.md`：`__init__.py` 只装配）。
"""
from .main import OJBankPlugin

PLUGIN = OJBankPlugin
__all__ = ["OJBankPlugin"]

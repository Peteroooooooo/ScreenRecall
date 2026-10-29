"""真实 Windows 上的集成检查（单独运行：python -m pytest tests_win -q -p no:cacheprovider）。

只把仓库根目录加进 sys.path，不安装 tests/conftest.py 里的假 winapi。
只操作测试自己打开的窗口和进程（tkinter、自己启动的控制台、临时配置的 Edge），用完只关这些。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

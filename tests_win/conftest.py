"""真实 Windows 上的集成检查（不装假 winapi）。只操作本测试自己开的 tkinter 窗口。

单独运行：python -m pytest tests_win -q -p no:cacheprovider
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

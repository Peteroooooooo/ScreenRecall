"""真实 Windows 检查（单独运行，不装假 winapi）：只把仓库根目录加进 sys.path。

只操作本测试自己打开的窗口（tkinter、自己启动的控制台、临时配置的 Edge），用完只关这些。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

"""ScreenRecall：轻量本地屏幕记忆系统（NUS-ISS PRS Group 9）。"""

import os as _os

# numpy 自带的 OpenBLAS 一导入就按 CPU 逻辑核数给每个线程提交一块缓冲：本机 16 线程约 490 MiB 已提交内存
# （"查询模型卸载后还剩约 490 MiB private"其实是它，不是模型）。本项目只有向量检索用矩阵乘：
# 1M x 384 一次 4 线程 32 ms、16 线程 25 ms；4 线程只提交约 110 MiB。须在 numpy 第一次导入之前设好；
# 环境变量里已经设了就照用户的。
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")

__version__ = "0.1.0"

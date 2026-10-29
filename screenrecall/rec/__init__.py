"""录制用的底层部件（v3 的 loop / trigger / pipeline 用）：只看像素和窗口状态，不碰数据库。

各部分：
- motion：相邻两帧哪些小块变了（tile_changes）；
- volatile：一直在变的小块（视频、时钟），判断"停下"时遮掉；
- wgc / sessions：Windows.Graphics.Capture 截图会话和会话池，PrintWindow 兜底；
- inputmon：键鼠活动、滚轮和"打字"时间（只记时间，不记键值）；
- windows：前台窗口、窗口状态、桌面 / 开始菜单、锁屏。
"""

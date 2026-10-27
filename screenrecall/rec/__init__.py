"""v2 录制核心：只看像素和窗口状态的快路径（截图、比对、静止判断、候选帧），不碰数据库。

各部分：
- motion：相邻两帧"怎么动了"（平移 / 原地变 / 接不上）；
- volatile：一直在变的小块（视频、时钟），比对时遮掉；
- wgc / sessions：Windows.Graphics.Capture 截图会话和会话池，PrintWindow 兜底；
- inputmon / windows：滚轮、键盘活动和前台窗口、窗口状态、排除规则；
- tracker：每个窗口的块、位置、静止、候选帧和证据帧判断，事件发给 Sink；RecorderCore 把它们串起来。
"""

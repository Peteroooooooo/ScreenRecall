"""ScreenRecall v2 主进程：托盘、网页服务、MCP、访问令牌、子进程看护、空闲检测、开机自启、大模型客户端。

这个包只做"外壳"，不加载模型、不碰录制细节；数据查询和录制控制通过接口注入（见 mcp_server.Queries、main.Controller）。
"""

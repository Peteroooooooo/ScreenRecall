"""v2 存储与检索：事件驱动录制的数据层（新库 recall.sqlite3，旧库 screenrecall.sqlite3 保持不动）。

- schema：表结构、迁移与连接（读写 / 只读）；
- writer：单写线程，调用方只排队、不等数据库；
- fts：文字规范化、索引文档与词法检索；
- vectors：独立文件里的追加式向量库（numpy 暴力检索）；
- delete：隐私删除（行、全文索引、证据图、向量都真删）；
- queries：网页与 MCP 共用的只读查询接口。
"""

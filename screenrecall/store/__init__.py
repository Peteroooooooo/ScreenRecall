"""存储的共用部件（v3 的数据层在 screenrecall/v3/store）：

- fts：文字规范化（docs.norm / fold）、全文索引文档与查询拆词（lexical.plan）；
- vectors：独立文件里的追加式向量库（numpy 暴力检索），可选的按意思搜用。
"""

"""cao-web-04 补采（2026-10-20）：artic.edu 两张（021/022）停在 Cloudflare 人机验证页，后 3 张因浏览器被关闭未截。
原图保留标 drop；改用 MoMA 作品页 5 张，编号 023 起。
"""
from web_batch import five

TASKS = {"cao-web-04": [dict(d, seq=23 + k, variation=d["variation"] + "（补采，替换停在人机验证页的 artic.edu）")
                        for k, d in enumerate(five([("https://www.moma.org/collection/works/79802", "en")], "博物馆网站"))]}

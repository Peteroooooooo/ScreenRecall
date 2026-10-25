"""cao-web-02 补采（2026-10-20）：nothing.tech 5 张（011-015）被"地区跳转"弹窗整屏遮挡，dji 016 被订阅弹窗遮挡。
原图保留并在清单里标 drop；补采换一个科技产品网站（Samsung Galaxy 页）5 张 + dji 首页顶部 1 张，编号 026 起。
"""
from web_batch import FULL, HALF, NOTE

S = "https://www.samsung.com/sg/smartphones/galaxy-s25-ultra/"
TASKS = {"cao-web-02": [
    dict(seq=26 + k, url=S, scroll=sc, window=w, language="en", settle=3.0, source_kind="real_web", note=NOTE,
         variation=f"科技产品页 samsung.com：{d}（补采，替换被弹窗遮挡的 nothing.tech）", **({"zoom": z} if z != 1 else {}))
    for k, (sc, w, z, d) in enumerate([(0, FULL, 1, "顶部，全窗口"), (900, FULL, 1, "滚动到中部"),
                                       (1800, FULL, 1, "滚动到下部"), (0, HALF, 1, "半屏窗口顶部"),
                                       (600, FULL, 0.8, "缩小到 80% 的中上部")])
] + [dict(seq=31, url="https://www.dji.com/", scroll=0, window=FULL, language="en", settle=3.0, source_kind="real_web",
          note=NOTE, variation="科技产品页 dji.com：顶部，全窗口（补采，替换被订阅弹窗遮挡的 016）")]}

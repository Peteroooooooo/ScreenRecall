"""cao-web-01 补采（2026-10-18）：替换被弹窗遮挡或未加载完整的 001 / 009 / 010。

原图保留在任务目录里，清单里标 keep=drop 并写原因；补采用新文件、新编号（seq 11-13）。
截图前自动关 cookie 条和促销弹窗（只点拒绝 / 关闭），等视口内图片加载完，鼠标移出内容区。
"""
FULL = (1400, 860)
NOTE = "公开网站原样显示，未登录、未点击；画面事实待人工看图后补"
# 第一轮补采（10:35）的 012 / 013 仍有问题（弹窗在 iframe 里没关掉；滚动 700 处是拒绝 cookie 后不渲染的
# SWISS 航班搜索组件，空白是页面本身状态），原图保留并标 drop；第二轮用 seq 14 / 15，013 改滚到 1400。
ROUND1 = {"cao-web-01": [
    dict(seq=11, url="https://www.jungfrau.ch/en-gb/", scroll=0, window=FULL, language="en", settle=3.0,
         source_kind="real_web", note=NOTE, variation="Jungfrau 首页顶部（补采，替换 cao-web-01-001：cookie 弹窗遮挡）"),
    dict(seq=12, url="https://www.myswitzerland.com/en/", scroll=0, window=FULL, language="en", settle=3.0,
         source_kind="real_web", note=NOTE,
         variation="MySwitzerland 首页顶部（补采，替换 cao-web-01-009：促销弹窗遮挡）"),
    dict(seq=13, url="https://www.myswitzerland.com/en/", scroll=700, window=FULL, zoom=0.8, language="en",
         settle=3.0, source_kind="real_web", note=NOTE,
         variation="MySwitzerland 中部，缩小（补采，替换 cao-web-01-010：左侧卡片未加载）"),
]}

# 第二轮的 014 仍有促销弹窗（拒绝 cookie 后几秒才出现，工具已提前停止等待），原图保留标 drop；第三轮 seq 16。
ROUND2 = {"cao-web-01": [
    dict(seq=14, url="https://www.myswitzerland.com/en/", scroll=0, window=FULL, language="en", settle=3.0,
         source_kind="real_web", note=NOTE,
         variation="MySwitzerland 首页顶部（补采，替换 cao-web-01-009：促销弹窗遮挡）"),
    dict(seq=15, url="https://www.myswitzerland.com/en/", scroll=1400, window=FULL, zoom=0.8, language="en",
         settle=3.0, source_kind="real_web", note=NOTE,
         variation="MySwitzerland 中下部，缩小（补采，替换 cao-web-01-010：滚动 700 处的航班搜索组件空白）"),
]}

TASKS = {"cao-web-01": [
    dict(seq=16, url="https://www.myswitzerland.com/en/", scroll=0, window=FULL, language="en", settle=3.0,
         source_kind="real_web", note=NOTE,
         variation="MySwitzerland 首页顶部（补采，替换 cao-web-01-009：促销弹窗遮挡）"),
]}

"""cao 正式采集：公开网站在独立 Chromium 窗口里的真实显示（capture_mac.py --plan 使用）。

每个任务 5 个不同网页 × 5 种画面变化（顶部、两个滚动位置、半屏、缩小后中部）= 25 张；地图类改为不同地点 /
缩放级别的 URL（滚轮在地图上会缩放地图）；汇率表用不同基准币种和日期。未登录、不提交任何内容，只点
cookie / 促销弹窗的拒绝或关闭。画面事实（外观描述）需要人看图后写，这里不预填。
"""
FULL, HALF = (1400, 860), (900, 860)
NOTE = "公开网站原样显示，未登录、未点击；画面事实待人工看图后补"


def five(urls_langs, what):
    out = []
    for url, lang in urls_langs:
        site = url.split("/")[2].removeprefix("www.")
        for k, (scroll, window, zoom, desc) in enumerate([
            (0, FULL, 1.0, "顶部，全窗口"), (900, FULL, 1.0, "滚动到中部"), (1800, FULL, 1.0, "滚动到下部"),
            (0, HALF, 1.0, "半屏窗口顶部"), (600, FULL, 0.8, "缩小到 80% 的中上部")]):
            d = dict(url=url, scroll=scroll, window=window, language=lang, settle=3.0, source_kind="real_web",
                     note=NOTE, variation=f"{what} {site}：{desc}")
            if zoom != 1.0:
                d["zoom"] = zoom
            out.append(d)
    return out


def single(urls_langs, what, window=FULL):
    return [dict(url=u, scroll=0, window=window, language=lang, settle=4.0, source_kind="real_web", note=NOTE,
                 variation=f"{what}：{desc}") for u, lang, desc in urls_langs]


OSM = "https://www.openstreetmap.org/#map="
MAPS = [(OSM + z, "en", d) for z, d in [
    ("12/1.3521/103.8198", "新加坡全岛 12 级"), ("15/1.2966/103.7764", "NUS 校园 15 级"),
    ("17/1.2838/103.8591", "滨海湾 17 级"), ("10/46.5590/7.9980", "少女峰一带 10 级"),
    ("13/46.0207/7.7491", "采尔马特 13 级"), ("12/35.6812/139.7671", "东京 12 级"),
    ("16/35.6586/139.7454", "东京塔周边 16 级"), ("11/22.3193/114.1694", "香港 11 级"),
    ("14/48.8584/2.2945", "巴黎铁塔周边 14 级"), ("9/-8.4095/115.1889", "巴厘岛 9 级"),
    ("12/51.5074/-0.1278", "伦敦 12 级"), ("15/40.7580/-73.9855", "纽约时代广场 15 级"),
    ("6/36.20/138.25", "日本全境 6 级"), ("13/31.2304/121.4737", "上海 13 级"),
    ("12/-33.8688/151.2093", "悉尼 12 级")]] + \
    [(f"https://www.openstreetmap.org/#map={z}&layers=C", "en", d + "（自行车图层）") for z, d in [
        ("13/1.3521/103.8198", "新加坡"), ("12/47.3769/8.5417", "苏黎世")]] + \
    [(f"https://www.openstreetmap.org/#map={z}&layers=T", "en", d + "（交通图层）") for z, d in [
        ("13/1.3000/103.8500", "新加坡市区"), ("12/52.5200/13.4050", "柏林")]] + \
    [(u, "en", d) for u, d in [
        ("https://www.meteoblue.com/en/weather/week/singapore_singapore_1880252", "meteoblue 新加坡一周天气"),
        ("https://www.meteoblue.com/en/weather/week/z%c3%bcrich_switzerland_2657896", "meteoblue 苏黎世一周天气"),
        ("https://www.meteoblue.com/en/weather/week/tokyo_japan_1850147", "meteoblue 东京一周天气"),
        ("https://www.windy.com/?1.352,103.820,8", "windy 新加坡风场"),
        ("https://www.windy.com/-Temperature-temp?temp,46.56,7.99,7", "windy 阿尔卑斯气温图"),
        ("https://www.windy.com/-Rain-thunder-rain?rain,35.68,139.77,6", "windy 东京降雨雷达")]]

XR = "https://www.x-rates.com"
RATES = [(f"{XR}/table/?from={c}&amount=1", "en", f"{c} 为基准的当前汇率表") for c in ("USD", "SGD", "EUR", "CNY", "JPY")] + \
    [(f"{XR}/historical/?from={c}&amount=1&date={d}", "en", f"{c} 为基准、{d} 的历史汇率表")
     for c in ("USD", "SGD", "EUR", "CNY", "JPY") for d in ("2026-01-02", "2025-06-30", "2024-12-31", "2023-03-15")]

TASKS = {
    "cao-web-02": five([("https://www.apple.com/airpods-pro/", "en"), ("https://www.apple.com/macbook-pro/", "en"),
                        ("https://nothing.tech/", "en"), ("https://www.dji.com/", "en"),
                        ("https://www.apple.com/apple-vision-pro/", "en")], "科技产品页"),
    "cao-web-03": five([("https://www.bbcgoodfood.com/recipes/collection/easy-recipes", "en"),
                        ("https://www.recipetineats.com/", "en"),
                        ("https://www.bbcgoodfood.com/recipes/collection/chicken-recipes", "en"),
                        ("https://www.recipetineats.com/category/main-dishes/", "en"),
                        ("https://www.xiachufang.com/", "zh")], "食谱网站"),
    "cao-web-04": five([("https://www.rijksmuseum.nl/en", "en"), ("https://www.rijksmuseum.nl/en/collection/SK-C-5", "en"),
                        ("https://www.rijksmuseum.nl/en/collection/SK-A-2344", "en"),
                        ("https://www.nationalgallery.org.uk/", "en"),
                        ("https://www.artic.edu/artworks/27992/a-sunday-on-la-grande-jatte-1884", "en")], "博物馆网站"),
    "cao-web-05": five([("https://www.bbc.com/news", "en"), ("https://www.theguardian.com/international", "en"),
                        ("https://www.straitstimes.com/", "en"), ("https://www.channelnewsasia.com/", "en"),
                        ("https://www.zaobao.com.sg/", "zh")], "新闻首页"),
    "cao-web-06": five([("https://www.themoviedb.org/movie/872585", "en"),
                        ("https://letterboxd.com/film/dune-part-two/", "en"),
                        ("https://www.imdb.com/title/tt15398776/", "en"), ("https://www.themoviedb.org/tv/1399", "en"),
                        ("https://letterboxd.com/film/oppenheimer-2023/", "en")], "电影介绍页"),
    "cao-web-07": five([("https://nus.edu.sg/", "en"), ("https://www.ntu.edu.sg/", "en"), ("https://www.mit.edu/", "en"),
                        ("https://ethz.ch/en.html", "en"), ("https://www.tsinghua.edu.cn/", "zh")], "大学官网"),
    "cao-web-08": five([("https://www.premierleague.com/", "en"), ("https://www.bbc.com/sport", "en"),
                        ("https://www.fifa.com/en", "en"), ("https://www.espn.com/", "en"),
                        ("https://www.formula1.com/", "en")], "体育网站"),
    "cao-web-09": single(MAPS, "地图 / 天气"),
    "cao-web-10": five([("https://dribbble.com/shots/popular", "en"), ("https://www.awwwards.com/websites/", "en"),
                        ("https://www.awwwards.com/websites/sites_of_the_day/", "en"),
                        ("https://dribbble.com/shots/popular/illustration", "en"),
                        ("https://www.behance.net/galleries", "en")], "设计作品集"),
    "cao-table-03": five([("https://en.wikipedia.org/wiki/IPhone_16_Pro", "en"),
                          ("https://en.wikipedia.org/wiki/Samsung_Galaxy_S25", "en"),
                          ("https://en.wikipedia.org/wiki/Pixel_9", "en"),
                          ("https://www.apple.com/iphone/compare/", "en"),
                          ("https://www.apple.com/macbook-air/specs/", "en")], "商品参数页"),
    "cao-table-10": single(RATES, "汇率表"),
}

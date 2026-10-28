"""演示数据：方案"实际跑一遍"那 15 分钟（14:00–14:15 的 6 个时刻），网页和 MCP 手动演示用。

python -m screenrecall.v3.demo --data-dir <空目录> [--day YYYY-MM-DD]

只用 Store 的公开方法写库（和录制进程的处理线程走同一套写法）；截图是 Pillow 画的合成图，框和行对得上。
写完：字 19 行（网页 12 行、微信 7 行）、截图 2 张、时间线 4 项（网页 / VS Code 灰字 / 微信 / 回到网页、还在看）、复制 1 段。
数据目录里已经有库文件时拒绝运行（不会往已有的库里掺演示数据）。
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import date, datetime, time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..store.fts import docs
from .config import V3Config, build
from .store import Store
from .types import (GREY_UNLISTED, KIND_APP, KIND_URL, REASON_COPY, REASON_STILL, SHOT_FIRST, SHOT_VISIT,
                    EncodedShot, SourceLabel, TextLine)

WEB = "https://blog.example.com/transformer-intro"
WEB_TITLE = "Transformer 入门"
NAV = "首页　归档　关于　登录"
ART = ["Transformer 入门：从注意力机制讲起", "2017 年的论文《Attention Is All You Need》提出了 Transformer。",
       "它完全不用循环结构，只用注意力。", "自注意力：每个词都去看句子里的所有词。", "计算方式是 Q、K、V 三个矩阵。",
       "注意力分数 = softmax(QKᵀ / √d)。", "多头注意力把这个过程并行做几遍。", "下一节讲位置编码。",
       "位置编码：给每个位置加一个向量。", "原论文用的是正弦和余弦函数。", "这样模型能知道词的先后顺序。",
       "下一节讲编码器和解码器。"]
CHAT_APP, CHAT_TITLE = "Weixin.exe", "微信"
CHAT_1 = [("小王：明天组会改到下午三点", False), ("小李：好的，我把图表再整理一下", True),
          ("小王：记得带上周的实验结果", False)]
CHAT_2 = [("小李：数据集链接我发群里了", True), ("小王：https://example.com/dataset", False),
          ("小李：老师说报告要加一段局限性分析", True), ("小王：好，那部分我来写", False)]
COPIED = "小李：老师说报告要加一段局限性分析"
UNLISTED_APP = "Code.exe"          # 不在名单里：时间线上一行灰字"在 Code.exe 里（未录）"
ALWAYS_APP = "Zoom.exe"            # 名单页演示"一直录"这一档

W, H = 1280, 800
_FONTS = ("msyh.ttc", "simhei.ttf", "simsun.ttc", "arialuni.ttf")


class DemoError(RuntimeError):
    pass


def _font(size: int):
    for name in _FONTS:
        for p in (Path("C:/Windows/Fonts") / name, Path(name)):
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default(size)  # 没有中文字体时字会是方块，框照样对得上


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def render_web(lines: list[str], *, quality: int) -> tuple[EncodedShot, list[TextLine]]:
    """浏览器窗口：标题栏、地址栏、菜单（不是正文，不算行）、正文若干行（系统文字）。"""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    small, body = _font(16), _font(24)
    d.rectangle((0, 0, W, 36), fill=(222, 225, 230))
    d.text((14, 8), f"{WEB_TITLE} - Edge", font=small, fill=(30, 30, 30))
    d.rectangle((0, 36, W, 76), fill=(245, 246, 248))
    d.rounded_rectangle((90, 42, W - 90, 70), radius=12, fill=(255, 255, 255), outline=(200, 200, 205))
    d.text((106, 47), WEB, font=small, fill=(60, 60, 60))
    d.text((80, 96), NAV, font=small, fill=(120, 120, 120))
    out: list[TextLine] = []
    y = 150
    for text in lines:
        l, t, r, b = d.textbbox((80, y), text, font=body)
        d.text((80, y), text, font=body, fill=(20, 20, 20))
        out.append(TextLine(text, (l, t, r - l, b - t), "uia"))
        y += 64
    return _shot(img, out, quality), out


def render_chat(msgs: list[tuple[str, bool]], *, quality: int) -> tuple[EncodedShot, list[TextLine]]:
    """聊天窗口：标题只有"微信"，消息气泡（自己发的在右边）。系统文字给不出消息，这些行当成 OCR 读到的。"""
    img = Image.new("RGB", (W, H), (237, 237, 237))
    d = ImageDraw.Draw(img)
    small, body = _font(16), _font(22)
    d.rectangle((0, 0, W, 48), fill=(245, 245, 245))
    d.text((W // 2 - 20, 14), CHAT_TITLE, font=small, fill=(30, 30, 30))
    out: list[TextLine] = []
    y = 100
    for text, me in msgs:
        l, t, r, b = d.textbbox((0, 0), text, font=body)
        tw, th = r - l, b - t
        x = W - 120 - tw if me else 120
        d.rounded_rectangle((x - 16, y - 12, x + tw + 16, y + th + 16), radius=8,
                            fill=(149, 236, 105) if me else (255, 255, 255))
        d.text((x - l, y - t), text, font=body, fill=(20, 20, 20))
        out.append(TextLine(text, (x, y, tw, th), "ocr"))
        y += th + 70
    return _shot(img, out, quality), out


def _shot(img: Image.Image, lines: list[TextLine], quality: int) -> EncodedShot:
    return EncodedShot(_encode(img, quality), img.width, img.height, 1.0,
                       [(docs.norm(ln.text), ln.box) for ln in lines])


class _Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t


def run(data_dir: Path, day: date) -> dict:
    """写演示数据，返回计数。"""
    cfg: V3Config = build({}, data_dir=Path(data_dir), home=Path(data_dir))
    if cfg.db_path.exists():
        raise DemoError(f"数据目录里已经有库文件，不往里写演示数据：{cfg.db_path}")

    def at(hh: int, mm: int, ss: int = 0) -> int:
        clock.t = int(datetime.combine(day, time(hh, mm, ss)).timestamp() * 1000)
        return clock.t

    clock = _Clock(0)
    at(13, 59, 50)
    st = Store(cfg, clock=clock)
    counts = {"lines": 0, "shots": 0, "snaps": 0, "clips": 0}
    try:
        st.init_defaults(cfg.default_record_apps)
        st.set_app_tier(CHAT_APP, "record")        # 方案："假设你已把微信加进名单"
        st.set_app_tier(ALWAYS_APP, "always")
        web = SourceLabel(KIND_URL, WEB, "msedge.exe", WEB_TITLE, url=WEB)
        chat = SourceLabel(KIND_APP, f"{CHAT_APP.lower()}\t{CHAT_TITLE}", CHAT_APP, CHAT_TITLE)

        def snap(label: SourceLabel, sid: int, vid: int, t: int, reason: str, via: str, lines: list[TextLine]):
            out = st.record_lines(sid, lines, t, visit_id=vid)
            snap_id = st.add_snap(t_ms=t, source_id=sid, visit_id=vid, reason=reason, via=via,
                                  new_lines=out.new_count, seen_lines=len(out.seen_ids))
            st.visit_touch(vid, t)
            st.heartbeat(recorder_state="recording", last_snap_ms=t, current_app=label.app, current_title=label.title)
            counts["lines"] += out.new_count
            counts["snaps"] += 1
            return out, snap_id

        def keep(sid: int, vid: int, snap_id: int, t: int, kind: str, shot: EncodedShot, ids: dict) -> None:
            if st.save_shot(source_id=sid, visit_id=vid, snap_id=snap_id, t_ms=t, kind=kind, shot=shot,
                            ids_by_norm=ids) is not None:
                counts["shots"] += 1

        # 14:00 打开文章，停下来读：强标签，存 8 行，留第一张图
        sid_web, _ = st.upsert_source(web, at(14, 0))
        v1 = st.visit_enter(sid_web, clock.t)
        shot, lines = render_web(ART[0:8], quality=cfg.jpeg_quality)
        out, s = snap(web, sid_web, v1, at(14, 0, 2), REASON_STILL, "uia", lines)
        keep(sid_web, v1, s, clock.t, SHOT_FIRST, shot, out.ids_by_norm)
        # 14:02 往下滚了半屏：4 行见过、新存 4 行；网址已有图，不留
        _, lines = render_web(ART[4:12], quality=cfg.jpeg_quality)
        st.upsert_source(web, at(14, 2))
        snap(web, sid_web, v1, at(14, 2, 2), REASON_STILL, "uia", lines)
        # 14:05 切到 VS Code（不在名单）：一行灰字
        st.visit_leave(v1, at(14, 5))
        grey = st.grey_enter(GREY_UNLISTED, UNLISTED_APP, clock.t)
        st.heartbeat(recorder_state="recording", current_app=None, current_title=None)
        # 14:12 点开微信：弱标签，OCR 3 条，先当候选
        st.grey_leave(grey, at(14, 12))
        sid_chat, _ = st.upsert_source(chat, clock.t)
        v2 = st.visit_enter(sid_chat, clock.t)
        _, lines = render_chat(CHAT_1, quality=cfg.jpeg_quality)
        snap(chat, sid_chat, v2, at(14, 12, 2), REASON_STILL, "ocr", lines)
        # 14:14 往上翻停下：新 4 条，候选换成这张；复制一句 → 记复制、再拍一次（0 新）
        best, lines = render_chat(CHAT_2, quality=cfg.jpeg_quality)
        st.upsert_source(chat, at(14, 14))
        best_out, best_snap = snap(chat, sid_chat, v2, at(14, 14, 2), REASON_STILL, "ocr", lines)
        best_t = clock.t
        st.add_clip(t_ms=at(14, 14, 20), text=COPIED, app=CHAT_APP, title=CHAT_TITLE, source_id=sid_chat,
                    visit_id=v2)
        counts["clips"] += 1
        snap(chat, sid_chat, v2, clock.t, REASON_COPY, "ocr", lines)
        # 14:15 切回文章：微信这一次结束，存下候选（14:14 那张）；离开文章已超过 5 分钟 → 另开一条，0 新
        st.visit_leave(v2, at(14, 15))
        keep(sid_chat, v2, best_snap, best_t, SHOT_VISIT, best, best_out.ids_by_norm)
        st.upsert_source(web, clock.t)
        v3 = st.visit_enter(sid_web, clock.t)
        _, lines = render_web(ART[4:12], quality=cfg.jpeg_quality)
        snap(web, sid_web, v3, at(14, 15, 2), REASON_STILL, "uia", lines)
        # 演示数据不是真的在录：录制进程状态记成已停（这一条保持"还在看"）
        st.heartbeat(recorder_state="stopped", current_app=None, current_title=None)
    finally:
        st.close()
    counts["visits"] = 3
    counts["grey_spans"] = 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.v3.demo", description="写方案'实际跑一遍'的演示数据")
    ap.add_argument("--data-dir", required=True, help="空的数据目录（已有库文件时拒绝运行）")
    ap.add_argument("--day", help="本机日期 YYYY-MM-DD，默认今天")
    args = ap.parse_args(argv)
    try:
        day = date.fromisoformat(args.day) if args.day else date.today()
    except ValueError:
        print("--day 要写成 YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        n = run(Path(args.data_dir).resolve(), day)
    except DemoError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"演示数据已写入 {Path(args.data_dir).resolve()}（{day.isoformat()} 14:00–14:15）："
          f"字 {n['lines']} 行，截图 {n['shots']} 张，拍 {n['snaps']} 次，复制 {n['clips']} 段，"
          f"时间线 {n['visits']} 条 + 灰字 {n['grey_spans']} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

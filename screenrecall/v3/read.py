"""② 读字（方案"读字"一节）：先读系统文字（UIA，只要屏幕上看得见的正文），不够时当场对整帧 OCR，合并去重。

1. UIA 读一次（最多 uia_timeout_ms、uia_max_nodes 个控件；超时不重读）；只要 role == 'text' 的行（正文；菜单按钮、标签页、
   地址栏是 'ui'），密码框的值 text/uia.py 本来就不取；
2. 正文少于 ocr_min_chars 个字，或正文不到读到的字的 ocr_min_body_ratio（大多是菜单按钮）→ 整帧 OCR
   （PP-OCRv6 tiny 约 0.7 秒）；OCR 行里落在窗口上部界面条（标签页条 / 工具栏，UIA 的 chrome_bottom）和
   系统标题栏以上的不要；
3. 合并：UIA 行在前（屏幕坐标换成整帧坐标），OCR 行按 norm 去重（和已有的行 norm 相同；或框重叠、并且一方包含另一方
   或几乎一样——OCR 认错一两个字、标点不同），
   按 (y, x) 排阅读顺序；norm 少于 2 个字的行、只有时间 / 日期的行、OCR 出来的别的行开头的一截（列表里截断的预览）不要；
4. url = 最大的 Document 元素的值（UiaResult.doc_url；正文读超时时是读字前单独找的那个），page_title = 它的名字；
   window_title = 读完系统文字时窗口的标题（调用方据此认出"拍完到读字之间换了页面"）；
   window_names = 辅助功能里的窗口名字（ReadResult，调用方据此认出标题里不写"无痕"的无痕窗口）；
5. 要 OCR 之前问调用方 want_ocr(到这一步的结果)：这一拍反正不存（内部页、本地文件、换了页面）就不 OCR。
从不抛异常：出错时 status='error'、lines 为空。日志只记次数和原因，从不记屏幕上的字。
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable

from ..rec.frames import FrameMap
from ..store.fts.docs import norm
from ..text import uia as uia_mod
from .config import V3Config
from .types import Box, TextLine, TextResult

log = logging.getLogger(__name__)

MIN_NORM_CHARS = 2       # norm 少于这么多字的行（单个符号、图标字）不要
NEAR_DUP_RATIO = 0.85    # 框重叠的两行 norm 相似度（difflib）到这么多就算同一行（OCR 认错一两个字、标点不同）
FIRST_TOUCH_GAP_S = 0.25  # 第一次读一个窗口只拿到外框时：隔这么久再读
FIRST_TOUCH_TRIES = 6     # 最多再读这么多次（约 1.5 秒）
_UIA_OK = ("ok", "empty")
# 只有时间 / 日期的行（norm 之后）：14:05、上午9:30、昨天12:30、星期三、2026/10/28、10月28日、2026-10-2814:05
_DAY = r"(?:上午|下午|中午|晚上|凌晨|今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]|am|pm)"
_TIME_ONLY = re.compile(rf"^{_DAY}?(?:\d{{1,4}}[年/\-]\d{{1,2}}(?:[月/\-]\d{{1,2}}日?)?|\d{{1,2}}月\d{{1,2}}日)?{_DAY}?"
                        rf"(?:\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?{_DAY}?$")
_UIA_SKIP = ("minimized", "no_window")   # 窗口不在 / 最小化：读不了，也不算"读过"
# 鼠标停在链接上时，浏览器在窗口左下角弹出的小框里写着链接地址：屏幕上有，但不是页面内容
_HOVER_LINK = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://)?[\w.\-]+(?::\d+)?/\S*$", re.IGNORECASE)
HOVER_ZONE_Y = 0.92       # 框的中线在帧的这么低以下
HOVER_ZONE_X = 0.5        # 并且从帧的左半边开始


def _hover_link(text: str, box: tuple[float, float, float, float], fw: float, fh: float) -> bool:
    l, t, r, b = box
    return bool(_HOVER_LINK.match(text)) and (t + b) / 2 > HOVER_ZONE_Y * fh and l < HOVER_ZONE_X * fw


@dataclasses.dataclass(frozen=True)
class ReadResult(TextResult):
    """TextResult 再加一项（types.py 冻结，先在这里加；调用方用 getattr(res, "window_names", ()) 读）：
    window_names：辅助功能里窗口根元素和直接子元素的名字（UiaResult.window_names）。有的浏览器的无痕窗口
    只在这里写着"（无痕）"、窗口标题里没有，调用方拿它和窗口标题一起按无痕关键词判。"""
    window_names: tuple[str, ...] = ()
    uia_timed_out: bool = False


def _usable(res) -> bool:
    return bool(getattr(res, "usable", True))


def _box(l: float, t: float, r: float, b: float) -> Box:
    return (int(round(l)), int(round(t)), max(0, int(round(r - l))), max(0, int(round(b - t))))


def _overlap(a: Box, b: Box) -> bool:
    return min(a[0] + a[2], b[0] + b[2]) > max(a[0], b[0]) and min(a[1] + a[3], b[1] + b[3]) > max(a[1], b[1])


def _near(a: str, b: str) -> bool:
    """两行 norm 几乎一样（长度先粗筛，再 difflib 相似度）。"""
    la, lb = len(a), len(b)
    if min(la, lb) < NEAR_DUP_RATIO * max(la, lb) * 0.9:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= NEAR_DUP_RATIO


def merge_lines(uia_lines: list[TextLine], ocr_lines: list[TextLine]) -> list[TextLine]:
    """UIA 行在前、OCR 行在后合并去重，按 (y, x) 排序。

    - UIA 行之间：norm 相同的只留第一行；
    - OCR 行：和已留的任一行 norm 相同，或框重叠且一方 norm 包含另一方 / 相似度 ≥ NEAR_DUP_RATIO → 去掉；
      和已留的 OCR 行重叠、并且把它包含在内（更完整）→ 换成这一行（UIA 行永远优先）；
    - norm 少于 MIN_NORM_CHARS 个字的行、只有时间 / 日期的行不要；OCR 行是另一行开头的一截（截断的预览）不要。
    """
    kept: list[tuple[str, TextLine]] = []
    seen: set[str] = set()
    for ln in uia_lines:
        k = norm(ln.text)
        if len(k) < MIN_NORM_CHARS or k in seen:
            continue
        seen.add(k)
        kept.append((k, ln))
    for ln in ocr_lines:
        k = norm(ln.text)
        if len(k) < MIN_NORM_CHARS or k in seen:
            continue
        drop, replace = False, None
        for i, (kk, other) in enumerate(kept):
            if not _overlap(ln.box, other.box):
                continue
            if k in kk:
                drop = True
                break
            if kk in k:
                if other.via == "ocr" and replace is None:
                    replace = i
                else:
                    drop = True
                    break
            elif _near(k, kk):
                drop = True
                break
        if drop:
            continue
        if replace is not None:
            seen.discard(kept[replace][0])
            kept[replace] = (k, ln)
        else:
            kept.append((k, ln))
        seen.add(k)
    # 只是时间 / 日期的行（聊天、列表里每条旁边的 "14:05"、"昨天 12:30"）不要：每个不同的钟点都会成一行新字；
    # OCR 行是这一拍里另一行开头的一截（列表里截断的预览）也不要
    norms = [k for k, _ in kept]
    out = [ln for k, ln in kept if not _TIME_ONLY.match(k)
           and not (ln.via == "ocr" and any(o != k and o.startswith(k) for o in norms))]
    out.sort(key=lambda ln: (ln.box[1], ln.box[0]))
    return out


class TextReader:
    """录制进程处理线程里用的读字器（一个进程一个）。

    uia：有 read(hwnd, timeout_s) -> UiaResult 的对象；None = 按配置新建 text.uia.UiaReader。
    ocr：有 recognize(bgr) -> list[OcrLine] 的对象，或返回它的零参函数；None = 第一次要用时才建 OcrEngine；
         False = 不用 OCR（录制进程的 --no-ocr）。模型缺失 / 建不起来时记一次日志，之后只用 UIA。
    """

    def __init__(self, cfg: V3Config, *, uia=None, ocr=None, title_of: Callable[[int], str] | None = None):
        self.cfg = cfg
        self._title_of = title_of
        if uia is None:
            uia = uia_mod.UiaReader(uia_mod.UiaConfig(
                timeout_s=cfg.uia_timeout_ms / 1000.0,
                max_elements=cfg.uia_max_nodes,
                comtypes_cache_dir=str(cfg.home / "comtypes_cache"),
            ))
        self._uia = uia
        self._engine: Any = None
        self._factory: Callable[[], Any] | None = None
        self._ocr_dead = ocr is False
        if ocr is None:
            self._factory = self._default_engine
        elif ocr is not False:
            if hasattr(ocr, "recognize"):
                self._engine = ocr
            elif callable(ocr):
                self._factory = ocr
            else:
                raise TypeError("ocr 要么有 recognize()，要么是返回它的零参函数")
        self.stats = {"reads": 0, "ocr": 0, "ocr_ms": 0.0, "ocr_skipped": 0, "ocr_errors": 0, "errors": 0,
                      "uia_timeouts": 0}
        self._touched: OrderedDict[int, None] = OrderedDict()   # 读过的窗口（最近 256 个）：只有第一次读才等辅助功能树

    def _default_engine(self):
        from ..text.ocr2 import OcrEngine

        return OcrEngine(self.cfg.ocr_model, threads=self.cfg.ocr_threads, models_dir=self.cfg.ocr_models_path)

    def _ocr(self):
        """OCR 引擎（第一次要用时才建）；不用 / 建不起来时 None。"""
        if self._engine is not None or self._ocr_dead:
            return self._engine
        try:
            self._engine = self._factory()
        except Exception as exc:  # noqa: BLE001 - 模型缺失（OcrModelsMissing）、rapidocr 没装……
            self._ocr_dead = True
            log.warning("OCR 用不了（%s），之后只读系统文字", type(exc).__name__)
            return None
        if self._engine is None:
            self._ocr_dead = True
        return self._engine

    # ----- 对外 -----

    def read_text(self, hwnd: int, frame, *, want_ocr: Callable[[TextResult], bool] | None = None,
                  use_uia: bool = True) -> TextResult:
        """frame 是 rec.frames.Frame（.bgr、.fmap）。从不抛异常。
        want_ocr：系统文字读完、要 OCR 之前问一下调用方（参数是到这一步的结果：url / page_title / window_title）；
        返回 False（这一拍反正不存：内部页、本地文件、拍完换了页面……）就不 OCR，结果 ocr_skipped=True。
        use_uia=False：不碰这个窗口的辅助功能树，只 OCR（读系统文字会卡住的软件）。"""
        self.stats["reads"] += 1
        try:
            return self._read(hwnd, frame, want_ocr, use_uia)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            log.warning("读字出错：%s", type(exc).__name__)
            return TextResult((), "none", status="error")

    def close(self) -> None:
        close = getattr(self._uia, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # ----- 内部 -----

    def _read_uia(self, hwnd: int):
        timeout_s = self.cfg.uia_timeout_ms / 1000.0
        try:
            res = self._uia.read(hwnd, timeout_s)
            if res.status in _UIA_SKIP:
                return res
            first = hwnd not in self._touched
            self._touched[hwnd] = None
            self._touched.move_to_end(hwnd)
            while len(self._touched) > 256:
                self._touched.popitem(last=False)
            if res.status != "timeout" and (getattr(res, "first_touch", False)
                                            or first and not (res.status in _UIA_OK and _usable(res))):
                # 对这个窗口的第一次读取：浏览器这时常常只给外框（或者第一次初始化 COM 时答"忙"），辅助功能树要几百毫秒
                # 才建好（实测 Edge 0.2 秒不够，第一拍就只能 OCR、也拿不到网址）。处理线程不怕多等：隔一会儿再读，
                # 直到读得出字、元素数也不再变多。只在第一次读这个窗口时等：以后读不出就是真没有字（图片、视频、
                # 自己画字的软件），交给 OCR。超时不重读：页面就是大，再读一次也是超时（网址已经单独拿到了）
                for _ in range(FIRST_TOUCH_TRIES):
                    time.sleep(FIRST_TOUCH_GAP_S)
                    res2 = self._uia.read(hwnd, timeout_s)
                    if res2.status == "timeout":
                        if res.status not in _UIA_OK:
                            res = res2
                        break
                    if res2.status not in _UIA_OK:
                        continue
                    grew = res.status not in _UIA_OK or res2.element_count > res.element_count
                    if grew or res2.element_count >= res.element_count:
                        res = res2
                    if _usable(res) and not grew:
                        break
            return res
        except Exception as exc:  # noqa: BLE001
            log.warning("系统文字读取出错：%s", type(exc).__name__)
            return None

    @staticmethod
    def _fmap(res, frame) -> FrameMap:
        """UIA 矩形是读字那一刻的屏幕坐标；窗口在截到这一帧之后被拖动过（大小没变）就按读字时的位置换算。"""
        fmap = getattr(frame, "fmap", None)
        if fmap is None:
            h, w = frame.bgr.shape[:2]
            fmap = FrameMap(0, 0, w, h)
        wr = getattr(res, "window_rect", None) if res is not None else None
        if wr is not None:
            bw, bh = round(fmap.width * fmap.scale_x), round(fmap.height * fmap.scale_y)
            same_size = abs((wr[2] - wr[0]) - bw) <= 3 and abs((wr[3] - wr[1]) - bh) <= 3
            if same_size and (wr[0], wr[1]) != (fmap.left, fmap.top):
                fmap = dataclasses.replace(fmap, left=int(wr[0]), top=int(wr[1]))
        return fmap

    def _ocr_top(self, hwnd: int, res, fmap: FrameMap) -> int:
        """帧里从第几行开始收 OCR 行：系统标题栏以下，UIA 认出了窗口上部界面条时从它下面开始。"""
        top = 0
        try:
            cap = uia_mod.caption_height(hwnd)
            if cap:
                top = int(cap / (fmap.scale_y or 1.0))
        except Exception:  # noqa: BLE001
            top = 0
        cb = getattr(res, "chrome_bottom", None) if res is not None else None
        if cb is not None:
            top = max(top, fmap.to_frame(fmap.left, cb)[1])
        return max(0, top)

    def _window_title(self, hwnd: int) -> str | None:
        try:
            if self._title_of is None:
                from .. import winapi

                self._title_of = winapi.window_title
            return self._title_of(hwnd) or ""
        except Exception:  # noqa: BLE001 - 取不到就不比
            return None

    def _read(self, hwnd: int, frame, want_ocr=None, use_uia: bool = True) -> TextResult:
        res = self._read_uia(hwnd) if use_uia else None
        uia_ok = res is not None and res.status in _UIA_OK
        if res is not None and res.status == "timeout":
            self.stats["uia_timeouts"] += 1
        uia_ms = float(getattr(res, "elapsed_ms", 0.0) or 0.0)
        fmap = self._fmap(res, frame)
        # 网址：正文读超时 / 出错时也有（读字前单独找的）；窗口不在、最小化时没有
        url = getattr(res, "doc_url", None) if res is not None and res.status not in _UIA_SKIP else None
        page_title = getattr(res, "doc_name", None) if url else None
        names = tuple(getattr(res, "window_names", None) or ()) if res is not None else ()
        window_title = self._window_title(hwnd)

        body: list[TextLine] = []
        uia_chars = 0
        if uia_ok:
            for ln in res.lines:
                uia_chars += len(norm(ln.text))
                if ln.role != "text":
                    continue
                r = fmap.rect_to_frame(*ln.rect)
                if r is None:
                    continue
                body.append(TextLine(ln.text, _box(*r), "uia"))
        body_chars = sum(len(norm(ln.text)) for ln in body)

        need_ocr = body_chars < self.cfg.ocr_min_chars or body_chars < self.cfg.ocr_min_body_ratio * uia_chars
        ocr_lines: list[TextLine] = []
        ocr_ran = False
        ocr_ms = 0.0
        ocr_skipped = False
        if need_ocr and want_ocr is not None:
            partial = ReadResult((), "none", url=url, page_title=page_title, window_title=window_title,
                                 window_names=names)
            if not want_ocr(partial):
                need_ocr, ocr_skipped = False, True
                self.stats["ocr_skipped"] += 1
        if need_ocr:
            engine = self._ocr()
            if engine is not None:
                t0 = time.perf_counter()
                try:
                    raw = engine.recognize(frame.bgr)
                    ocr_ran = True
                except Exception as exc:  # noqa: BLE001 - 这一帧没 OCR 上：照样交出系统文字
                    raw = []
                    self.stats["ocr_errors"] += 1
                    log.warning("OCR 出错：%s", type(exc).__name__)
                ocr_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                if ocr_ran:
                    self.stats["ocr"] += 1
                    self.stats["ocr_ms"] = round(self.stats["ocr_ms"] + ocr_ms, 1)
                    top = self._ocr_top(hwnd, res if url or uia_ok else None, fmap)  # 超时也有 Document 的上沿
                    fh, fw = fmap.height, fmap.width
                    for o in raw:
                        if (o.y0 + o.y1) / 2 < top:  # 标签页条 / 工具栏 / 标题栏里的字
                            continue
                        l, t = max(0.0, o.x0), max(0.0, o.y0)
                        r, b = min(float(fw), o.x1), min(float(fh), o.y1)
                        if r <= l or b <= t or not o.text.strip():
                            continue
                        if _hover_link(o.text.strip(), (l, t, r, b), fw, fh):
                            continue
                        ocr_lines.append(TextLine(o.text.strip(), _box(l, t, r, b), "ocr"))

        lines = merge_lines(body, ocr_lines)
        has_uia = any(ln.via == "uia" for ln in lines)
        has_ocr = any(ln.via == "ocr" for ln in lines)
        via = "uia+ocr" if has_uia and has_ocr else "uia" if has_uia else "ocr" if has_ocr else "none"
        if not uia_ok and not ocr_ran:
            status = "error"
            lines = []
            via = "none"
        else:
            status = "ok" if lines else "empty"
        return ReadResult(
            lines=tuple(lines), via=via, url=url,
            uia_chars=uia_chars, body_chars=body_chars, uia_ms=uia_ms, ocr_ms=ocr_ms, status=status,
            page_title=page_title, window_title=window_title, ocr_skipped=ocr_skipped, window_names=names,
            uia_timed_out=res is not None and res.status == "timeout",
        )

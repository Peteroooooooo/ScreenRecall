"""③ 记出处：这一拍看的是哪个东西（方案"记出处"一节），以及几条按网址 / 标题跳过的规则。全部是纯函数。

- 强标签：网址（系统文字里最大的 Document 元素的值，去掉 #片段 和 utm_* 参数）或本地文件的完整路径
  （file:/// 网址，或标题里写着完整路径）；标题用网页 / 文档自己的名字，文件没有名字时用文件名；
- 弱标签：软件名 + 窗口标题（去掉未读数 "(3)"、转圈符号）。
所有软件一套规则：这里不出现任何软件名，名单、关键词都由调用方从配置 / 库里传进来。
"""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from ..store.fts.docs import fold
from .types import KIND_APP, KIND_FILE, KIND_URL, SourceLabel

# 网址形状：协议至少两个字母（免得把盘符 C: 当成协议）+ 冒号
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):")


# ---------- 网址 ----------

def url_scheme(url: str | None) -> str:
    """小写协议名：'https' / 'file' / 'edge' / 'about' …；没有网址（或不像网址）时 ''。"""
    m = _SCHEME.match((url or "").strip())
    return m.group(1).lower() if m else ""


def url_host(url: str | None) -> str:
    """小写主机名（不含端口、用户名）；没有时 ''。"""
    if not url_scheme(url):
        return ""
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def clean_url(url: str) -> str:
    """去掉 #片段 和 utm_* 参数（其余参数原样、原顺序），协议和主机名小写。不像网址的原样返回（去首尾空白）。"""
    s = (url or "").strip()
    if not url_scheme(s):
        return s
    try:
        p = urlsplit(s)
    except ValueError:
        return s.split("#", 1)[0]
    netloc = p.netloc
    if netloc:
        user, at, host = netloc.rpartition("@")
        netloc = f"{user}{at}{host.lower()}"
    kept = [kv for kv in p.query.split("&") if kv and not kv.split("=", 1)[0].lower().startswith("utm_")]
    return urlunsplit((p.scheme.lower(), netloc, p.path, "&".join(kept), ""))


def _norm_path(path: str) -> str:
    """路径规范化：正斜杠换成反斜杠、盘符大写、去掉结尾的空白和句点。"""
    p = path.replace("/", "\\").rstrip(" .")
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        p = p[0].upper() + p[1:]
    return p


def _is_full_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:\\", p)) or bool(re.match(r"^\\\\[^\\]+\\[^\\]+", p))


def file_path_from_url(url: str) -> str | None:
    """file:/// 网址 → Windows 完整路径（百分号解码）：file:///C:/a%20b/x.pdf → C:\\a b\\x.pdf；
    file://server/share/x → \\\\server\\share\\x。不是 file 网址、或解不出完整路径时 None。"""
    if url_scheme(url) != "file":
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    path = unquote(p.path or "")
    host = (p.netloc or "").lower()
    if host and host != "localhost":
        full = "\\\\" + p.netloc + path  # UNC：主机名原样
    else:
        if re.match(r"^/[A-Za-z][:|]", path):
            path = path[1:]
        full = path
        if len(full) >= 2 and full[1] == "|":  # 很老的写法 file:///C|/x
            full = full[0] + ":" + full[2:]
    full = _norm_path(full)
    return full if _is_full_path(full) else None


# ---------- 标题 ----------

# 标题里的完整路径的起点：盘符 C:\ 或 UNC \\server\share\；往后取到不能出现在路径里的字符为止，
# 最后一节再在标题分隔符（" - " " — " " | "）处截断、截到最后一个扩展名
_PATH_CHAR = r'(?![A-Za-z]:\\)[^:*?"<>|\r\n\t]'  # 路径里能有的字符；遇到下一个盘符就停
_PATH_START = re.compile(rf'[A-Za-z]:\\(?:{_PATH_CHAR})*|\\\\[^\\/:*?"<>|\s]+\\[^\\/:*?"<>|\s]+\\(?:{_PATH_CHAR})*')
_TITLE_SEP = re.compile(r"\s+[-—–|·]\s+")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}(?![A-Za-z0-9])")
# 程序本身的路径（控制台窗口的标题默认就是 C:\WINDOWS\system32\cmd.exe 这类）：是在运行它，不是在看一个文件
PROGRAM_EXTS = frozenset({".exe", ".com", ".bat", ".cmd", ".ps1", ".msc"})


def path_in_title(title: str) -> str | None:
    """标题里写着的文件完整路径（盘符路径 C:\\…\\x.ext 或 UNC \\\\server\\share\\…\\x.ext），有几段时取最长的；
    最后一节要有扩展名（是文件，不是标题里顺带的文件夹），并且不是程序（PROGRAM_EXTS：终端标题里的 cmd.exe 这类）。
    没有时 None。"""
    best = None
    for m in _PATH_START.finditer(title or ""):
        s = m.group(0)
        cut = s.rfind("\\") + 1
        head, last = s[:cut], s[cut:]
        sep = _TITLE_SEP.search(last)
        if sep:
            last = last[:sep.start()]
        exts = list(_EXT.finditer(last))
        if not exts or exts[-1].group(0).lower() in PROGRAM_EXTS:
            continue
        p = _norm_path(head + last[:exts[-1].end()])
        if _is_full_path(p) and (best is None or len(p) > len(best)):
            best = p
    return best


# 转圈 / 加载中的符号：盲文点阵（⠋⠙⠹…）、四分圆（◐◓◑◒ ◴◵◶◷ ◰◱◲◳）、沙漏
_SPINNER = re.compile("[\u2800-\u28ff\u25d0-\u25d3\u25f0-\u25f7\u231b\u23f3]")
# 未读数：(3) [12] （3） 【99+】，在开头 / 结尾，或紧挨着标题分隔符（"Chat (3) | App"、"App - (3) Chat"）；
# 词中间、文件名里的（"report (3).pdf"）不动
_COUNT = r"[\(\[（【]\s*\d+\+?\s*[\)\]）】]"
_LEAD_COUNT = re.compile(rf"^\s*{_COUNT}\s*")
_TAIL_COUNT = re.compile(rf"\s*{_COUNT}\s*$")
_COUNT_BEFORE_SEP = re.compile(rf"\s+{_COUNT}(?=\s+[-—–|·]\s)")
_COUNT_AFTER_SEP = re.compile(rf"(\s[-—–|·])\s+{_COUNT}(?=\s)")


def clean_title(title: str) -> str:
    """去掉转圈符号和未读数（开头 / 结尾 / 挨着标题分隔符的），空白合并成一个空格。"""
    s = _SPINNER.sub("", title or "")
    while True:
        t = _TAIL_COUNT.sub("", _LEAD_COUNT.sub("", s))
        t = _COUNT_AFTER_SEP.sub(r"\1", _COUNT_BEFORE_SEP.sub("", t))
        if t == s:
            break
        s = t
    return " ".join(s.split())


# ---------- 出处 ----------

def _file_key(path: str) -> str:
    """文件出处的 key：规范化路径再整个小写（Windows 路径不分大小写，标题里和网址里的写法可能不同）。"""
    return path.lower()


def _file_name(path: str | None) -> str:
    """完整路径的最后一节（文件名）；没有时 ''。"""
    return (path or "").rstrip("\\").rpartition("\\")[2]


def label_source(app: str, title: str, url: str | None, page_title: str | None = None) -> SourceLabel:
    """这一拍的出处：http(s) 网址 → url；file:/// 网址或标题里有完整路径 → file；否则 app_title。
    标题：网址出处用 page_title（网址所在 Document 的名字，网页自己的标题）；文件出处用 page_title，没有时用文件名
    （浏览器的 PDF 查看器不给 Document 名字）。都没有才退回清理后的窗口标题。免得窗口标题里浏览器 / 编辑器加的
    "- 个人 - Microsoft Edge"这类尾巴跟进时间线和搜索结果。"""
    t = clean_title(title)
    page = clean_title(page_title or "")
    scheme = url_scheme(url)
    if scheme in ("http", "https"):
        u = clean_url(url)
        return SourceLabel(KIND_URL, u, app, page or t, url=u)
    if scheme == "file":
        u = clean_url(url)
        path = file_path_from_url(url)
        return SourceLabel(KIND_FILE, _file_key(path or u), app, page or _file_name(path) or t, url=u, path=path)
    path = path_in_title(title)
    if path:
        return SourceLabel(KIND_FILE, _file_key(path), app, _file_name(path) or t, path=path)
    return SourceLabel(KIND_APP, f"{(app or '').lower()}\t{t}", app, t)


# ---------- 跳过 ----------

def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    s = fold(text or "")
    return any(k and k.strip() and fold(k.strip()) in s for k in keywords)


def is_private_title(title: str, keywords: Sequence[str]) -> bool:
    """无痕窗口：标题里有任一关键词（不分大小写、全半角）。"""
    return _contains_any(title, keywords)


def title_excluded(title: str, keywords: Sequence[str]) -> bool:
    """你设的不录窗口（excludes kind='title'）：标题里有任一关键词（不分大小写）。"""
    return _contains_any(title, keywords)


def _host_of(value: str) -> str:
    """不录网站的写法可以是主机名，也可以是整个网址：都取小写主机名，去掉开头的 "." 和 "*."。"""
    v = (value or "").strip()
    if "://" in v:
        v = url_host(v)
    else:
        v = v.split("/", 1)[0].split(":", 1)[0]
    return v.lower().lstrip("*").lstrip(".")


def url_skip_reason(url: str | None, *, schemes: Sequence[str], own_origins: Sequence[str],
                    excluded_hosts: Sequence[str], internal_prefixes: Sequence[str] = ()) -> str | None:
    """按网址跳过的原因：'internal'（协议不在 schemes 里：edge:// chrome:// about: …；或以 internal_prefixes
    里任一个开头：浏览器新标签页实际打开的 https 页面）、'own'（ScreenRecall 自己的网页：以 own_origins 里任一个开头）、
    'excluded'（主机名等于或是不录网站的子域）；不跳过（或没有网址）时 None。"""
    scheme = url_scheme(url)
    if not scheme:
        return None
    if scheme not in {s.lower() for s in schemes}:
        return "internal"
    low = url.strip().lower()
    if any(p and low.startswith(p.strip().lower()) for p in internal_prefixes):
        return "internal"
    for o in own_origins:
        o = (o or "").strip().lower().rstrip("/")
        if o and low.startswith(o) and (len(low) == len(o) or low[len(o)] in "/?#"):
            return "own"
    host = url_host(url)
    if host:
        for h in excluded_hosts:
            h = _host_of(h)
            if h and (host == h or host.endswith("." + h)):
                return "excluded"
    return None

"""v3 ③ 记出处：网址清理、文件路径、标题清理、强 / 弱标签，以及按标题 / 网址跳过的规则（纯函数）。"""

from __future__ import annotations

from screenrecall.v3 import source as S
from screenrecall.v3.types import KIND_APP, KIND_FILE, KIND_URL


def test_clean_url_strips_fragment_and_utm_keeps_other_params():
    u = "HTTPS://Example.TEST/Path/A?b=2&utm_source=x&a=1&UTM_Medium=y&utm_campaign=z#section-3"
    assert S.clean_url(u) == "https://example.test/Path/A?b=2&a=1"
    assert S.clean_url("https://example.test/p?utm_source=x#top") == "https://example.test/p"
    assert S.clean_url("https://User@Host.TEST:8443/x") == "https://User@host.test:8443/x"
    assert S.clean_url("  https://example.test/q?x=%20y  ") == "https://example.test/q?x=%20y"
    assert S.clean_url("file:///C:/a%20b/x.pdf#page=2") == "file:///C:/a%20b/x.pdf"
    assert S.clean_url("about:blank") == "about:blank"
    assert S.clean_url("不是网址") == "不是网址"


def test_scheme_and_host():
    assert S.url_scheme("HTTPS://a.test/") == "https"
    assert S.url_scheme("edge://settings") == "edge"
    assert S.url_scheme("about:blank") == "about"
    assert S.url_scheme(None) == "" and S.url_scheme("") == ""
    assert S.url_scheme("C:\\Users\\x.txt") == ""  # 盘符不是协议
    assert S.url_host("https://Sub.Example.TEST:8080/x") == "sub.example.test"
    assert S.url_host("about:blank") == "" and S.url_host(None) == ""


def test_file_path_from_url():
    assert S.file_path_from_url("file:///C:/a%20b/x.pdf") == "C:\\a b\\x.pdf"
    assert S.file_path_from_url("file:///d:/%E5%90%88%E6%88%90/%E6%96%87%E4%BB%B6.txt") == "D:\\合成\\文件.txt"
    assert S.file_path_from_url("file://server/share/dir/x.docx") == "\\\\server\\share\\dir\\x.docx"
    assert S.file_path_from_url("file://localhost/C:/x.txt") == "C:\\x.txt"
    assert S.file_path_from_url("file:///synthetic/page.html") is None  # 没有盘符：不是完整路径
    assert S.file_path_from_url("https://a.test/x.pdf") is None


def test_path_in_title():
    assert S.path_in_title("C:\\Users\\合成\\Docs\\report v1.2.docx - 合成编辑器") == "C:\\Users\\合成\\Docs\\report v1.2.docx"
    assert S.path_in_title("d:\\proj\\main.py — 合成 IDE") == "D:\\proj\\main.py"
    assert S.path_in_title("\\\\server\\share\\dir\\x.pdf | 合成阅读器") == "\\\\server\\share\\dir\\x.pdf"
    # 两段路径取最长的
    assert S.path_in_title("C:\\a.txt 和 D:\\longer\\path\\b.txt") == "D:\\longer\\path\\b.txt"
    assert S.path_in_title("C:\\Users\\合成 - 文件夹") is None  # 文件夹，不是文件
    assert S.path_in_title("合成文章标题 - 浏览器") is None
    assert S.path_in_title("") is None
    # 程序本身的路径（控制台窗口默认标题）：是在运行它，不是在看文件
    assert S.path_in_title(r"C:\WINDOWS\system32\cmd.exe") is None
    assert S.path_in_title(r"管理员: C:\Program Files\PowerShell\7\pwsh.exe") is None
    assert S.path_in_title(r"C:\WINDOWS\system32\cmd.exe - python x.py") is None
    assert S.label_source("WindowsTerminal.exe", r"C:\WINDOWS\system32\cmd.exe", None).kind == KIND_APP


def test_clean_title_unread_counts_and_spinners():
    assert S.clean_title("(3) 合成收件箱 - 邮件") == "合成收件箱 - 邮件"
    assert S.clean_title("[12] 合成频道") == "合成频道"
    assert S.clean_title("（3）合成群聊") == "合成群聊"
    assert S.clean_title("合成群聊 (99+)") == "合成群聊"
    assert S.clean_title("⠋ (2) 合成页面加载中") == "合成页面加载中"
    assert S.clean_title("◐ 合成  页面 ◓") == "合成 页面"
    assert S.clean_title("合成 (3) 中间的数字留着") == "合成 (3) 中间的数字留着"
    # 挨着标题分隔符的未读数也去掉；文件名里的不动
    assert S.clean_title("合成聊天 (3) | 合成软件") == "合成聊天 | 合成软件"
    assert S.clean_title("收件箱 [12] - 合成邮件") == "收件箱 - 合成邮件"
    assert S.clean_title("合成软件 - (5) 群聊") == "合成软件 - 群聊"
    assert S.clean_title("report (3).pdf - 合成阅读器") == "report (3).pdf - 合成阅读器"
    assert S.clean_title("  ") == ""


def test_label_source_kinds_and_keys():
    a = S.label_source("msedge.exe", "(2) 合成文章 - 浏览器", "https://Example.TEST/a?utm_source=x#p")
    assert a.kind == KIND_URL and a.strong and a.key == "https://example.test/a" and a.url == a.key
    assert a.title == "合成文章 - 浏览器" and a.path is None
    f = S.label_source("msedge.exe", "x.pdf", "file:///C:/Docs/x.pdf")
    assert f.kind == KIND_FILE and f.strong and f.path == "C:\\Docs\\x.pdf" and f.key == "c:\\docs\\x.pdf"
    assert f.url == "file:///C:/Docs/x.pdf"
    t = S.label_source("Editor.exe", "c:\\Docs\\X.PDF - 合成编辑器", None)
    assert t.kind == KIND_FILE and t.path == "C:\\Docs\\X.PDF" and t.key == f.key  # 同一个文件，同一个 key
    w = S.label_source("Chat.EXE", "⠙ (5) 合成群聊", None)
    assert w.kind == KIND_APP and not w.strong and w.key == "chat.exe\t合成群聊" and w.title == "合成群聊"
    # 内部页 / 不像网址的值：按"软件 + 标题"
    assert S.label_source("msedge.exe", "新标签页", "about:blank").kind == KIND_APP


def test_private_and_excluded_titles():
    kw = ("InPrivate", "无痕", "隐身", "Incognito", "Private Browsing")
    assert S.is_private_title("合成页面 - [inprivate] - 浏览器", kw)
    assert S.is_private_title("合成页面 - 无痕浏览", kw)
    assert S.is_private_title("合成新标签页 — PRIVATE BROWSING", kw)
    assert not S.is_private_title("合成文章 - 浏览器", kw)
    assert S.title_excluded("合成网银 - 浏览器", ["网银"]) and not S.title_excluded("合成文章", ["网银", "", "  "])


def test_url_skip_reason():
    kw = dict(schemes=("http", "https", "file"), own_origins=("http://127.0.0.1:8611", "http://localhost:8611"),
              excluded_hosts=("bank.test", "https://Mail.Example.TEST/inbox"))
    for u in ("edge://settings", "chrome://newtab/", "about:blank", "chrome-extension://abc/x.html"):
        assert S.url_skip_reason(u, **kw) == "internal", u
    assert S.url_skip_reason("http://127.0.0.1:8611/search?q=x", **kw) == "own"
    assert S.url_skip_reason("http://localhost:8611", **kw) == "own"
    assert S.url_skip_reason("http://127.0.0.1:86110/", **kw) is None  # 端口只是前缀相同
    assert S.url_skip_reason("https://www.bank.test/login", **kw) == "excluded"
    assert S.url_skip_reason("https://bank.test/", **kw) == "excluded"
    assert S.url_skip_reason("https://mail.example.test/x", **kw) == "excluded"
    assert S.url_skip_reason("https://notbank.test/", **kw) is None
    assert S.url_skip_reason("https://article.test/a", **kw) is None
    assert S.url_skip_reason("file:///C:/x.pdf", **kw) is None
    assert S.url_skip_reason(None, **kw) is None


def test_url_label_prefers_page_title():
    """有网址时用网页自己的标题（Document 的名字），没有时退回清理后的窗口标题；弱标签不受影响。"""
    win = "合成文章 (3) - 个人 - Microsoft\u200b Edge"
    lab = S.label_source("msedge.exe", win, "https://a.test/x", "合成文章")
    assert lab.title == "合成文章"
    assert S.label_source("msedge.exe", win, "https://a.test/x", None).title == S.clean_title(win)
    assert S.label_source("msedge.exe", "x.pdf - 浏览器", "file:///C:/x.pdf", "x.pdf").title == "x.pdf"
    assert S.label_source("chat.exe", "群聊", None, "不会用到").title == "群聊"


def test_internal_prefixes_skip_new_tab_pages():
    kw = dict(schemes=("http", "https", "file"), own_origins=(), excluded_hosts=())
    pre = ("https://ntp.msn.com/edge/ntp",)
    assert S.url_skip_reason("https://NTP.msn.com/edge/ntp?locale=zh-CN", internal_prefixes=pre, **kw) == "internal"
    assert S.url_skip_reason("https://ntp.msn.com/other", internal_prefixes=pre, **kw) is None
    assert S.url_skip_reason("https://ntp.msn.com/edge/ntp", **kw) is None  # 没给前缀：照常

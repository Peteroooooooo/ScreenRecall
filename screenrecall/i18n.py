"""网页界面的英文模式：把后台各模块写给用户看的中文提示翻成英文（python -m screenrecall.webapi 在 lang=en 时调用）。

各模块的提示仍以中文为准、不改动；这里按整句（精确匹配或带参数的正则）对照翻译，
没有收录的句子原样返回中文，不做机器翻译，也不碰 OCR 文字、窗口标题等用户内容。
"""
from __future__ import annotations

import re
from typing import Callable

GAP_EN = {
    "屏幕锁定": "screen locked", "窗口最小化": "window minimized", "窗口隐藏": "window hidden",
    "取不到窗口内容": "window content unavailable", "窗口尺寸为空": "empty window size",
    "无法读取窗口位置": "window position unreadable", "截图调用失败": "capture call failed", "截图出错": "capture error",
    "命中排除规则": "matched an exclusion rule", "画面有变化但未保存": "screen changed but not saved",
}

EXACT = {
    # 检索（search.py / retrieval.py）
    "引号没有成对，请补全后再搜索。": "Unbalanced quotes. Close the quote and search again.",
    "放宽匹配：忽略大小写和全部空白（含 OCR 行之间的换行），所有引号原串仍须同时包含。放宽可能改变代码含义，请回原图确认。":
        "Relaxed match: ignores case and all whitespace (including line breaks between OCR lines); every quoted string "
        "must still appear. Relaxing can change the meaning of code, so check against the original image.",
    "没有画面同时包含全部引号原串。": "No screen contains all of the quoted strings.",
    "引号原串去掉空白后为空，无法放宽匹配。": "The quoted strings are empty after removing whitespace, so they cannot be relaxed.",
    "没有找到匹配的记录。": "No matching records.",
    "有匹配的文字，但不在当前日期或应用筛选范围内。": "Matching text exists, but not within the current date or app filter.",
    "当前日期或应用筛选范围内没有记录。": "No records within the current date or app filter.",
    "放宽匹配后仍没有符合条件的记录。": "Still no matching records after relaxing.",
    "请输入要查找的文字。": "Enter some text to search for.",
    "请输入要查找的文字或画面描述。": "Enter text or a description of the screen.",
    "只有引号原串：按捕获时间展示，画面模式只列原图仍可用的记录。":
        "Quoted strings only: shown by capture time; screen mode lists only records whose image is still available.",
    "当前日期、应用或引号条件下没有候选画面。": "No candidate screens for the current date, app or quoted strings.",
    "原图已过期或缺失的记录不参与画面模式。": "Records whose image expired or is missing are excluded from screen mode.",
    "没有可用于画面排序的候选（全图向量可能尚未完成）。": "No candidates can be ranked by screen (image vectors may not be ready yet).",
    "文本模型未加载": "text model not loaded", "画面模型未加载": "image model not loaded",
    "配置中已关闭向量模型（models.enabled = false）": "vector models are disabled in config (models.enabled = false)",
    "词法": "lexical", "语义": "semantic", "画面": "screen",
    # 证据浏览（evidence.py）
    "仍在录制": "still recording", "录制段未正常结束（后台可能异常退出）": "segment did not end cleanly (the daemon may have crashed)",
    "已结束": "ended", "用户暂停": "paused by user", "切换了录制窗口": "switched recording window",
    "目标窗口失效": "target window became invalid", "目标窗口命中排除规则": "target window matched an exclusion rule",
    "目标窗口已关闭": "target window closed", "存储空间不足，已暂停": "out of storage, paused", "后台退出": "daemon exited",
    "导入批次": "import batch", "补拍": "manual", "导入": "import", "自动": "auto",
    # 录制控制（control.py / capture.py / daemon.py / hotkey.py）
    "后台已启动，录制默认关闭": "Daemon started; recording is off by default", "后台已退出": "Daemon has exited",
    "屏幕已锁定，暂不采集": "Screen locked; not capturing", "目标窗口已最小化，暂不采集": "Target window minimized; not capturing",
    "目标窗口已隐藏，暂不采集": "Target window hidden; not capturing",
    "取不到窗口内容（可能是硬件加速窗口且不在前台）": "Window content unavailable (possibly a hardware-accelerated window not in front)",
    "窗口尺寸为空": "Empty window size", "无法读取窗口位置": "Cannot read window position", "截图调用失败": "Capture call failed",
    "截图出错": "Capture error", "窗口命中排除规则，暂不采集": "Window matched an exclusion rule; not capturing",
    "画面正在变化，等稳定后保存": "Screen is changing; will save once stable",
    "排错限频中，稍后保存最新画面": "Debug rate limit; the latest screen will be saved shortly",
    "目标窗口不存在或已变化，请重新选择": "Target window no longer exists or has changed; select it again",
    "已按请求暂停": "Paused as requested", "已按请求回到普通模式": "Back to normal mode as requested",
    "存储空间已恢复，可以重新开启录制": "Storage recovered; recording can be started again", "采样出错": "Sampling error",
    "目标窗口已关闭，录制已自动暂停": "Target window closed; recording paused automatically",
    "未在录制，补拍没有执行": "Not recording; snapshot not taken",
    "已补拍：与已保存画面相同，记为一次新的出现": "Snapshot taken: same as a saved screen, recorded as a new occurrence",
    "补拍没有执行：内容已失效，请重试": "Snapshot not taken: content became stale, try again",
    "已补拍：新画面已保存，等待 OCR": "Snapshot taken: new screen saved, waiting for OCR",
    "补拍没有执行：录制状态已变化": "Snapshot not taken: recording state changed",
    "未配置快捷键，可在页面点击补拍": "No hotkey configured; use the snapshot button",
    # 生命周期 / 导入 / 重试
    "确认后这幅画面又有新的收藏，请重新确认删除范围": "This screen gained a new favorite after you confirmed; review the deletion again",
    "确认后范围内又有新的收藏，请重新确认删除范围": "New favorites appeared in the range after you confirmed; review the deletion again",
    "不支持的文件类型，只接收 PNG、JPEG、WebP": "Unsupported file type; only PNG, JPEG and WebP are accepted",
    "文件为空": "File is empty", "动图不支持，只接收静态图片": "Animated images are not supported",
    "本批次之前已导入": "Already imported in this batch earlier", "磁盘剩余空间不足 2 GiB，未写入": "Less than 2 GiB of free disk space; not written",
    "原图文件缺失": "image file missing", "原图已到期清理": "image expired and was cleaned up", "画面已删除": "screen deleted",
    "画面已更新，旧待办作废": "screen was updated; the old job is obsolete",
    "文字识别（OCR）": "Text recognition (OCR)", "语义向量": "Text vectors", "全图向量": "Image vectors",
    # webapi.py 自己的提示
    "页面只在本机访问（127.0.0.1），没有登录鉴权。请只录制受控的测试内容。":
        "This page is local-only (127.0.0.1) with no login. Record controlled test content only.",
    "请求已发送，等待后台确认": "Request sent; waiting for the daemon to confirm", "补拍请求已发送": "Snapshot request sent",
    "后台未运行，请求没有发送。": "The daemon is not running; request not sent.", "请先选择要录制的窗口": "Select a window to record first",
    "这个窗口已经关闭或不可录制，请刷新窗口列表": "That window is closed or cannot be recorded; refresh the window list",
    "未在录制，模式没有切换": "Not recording; mode not changed", "未在录制，补拍没有发送。": "Not recording; snapshot not sent.",
    "这条记录已不存在。": "This record no longer exists.", "这条记录已不存在，无法收藏。": "This record no longer exists and cannot be favorited.",
    "已收藏": "Added to favorites", "已取消收藏": "Removed from favorites", "这幅画面已被删除。": "This screen has been deleted.",
    "请先选择日期": "Select a date first", "结束时间必须晚于开始时间。": "End time must be later than start time.",
    "请先选择截图文件": "Choose screenshot files first", "原图已不可用": "Image unavailable",
    "画面编号不合法": "Invalid screen id", "数量只能是 10、20 或 50": "Count must be 10, 20 or 50",
    "数据库正忙（后台正在写入），请稍后重试": "Database busy (the daemon is writing); try again shortly",
    "请求体不是合法的 UTF-8 JSON": "Request body is not valid UTF-8 JSON",
    "请求体应为 JSON 对象": "Request body must be a JSON object", "参数应为列表": "Parameter must be a list",
    "参数不合法": "Invalid parameter", "mode 应为 text 或 image": "mode must be text or image",
    "semantic_lines 应为整数列表": "semantic_lines must be a list of integers",
    "找不到这个接口": "No such endpoint", "不支持这种请求方法": "Method not allowed",
    "只接受本机地址访问": "Only local addresses are accepted", "缺少 X-ScreenRecall 请求头": "Missing X-ScreenRecall header",
    "不接受其他网页发来的修改请求": "Changes sent from other web pages are not accepted",
    "上传的表单字段太多或太大": "The upload form has too many or too large fields",
    "上传内容不是合法的表单（multipart/form-data）": "The upload is not a valid multipart/form-data form",
}


def _gaps(m: re.Match) -> str:
    parts = []
    for p in m[1].split("、"):
        label, _, n = p.partition("×")
        parts.append(GAP_EN.get(label, label) + (f" ×{n}" if n else ""))
    return "known gaps in between: " + ", ".join(parts) + ". Screens in this period were not saved; do not treat it as continuous footage."


def _counted(s: str) -> str:
    """"原因 数量、原因 数量" → 逐项翻译原因。"""
    parts = []
    for p in s.split("、"):
        label, _, n = p.rpartition(" ")
        parts.append(f"{to_en(label)} {n}" if label else to_en(p))
    return ", ".join(parts)


PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], str]]] = [(re.compile(p), f) for p, f in [
    (r"^中间有已知缺口：(.+)。这段时间的画面没有保存，不能当成连续录像。$", _gaps),
    (r"^语义分支不可用：(.+)；当前只按词法排序。$", lambda m: f"Semantic branch unavailable: {to_en(m[1])}; ranking by lexical only."),
    (r"^(\d+) 个候选画面的语义向量尚未完成，暂时只参与词法排序。$",
     lambda m: f"{m[1]} candidate screens have no text vectors yet and are ranked by lexical only for now."),
    (r"^(\d+) 个候选画面的全图向量尚未完成，本次未参与画面排序。$",
     lambda m: f"{m[1]} candidate screens have no image vectors yet and were not ranked by screen this time."),
    (r"^画面模式暂不可用：(.+)。不会自动切换到文字模式。$",
     lambda m: f"Screen mode is unavailable: {to_en(m[1])}. It will not switch to text mode automatically."),
    (r"^模型未下载（(.+)），请运行 python -m screenrecall\.models --download$",
     lambda m: f"models not downloaded ({m[1]}); run python -m screenrecall.models --download"),
    (r"^模型加载失败：(.+)$", lambda m: f"model failed to load: {m[1]}"),
    (r"^程序 (.+) 在排除列表中$", lambda m: f"Program {m[1]} is on the exclusion list"),
    (r"^窗口标题含排除关键词「(.+)」$", lambda m: f"Window title contains the excluded keyword “{m[1]}”"),
    (r"^程序 (.+) 不在允许列表中$", lambda m: f"Program {m[1]} is not on the allow list"),
    (r"^(.+)，不能录制。$", lambda m: f"{to_en(m[1])}; cannot record."),
    (r"^(.+)，未开始录制$", lambda m: f"{to_en(m[1])}; recording not started"),
    (r"^允许：(.+)$", lambda m: "Allowed: " + ("any" if m[1] == "不限制" else m[1].replace("、", ", "))),
    (r"^排除程序：(.+)$", lambda m: "Excluded programs: " + ("none" if m[1] == "无" else m[1].replace("、", ", "))),
    (r"^排除标题关键词：(.+)$", lambda m: "Excluded title keywords: " + ("none" if m[1] == "无" else m[1].replace("、", ", "))),
    (r"^排错模式已满 (.+) 分钟，已自动回到普通模式$", lambda m: f"Debug mode reached {m[1]} minutes; back to normal mode"),
    (r"^OCR 待办达到 (\d+)，排错模式已提前回到普通模式$", lambda m: f"OCR backlog reached {m[1]}; left debug mode early"),
    (r"^补拍没有执行：(.+)$", lambda m: f"Snapshot not taken: {to_en(m[1])}"),
    (r"^快捷键 (.+) 注册失败（可能已被其他程序占用），可在页面点击补拍$",
     lambda m: f"Hotkey {m[1]} failed to register (possibly taken by another program); use the snapshot button"),
    (r"^快捷键 (.+) 可随时补拍$", lambda m: f"Press {m[1]} to take a snapshot any time"),
    (r"^快捷键配置无效（(.+)），可在页面点击补拍$", lambda m: f"Invalid hotkey ({m[1]}); use the snapshot button"),
    (r"^与本批次第 (\d+) 个文件内容相同$", lambda m: f"Same content as file #{m[1]} in this batch"),
    (r"^文件超过 (\d+) MiB$", lambda m: f"File larger than {m[1]} MiB"),
    (r"^实际格式为 (.+)，只接收 PNG、JPEG、WebP$", lambda m: f"Actual format is {m[1]}; only PNG, JPEG and WebP are accepted"),
    (r"^尺寸 (.+) 不在允许范围（(.+)）$", lambda m: f"Size {m[1]} is out of range ({m[2]})"),
    (r"^无法解码：(.+)$", lambda m: f"Cannot decode: {m[1]}"),
    (r"^原图状态 (.+)$", lambda m: f"image status {m[1]}"),
    (r"^存储空间不足，已暂停采集：", lambda m: "Out of storage; capture paused. Remove some favorites, delete records "
                                       "or raise the quota, then start recording again."),
    (r"^已删除 (\d+) 次出现记录(?:、(\d+) 个收藏)?，画面内容已一并删除。$",
     lambda m: f"Deleted {m[1]} occurrence(s)" + (f" and {m[2]} favorite(s)" if m[2] else "") + "; the screen content was deleted too."),
    (r"^已删除 (\d+) 次出现记录(?:、(\d+) 个收藏)?，画面内容仍被其他记录引用，已保留。$",
     lambda m: f"Deleted {m[1]} occurrence(s)" + (f" and {m[2]} favorite(s)" if m[2] else "")
     + "; the screen content is still referenced by other records and was kept."),
    (r"^已删除这幅画面的 (\d+) 次出现记录(?:和 (\d+) 个收藏)?。$",
     lambda m: f"Deleted {m[1]} occurrence(s) of this screen" + (f" and {m[2]} favorite(s)" if m[2] else "") + "."),
    (r"^已按范围删除 (\d+) 次出现记录、(\d+) 幅画面(?:、(\d+) 个收藏)?。$",
     lambda m: f"Deleted {m[1]} occurrence(s) and {m[2]} screen(s) in range" + (f", plus {m[3]} favorite(s)" if m[3] else "") + "."),
    (r"^(.+)。删除范围已更新。$", lambda m: f"{to_en(m[1])}. The deletion scope has been updated."),
    (r"^(.+)。请重新预览。$", lambda m: f"{to_en(m[1])}. Preview again."),
    (r"^已改回待处理 (\d+) 条，后台会自动重新处理。(?:未改：(.+)。)?$",
     lambda m: f"Re-queued {m[1]} job(s); the daemon will process them again."
     + (f" Unchanged: {_counted(m[2])}." if m[2] else "")),
    (r"^数据库结构是 v(\d+)，代码需要 v(\d+)。请重启后台完成升级。$",
     lambda m: f"Database schema is v{m[1]} but the code needs v{m[2]}. Restart the daemon to upgrade."),
    (r"^数据库结构是 v(\d+)，比代码支持的 v(\d+) 新，请更新代码。$",
     lambda m: f"Database schema v{m[1]} is newer than the v{m[2]} this code supports. Update the code."),
    (r"^数据库读写出错：(.+)。请确认后台已运行并完成升级，详情见网页服务的日志。$",
     lambda m: f"Database error: {m[1]}. Make sure the daemon is running and has upgraded the database; "
               "see the web server log for details."),
    (r"^服务器内部错误（(.+)），详情见网页服务的日志。$",
     lambda m: f"Internal server error ({m[1]}); see the web server log for details."),
    (r"^一次最多导入 (\d+) 个文件，请分批上传$", lambda m: f"At most {m[1]} files per import; upload in smaller batches"),
    (r"^日期超出范围：(.+)$", lambda m: f"Date out of range: {m[1]}"),
    (r"^还没有找到数据库：(.+)。请先运行后台：python -m screenrecall\.daemon$",
     lambda m: f"Database not found: {m[1]}. Start the daemon first: python -m screenrecall.daemon"),
    (r"^日期格式不对：(.+)（应为 YYYY-MM-DD）$", lambda m: f"Invalid date: {m[1]} (expected YYYY-MM-DD)"),
    (r"^时间不合法：(.+)$", lambda m: f"Invalid time: {m[1]}"),
    (r"^无法列出窗口：(.+)$", lambda m: f"Cannot list windows: {m[1]}"),
    (r"^未知操作：(.+)$", lambda m: f"Unknown action: {m[1]}"),
]]


def to_en(text: str) -> str:
    if text in EXACT:
        return EXACT[text]
    for pat, fn in PATTERNS:
        m = pat.match(text)
        if m:
            return fn(m)
    return text


# 只翻译这些字段里的字符串（以及字符串列表）：都是系统写给用户的提示，不含 OCR 文字、窗口标题等内容
KEYS = frozenset({
    "error", "message", "notes", "relax_rule", "sources", "gaps_before", "gaps_after", "end_label", "source_label",
    "reason", "why_not", "state_reason", "mode_reason", "capture_note", "snapshot_result", "hotkey_status", "rules",
    "name", "stage_name", "db_problem", "notice", "problem",
})


def localize(data, lang: str):
    """lang 为 en 时把 KEYS 字段翻成英文；其他语言原样返回。"""
    if lang != "en":
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in KEYS and isinstance(v, str):
                out[k] = to_en(v)
            elif k in KEYS and isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = [to_en(x) for x in v]
            else:
                out[k] = localize(v, lang)
        return out
    if isinstance(data, list):
        return [localize(v, lang) for v in data]
    return data

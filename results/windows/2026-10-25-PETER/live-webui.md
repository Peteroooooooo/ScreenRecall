# Windows 实测：2026-10-25，PETER — 网页界面录制控制与真实后台联调

补上 10-23"网页界面替换 Streamlit"里没做的"录制控制与真实后台联调"，以及"待验证"1、2、4、5。
全部在独立数据目录 `data-dev`（不进 git）上做：后台 `python -m screenrecall.daemon --data-dir data-dev`，网页服务 `python -m screenrecall.webapi --data-dir data-dev --port 8611`。
`data-dev/config.toml` 只写了 `[capture] debug_max_minutes = 1`。录制对象是临时写的 tkinter 小窗口（640×300，标题 `SR-LiveTest A`，显示合成文字，不抢焦点），没有录任何真实应用。查询都是自拟的合成词（如 `KX-5520`、"潜水艇什么时候出发"），没有用 `datasets/v1/queries.json` 里的查询。

## 机器

| 项 | 值 |
| --- | --- |
| 型号 | ASUS 台式机 |
| CPU | Intel Core i5-12600KF，10 核 / 16 线程 |
| 内存 | 32 GB（系统可用 31.8 GiB） |
| GPU | NVIDIA RTX 4070 Ti SUPER（本项目只用 CPU） |
| 系统 | Windows 11 专业工作站版 中文版 10.0.26200 |
| 屏幕 | 3840×2160，缩放 150%（AppliedDPI 144） |
| 软件 | 仓库 `.venv`，Python 3.12.13，rapidocr-onnxruntime 1.4.4；Edge 156.0.4314.42；代码 `feat/polish`（开始时 `f59ae7e`，结束时 HEAD `24b0222`，工作区有其他人未提交的改动） |
| 页面 | API 用脚本直接请求（带 `X-ScreenRecall: 1` 与 `Origin`）；另在 Chrome 里打开 8611 的录制页点了一遍按钮（`web/dist` 是主检出里的构建产物） |

## 结论速览

- 7 项都通过：开始 / 暂停 / ACK、录到的画面可按原词和语义搜到、重启后默认不录、排错 1 分钟自动退出与手动退出、页面补拍与快捷键补拍、快捷键被占用的提示、真实 Edge InPrivate 拒绝录制、后台离线时 409、失败待办列出与重试。
- 没有发现阻断问题。几处小问题见文末"发现的问题"。

## 逐项结果

| # | 项目 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1a | 窗口列表 | 通过 | `/api/windows` 列出 `SR-LiveTest A`（python.exe，644×347，`allowed: true`）。 |
| 1b | 开始 → ACK | 通过 | `POST /api/control {"action":"start","hwnd":…}` → 200"请求已发送，等待后台确认"；约 1 秒后 `/api/status`：`pending false`、`actual_state recording`、`target_title SR-LiveTest A`。立即抓拍第一帧（`method=print_window dpi=144`）。 |
| 1c | 文字变化 → 保存 / OCR / 向量 | 通过 | 20:58:04 改窗口文字；20:58:04.768 `gap start unsaved_change`，20:58:07.791 等稳后保存；OCR 0.8 s 完成（3 行）；20:58:10 文本 / 全图向量完成。改文字到三项都完成约 6 秒。 |
| 1d | 原词搜索 | 通过 | `"KX-5520"`（带引号）→ total 1，就是新画面，排序依据"最新匹配时间"。 |
| 1e | 语义搜索 | 通过 | "潜水艇什么时候出发" → total 2，排序依据 RRF，第一名是含"旱獭潜水艇计划"的画面；网页服务第一次语义查询含加载模型共 11 s。英文"submarine plan tomorrow morning"也把它排第一，结果框都是语义块（蓝色）。 |
| 1f | 暂停 → 不再存图 | 通过 | `pause` → 状态 `paused`、"已按请求暂停"；之后改窗口文字等 12 s，`frames` 仍 2 条。暂停时 `snapshot` / `debug` → 409"未在录制，补拍没有发送。" / "未在录制，模式没有切换"；未知操作 → 400。 |
| 1g | 重启后默认不录 | 通过 | 正常退出（CTRL_BREAK，2.5 s 退出，日志"后台已退出"）和强制结束两种情况后重启，`/api/status` 都是 `desired_state paused`、`actual_state paused`、"后台已启动，录制默认关闭"；重启后改窗口文字等 8 s 没有新画面。 |
| 1h | 浏览器里点按钮 | 通过 | Chrome 打开 `#/recorder`：选窗口 → "Start recording" → 显示 Recording、python.exe · SR-LiveTest A；"Debug" → 倒计时；"Exit debug mode"；"Snapshot now"；"Pause"，后台日志依次是 seq 10–13 和 `snapshot seq=2`。 |
| 2a | 进入排错 + 倒计时 | 通过 | `debug` → `actual_mode debug`、`mode_until_ms` = 请求时间 + 60 s；页面显示"back to normal in 00:57"和"Exit debug mode"按钮。排错时每改一次文字（间隔 4 s）都按 `mode=debug` 保存。 |
| 2b | 1 分钟自动退出 | 通过 | 21:02:05 进入，21:03:05.186 日志"排错模式已满 1 分钟，已自动回到普通模式"；状态 `actual_mode normal`，`mode_reason` 同一句，仍在录制。 |
| 2c | 手动退出 | 通过 | 再进入排错后 `normal` → 2 s 内 `actual_mode normal`，"已按请求回到普通模式"。浏览器里的"Exit debug mode"按钮也一样。 |
| 3a | 页面补拍 | 通过 | `snapshot` → "补拍请求已发送"；`snapshot_result`"已补拍：与已保存画面相同，记为一次新的出现"，新增 1 条 `source_type = manual` 的出现记录。 |
| 3b | 快捷键 Ctrl+Alt+S | 通过 | 快捷键没被占用时，用 `keybd_event` 模拟按键：画面没变时复用画面，新增一条 manual 出现记录（`snapshot seq=None`）；改文字后 0.8 s 再按，新画面以 `trigger=manual` 保存，结果"已补拍：新画面已保存，等待 OCR"。 |
| 4 | 快捷键被占用 | 通过 | 后台启动前，辅助进程先用 `RegisterHotKey` 占了 Ctrl+Alt+S。后台启动日志 `WARNING [hotkey] 快捷键 Ctrl+Alt+S 注册失败（可能已被其他程序占用），可在页面点击补拍`，`/api/status.hotkey_status` 是同一句，可以直接显示给用户。这时按快捷键只有辅助进程收到 `WM_HOTKEY`，后台不补拍；页面补拍照常工作。释放后重启后台 → "快捷键 Ctrl+Alt+S 可随时补拍"。后台运行期间不会重试注册，要重启才能拿到快捷键。 |
| 5 | 真实 Edge InPrivate | 通过 | `msedge --inprivate --user-data-dir=<临时目录>` 打开 about:blank，窗口标题 `about:blank - [InPrivate] - Microsoft​ Edge`。`/api/windows` 标记 `allowed: false`，原因"窗口标题含排除关键词「InPrivate」"；`start` → 409"窗口标题含排除关键词「InPrivate」，不能录制。"。另外绕过页面直接写控制表请求录这个窗口，后台也拒绝：`paused`"…，未开始录制"，没有存图。测完关闭该 Edge 实例并删除临时配置目录。 |
| 6 | 后台离线 | 通过 | 正常退出后 `/api/status` 是 `alive false`、`actual_state stopped`、"后台已退出"。强制结束后 `actual_state` 仍是 `recording`，但心跳超过 5 s 后 `alive false`，页面按 `alive` 显示"后台未运行或没有响应，已保存的内容仍可搜索"。这时 `start` / `pause` → 409"后台未运行，请求没有发送。"，`snapshot` / `debug` / `normal` → 409"未在录制…"；浏览器里点"Start recording"弹出"The daemon is not running; request not sent."。 |
| 7 | 失败待办与重试 | 通过 | 后台停止时用 `/api/import` 导入 3 张合成图，然后删掉 A 的图片文件、把 B 的文件内容换成垃圾数据。启动后台：A 在启动核查时标记缺失（`image_missing`，不可重试）；B 的 OCR 按退避重试 3 次后 `failed`（`image_decode`），全图向量待办也失败 3 次。`/api/failed` 列出 2 条 OCR 失败（可重试 1、不可重试 1，原因"原图文件缺失"），`index.failed 2`、`embed_failed 1`。把 B 的原文件放回后 `POST /api/failed/retry` → "已改回待处理 2 条，后台会自动重新处理。未改：原图文件缺失 1。"；约 15 s 内 B 的 OCR、文本、全图向量都完成，`"IMG-B"` 能搜到；A 保持不变。与 `retry.py` 和 PROGRESS.md 的说明一致。 |

## 发现的问题（都不阻断）

1. **排错按钮文字写死"30 分钟"**：`web/src/i18n.tsx:182`（`排错 30 分钟`）、`:444`（`Debug 30 min`）。配置 `debug_max_minutes = 1` 时按钮仍写 30 分钟，倒计时显示的是正确的 1 分钟。建议按钮不写时长，或者从状态 / 配置读。
2. **"开始录制"按钮在已经录制和后台离线时都能点**：点了不会出错（录制中会再发一次开始请求，离线时返回 409 并提示），但和"补拍"、"排错"不一致，那两个按钮在这种情况下是禁用的。
3. **后台被强制结束后，状态里还是 `actual_state = recording`**（`control.on_daemon_stop` 没执行），直到下次后台启动才改。页面和 `api_control` 都先看心跳（`alive`），实际没有影响；直接读 `control` 字段的工具要注意。这次被强制结束的录制段重启后仍是 `ended_at_ms = NULL`，证据页显示"录制段未正常结束（后台可能异常退出）"，符合设计。
4. **图片缺失的画面，向量状态一直是 `pending`**：画面 A 在 `frames` 里 `ocr_status failed`、`image_status missing`，但 `text_embed_status` / `image_embed_status` 仍是 `pending`，而且没有对应的待办。`index_stats` 按待办计数，所以积压显示 0，没有可见影响。
5. 测试窗口的长行超出了窗口宽度，OCR 只认出了窗口里看得见的部分（如"…校准紫外："）。这是测试窗口太窄，不是产品问题，也不影响上面的结论。

## 未覆盖

- 只用了 tkinter 窗口和一个 about:blank 的 InPrivate 窗口，真实应用的录制节奏（"待验证"3）没有测。
- 排错模式因积压 ≥ 50 提前退出没有测（积压很难安全地堆到 50）。
- 只看了录制页英文界面的文字，没有看排版截图；仓库里不放截图。

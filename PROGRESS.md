# 进度与交接

依据《ScreenMemory_Brain_详细实现方案_独立版》（下称"方案"）。首期运行目标是 **Windows 10/11**；开发机可以是 macOS 或 Windows（见"开发环境"）。

协作方式：两人**轮流接力**开发整个项目，不按方案 8.1 的旧分工划范围。每轮结束更新本文件的"交接记录"和"下一步"，下一位从这里接着做。

## 2026-10-28 v3（分支 `feat/recall-v3`，2026-10-29 合入 `main`）

下面各轮里写的"未提交"是当时的状态，之后都已提交；最新一轮是本节最后的"第一次真实使用 + 修复轮"。

方案：`ScreenRecall/docs/walkthrough-v3.html`；分工与接口：`V3_CONTRACT.md`。一句话：能读成字的就存字，读不成字的就存截图。

做了什么
- 新包 `screenrecall/v3/`：录制子进程（loop / trigger / pipeline / control / clipboard）、读字（UIA 正文 + 不够时 OCR）、出处（网址 / 文件 / 软件 + 标题）、截图策略、数据层（`recall-v3.sqlite3`，每行字全库只存一份）、`/api/v3` 网页接口、空闲整理（删过期截图、可选大模型起名 / 每小时一句话、可选向量）、MCP 查询（加了 `copied` 工具）、托盘控制。前端改成 时间线 / 条目 / 查找 / 名单 / 设置 五页。
- 主进程 `app/main.py` 接到 v3：子进程 `screenrecall.v3.recorder` / `screenrecall.v3.worker`，默认数据目录 `<仓库>/data-v3`；`config.default.toml` 换成一节 `[v3]`（删了 `[v2.*]`）。vite 开发代理改到 8611。
- 删掉 v2：`screenrecall/v2/`、`rec/core.py`、`rec/tracker.py`、`store/{aliases,bench,delete,gate,queries,schema,writer}.py`、`text/clipbatch.py` 及只测它们的测试；`tests/test_v2_stdin_eof.py` 改名 `test_v3_stdin_eof.py`。
- `tools/agent_eval.py` 删了：它靠 v2 demo 的 30 天合成数据和问题集打分，v3 的 demo 只有 15 分钟，没有问题集可移植。`experiments/` 里引用 v2 的脚本只作记录，不再能运行。
- 集成时改的小问题：托盘在录制进程没运行时也显示"已暂停"（`v3/controller.py`）；读字第一次碰到浏览器窗口时多等辅助功能树建好（最多约 1.5 秒），不然第一拍只能 OCR、拿不到网址（`v3/read.py`，实测 Edge 0.2 秒不够）。

怎么跑：见 README 的"v3"一节。

测了什么
- `pytest tests`：654 passed（真实软件修复轮之后）；前端 `npm run typecheck`、`npx vitest run`（37）、`npm run build` 通过。
- 端到端（只用自己开的窗口：临时 profile 的 Edge + 自己起的 `http.server` 页面，录制进程用 `SCREENRECALL_TEST_ONLY_PIDS` 只录它）：
  - v3.demo 临时库 + `--no-children`：时间线 4 项、搜索有结果、首页 200、MCP 6 个工具。
  - 后台录（Edge 放在"一直录"、压在别的窗口下面、全程 CDP 驱动，不抢前台不发键；跑时 Edge 在前台 0/112 次采样）：停下 2 秒后拍（滚动后第一拍 2.1 秒）；网址出处去掉了 `#` 和 `utm_*`，回到同一篇合进同一个出处；全库 `norm` 重复 0；强标签第一张图 1 张；ScreenRecall 自己的页面跳过；搜一句话命中、截图上有黄框；网页 API 和 MCP 都通。录制进程 CPU 约 17% 单核（1% 全机）、内存约 400 MB；库 + 图约 1.25 MB / 分钟（一直在翻页时，含 OCR）。
  - 前台录（较早一轮，把自己的窗口切到前台）：复制记下了出处（网址）；切到不在名单的 Tk 窗口记了一行灰字；无痕窗口什么都没存。
- 没测 / 还有问题
  - 打字跳过：真实窗口上已测过（10-28 前台测试员，SendInput 往自己的 Edge 发键）：最后一个键后 5.6 秒才拍、Ctrl+V 后 2.6 秒、PageDown 后 2.5 秒照常拍。早先"没被识别为打字"是因为那次用的是 CDP `Input.dispatchKeyEvent`，它直接进渲染进程、不经过 Windows 输入，Raw Input 看不到，不是代码问题。方向键、Ctrl+C 复制、切到不在名单的软件出灰字、锁屏、走开扣时间、30 天过期、硬盘不足、大模型起名只有单元测试（用户一直在用电脑，前台测试抢不到焦点）。
  - ~~后台模式下 `edge://version` 读不到网址被当成"软件 + 标题"存了~~：已修（评审修复轮）。"带网址的软件"（默认名单 + 库里出过网址出处的软件）这一拍读不到网址就不存（`skip_no_url`）；复测：内部页 0 个出处、`skip_no_url` 1 次。
  - ~~后台条目 dwell 一直是 0~~：已修，后台条目的"看了多久" = 第一拍到最后一拍的跨度，页面显示"后台 · N 分钟"。
  - 同一句被 UIA 和 OCR 各存一份：已加近似去重（框重叠且相似度 ≥ 0.85 只留 UIA 那份），单元测试覆盖；复测那轮没有触发 OCR，真实窗口上没验证到。
- 评审修复轮（2026-10-28 晚）另改：读不出字的新画面只算"和上次看这个出处时比变了、像图（没有一大片底色）、又没字盖住"的部分（回到看过的页面、往回翻都不再留 fallback 图）；本地文件只留 1 张；控制台标题里的 `cmd.exe` 等程序路径不当文件；标题分隔符旁的未读数去掉；走开后锁屏 / 换前台时先扣走开时间；隐私删除后录制进程丢掉指向已删条目的状态（meta `delete_rev`），编号列改 AUTOINCREMENT 不重用；时间线超过上限留最近的；缩略图缓存按文件路径、图不在就 404；按网址跳过的页面标题不进心跳；输入监测只注册键盘；删了 `text/uia.py` 的区域 OCR 辅助函数、`store/fts/docs.py` 的跨行 tail 代码。复测（自己开的 Edge 放在"一直录"、压在下面、CDP 驱动，前台采样 0/152）：2 个网址出处各 1 张 first 图、没有 fallback 图、行 norm 重复 0、后台条目 2 条看了多久都 > 0。打字跳过仍没在真实窗口上测（CDP 派发的按键不经过系统输入，Raw Input 看不到；要测得往前台窗口发 SendInput，会抢用户焦点）。

### 真实软件测试 + 修复轮（2026-10-28 深夜 → 10-29）

五个测试员在真实网站（维基中英、docs.python.org、Reddit、V2EX、知乎、贴吧、Bing、B 站、YouTube、本地 PDF / HTML）、用户开着的微信和 QQ（只看不点）上跑了 v3，之后一个修复员逐条核对代码、改掉真问题、复测。

修了什么（都是通用规则或数字，没有按软件名写分支）
- **最大的问题：浏览器页面几乎都不存**。原来的 UIA 上限 250 ms 在后台 Edge 上读维基、文档页、视频页每次都超时，超时连网址也丢了，于是整拍按 `skip_no_url` 扔掉（测试员 1 一轮 60 拍扔 57 拍），而且 OCR 已经白跑了。改成：读整棵树之前先用一次 `FindAll(Document)` 单独找网址（后台 Edge 实测 40–260 ms，取面积最大、面积 > 0 的那个：Edge 树里排在前面的可能是藏起来的 `edge://` 小对话框），整棵树超时网址照样有，正文交给 OCR；Document 的上沿当标签页条 / 地址栏的下沿，OCR 不收界面条里的字。`uia_timeout_ms` 改成 1500（实测维基整棵树 1–2 秒、视频页 0.7 秒；docs.python.org 7 千个节点 3 秒也读不完，就走"网址 + OCR"）。超时不再重读（`text/uia.py`、`v3/read.py`）。
- **反正不存的拍不 OCR**：要 OCR 之前先问处理线程（`read_text(want_ocr=…)`），内部页、读不到网址、本地文件（不存字）、拍完换了页面的拍不 OCR（`ocr_skipped`）。
- **截图和字对不上**（a.html 的图其实是 Reddit）：读完系统文字再取一次窗口标题，和拍的时候不一样就扔掉这一拍（`skip_stale_page`，不算"这个页面要跳过"），下一拍新页面停下来再拍。
- **新标签页被当网页存了**：Edge 新标签页的 Document 是 `https://ntp.msn.com/edge/ntp…`。`[v3]` 加了一份数据 `internal_url_prefixes`，以它们开头也算内部页。
- **QQ NT 的页面被当成浏览器内部页**：它的 Document 值是 `app://…`。不在"带网址的软件"里的软件，Document 值不是 http(s) 时不当网址（`app://`、程序目录里的 `file:///…/index.html`），按 软件 + 标题 记、照常存字；出过 http(s) 网址后那个软件才算带网址。
- **弱标签截图太多**（两个聊天窗口 25 分钟 23 张几乎一样的图）：原来每次切前台 / 后台都把这一段的候选图存一张。改成一条时间线条目一张：第一次有新字当场存，之后新字更多的那拍**替换**它（`save_shot(replaces=…)`，旧图的行、框、文件一起删），一直不走每 `weak_visit_ms` 另存一张。顺带解决"10 分钟里搜到了但没有图"和"进程被杀候选图丢了"。
- **同一个东西前台、后台两条并排**：`visit_enter` 不再按 background 分开找。后台条目到了前台就变成前台条目（看了多久从 0 算），切回后台合进同一条、不打开（后台那段不算看了多久）。
- **暂停时后台条目一直开着、把暂停算进时长**：暂停 / 锁屏开始、窗口到了前台、收尾时后台条目在那一刻离开；时间线上开着的后台条目不再把结束时间拉到现在。
- 网页出处的名字用网页自己的标题（Document 的名字），时间线、搜索里不再带" - 个人 - Microsoft Edge"。
- OCR 行里只有时间 / 日期的（"14:05"、"昨天 12:30"）、和这一拍里别的行开头一截一样的（列表里截断的预览）不要。
- 名单里加 "Notepad" 这类不带扩展名的进程名时补成 `notepad.exe`；顶栏前台没在录、后台在录时显示"后台在录 N 个窗口"（`/status` 加 `background`）；手机宽度上"最后一次拍"换到第二行不被截掉；结束了的暂停显示"暂停了（未录）"；大图里命中的行不在图上时说明"网页和文件只留第一次看时的那一屏"。
- 录制进程每分钟的统计里加了读字器的次数：`read.ocr / ocr_ms / ocr_skipped / uia_timeouts`、`ocr_ms_avg`。
- `tests/test_app_mcp.py` 的 stdio 端到端测试用 "today" 查 50 分钟前的合成数据，半夜 0:00–0:50 必挂，改成 "2h ago"。

没改（核对后不算问题，或要改方案）
- OCR 比方案写的 0.7 秒慢：实测 0.7 秒（微信 1080 宽）到 1.5–1.9 秒（1900 宽的浏览器整帧、机器上别的软件在跑）。按实测记，没加缩小再识别的机制。
- 视频页每 10 秒强制拍一次：现在网址先拿到、视频页的系统文字够（YouTube 1.8 千字、B 站 2.3 千字）就不 OCR，单拍成本是一次 UIA（约 0.7 秒），没再加退避机制。
- 什么都没存时库文件也在长：是每 2 秒心跳写的 WAL，检查点后主文件不长；状态"在录"要靠心跳，不改。
- 微信 / QQ 主窗口标题一直是"微信" / "QQ"，所有聊天合成一个出处：方案的弱标签就是 软件 + 标题，又不许按软件写规则，记为已知限制（可选的大模型起名可以给每条起名）。
- 长文命中的行常常不在截图上：方案就是一个网址 / 文件只留第一次那张，页面上加了说明。
- 后台条目"看了多久"：后台本来就不是在看，页面写"后台 · 跨度"，今天总时长不算它；静止的后台页面只拍一次，跨度近 0（没有新帧就没有证据它还在屏幕上）。

复测（修复员，只用 `--only-pids` 隔离、临时数据目录，跑完删掉）
- A：自己开的 Edge（临时访客 profile，`--disable-features=CalculateNativeWinOcclusion`），放在"一直录"、压在别的窗口下面、CDP 驱动，前台采样 0/480；默认配置，9 分钟，11 个页面 + 一次 40 秒暂停：
  - 出处 10 个（9 网址 + 1 本地 PDF），行 1188（UIA 824、OCR 364），`norm` 重复 0，每个出处 1 张 first 图，PDF 0 行；`utm_` 和 `#` 去掉了；新标签页跳过（`skip_internal` 1）；`skip_no_url` 0；docs.python.org 这次存了 142 行（UIA 超时 → 网址 + OCR）；离开 5 分钟以上回来另开一条；暂停那条后台条目在暂停时离开、回来合进同一条（跨度 106 秒、算 64 秒）。看了一张文档页的图：框都落在字上。
  - 资源：CPU 平均 7.6% 单核（全机 0.48%），p95 35.5%；内存平均 244 MB、最高 511 MB；库 0.71 MB、图 2.35 MB（10 张，平均 241 KB）≈ 每 10 分钟库 0.8 MB、图 2.6 MB；OCR 8 次、平均 1485 ms；UIA 超时 8 / 28 次；读字平均 1353 ms。
- B：用户开着的微信（QQ 这次没有可见的主窗口，没录到），"一直录"、后台，8 分钟：18 拍全部 OCR（平均 693 ms），新行 159、见过的 850，1 条条目 1 张图（之前同样的场景 25 分钟 23 张）；CPU 平均 6.3% 单核（0.4% 全机）、内存平均 174 MB、最高 362 MB；库 0.23 MB、图 0.18 MB。

还有的问题
- **后台（被挡住的）浏览器读不到网页文字**：Chromium 的窗口遮挡检测会把被别的窗口完全挡住的 Edge 网页藏起来，辅助功能树里就没有页面了（只剩 `edge://` 的小对话框，按内部页跳过），帧也几乎不来。复测 A 不加 `CalculateNativeWinOcclusion` 那个开关时，前 3 页存了、后面全部跳过。用户正常在前台看浏览器不受影响；"一直录"本来是给会议软件用的，浏览器放这一档、又被挡住时等于没录。记为已知限制，没找到不按软件写规则的办法。
- ~~本地 PDF 的出处名字还是窗口标题~~：已修（见下面"补测 + 修复轮"）。
- 前台测试（方向键、Ctrl+C 复制记出处、切到不在名单的软件出灰字、走开扣时间）还没跑成，见下面"补测 + 修复轮"。锁屏用户说不用测。
- 测试顺带的事：修复员前两次复测里新开的临时 profile 被 Edge 自动登录成了 Windows 账号（弹出同步确认框），其中一次 Edge 拿到了前台（约 6 分钟，没有发任何键鼠）；之后改用访客模式、并且一拿到前台就交还。临时 profile 都已删除。

### 补测 + 修复轮（2026-10-29 凌晨，提交 `cc38ffc` 之后，未提交）

前台测试员：7 项前台检查都没跑成（只有"状态栏"测了一半：主进程 `--no-children` 时 `/api/v3/status` 200、state=stopped；`POST /api/v3/apps` 加微信 / QQ 后名单 8 个）。原因是用户一直在用电脑：约 150 秒的采样里键鼠空闲最长 10.9 秒，前台在 Chrome / OneNote / VS Code 之间换；规则是空闲 60 秒以上才发键，所以一个键都没发、没抢前台、没碰微信 / QQ / VS Code，剪贴板没写过。锁屏按用户说的不测。

screenpipe 对照（读的是 screenpipe/screenpipe main 分支源码）：方向键 / 翻页键 / Home / End / Ctrl 组合键不算打字，Shift+字母、回车、Tab、退格算打字，这几条两边一样；我们 Alt / Win 组合键也不算（更严）；空格我们当翻页；Esc / F 键 / Delete 我们算打字（后果只是晚 5 秒拍，不会漏拍）。screenpipe 停 0.3 秒就拍、最多每 1.5 秒一张；我们 5 秒内打过字不拍、停了只拍最后一张，这是方案"看完再拍、少存重复"有意的选择。**打字规则不改。**

修了什么
- 本地文件出处的名字：有 Document 名字用它（本地 HTML 是网页自己的 `<title>`），没有时用文件名（Edge 的 PDF 查看器不给名字），标题里写着完整路径的（编辑器）也只用文件名；窗口标题里浏览器 / 编辑器加的" - 个人 - Microsoft Edge"这类尾巴不再进时间线和搜索（`v3/source.py`，单元测试 `test_file_label_uses_document_name_or_file_name`）。
- MCP `timeline` 半夜查不到刚才的：`time_from="2h ago"` 不写结束时间时，结束时间取"开始那天结束"，0:00–2:00 之间查等于只查到昨天 24 点，0 点以后的都漏掉（凌晨 1:06 跑全套测试时 `test_shim_end_to_end` 挂了才发现）。改成开始是相对时间（"2h ago"、"30 分钟前"）时到今天结束；写了哪天的（"yesterday 14:00"、"today"）还是到那天结束（`app/mcp_server.py`，单元测试固定在 0:30）。

测了什么
- `pytest tests`：656 passed；前端 `npm run typecheck`、`npx vitest run`（37）、`npm run build` 通过。
- 真实录制进程复测出处名字（`--only-pids` 只录自己开的 Edge：临时访客 profile、`--disable-features=CalculateNativeWinOcclusion`、"一直录"、`--no-ocr`、75 秒；先开本地 PDF，30 秒后用 CDP `Page.navigate` 换到本地 HTML；没发任何键鼠）：2 个文件出处，名字分别是文件名"合成 报告.pdf"（中文 + 空格的 `file:///` 网址解码正确）和网页标题"SRFIX 本地网页"，都没有浏览器尾巴；各 1 张 first 图、0 行字（本地文件不存字）、`ocr_skipped` 1。临时目录和自己开的 Edge 已删掉 / 关掉。
- 顺带的事：用"最小化、不激活"启动的 Edge 还是拿到了前台（启动那一刻），之后只是放到最底层、没有交还焦点；跑的 75 秒里前台采样 3/151 次是它，期间没有发任何键鼠。以后起测试 Edge 要在用户确定不在时，或者接受启动那一下抢前台。

还剩的
- 前台检查（方向键 / 翻页不算打字、真打字后 5 秒才拍、Ctrl+C 复制记出处、微信 / QQ 里 Ctrl+C 和滚动、不在名单的软件出灰字 + "以后录它"、5 分钟内回来合并 / 超过另开、走开 5 分钟以上不算"看了多久"、状态栏在录 / 最后一次拍 / 暂停 5 分钟）仍要用户真的走开 15 分钟以上再测，或用户自己手动试。
- 被挡住的后台浏览器读不到网页、微信 / QQ 标题固定：同上一轮，记为已知限制。

### 前台测试 + 修复轮（2026-10-29 凌晨 02:00 之后，未提交）

前台测试员（用户空闲时，自己开的访客 Edge + charmap，SendInput，`away_ms` / `merge_ms` 临时缩到 60 秒）：7 项里 6 项通过，微信 / QQ 没跑。
- 通过：字母打字 5 秒后才拍（Edge 4.92 s、charmap 4.86 s）；方向键 / PageDown / Home / End / Ctrl+A / Ctrl+V 都约 2 秒就拍，不算打字；Ctrl+C 立刻拍一张 copy、记下复制（网址出处），`/api/v3/copied` 和 MCP `copied` 都查得到；不在名单的 charmap 切进去 17 ms 出灰字，`POST /api/v3/apps` 改成录以后下次切进去就拍、灰字不再显示按钮；"看了多久" = 切进来到切走，60 秒内回来合成一条（3 段 41.2 s，对照 38.9 s），超过另开一条；走开 90 秒那段不算进去（96.3 s 前台只记 6.1 s）；暂停 / 继续正常。
- 没跑：微信、QQ 都只在托盘里、没有可见窗口，按规则不去打开。
- 资源（约 100 秒采样）：录制进程平均 36% 单核、峰值 226%（OCR 多线程），内存平均 269 MB、峰值 587 MB；主进程 0.2%、79 MB。4.8 分钟存了 3.3 MB 库 + 5 张图 0.93 MB。

修了什么
- 查找栏（Ctrl+F）拿着焦点时整拍丢掉（两次都 `skip_no_url`=1，粘贴后那页 20 秒没存）：浏览器这一拍读不到网址，但同一窗口上一拍存下的就是这个标题的网址出处时，沿用那个网址（照样过内部页 / 自己的网页 / 不录网站的判断，计数 `url_reused`）；标题变了、上一拍被跳过、不是网址出处时不沿用（`v3/pipeline.py` `_known_url`，单元测试 `test_url_missing_once_mid_visit_reuses_last_url`、`test_reused_url_still_checked_against_excluded_hosts`）。没查清 UIA 在查找栏打开时具体返回了什么，只按"读不到"处理。
- 点"继续"后的响应还写着 paused：`Queries.status` 之前把录制端心跳里的 `paused` 也当暂停，心跳要 1–2 秒才更新。录制端的暂停只由 `paused_until_ms` 决定，所以状态改为只看它（`v3/store/queries.py`，`test_status_recorder_alive_and_states` 加了这一步）。真实主进程 + 录制进程复测：暂停 → paused，3 秒后 paused，`POST /resume` 响应 recording，3 秒后 recording。
- 打字时也在学"一直在变的小块"（代码检查发现，测试里没漏拍）：`SnapTrigger.feed` 加 `typing`，打字时不学；`loop.py` 先算打字再取帧，只有前台窗口传 typing（`test_trigger_does_not_learn_volatile_while_typing`，带对照）。

测了什么
- `pytest tests`：665 passed；前端 `npm run typecheck`、`npx vitest run`（37）、`npm run build` 通过。
- 真实主进程 + 录制进程（临时目录，`--only-pids` 一个不存在的进程号，不录任何窗口、没发键鼠）：上面的暂停 / 继续复测，录制进程退出码 0、日志 0 错误；临时目录已删。

还剩的
- 查找栏那一条只有单元测试，没在真窗口里再按 Ctrl+F 复测（要抢前台、等用户空闲）。
- 微信 / QQ 前台复制和滚动：要用户先把窗口打开放着，再在空闲时测。
- 真的走开 5 分钟（不缩短 `away_ms`）没测；逻辑和 60 秒版一样。

### 微信 / QQ 前台测试 + 修复轮（2026-10-29，`423b353` 之后，未提交）

测试员（用户打开了微信和 QQ 的窗口；只做前置、滚轮、气泡里拖选、Ctrl+C）：8 项里 7 项通过。
- 通过：微信（弱标签"微信"，全 OCR）和 QQ（弱标签"QQ"，UIA 为主）滚动每停一次都有新字、见过的去重，一条只留新字最多的那 1 张；Ctrl+C 记下复制（API / MCP `copied` 都有）；5 分钟内微信 → QQ → 微信合成一条，"看了多久"对得上；搜存过的词能回到条目、有图有黄框。录制进程平均 24% 单核、内存平均 208 MB，OCR 约 0.7–0.8 秒一次，0 错误。
- 没过：真 Edge 里 Ctrl+F 仍然 `skip_no_url`、`url_reused` 从没出现。

修了什么
- 查找栏：Edge / Chrome 的查找栏是自己一个顶层窗口（无标题、所有者是主窗口、同进程），`GetForegroundWindow` 返回它，拍的窗口和标题都成了这个小窗，`_known_url` 按窗口 + 标题对不上。通用规则 `rec/windows.py` `popup_owner`：前台是无标题、有所有者（`GA_ROOTOWNER`，`rec/win32.py` `root_owner`）、同进程、所有者能录的小窗时，算主窗口在前台（截图、标题、出处都按主窗口）；有自己标题的对话框、别的进程的小窗、主窗口最小化时照旧。`RealWindows` 和 `FakeWindows`（`add(…, owner=)`）都走这条。测试 `test_popup_owner_rule`、`test_find_bar_popup_keeps_main_window_in_front`（查找栏前置后没有新的 FocusEvent，照常拍主窗口）。
- 重启后聊天窗口多出 fallback 图（同一库重开录制进程，滚回看过的消息，微信 / QQ 各多 1 张）：没有上一拍的小块指纹可比时整幅都算新，头像 / 表情被当成新照片。现在没有指纹、而这一屏的字全都存过（新 0、见过 > 0）时这一拍只当参照、不算"像图"面积；之后真变了照样 fallback，读不出字的画面第一次见照旧算新（`v3/pipeline.py`，`test_no_fallback_on_first_sight_when_all_text_seen`）。代价：重启后第一拍正好是"字都见过 + 新图"时这张图不留，要等下一次画面变化。

没改的
- 长时间没碰电脑后，程序前置窗口（SetForegroundWindow 不算输入）要等第一次输入才拍（微信 6.5 秒）：走开状态本来就不算在看，真人切窗口（点击、Alt+Tab）就是输入。以后的前台测试先发一个无害输入或预期这段延迟。

测了什么
- `pytest tests`：668 passed（新增 3 个）；没改前端。只读检查了真桌面上的 `root_owner` / `RealWindows.foreground`（普通窗口映射到自己）。
- 查找栏这条没在真 Edge 里按 Ctrl+F 复测。

### 第一次真实使用 + 修复轮（2026-10-29 下午，`21d27ec` … `55890ba`，已推送并合入 `main`）

用户在自己的电脑上实际用了一下午（数据目录 `sr-v2\data-v3`），按反馈逐条改。规则都是通用的，没有按软件名写分支。

改了什么
- **VS Code 被卡住**（`9c6cdaa`）：读系统文字（UIA）要在被读软件自己的界面线程上逐项交出，VS Code 界面元素多，读一次就卡。现在某个软件读系统文字超时一次，以后它只截图认字（记在 meta `uia_slow_apps`，重启也记得）。浏览器不记，因为网址要靠 UIA，而且找网址很快。
- **应用名单页**（`21d27ec`、`15e9bc4`、`be94b30`）：可以从"现在开着的软件"里直接选；显示软件真实的图标和名字（从 exe 读，`PrivateExtractIconsW`）。exe 的查找顺序是：开着的窗口 → 进程列表（托盘里没有窗口的微信也能找到）→ App Paths → 已安装程序的 DisplayIcon。图标走 `/api/v3/apps/icon`，缓存 1 天；找不到的 60 秒后再试。改档位不再让行跳位置（按加入顺序排）。Windows 自带程序不出现在可选列表里。
- **第一次用的人更容易上手**（`16e7b2f`）：欢迎步骤；灰色行折叠；每个软件一个开关；Agent 页显示连接状态、一键接入；开发者设置折起来；默认中文。
- **时间线**（`9ae6e45`、`ad28213`）：最新的在上面（小时块倒序，块内一小时一句话在最上、条目新的在前）。时间线、条目、查找、复制记录都显示软件图标和真实名字（API 里每个带 `app` 的对象加 `app_name`、`app_icon`），窗口标题末尾的" - 软件名"去掉。
- **读错标签页**（`02c9348`）：Chrome 把切走的标签页的 Document 留在辅助功能树里，大小一样、不标 offscreen、排在前面，原来"取面积最大的"会拿到它，于是正在看的页面被记到别的标签页的网址下（用户搜 QQ 出来 New API）。现在取名字和窗口标题开头对得上的 Document，其他顶层网页 Document 里的字不收。
- **Codex 被拦**（`55890ba`）：用户装的一个 Codex 插件在问题里有"只回答"时，按工具名里有没有 list / get / read / search 这类动词判断只读，不看 readOnlyHint。MCP 工具改名：`timeline` → `list_timeline`，`summary` → `get_summary`，`copied` → `list_copied`（`search`、`read`、`get_frame` 不变）。改名后的 MCP 客户端要重开才认新名字。
- **界面零碎字**（`55890ba`）：网页链接（Hyperlink）的值是它指向的网址，屏幕上没画出来，不再存；OCR 里整行只是一个网址、在窗口最下面、从左半边开始的（浏览器鼠标停在链接上时左下角弹出的网址）丢掉。

数据清理（用户的真实库，改了数据）
- 修"读错标签页"之前（10-29 15:59:30 到 16:47:38）Chrome 的记录是混的：好几个标签页的字挂在错的网页名下。删了这段 Chrome 的记录，以及测试页面（127.0.0.1:8698）整个出处：7 条时间线条目、46 次拍摄、3 张截图、994 行字（全文索引一起删）、2 个出处。8 条复制记录保留内容、软件和标题，只把指向已删条目的链接置空。`delete_rev` 加 1。
- 先在副本上演练；第一次没开 `PRAGMA foreign_keys=ON` 留下了坏关联，修正后重做，再在真库上执行。执行后 integrity ok、外键检查无问题。
- 备份：整库和删掉的 3 张截图在 `data-v3\cleanup-2026-10-29-wrong-tab\`（`data-*/` 不进 git）。

测了什么
- `pytest tests`：681 passed；前端 `npm run typecheck`、`npx vitest run`（42）、`npm run build` 通过。
- 用本机 Claude Code（claude-opus-5-5-thinking）和 Codex（gpt-6.1-sol）按真实数据自己出题测：Claude Code 5 题全对（含一道陷阱题）。Codex 改名前只要问题里有"只回答"就被拦；改名后重启 ScreenRecall，用带"只回答"的问题复测，`list_timeline`、`search`、`read`、`get_frame` 都正常调用，9 个渠道名和真值一致。

还剩的
- 被挡住的后台浏览器读不到网页、微信 / QQ 标题固定（所有聊天一个出处）：同前，已知限制。
- 查找栏（Ctrl+F）那条仍没在真 Edge 里复测。
- 课程交付：报告、Slides、演示视频；设计文档合并成一份、给用户的讲解页。

### 按意思搜默认打开（2026-10-29 晚，`eab5779` 之后，未提交）

原来关着（方案里写的"有空再做"），而且开了也没法用：每次查找都把最像的 300 行全部当命中，不管分数多低，任何查询（"红烧肉怎么做"）都会在后面带出一百多条；只按意思搜到的结果还按时间排，最像的那条常常不在前 5。

改了什么
- `[v3] vectors = true`（`config.default.toml` 和 `V3Config` 都改）。游标从 0 开始，已有的库第一次空闲整理就把旧的行全补上。
- 两个数（都在 `[v3]`，设置页能看到）：`semantic_min_score = 0.60`，余弦低于它不算搜到；`semantic_min_chars = 6`，少于 6 个字的行不参与按意思搜（"数据看板"、"Tokyo"这类短的零碎字和什么都像，它们靠字面搜就够了）。规则在 `Queries.search` 里，网页和 MCP 都走它。
- 只按意思搜到的结果改成按分数从高到低（字面 / 标题 / 网址命中的仍在前、从新到旧）。
- 按意思搜出错（模型没装、加载失败）只记一条警告，照样返回字面命中，不让整个查找失败。

测了什么（在真实库的副本上，`results/search/v3-semantic-2026-10-29.json`）
- 副本：7420 行、52 个出处。编码 7420 行用 67 秒（2 线程，约 110 行/秒），进程峰值约 1.17 GB，向量文件 11.6 MB；再跑一次补 0 行。
- 查询：34 条相关（拿真实行改写，大多是中文行用英文问、英文行用中文问），20 条无关的生活话题、12 条数据里没有的技术话题。found@5 = 目标行所在出处进前 5；junk = 无关查询带出任何一条按意思搜命中。

| 做法 | found@5 | 目标行进前 5 | junk（生活 / 技术） |
| --- | --- | --- | --- |
| 只用字面 | 0 | 0 | 0 / 0 |
| 不设下限（原来的做法，按时间排） | 0.26 | 0.15 | 100% / 100% |
| 不设下限，按分数排 | 0.82 | 0.71 | 100% / 100% |
| 下限 0.50 | 0.82 | 0.62 | 60% / — |
| 下限 0.55 | 0.71 | 0.53 | 40% / 50% |
| **下限 0.60 + 短行 6 字（默认）** | **0.68** | **0.53** | **20% / 17%** |
| 下限 0.65 | 0.47 | 0.32 | 15% / 8% |
| v2 的 hubness 门（宽 / 严） | 0.79 / 0.56 | 0.56 / 0.38 | 15% / 42%；0% / 8% |

- 简单下限落在 v2 两档门的中间，不比它差到要换回去；减去平均向量（三种居中做法）也没更好。再加"离最高分不超过多少"：found 不变、无关查询有没有命中的比例也不变，没加。
- `pytest tests`：683 passed（新增 2 个）；前端 `npm run typecheck`、`npx vitest run`（42）、`npm run build` 通过（只加了设置页两个数的名字）。

还剩的
- 正在跑的 ScreenRecall 要重启才用上；重启后等一次空闲整理（键鼠 5 分钟不动）把旧行补完。
- 小模型（MiniLM）的已知毛病：少数查询"什么都像"，例如"唐诗宋词鉴赏"能带出约 100 条聊天记录、"薛定谔方程推导"带出日志行；改写得离原文太远的也找不到（"小组最多几个人"找不到"max 5 members"）。

### 名字和每小时一句话跟界面语言走（2026-10-29 晚，分支 `feat/llm-ui-language`）

原来提示词写死"中文短名字""中文概括"，整理进程不知道界面是英文，英文界面也显示中文名字；名字按字数硬截 12 个字，"Transformer模型"（13 个字）被截成"Transformer模"。

改了什么
- 语言：网页每个请求都带 `X-Lang`，主进程（`v3/web.py` 的 `V3Context.note_ui_lang`）把它记在 meta `ui_lang`，和上次一样就不碰库；没带这个头的请求（MCP、脚本）不记。整理进程起名字前读它（`names.language`），不知道就用英文。两种语言的提示词意思一样。
- 换了语言要重写：meta `llm_lang` / `llm_lang_ms` 记已有的名字和每小时一句话是哪种语言、从什么时候起（没有记录 = 旧版写的中文）。界面语言和它不一样时记下现在的时间，这个时间以前起的名字（`sources.name_ms`）和写的每小时一句话（`hour_lines.made_ms`）都算过期，`unnamed_sources` / `hours_without_line` 照样交给大模型重写，仍在 `llm_max_calls` 之内，没写完的下次空闲接着写；重写前页面还显示旧的，不清空。库结构不变，别的表不动。
- 长度：一个全角字（汉字）算 1，英文字母、数字、半角符号算 0.5。`[v3] llm_name_max = 14`（中文 14 个字，英文约 28 个字母），`llm_hour_max = 40`（写进提示词；回答超过两倍才截）。超了在最后一个放得下的词 / 字后面截，去掉末尾标点加"…"，不把英文单词截成两半（只有第一个词就比上限长时才没办法）。设置页能看到这两个数。
- 命令行：`python -m screenrecall.v3.worker --data-dir D --home H --lang en` 当作界面换成英文，马上按英文重写（演示数据用）。

测了什么
- `pytest tests`：690 passed（新增 7 个：提示词按 meta 的语言、不知道用英文、换语言后旧的算过期、两种查询都拿到换语言的时间、长度算法、截在词边界、记界面语言只在变了时写）；前端 `npx vitest run`（42）、`npm run build` 通过（只加了设置页两个数的名字）。
## 一句话状态

- **2026-10-29 最新**：v3（`feat/recall-v3`）合入 `main`，PR #8（`feat/polish`）的提交也一起进了 `main`。产品功能基本完成，见上面 v3 一节；剩下报告、Slides、演示视频。下面各条是更早的记录。
- **2026-10-25 最新**：`feat/merge-datasets` 已由 PR #7 合入 main；接手后第一轮（分支 `feat/polish`）见下方"2026-10-25"一节。下面各条是当时的记录。

- P0 最小闭环：已完成，Windows 实机通过。**PR #1 于 2026-10-16、PR #2–#5 于 2026-10-21 依次用 merge commit 合入 `main`**（#2 `fb6f1cf`、#3 `362219c`、#4 `5b9053c`、#5 `effee82`，main 现在是 `effee82`）；只剩草稿 PR #6（数据集工具，base 已改为 `main`）。以下各节里"PR 未合并"的说法是当时的记录。
- P1 证据浏览 / 批量导入 / 收藏删除到期配额：**2026-10-16 在 Windows 11 实机验证通过**（真实 OCR + 向量 + 真实数据的端到端 19 项全过，见"验证记录"）。
- P1 采集节奏：等稳、排错模式、补拍、允许/排除规则、未保存变化的中断标记已实现（分支 `feat/capture-cadence`）。2026-10-13 晚在 **Windows 11 实机**跑通主要场景（见"验证记录"），30 分钟自动退出、快捷键被占用两项仍待实机确认。
- P1 向量基础设施、向量 worker、文字模式（词法 + 语义 RRF）、画面模式（多语言 CLIP）：已实现（分支 `feat/vector-search`），Windows 实机端到端通过。
- P1 页面接入：**已完成**。`app.py` 有"文字 / 画面"切换，查询模型按需加载并用 `st.cache_resource` 缓存，侧栏显示语义 / 全图向量待办，结果标注命中分支，详情页蓝框标语义贡献块。
- P1 证据浏览（前后浏览 + 并排）：**已实现**（分支 `feat/evidence-browse`，2026-10-15）。Mac 上逻辑测试 8/8、页面 AppTest 4/4 通过；Windows 浏览器里的排版与真实录制数据**待实机查看**。
- P1 "放宽匹配"按钮与 A05–A10 自动化验收：**已实现**（同一分支，2026-10-15）。Mac 上 13 条新测试通过（A05–A10 共 12 条 + 页面 1 条）；A11、A12 等第 10 项，A13 需 Windows 实机。
- P1 批量导入：**已实现**（同一分支，2026-10-15）。PNG/JPEG/WebP 逐文件校验、时间未知标注、批次幂等与失败重试、页面"批量导入截图"面板与命令行；数据库升到 **v4**。Mac 上 9 条新测试通过；Windows 待实机。
- P1 收藏 / 删除 / 到期 / 配额：**已实现**（分支 `feat/lifecycle`，2026-10-16，数据库 **v5**）。规则按主方案 5.2、5.4 + 团队 10-16 决定（见"待团队拍板"2）。Mac 上新增 21 条测试通过（含 A11、A12 可在 Mac 验证的部分、删除中后台写回、共享内容引用、缓存刷新）；Windows 待实机。
- P1 资源测量（第 7 项）：**测量脚本已写好**（`python -m screenrecall.bench`，同一分支，2026-10-16），Mac 冒烟 + **Windows 单元测试通过**；**还没有任何测试机上的真实测量数字**，见"待验证"12。
- P1 按时间范围删除：**已实现**（同一分支 `ce66848`）；数据目录可带自己的 `config.toml`，用于独立测试库。
- P1 标注 / 划分工具与分类器流程：**代码完成**（同一分支 `94657e1`，数据库 **v6**），只在合成数据上测过流程；**没有真实标注数据，也没有分类实验结果**。
- P1 五组检索实验：**评测脚本完成**（同一分支 `dc9a355`），替身编码器上测过流程；**没有查询真值，也没有实验结果**。
- P1 受控数据采集与标注：规程和工具已就绪（分支 `feat/dataset-collection`）。**试采 45 帧已修正、待人工复核**（2026-10-18）：30 张自制 HTML 仿界面改标 `source_kind=mock_ui`，只作开发测试样本；10 张公开网站（含 3 张补采）；5 张**真实 Terminal 窗口**。正式数据集改为以真实应用界面为主。**还没有任何人工标注、分类或检索结果。** Mac 上 pytest 176/176。
- P1 其余（40 条查询真值、捕获覆盖实验、编码对照、失败待办人工重试、报告与演示视频）：未完成，见"完成度总表"与"下一步"。
- **证据浏览 / 批量导入 / 收藏删除到期配额已于 2026-10-16 在 Windows 11 实机验证**（真实 OCR + 向量 + 真实数据端到端 19 项全过，`pytest` 132/132；见"验证记录"）。注意：这是在 CAO 推送分类 / 评测（`94657e1`、`dc9a355`）**之前**的提交 `0fd523d` 上验证的；分类与评测脚本本身尚未在 Windows 上验证。

## 完成度总表（对照 `readable_Proposal.md`，2026-10-16）

四种状态分开写：**代码**（是否写完）、**模拟测试**（Mac/Windows 上用假窗口、替身 OCR / 编码器的 pytest）、**真实模型测试**（加载锁定版本的 MiniLM / CLIP 实际推理）、**Windows 实机**（真实窗口、真实录制数据）。测试通过条数只说明已写的代码按预期运行，不代表方案已完成。

| 方案内容 | 代码 | 模拟测试 | 真实模型测试 | Windows 实机 |
| --- | --- | --- | --- | --- |
| P0 主链：指定窗口采集、WebP、OCR、原串 / 短词 / FTS 检索、原图页 | 完成 | 通过 | 不涉及 | 通过（10-13） |
| 采集节奏：等稳、排错、补拍、允许/排除、`unsaved_change` | 完成 | 通过 | 不涉及 | 主要场景通过（10-14）；30 分钟自动退出、快捷键被占用、真实应用节奏、真实 InPrivate **待验证** |
| 向量、文字模式 RRF、画面模式、页面切换与模型缓存 | 完成 | 通过 | 仅 2 条冒烟（中文查英文餐厅、深色 vs 白色），Mac 与 Windows 都跑过 | 端到端通过（肖天奕 10-14 记录，未复核） |
| 证据浏览、并排（5.4 前后查看） | 完成 | 通过 | 不涉及 | **待验证**（"待验证"7） |
| 放宽匹配（5.2） | 完成 | 通过 | 不涉及 | **待验证**（8） |
| 批量导入（6.8） | 完成 | 通过（上传控件本身未自动化） | 不涉及 | **待验证**（10） |
| 收藏、两种删除、按时间范围删除、30/90 天到期、配额与暂停（5.2、5.4） | 完成 | 通过 | 不涉及 | **待验证**（11） |
| 验收 A01–A10 | 测试已写 | 通过 | 不涉及 | A01–A04 由采集节奏实机覆盖；A05–A10 **待验证** |
| A11、A12 | 测试已写 | 通过（时间、磁盘占用是注入的） | 不涉及 | **待验证**（11） |
| A13 断网与 100/125/150% 缩放对齐 | 无需新代码 | 不适用 | 不适用 | **待验证**（9） |
| 资源与速度测量（6.3） | 测量脚本完成（`bench.py`） | 冒烟通过 | 空库上能加载真实模型跑通 | **未运行，没有任何测量数字**（12） |
| 捕获覆盖实验（6.3 第一行：1/3/4/6/10 秒事件脚本） | **未完成**（事件脚本未写） | — | — | 未做 |
| WebP 编码对照（5.1：原始像素 / q92 / 无损） | **未完成** | — | — | 未做 |
| 失败待办人工重试入口（2.4 "允许重试"） | 自动有界重试已有，**人工重试入口未做** | — | — | — |
| 标注与分组划分工具（6.1） | 完成（`dataset.py`：导出标注清单、校验、按来源组划分） | 通过（合成数据） | 不涉及 | 未用真实数据跑过 |
| 受控数据采集与标注规程、工具（6.1） | 完成（`datasets/COLLECTION.md`、任务表、查询分配、模板；`collect.py`、`indexer.py`） | 通过（合成图片；近重复阈值未在真实截图上校准） | Mac 上真实 RapidOCR + 向量模型跑通命令行流程（4 张合成图，只验证流程） | `collect` / `indexer` 未在 Windows 运行 |
| 受控数据、标注、40 条查询真值（6.1） | **曹贺乔部分完成**：人工验收 1,033 帧（双人复核），类别表已冻结；曹的 20 条查询已起草（草稿）。肖天奕的约 1,000 帧与 20 条查询未开始 | — | — | 不需要 Windows 录制（两人都用截图 + 导入）；数据集不能代替捕获覆盖和资源实测 |
| 五组检索实验 Exp 1–5、Hit@5 / MRR@10（6.2） | 评测脚本完成（`evaluate.py`：五组同候选、逐条结果、按场景 / 语言给命中数 / 查询数、查询设计检查） | 通过（替身编码器） | 未在真实模型和真实查询上跑 | **40 条查询未冻结，没有任何检索实验结果** |
| 分类器：LR + 多数类 / 进程名规则基线、Macro F1、混淆矩阵（6.4），页面显示标签并可筛选 | 流程完成（`classify.py`、数据库 v6 `frame_labels`、页面标签与主动筛选） | 通过（合成数据） | **初步结果 cao-v0**（只用曹的数据，CLIP + LR）：val 0.854、test 1.000，基线 0.113 / 0.573，偏乐观，见下一步 11 | 最终结果待合并两人数据后重新划分、只跑一次 test |
| 捕获覆盖实验（6.3） | **脚本完成**（2026-10-21，`coverage.py`：冻结事件脚本 → 受控测试窗口 → 统计） | 通过（真实 Recorder + 假窗口 + 注入时钟模拟播放；**不是实测**） | 不涉及；测试窗口已在 Mac 上实际打开截图确认显示正常 | **Windows 实机未跑**，没有覆盖率数字 |
| WebP 编码对照（5.1、6.3） | **脚本完成**（2026-10-21，`codec_compare.py`） | 通过（合成文字图） | Mac 冒烟：16 张 Retina 截图 + 真实 RapidOCR（只验证流程，不作结论） | **Windows 实机画面未跑**，`webp_quality` 尚未冻结 |
| 失败待办人工重试（3.4） | **完成**（2026-10-21，`retry.py` + 侧栏「失败的待办」+ 命令行） | 通过（真实 OcrWorker + 替身引擎、页面 AppTest） | 不涉及 | Windows 页面未实机查看 |
| 报告、Slides、演示视频、运行包（8.3） | 未开始 | — | — | — |

## 2026-10-25 接手后第一轮：网页界面打磨、默认配置、实机联调（分支 `feat/polish`，基于 main @ `f59ae7e`）
第三位组员接手（`feat/merge-datasets` 已由 PR #7 合入 main）。数据用肖天奕 10-23 的合库包 `data-dataset-merged-20261023-part{1,2}of2.zip`（校验 3,691/3,691 一致，`dataset check` 已标注 2,184 帧）。
- **默认配置**：`[capture] webp_lossless = true`（依据 10-23 编码对照）；`[classify] model_dir = "results/classifier/v1"`。向量 worker 空闲时给"有全图向量、缺本模型预测"的画面补写类别（老数据、后配置的分类器、换模型），`classify.frames_needing_prediction`，测试 1 条。合库包里 `frame_labels` 是空的（打包早于 10-23 下午的 predict），已备份到 `data-dataset/_backup/screenrecall.pre-predict-20261025.sqlite3` 后重写 2,213 条：chat 610 / code 600 / web 518 / table_doc 485，与 10-23 记录一致。
- **后端 `webapi.py`**：所有错误都是 JSON `{"error"}` 且可翻译（参数错 400、超限 413、库被锁 503、未知异常 500 只给简短说明、日志里留堆栈）；库结构过旧 / 过新时除 `/api/meta`、`/api/import` 外返回 503；重复删除返回 404；单个上传文件超 64 MiB 记为该文件失败。模型：文字 / 画面各一把锁；启动后后台线程预加载（`--no-preload` 关闭）；新接口 `GET /api/models`（每种模式 `state` = idle / loading / ready / unavailable / disabled、`elapsed_ms`、`reason`）。本机实测启动后约 10 s 文字模型就绪，第一次语义查询不再等模型。`/api/meta` 新增 `debug_max_minutes`。
- **前端 `web/`**：连接中断（连续 2 次失败或 5 s 无成功）时侧栏 / 录制页显示"连接中断"并禁用录制控制，恢复后自动刷新；应用 / 类别 / 规则列表每分钟、回到窗口、导入后、恢复连接时刷新；搜索页显示模型真实加载状态，查询中显示已等待秒数并可"停止等待"；空输入提示、结果卡片"命中片段"、并排比较标题带应用 / 标题 / 来源、OCR 未完成显示"文字识别中"、采集说明只在录制中显示、导入默认当前时间并可点开导入的画面、1:1 改为 1 图像像素 = 1 CSS 像素、窄屏也显示本机提示、375 px 下详情页溢出修复；排错按钮显示配置的分钟数；后台未运行时"开始录制"不可点（录制中仍可选别的窗口再点开始来切换目标）。新增 Vitest 测试 19 条（`cd web; npm test`）。
- **一键启动** `start.ps1`：前端未构建或 `web/src` 比 `web/dist` 新时先构建，后台开新窗口，网页服务在当前窗口，就绪后打开浏览器；`-DataDir`、`-NoDaemon`、`-NoBrowser`、`-Port`。UTF-8 BOM，Windows PowerShell 5.1 也能解析。
- **实机联调**（`results/windows/2026-10-25-PETER/live-webui.md`，i5-12600KF / 32 GB / 3840×2160@150%，独立目录 `data-dev`，只录自建的 tkinter 合成文字窗口）：7 项全过——网页 API 开始 / 暂停 / ACK、录到的画面可按原词与语义搜到、重启后默认不录；排错 `debug_max_minutes = 1` 时 60 s 自动退回（"待验证"1 的时间部分）；页面补拍与 `Ctrl+Alt+S`；**快捷键被占用**时状态写明注册失败、页面补拍照常（"待验证"2）；**真实 Edge InPrivate** 窗口不可录制、开始被拒（"待验证"4）；后台离线时控制请求 409；失败待办列出与重试。
- 测试：pytest 247/247、前端 19/19、`npm run build` 通过。
- 已知小问题（未修）：强杀后台后 `control.actual_state` 停在 `recording` 直到下次启动（页面和 API 先看心跳，不受影响）；原图缺失的画面两个向量状态停在 `pending` 而没有对应待办（积压计数仍为 0）；快捷键被占用后后台不重试注册，要重启后台。
- 还没做：捕获覆盖实验（计划已冻结在 `results/coverage/2026-10-23/`，共约 80 分钟，期间测试窗口不能被遮挡）、A13（断网、100/125/150% 缩放对齐）、排错模式积压 50 提前退出、真实应用下的节奏（"待验证"3）；报告、Slides、演示视频。

## 2026-10-23 网页界面替换 Streamlit（分支 `feat/merge-datasets`，未提交时以 `git status` 为准）
Kiro 起头（`screenrecall/webapi.py`、`tests/test_webapi.py`、前端请求封装与样式令牌），额度用完后由 Claude Code 接着做完前端。技术栈按 10-23 与肖天奕商定：后端 Starlette（只调用现有模块，不重写检索 / 生命周期 / 采集控制逻辑）+ 前端 React 19 + Vite + Tailwind 4（`web/`）。
- 运行：`cd web; npm ci; npm run build`（Node.js 20+，`web/dist`、`web/node_modules` 不进 git），然后 `python -m screenrecall.webapi --data-dir data-dataset` → http://127.0.0.1:8600。旧页面 `app.py` 保留，功能对应关系一致。
- 页面：左栏录制状态（2 秒轮询）/ 窗口选择 / 开始暂停 / 排错 / 补拍 / 失败待办重试 / 规则；主区四个标签：搜索（文字 / 画面、日期 / 应用 / 预测类别筛选、放宽匹配，结果缩略图上直接叠加琥珀色原词行与蓝色语义块）、收藏、导入、按时间删除；原图核验页（适应宽度 / 原始像素 1:1、OCR 行悬停高亮对应框、前后浏览胶片条、并排比较、出现记录、收藏、两种删除都先看计划再确认）。查询条件在地址里（`#/?q=…`），可刷新、后退、深链接。
- 安全：只绑 127.0.0.1；核对 Host（防 DNS rebinding）；非 GET 必须带 `X-ScreenRecall: 1` 且 Origin 是本服务；不开 CORS。
- 改动：`webapi.py` 收藏列表补画面宽高、请求体非 UTF-8 返回 400（原来 500）；`requirements.txt` 显式加 `starlette==1.8.0`、`uvicorn==0.54.0`、`python-multipart==0.0.32`（原先靠 streamlit 间接装上）。
- 验证（Windows 11）：pytest 210/210（新增 `test_webapi.py` 10 条）；`npm run build` 类型检查通过；在 `data-dataset` 上用内置浏览器实测 dev 查询（"组会改到哪个会议室了"）与画面模式查询、应用筛选、原图核验、前后浏览、收藏 / 导入 / 按时间删除页面渲染，冷启动首次文字查询约 10 秒。**没有在正式库上点收藏、删除、导入**（会改库），这些只由 `test_webapi.py` 在临时库上覆盖；录制控制没有和真实后台联调。盲测查询没有搜。
- 已知：Claude 桌面版预览器直接拉起服务时首次语义查询会卡住（服务写日志到文件时正常），在普通终端里运行不受影响。
- **第二轮（同日，肖天奕在 Edge 里试用后反馈"布局乱、不够高级、要中英文切换"）**：
  - 布局乱的原因：浏览器扩展向页面注入了不分层的 Tailwind 样式（如 `.flex-col{flex-direction:column}`），压过了 Tailwind v4 放在 `@layer` 里的 `lg:flex-row` 等。在内置浏览器里只注入这一条规则就复现了截图。修法：`@import "tailwindcss" important`（工具类带 !important，分层的 important 声明优先于不分层样式），自定义组件类改为 `sr-` 前缀。注入 `.flex-col / .hidden / .btn` 后复测布局不变。
  - 视觉重做：深色侧栏导航 + 浅色工作区；搜索首页居中大标题 + 命令栏式搜索框；结果改为卡片网格（排名角标、长截图自动把第一处标记移进取景框）；原图核验改为深色看图台 + 右侧检查栏（OCR 行号、等宽字体）+ 深色胶片条；录制控制从左栏移到独立的"录制"页（状态卡、控制、索引进度、存储配额条、失败待办、规则）。字体换成 Inter（可变）+ JetBrains Mono，均随前端打包，不走 CDN；中文用系统字体。
  - 中英文切换：侧栏底部"中文 / EN"，选择存在浏览器 localStorage。界面文案在 `web/src/i18n.tsx`（英文字典的类型由中文推出，漏翻会编译失败）；后台各模块返回的提示（检索说明、缺口描述、规则、删除结果、录制状态等）由新文件 `screenrecall/i18n.py` 按请求头 `X-Lang: en` 整句对照翻译，只翻指定字段，未收录的句子保持中文，OCR 文字和窗口标题不动。
  - 测试：pytest 214/214（新增 `tests/test_i18n.py` 3 条、`test_webapi.py` 英文模式 1 条）；内置浏览器在 1600 宽、375 宽（手机）、中英文下看过搜索、结果、原图核验、录制、导入、按时间删除页。仍未在正式库上点收藏 / 删除 / 导入；录制控制未与真实后台联调。
- **第三轮（同日，同学反馈后，肖天奕确认按建议改）**：
  - **分类预测写入正式库**（改了 `data-dataset`，只动 `frame_labels` 表）：先用 SQLite 在线备份到 `data-dataset/_backup/screenrecall.pre-predict-20261023-1530.sqlite3`（integrity ok，写入前 `frame_labels` 0 行），再 `python -m screenrecall.classify --data-dir data-dataset predict --model results\classifier\v1`（模型 `2123143ee97f8f80`，即 test Macro F1 0.868 那个最终模型），写入 2,213 条（原图可用的全部画面）：chat 610 / code 600 / web 518 / table_doc 485。与人工标注对照：test 381/436 = 0.874（与 `results/classifier/v1` 报告一致，说明写入的就是那个模型）、train 1297/1313 = 0.988、val 417/435 = 0.959——train / val 是训练过的数据，偏高，**只用于页面展示类别和筛选，不能当评测结果**，报告仍只用 test 数字。`evaluate.py` 不读 `frame_labels`，检索实验与冻结指纹不受影响。恢复：停掉后台和网页服务后用备份文件覆盖 `screenrecall.sqlite3`。
  - 界面：库里没有任何预测时，结果卡片、原图核验页不显示类别，筛选里不显示"内容类别"。
  - 结果卡片：每行 1280 px 以上 3 张、1800 px 以上 4 张（原来 1536 px 以下只有 2 张）；卡片压缩到约 380 px 高（原 557 px），1440×900 一屏可见 6 张（3 张完整）。缩略图有命中框时裁到命中区域（优先原词命中行，约 9 行字高，最多放大 2.4 倍，长行从行首开始），并按放大后的显示宽度请求更清晰的缩略图；画面模式没有命中框时仍显示整图。终端、聊天截图的命中行在卡片上可直接读出。

## 2026-10-23 Windows 实测（第 5 节：资源与性能）
结果与机器信息见 `results/windows/2026-10-23-DESKTOP-PH4UL0H/README.md`（i7-13700HX / 16 GB / 2560×1600@150%，交流电）。
- 查询（`data-dataset` 2,213 帧）：热查询 P95 文字 0.35–0.37 s、画面 0.19 s；后台忙碌（排错 + OCR 积压 15–18）时 0.08 / 0.05 s。达标（≤2 s）。冷启动第一次语义查询加载模型 40–160 s（报告要写）。
- 实时录制（WPS 文字密集 PDF，`tools/scroll_window.py` 投递滚轮自动翻页，普通模式 15 分钟 125 帧，`data-live2`）：OCR 就绪 P50 4.3 s / P95 7.8 s，语义 / 全图向量 P95 约 9.5 s，达标（15 s / 30 s）；后台 CPU 中位 1.4 核、RSS 2.1 GiB，网页服务空闲 RSS 1.2 GiB。
- 排错模式在这类页面上 OCR 跟不上（约 3.3 秒一帧，OCR 每页 3–6 s）：就绪 P95 约 200 s，待办最高 33（未到 `debug_exit_backlog = 50`）。
- **作废的第一轮**：`bench mem` 默认每秒读 USS，Windows 上会遍历 2.4 GB 后台的工作集（约 50 ms/次），把 OCR 拖慢到就绪延迟 550 s。文件改名保留并注明不作结论；`bench mem` 新增 `--no-uss`（`tests/test_bench.py` 覆盖），与就绪延迟同时测必须用。
- **编码对照**（`results/windows/2026-10-23-DESKTOP-PH4UL0H/codec/`）：`data-codec` 无损录 30 分钟（WPS PDF、VS Code、Edge 公开文档页、`tools/mock_chat_window.py` 模拟聊天，共 172 帧），分层抽 120 张对照：无损平均 88 KiB、q92 164 KiB、q95 188 KiB；无损逐像素一致、OCR 120/120 不变，q92 OCR 0/120 不变、86 张数字串有变化；编码 P95 无损 373 ms、q92 319 ms。**建议把 `[capture] webp_lossless` 改为 `true`（待两人确认后改 `config.default.toml`）**；样本缺大面积照片画面，这一点写进报告的局限。Edge / VS Code 不处理投递的滚轮，实测用临时配置的独立实例 + DevTools 协议滚动（`tools/cdp_scroll.py`）。
- 顺手修复：`config.py` 读 `config.local.toml` / 数据目录 `config.toml` 改用 `utf-8-sig`——PowerShell 5.1 `Set-Content -Encoding utf8` 会写 BOM，原来直接报 `TOMLDecodeError`（本节"删除和配额只在独立测试库上测"的命令也会踩到），`tests/test_config.py` 补了一条。
- 还没做：捕获覆盖（计划已冻结在 `results/coverage/2026-10-23/`：普通 49 分钟、排错 31 分钟两段）、A13。`data-live`、`data-live2`、`data-codec`、`data-coverage` 都是独立数据目录，不进 git。

## 2026-10-22 肖天奕：两人数据合库、60 条查询（分支 `feat/merge-datasets`）
与 10-21 交接计划的差异（两人已知会）：肖天奕的约 1,000 帧**不是截图 + `collect import`**，而是 10-18 / 10-20 在 Windows 上用后台**实时录制**（`daemon`，采样临时设 0.5 秒，已恢复 3 秒）得到的 1,151 帧（微信 339、VS Code 270、WPS 249、Edge 293），按每张图主要可见内容人工标注。
- **合库**：`python datasets/merge_library.py --src data --dst data-dataset --apply`。保留 frame_id，原样搬运 OCR 文字 / 行 / FTS / 文本块 / 向量（两库 schema v6、OCR 版本、分块 c1/n1、MiniLM 与 CLIP 版本及预处理全部一致，脚本先检查，不一致拒绝）；frame_id、路径、像素指纹、录制段均无重叠。合库后 2,218 帧（原图可用 2,213 = 曹 1,062 + 肖 1,151），`integrity_check` ok、外键 0 违例、每帧都有文字与画面向量、每个文本块都有向量、无待办任务、原图文件 0 缺失。合并前的库备份在 `data-dataset/_backup/screenrecall.pre-merge.sqlite3`；目标库 `meta.merge_log` 记了这次合并。
- **实时录制帧的验收**：`dataset accepted` 的原始清单门禁对实时录制帧（出现记录全是 `live`）放行（`dataset.live_frames`），导入帧照旧要在 `raw/` 清单里、近重复要判完。新增测试 1 条。
- **标注**：`python datasets/live_labels.py --data-dir data-dataset --src <本地标注表> --annotator xiao --out data-dataset/labels-xiao.csv` 转成与 `labels-cao.csv` 同列。实时录制帧没有任务编号，task_id 按"应用 + 规范化窗口标题 + 5 分钟段"生成（63 组）；全按标题分组的话微信一组 339 帧、划分无法平衡，代价是同一窗口跨段的帧可能分到不同划分，报告里写明。`datasets/v1/labels.csv` = 曹 1,062 + 肖 1,151 行。**肖的 1,151 行还没有复核（review_label / reviewer 空），需曹复核后才能出正式名单和划分**。试划分（`--allow-unreviewed`，不是正式结果）：103 个来源组，train/val/test 各类别都有（test：chat 121 / code 116 / web 111 / table_doc 88）。
- **查询改为 60 条**（两人 10-22 商定）：曹 20 条（按 `query_slots.csv`）+ 肖 40 条（每侧每场景 5 条），开发 / 盲测各 30、中英各 30、每侧每场景 7–8 条；`evaluate.protocol_check` 按 60 条检查（`MAIN_SET`、`MIN_PER_CELL=7`），场景名必须是类别表里的四个（曹草稿里的 `table` 已统一成 `table_doc`）。`python datasets/build_queries_v1.py ...` 合成 `datasets/v1/queries.json`，id 加 `cao-` / `xiao-` 前缀，`source_id` 保留原编号。肖的 40 条由 kiro-ai 代起草（`drafted_by: ai`）、xiaotianyi 逐条看图复核（删 2 条后补 2 条）→ `approved`；曹的 20 条仍是 `draft`。
- **开发演练（合库后，`--rehearsal --split dev`，2,213 帧，不是正式结果）**：产品文字模式 Exp 4 Hit@5 20/30（曹 9/10、肖 11/20）；语义 20/30、词法 10/30、画面 7/30。chat 3/8、code 5/8 最弱，table_doc 7/7。肖的 20 条在只含自己数据的库上是 12/20，合库后 11/20。
- 全部测试 200/200（Windows 11，`.venv` Python 3.12）。

- **10-22 下午复核完成**（肖天奕转述，两人当面复核）：曹复核肖的 1,151 帧标注，改动 0 处（`reviewer=cao`，`review_label` = 初标）；肖逐条复核曹的 20 条查询，改动 0 处（`review_status=approved`）。复核前的文件备份在 `data-dataset/_backup/`。
- **正式名单**：`dataset accepted` → `data-dataset/accepted.txt`，**2,184 帧**（曹 1,033 + 肖 1,151；29 张 mock_ui 排除）：chat 593 / code 596 / web 514 / table_doc 481。
- **正式划分**（`datasets/v1/split.json`，`--fractions 0.6,0.2,0.2 --seed 0`，按类别分层、按来源组）：103 组；train 1,313（曹 413 / 肖 900）、val 435（277 / 158）、test 436（343 / 93）。各划分四类都有；采集者比例不均（test 里肖的帧少），报告写明。
- **最终分类（test 只跑了这一次）**：`results/classifier/v1/`。val 选出 C=1.0、不加权（val Macro F1 0.914）；train+val 重训后 **test Macro F1 0.868、准确率 87.4%**；基线多数类 0.105、进程名规则 0.377。test 各类 F1：chat 0.976、code 0.863、web 0.838、table_doc 0.796；主要混淆 web → table_doc 18、code → web 10 / table_doc 9。
- **正式开发集检索**（`results/retrieval/20261022-dev/`，`--candidates` 2,184 帧，30 条）：Exp 4 产品文字模式 Hit@5 20/30、MRR@10 0.44；Exp 2 语义 20/30 · 0.53；Exp 1 词法 10/30；Exp 3 画面 7/30；Exp 5 三路 16/30。chat 3/8、code 5/8 最弱。**盲测还没跑**：等两人决定开发集是否调规则，冻结后只跑一次。

- **冻结（10-22，盲测前）**：两人决定开发集阶段**不调整任何检索规则**，全部沿用方案默认值：词法规则（D006：片段全 ≥3 字符用 FTS5 trigram + BM25，含短片段扫描并按命中片段数排序）、文本语义块先 max 到帧再取 Top-50、RRF `k=60` 两路等权、画面模式独立排名、结果 Top-10、无阈值；模型 MiniLM-L12 多语言 `@e8f8c211226b`、CLIP ViT-B/32 `@327ab6726d33`、多语言 CLIP 文本 `@58edf8cada9e`；`config.default.toml` 中 `rrf_k=60`、`branch_top=50`，无本地覆盖。冻结文件 blake2b-128：
  - `datasets/v1/queries.json`（60 条，全部 approved）`41b537f7bcee3c9a4ca3421cc35da0a1`
  - `data-dataset/accepted.txt`（2,184 帧，不进 git）`b8892e1e7365905b159fdca5ca508a94`
  - `datasets/v1/split.json` `f2c95c5fcd118d577fdb0778a600fc38`；`datasets/v1/labels.csv` `d12e1fcc2aa82bc655bb3bd07952385c`
  - 开发集正式结果 `results/retrieval/20261022-dev/` 用的就是这两份查询与候选文件（指纹一致）。dev 上观察到的失败原因（窗口标签栏 / 菜单等界面文字压过正文、答案只占屏幕一小块）只写进报告的失败分析与后续改进，不在本版修。
  - 盲测在此提交之后只跑一次：`evaluate --data-dir data-dataset --queries datasets\v1\queries.json --split blind --candidates data-dataset\accepted.txt --out results\retrieval\20261022-blind`，结果原样提交，不重跑。

- **盲测（10-22，只跑了这一次，`results/retrieval/20261022-blind/`，结果文件里的查询 / 候选指纹与上面冻结值一致）**，候选 2,184 帧、30 条：Exp 4 产品文字模式 **Hit@5 22/30（0.73）、MRR@10 0.61**；Exp 2 语义 21/30 · 0.59；Exp 1 词法 8/30 · 0.23；Exp 3 画面 5/30 · 0.15；Exp 5 三路（离线）19/30 · 0.42。分场景（Exp 4）：web 7/8、table_doc 6/8、code 5/7、chat 4/7；中文 10/14、英文 12/16。按预先指定的产品模式演示口径：文字题 19/26（Exp 4）、画面题 4/4（Exp 3）。按出题人：曹 9/10、肖 13/20（Exp 4）。Exp 4 前 5 未命中 7 条：其中 3 条排在 7–10 名（RAG 课程广告、天空球馆晚场价、git 推送被拒），4 条前 10 无命中（串串店会员福利、买卖股票 III、华为机考题型与分值、刷题工作区每套卷题量）——失败原因还没有逐条看图分析（dev 上观察到的是答案只占画面一小块、被界面文字稀释），逐条排名见 JSON。三路融合低于文字模式，支持"画面分支不进文字模式"的设计。

- **盲测后的分析（不改正式结果）**：
  - 配对显著性 `results/retrieval/20261022-significance.md`（`datasets/significance.py`）：语义 vs 词法、文字 RRF vs 词法在 dev、blind、合并上都显著；文字 RRF vs 纯语义无可靠差异（合并 42 vs 41，p=1.0）；三路 vs 文字 RRF：Hit@5 p=0.09，MRR −0.14 [−0.24, −0.05]。
  - 失败归因 `results/retrieval/20261022-failures.md`（`datasets/failure_table.py`，块级相似度对比）：16 条未命中中分栏混排 8、相近内容竞争 6、短块抢分 4、跨语言 / 代码 4、疑似漏标 3、融合拖累 2；**界面文字只占排前错误块的 2/64，之前"界面噪声是主因"的判断不成立**。
  - 补充判定（pooling）：`datasets/pooling.py` 生成 `results/pooling/pool.json`（五组前 5 名中原真值外的帧，649 对）。kiro-ai 按判定依据做 OCR 关键词 + 看图预筛出 22 对（`results/pooling/draft/draft_pool.json`，排除理由在 excluded），**肖天奕 10-23 逐对看图复核**（`judgments-xiao.csv`）：14 对相关、8 对不相关。复判后（`results/pooling/draft/audit.md`）：blind Exp 4 22→23/30、Exp 2 21→22、Exp 5 19→20；dev Exp 4 20→21；Exp 1、3 不变。结论不变，正式分数不改。只有一人复核，未算 κ；作业群那 2 对是曹的题，宜由曹确认。
  - 报告检索部分草稿：`report/retrieval_section_draft.md`。

**下一步**（复核、正式名单、划分、最终分类、盲测已完成）：① 报告里写检索结果与失败分析（dev 与 blind 都给分子 / 分母与逐条结果） ② 第 5、6 节的 Windows 测量（资源与性能、捕获覆盖、编码对照、A13）照原计划 ③ 报告、Slides、演示视频。

## 2026-10-21 交接给肖天奕：合并数据集、实验与 Windows 测量

分工（10-21 两人商定）：肖天奕在 Windows 上做 ① 采集自己的约 1,000 帧并与曹的数据合并 ② 40 条查询复核与冻结 ③ 五组检索实验 ④ 最终分类结果 ⑤ 资源与性能实测 ⑥ 捕获覆盖实验、编码对照、A13（断网 / 缩放）。曹贺乔在 Mac 上完成的脚本与数据见下。

**代码**：分支 `feat/dataset-collection`（草稿 PR #6，base `main`），以分支最新提交为准。新增 `screenrecall/coverage.py`、`screenrecall/codec_compare.py`、`screenrecall/retry.py`；无新增第三方依赖（测试窗口用 Python 自带的 tkinter，python.org 的 Windows 安装包默认带；先跑 `.\.venv\Scripts\python.exe -c "import tkinter"` 确认）。

**数据包**：`data-dataset-cao-20261021-part1of2.zip` 和 `-part2of2.zip`（各约 625 MB，合计约 1.25 GB，曹贺乔用微信发给你，不放 GitHub：仓库是公开的）。两个包都解压到**同一个位置**（仓库根目录），合起来就是完整的 `data-dataset\`；数据库在第 1 包，校验文件在第 2 包。内含 `data-dataset/` 下的原图 `raw/cao/`、导入副本 `frames/`、数据库 `screenrecall.sqlite3`（一致性快照，`integrity_check` ok）、`labels-cao.csv`（1,062 行，人工标注 + 复核）、`accepted-cao.txt`（1,033 帧）、`review/queries-cao-draft.json`（曹的 20 条草稿查询）、检查报告和 `CHECKSUMS.blake2b.txt`。不含采集用的 venv、备份和作废批次。曹已在 Mac 上从这个 zip 解压出的副本重跑 `collect check`、`dataset accepted`、`evaluate --check-only`，结果与原库一致。

### 0. 准备
1. `git fetch origin`，`git switch feat/dataset-collection`，`git pull`；`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`；`.\.venv\Scripts\python.exe -m pytest tests --basetemp=data\pytest -q` 全部通过再继续。
2. 把两个 zip 都解压到仓库根目录（第 2 包解压时如提示同名文件夹，选合并），得到 `data-dataset\`（已被 git 忽略）。**不要重建数据库**：标注和查询里的 frame_id 都跟着这份库。每次导入前先整份复制 `data-dataset\screenrecall.sqlite3` 备份。

### 1. 你的约 1,000 帧（按 `datasets/COLLECTION.md`）
1. 按 `datasets/collection/tasks.csv` 里 `xiao-*` 的 40 个任务采集，截图放 `data-dataset\raw\xiao\<任务编号>\`，事实记在 `data-dataset\raw\xiao\facts.csv`。主题、应用不要和 `cao-*` 重复；表格优先 Excel / PDF（曹的 Numbers 同版式小表画面多样性低）。
2. 每个任务：`collect manifest data-dataset\raw\xiao\<任务> --device xiao-win`，补 app / language / facts / source_kind。
3. 全部：`collect check data-dataset\raw`（同时查两人之间的重复与近重复），0 错误后 `collect import data-dataset\raw --data-dir data-dataset`，`indexer --data-dir data-dataset`（OCR + 向量，0 失败再继续；有失败用 `python -m screenrecall.retry --data-dir data-dataset` 重试）。
4. `collect labels data-dataset\raw --data-dir data-dataset --collector xiao --out data-dataset\labels-xiao.csv`；你标 `label` / `annotator=xiao`，曹复核 `review_label` / `reviewer=cao`（把 `labels-xiao.csv` 和 `raw\xiao\` 发给曹）。**按每张图的主要可见内容标**，不要整任务统一填。
5. 任务内近重复在 `data-dataset\raw\xiao\near_dups.csv` 逐对判定（keep_both + 具体差异 + 判定人），否则清单里删一张；已导入的删除要同时从库里删、标注行移到 `labels-xiao-dropped.csv`（`dataset accepted --raw` 会检查）。注意：曹那 322 对是 kiro-ai 按 OCR 差异判定的（reviewer=kiro-ai），你复核曹的数据时可以抽查 table-01 / table-10，另外 `cao-web-08-021`–`023` 有大面积 cookie 弹窗、仍被验收，请再判断一次。

### 2. 40 条查询
1. 你负责 `query_slots.csv` 里 owner=xiao 的 20 条，格式照抄 `review/queries-cao-draft.json`（相关帧只按采集记录和看图定，**不要用检索结果反推**）。
2. 复核曹的 20 条：逐条打开相关帧原图核对，看 `review_flag_maybe_relevant` 列出的帧是否漏标；曹确认自己的 20 条，你们互相复核后把 `review_status` 改成 `approved`。
3. 合成 `datasets/v1/queries.json`（40 条），`evaluate --check-only` 不再提示数量不足后冻结：记下文件 blake2b 指纹写进本文件，提交。**盲测查询任何人都不在页面或命令行里搜。**

### 3. 合并、验收、划分与最终分类
1. `collect labels data-dataset\raw --data-dir data-dataset --out datasets\v1\labels.csv --keep-from data-dataset\labels-cao.csv data-dataset\labels-xiao.csv`
2. `dataset --data-dir data-dataset accepted --labels datasets\v1\labels.csv --labelset datasets\labelset.json --raw data-dataset\raw --out data-dataset\accepted.txt`
3. `dataset --data-dir data-dataset split --labels datasets\v1\labels.csv --labelset datasets\labelset.json --fractions 0.6,0.2,0.2 --seed 0 --out datasets\v1\split.json`（已改为按类别分层；80 个任务组）
4. `classify --data-dir data-dataset train --labels datasets\v1\labels.csv --labelset datasets\labelset.json --split datasets\v1\split.json --out results\classifier\v1 --report results\classifier\v1\report.json`；test 只看这一次，结果原样写进本文件。

### 4. 五组检索实验
1. 开发：`evaluate --data-dir data-dataset --queries datasets\v1\queries.json --split dev --candidates data-dataset\accepted.txt --out results\retrieval\<日期>-dev`，据此调规则，把冻结的参数写进本文件。
2. 冻结后只跑一次：`--split blind --out results\retrieval\<日期>-blind`，结果原样提交，不重跑。

### 5. 资源与性能（结果放 `results\windows\<日期>-<机器名>\`）
用合并后的 `data-dataset`（约 2,000 帧真实数据，不需要再 expand；要更大的库才用 `bench expand`，扩出来的只用于速度测量）：`bench query --data-dir data-dataset --mode text` 和 `--mode image`、录制一段后 `bench readiness --data-dir <录制用数据目录>`、后台和页面都开着时 `bench mem --duration 600`。机器型号、CPU、内存、分辨率、缩放比例一起记下。

### 6. 捕获覆盖、编码对照、A13
- **捕获覆盖**（用单独的数据目录，例如 `data-coverage`，不要混进数据集库）：
  1. `coverage plan --mode normal --out results\coverage\<日期>\plan-normal.json`（60 组合 × 2 次，约 45 分钟）；排错模式 `--mode debug` 会自动切成每段 ≤25 分钟（30 分钟会自动退出排错）。
  2. 用单独的数据目录启动：后台 `.\.venv\Scripts\python.exe -m screenrecall.daemon --data-dir data-coverage`，页面先 `$env:SCREENRECALL_DATA_DIR="data-coverage"` 再启动；然后 `coverage stimulus --plan ...plan-normal.json --log ...\log-normal.jsonl`，倒计时 15 秒内在页面选「ScreenRecall Coverage Stimulus」窗口、开始录制（排错：每段开始前在页面进入排错模式，`--part 0`、`--part 1`……依次播放）。播放期间不要遮挡、移动这个窗口。
  3. 结束后停止录制、等 OCR 跑完，`coverage analyze --data-dir data-coverage --plan ... --log ... --out results\coverage\<日期>\normal.json`（同名 `.md` 是汇总表）。普通 / 排错各一份，原样提交。
  - Mac 上用模拟播放验证过口径（不是实测）：普通模式下主体静止、事件 ≥6 秒全部覆盖，1–4 秒事件和大面积动画 / 滚动背景下覆盖明显下降——这符合等稳规则的预期，实机数字以你跑的为准。60/60 的 95% 置信下界约 94%，要声称 ≥95% 需要更多试次（`--reps` 调大）。
- **编码对照**：先建一个单独的数据目录（例如 `data-codec`），在它自己的 `data-codec\config.toml` 的 `[capture]` 里设 `webp_lossless = true`，用 `--data-dir data-codec` 启动后台录约 30 分钟代表性画面（四类场景都有），再 `codec_compare --images <该目录>\frames --data-dir <该目录> --out results\windows\<日期>-<机器名>\codec --note "Windows 11，<分辨率>，缩放 <x>%"`。看 `summary.md` 和 `crops\` 并排放大图，决定 `webp_quality`（或改无损）并写进本文件。Mac 冒烟参考（16 张 Retina 截图，不作结论）：无损平均 95 KiB、q92 平均 109 KiB——这批截图无损反而更小，但无损编码 P95 约 350 ms、q92 约 125 ms；q90–q95 都有个别图 OCR 数字串变化，需回原图核对是否编码造成。
- **A13**：断网后搜索、查看原图、导入仍可用；100% / 125% / 150% 缩放下截图与高亮框对齐。
- **失败重试**：侧栏「失败的待办」能看到失败原因，点「重试全部可重试的」后待办被重新处理；原图缺失的不改动。

## Windows 交接步骤（给下一位在 Windows 上接手的同学）

接 **`feat/lifecycle`**，代码提交至少包含 `1c4f0f5`（收藏 / 删除 / 到期 / 配额）、`0fd523d`（测量脚本）、`ce66848`（按时间范围删除、数据目录独立配置）、`94657e1`（标注 / 划分工具、分类器，数据库 v6）、`dc9a355`（检索评测脚本）和之后的文档提交；以分支最新提交为准，`git log --oneline -5` 应能看到这三个。这些已于 2026-10-21 全部合入 `main`，现在直接从 `main` 开始即可。

### 1. 取代码、装环境

```powershell
cd <你的 ScreenRecall 目录>
git fetch origin
git switch feat/lifecycle            # 本地没有这个分支时：git switch -c feat/lifecycle --track origin/feat/lifecycle
git pull --ff-only
git log --oneline -8                 # 确认含 dc9a355、94657e1、ce66848、0fd523d、1c4f0f5
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt   # 本轮新增 psutil==7.1.3
.\.venv\Scripts\python.exe -m screenrecall.models   # 应输出"模型齐全"；否则加 --download（代理与 Xet 问题见"开发环境 → Windows"）
.\.venv\Scripts\python.exe -m pytest tests          # Mac 上为 151 passed（含 2 条真实模型）；Windows 结果请写进"验证记录"
```

### 2. 备份正式数据，再升级到 v6

旧代码打不开新结构的库（v4 代码打不开 v5/v6），所以升级前必须备份。

```powershell
# 先在后台终端按 Ctrl+C 停掉后台，并关掉页面；确认没有 python 进程还在用 data\
$stamp = Get-Date -Format yyyyMMdd-HHmm
Copy-Item -Recurse data "data-backup-$stamp"         # data-*/ 已在 .gitignore，不会被提交
.\.venv\Scripts\python.exe -m screenrecall.daemon --check
.\.venv\Scripts\python.exe -m screenrecall.daemon    # 终端 1：第一次启动时自动迁移到 v6（v5 收藏、v6 分类标签）
.\.venv\Scripts\streamlit.exe run app.py              # 终端 2：http://127.0.0.1:8501
.\.venv\Scripts\python.exe -m screenrecall.db       # 另开终端；应显示"结构版本 v6"（只读，不改库）
```

回退：停后台和页面 → `Rename-Item data data-upgrade-failed-$stamp` → `Copy-Item -Recurse data-backup-$stamp data` → `git switch feat/evidence-browse`。

### 3. 删除和配额只在独立测试库上测

不要在正式 `data\` 上试删除和小配额。用一个单独的数据目录，并把测试用的配额写在**这个目录自己的** `config.toml` 里（只影响这份数据，不影响正式数据；不要改 `config.local.toml`）：

```powershell
New-Item -ItemType Directory data-lifecycle-test | Out-Null
Set-Content -Encoding utf8 data-lifecycle-test\config.toml "[retention]`nquota_gib = 0.05`nreserve_mib = 8`nmaintenance_interval_seconds = 30"
.\.venv\Scripts\python.exe -m screenrecall.daemon --data-dir data-lifecycle-test     # 终端 1
$env:SCREENRECALL_DATA_DIR = "$PWD\data-lifecycle-test"; .\.venv\Scripts\streamlit.exe run app.py   # 终端 2（同一个 PowerShell 窗口里设变量）
```

在测试窗口里录一些画面（或用"批量导入截图"导入一批测试截图），再按"待验证"11 逐项测。测完 `Remove-Item -Recurse data-lifecycle-test`。

### 4. 本分支链上还没在 Windows 验证过的项目（都要测）

按"验证记录 → 待验证（需要 Windows 实机）"逐条执行，至少包括：

- 7 证据浏览与并排（`feat/evidence-browse` 引入）
- 8 放宽匹配
- 9 A13 断网与缩放对齐
- 10 批量导入（含 daemon 录制时同时导入、v3→v4 由页面触发升级）
- 11 收藏、两种删除、按时间范围删除、到期、配额暂停（用第 3 步的测试库）
- 1、2、3、4 采集节奏的剩余场景
- 12 资源测量
- 13 分类标签：没有训练产物时结果卡片显示"类别：待分类"、类别筛选不可用；有训练产物后（见 `datasets/README.md`）标签与筛选正常，筛选默认关闭

### 5. 测量命令与结果保存位置

结果统一放 `results/windows/<日期>-<机器名>/`（会提交；**不要放截图或真实聊天内容**），原始大文件放 `results/raw/`（已忽略）。每个目录写一个 `README.md`：机器型号、CPU、内存、Windows 版本、缩放、分辨率、线程配置、后台是否在录制。

```powershell
$out = "results\windows\2026-10-xx-<机器名>"; New-Item -ItemType Directory -Force $out | Out-Null
# 扩库（只用于速度 / 资源，不能用于准确率）：源库至少要有一些 OCR 和向量都完成的帧
.\.venv\Scripts\python.exe -m screenrecall.bench expand --src data --dst data-bench --frames 2000 --out "$out\expand.json"
# 查询耗时：后台空闲、后台忙碌（正在录制 + OCR 积压）各跑一遍，文件名区分
.\.venv\Scripts\python.exe -m screenrecall.bench query --data-dir data-bench --mode text  --repeat 5 --out "$out\query-text-idle.json"
.\.venv\Scripts\python.exe -m screenrecall.bench query --data-dir data-bench --mode image --repeat 5 --out "$out\query-image-idle.json"
# 两进程 CPU / 内存：后台和页面都在运行、页面已做过一次语义查询后再开始，采 10 分钟
.\.venv\Scripts\python.exe -m screenrecall.bench mem --duration 600 --out "$out\mem-recording.json"
# 就绪延迟：实时录制 30 分钟后
.\.venv\Scripts\python.exe -m screenrecall.bench readiness --data-dir data --out "$out\readiness-30min.json"
```

对照方案 6.3 的初始目标（文字就绪 P95 ≤15 秒、语义 / 画面就绪 P95 ≤30 秒、热查询 P95 ≤2 秒），把数字和未完成 / 失败数写进本文件"验证记录"，达不到就写未达标，不要只挑好的一次。

## 交接记录（最新在上）

| 日期 | 分支 | 基于 | 本轮提交 / PR | 内容 | 验证状态 |
| --- | --- | --- | --- | --- | --- |
| 2026-10-18 | `feat/dataset-collection`（CAO，同一分支续推） | `ed09461` | 本轮提交见 `git log`；仍在草稿 PR #6 | 按 40 帧试采检查报告修正：`check` 报告近重复对数 / 待人工判定数 / 来源分布，任务内近重复只认人工写的「近重复保留：」；清单新增 `source_kind`（`real_app / real_web / mock_ui`），`mock_ui` 默认不进 `dataset check / split / classify`；app 只写实际进程名；标注表新增 `source_kind`、`ocr_task_id_leak`；网页截图自动关 iframe 内弹窗、等图片加载；受控页面文件名不带任务编号；新增真实应用采集工具 `capture_real_mac.py`（Terminal / Numbers，prepare + attach）和复核材料脚本 `pilot_review.py` | macOS：pytest 176/176（新增 7 条）、pyflakes 无告警、2 处变异检查（mock_ui 排除、近重复判定标记）均被测试发现；试采数据见"下次恢复"；Windows 未测 |
| 2026-10-17 | `feat/dataset-collection`（CAO 新分支） | `feat/lifecycle` @ `1e124b5` | `eaa130c`；草稿 PR #6（base `feat/lifecycle`） | 受控数据采集与标注规程、80 个任务表、40 条查询分配、模板；`collect`（清单 / 检查 / 导入 / 标注表 / 事实→相关帧）、`indexer`（只建索引，Mac 可用）；导入支持逐张应用 / 标题 / 时间；`dataset check` 新增复核人≠标注人 | macOS：pytest 169/169（含 1 条真实 RapidOCR、2 条真实向量模型）、pyflakes 无告警、3 处变异检查均被测试发现；命令行全流程用真实 OCR + 向量跑通；**无真实数据**；Windows 未测 |
| 2026-10-16 | `feat/lifecycle`（肖天奕接力，同一分支） | `0fd523d` | 本轮提交（修 bench 测试、记录验证）；仍在草稿 PR #5 | 合并 PR #1 进 main；证据浏览 / 批量导入 / 收藏删除到期配额的 **Windows 实机验证**；修一个在 Windows venv 下误判的 bench 测试（产品代码未改） | Windows 11：pytest 132/132（含真实模型）、环境检查 9/9、#4+#5 实机端到端 19/19 |
| 2026-10-16 | `feat/lifecycle`（CAO 新分支，之后又续推） | `feat/evidence-browse` @ `c35ae9e` | `1c4f0f5`（收藏/删除/到期/配额）、`0fd523d`（测量脚本）、`ce66848`（按时间范围删除、数据目录独立配置）、`94657e1`（标注/划分工具 + LR 分类器，schema v6）、`dc9a355`（五组检索评测）；草稿 PR #5（base `feat/evidence-browse`） | 收藏、两种删除 + 按时间范围删除、30/90 天到期、配额暂停（v5）；测量脚本；分类器与评测脚本（仅合成/替身数据验证流程，无真实结果） | macOS：pytest 151/151（含 2 条真实模型）、pyflakes 无告警、5 处变异检查均被测试发现；Windows 未测 |
| 2026-10-15 | `feat/evidence-browse`（新分支） | `feat/vector-search` @ `40aad90` | `72f36ac`（证据浏览）、`da23ba5`（放宽匹配 + A05–A10）、批量导入提交（见 `git log`）；草稿 PR #4（base `feat/vector-search`） | 证据浏览：同一录制段内前后浏览、缺口/段边界提示、两图并排、切换到同帧的其他出现；"放宽匹配"按钮；A05–A10 自动化验收测试；批量导入（schema v4） | macOS：pytest 105/105（含 2 条真实模型）、pyflakes 无告警；Windows 未测 |
| 2026-10-14 | `feat/vector-search`（同一分支接力） | `95f4995` | 本轮提交（见 `git log`）；仍在草稿 PR #3 | 页面接入文字/画面模式；采集节奏与向量功能 Windows 实机验证 | Windows 11：pytest 71/71（含真实模型）、环境检查 9/9、实机端到端 35/35、页面 AppTest 16/16 |
| 2026-10-13 | `feat/vector-search` | `feat/capture-cadence` @ `244c19f` | `fad7ab7`（v3 基础设施）+ 本轮收尾提交（见 `git log`）；草稿 PR #3（base `feat/capture-cadence`） | 向量基础设施、向量 worker、文字/画面检索后端；**页面未接入（WIP）** | macOS 71/71（含 2 条真实模型冒烟）；Windows 未测 |
| 2026-10-13 | `feat/capture-cadence` | `feat/p0-closed-loop` @ `7bfe972` | `e0739a2`；草稿 PR #2（base `feat/p0-closed-loop`） | 采集节奏（P1） | macOS 单元测试 41/41；Windows 实机待验证 |
| 2026-10-13 | `feat/p0-closed-loop` | `main` @ `1d01dbe` | `7bfe972`；PR #1（base `main`，未合并） | P0 最小闭环 | Windows 11 实机通过 |

### 分支依赖与合并顺序

```
main (1d01dbe) ── feat/p0-closed-loop (7bfe972, PR #1) ── feat/capture-cadence (d325790, 草稿 PR #2) ── feat/vector-search (40aad90, 草稿 PR #3) ── feat/evidence-browse (c35ae9e, 草稿 PR #4) ── feat/lifecycle (草稿 PR #5) ── feat/dataset-collection (eaa130c, 草稿 PR #6)
```

截至 2026-10-17：PR #1 已合入 `main`（`4525e6e`），PR #2–#5 仍是草稿。`feat/dataset-collection` 用到 `feat/lifecycle` 的 `dataset.py` 和导入 v6 结构，所以从 `feat/lifecycle` @ `1e124b5` 建，PR base 选 `feat/lifecycle`，排在 #5 之后合并。

截至 2026-10-16：PR #1–#4 都没有合并。`feat/lifecycle` 改动了 #4 引入的页面和导入、证据模块，所以从 `feat/evidence-browse` 的最新提交建，PR base 选 `feat/evidence-browse`，排在 #4 之后合并。

截至 2026-10-15：PR #1–#3 都没有合并，`main` 仍是 `1d01dbe`。新分支 `feat/evidence-browse` 依赖页面接入（`app.py` 在 PR #3 里改过），所以从 `feat/vector-search` 的最新提交建，PR base 选 `feat/vector-search`；按 #1 → #2 → #3 → #4 顺序合并，前一个合并后把下一个的 base 改成 `main`。

1. 先合 PR #1 到 `main`。**请用 "Create a merge commit"，不要 Squash**：Squash 会改写 P0 的提交，`feat/capture-cadence` 再合时会重复出现 P0 的全部改动。
2. PR #1 合并后，把采集节奏 PR 的 base 改成 `main`（PR 页面标题旁 Edit → base）。若 PR #1 已被 Squash，则在本地 `git fetch origin && git rebase --onto origin/main 7bfe972 feat/capture-cadence`，检查无误后用 `git push --force-with-lease origin feat/capture-cadence`（只覆盖自己的功能分支）。
3. 采集节奏在 Windows 实机验证通过后，再把草稿 PR 转为 Ready 并合并。
4. PR #3、#4、#5 依次照做：前一个合并进 `main` 后，把下一个的 base 改成 `main`（同样用 merge commit）；每个都要在对应的 Windows 实机项通过后再转 Ready。截至 2026-10-16 五个 PR 均未合并、#2–#5 为草稿，`main` 仍是 `1d01dbe`。合并 PR 需要仓库写权限的人在 GitHub 页面操作，代理不会自动合并。
5. 之后的新任务从最新 `main` 建分支；如果依赖的 PR 还没合，就从那个功能分支建，并在本表写明"基于"哪一个提交。

## 本轮接力（2026-10-16，肖天奕，在 `feat/lifecycle` 同一分支上）

- **PR #1 已合入 `main`**（`gh pr merge 1 --merge`，用 merge commit 不是 squash，main 现在是 `4525e6e`）。PR #2–#5 仍是草稿，合并顺序不变（见"分支依赖与合并顺序"）。
- **Windows 实机验证了 #4、#5**（证据浏览、批量导入、收藏/删除/到期/配额），见"验证记录"里 2026-10-16 Windows 那条；`pytest` 在 Windows 上 132/132。
- **修了一个测试**：`test_bench.py::test_mem_samples_process_tree` 在 Windows venv 下会误判（启动器 shim 会多 spawn 一个解释器，两者都被 bench 正确采集）。只改了测试断言，`bench.py` 产品代码没动。
- **没有开始任何新功能**。距离"项目完成"还缺的硬交付见"一句话状态 → 距离方案要求的交付还缺什么"。
- **下一步建议**（按价值与依赖）：① 真实资源测量（第 7 项，bench 脚本已就绪，先录/导入到 1,500–2,000 帧再跑 P95 与两进程内存，据此冻结初值）→ ② 受控数据采集 + 40 条查询真值（第 13 项，需 Windows 录制）→ ③ 分类（第 11 项，依赖 ② 的标注数据）→ ④ 五组检索实验、报告、演示视频（方案 7、8.2）→ ⑤ 浏览器里人工过一遍页面 UI（"待验证"里的交互项）。

## 下次恢复（2026-10-18，`feat/dataset-collection`）

1. `git switch feat/dataset-collection && git pull`；`source .venv/bin/activate`；`python -m pytest tests -q --basetemp=data/pytest` 应为 176 passed。
2. 试采数据（全被 git 忽略，只在曹贺乔的 Mac 上）：`data-dataset/raw/cao/` 原图 51 张、清单保留 45、剔除 6（`keep=drop` 带原因，原图未删）。来源：`mock_ui` 30（code/chat/table 各 10，开发测试样本）、`real_web` 10、`real_app` 5（`cao-code-01-011…015`，真实 Terminal）。导入 45、OCR / 向量 0 失败。检查报告 `data-dataset/reports/check-pilot-cao-v3.txt`：完全重复 0，任务内近重复 19 对（全部待人工判定），跨任务 0。修正前的清单 / 日志 / 数据库备份在 `data-dataset/backups/2026-10-18-before-pilot-fix/`。
3. 复核材料在 `data-dataset/review/`：`pilot-review-cao.csv`（逐帧问题与空白人工列）、`web-facts-draft.csv`（网页事实草稿）、`queries-dev-draft.json`（四场景各 1 条开发查询草稿，`relevant` 是建议）。**都没有人工确认过**；没有跑过任何检索。
4. 真实应用采集：代理沙箱不能让 Terminal / Numbers 打开文档，需要人先执行一次 `open`（`datasets/COLLECTION.md` 第 9 节）。Numbers 场景已 prepare（`data-dataset/scenes-real/wdbf1bb/`，3 个虚构预算表），等人打开后 `numbers attach --task cao-table-01 --seq-start 11`。
5. 扩量前要人做：标注 + 互相复核这 45 帧；判定 19 对近重复和 `pilot-review-cao.csv` 里的问题；确认网页事实草稿；真实聊天应用的测试账号（见"待团队拍板"6）。

## 下次恢复（2026-10-16，`feat/lifecycle`）

- 分支 `feat/lifecycle`，基于 `feat/evidence-browse` @ `c35ae9e`，草稿 PR #5（base `feat/evidence-browse`）。远程 10-16 无他人新提交，PR #1–#4 都未合并。
- **本轮完成**："下一步"第 10 项（收藏 / 删除 / 配额 / 到期），A11、A12 中可在 Mac 上验证的部分。新增 `screenrecall/lifecycle.py`、`tests/test_lifecycle.py`；改了 `db.py`（v5 `favorites`）、`config.py` 与 `config.default.toml`（`[retention]`）、`capture.py`（删除当前记录后不延长不重存、空间不足暂停）、`daemon.py`（维护线程）、`evidence.py`（段结束原因"存储空间不足"）、`app.py`（收藏、两种删除确认、我的收藏、侧栏存储状态、"原图已过期"文案）。
- **恢复（macOS）**：`git switch feat/lifecycle && git pull --ff-only`，`source .venv/bin/activate`，`python -m pytest tests --basetemp=data/pytest` 期望 151 passed。新增依赖 `psutil==7.1.3`（已写入两个 requirements 文件，旧 venv 需补装）。
- **升级注意**：新代码第一次启动后台会把库迁到 v5；页面遇到旧结构会提示先重启后台。
- **本轮还完成**：第 7 项的测量脚本 `screenrecall/bench.py`（`tests/test_bench.py` 6 条冒烟）。
- **下一步**：Mac 上已经没有不依赖 Windows 数据的大项。Windows 上按顺序："待验证"11（收藏/删除/配额实机）→ 12（用 bench 跑第 7 项测量，结果写进"验证记录"并据此冻结预算）→ 第 13 项受控数据采集与标注 → 第 11 项分类（Mac 可做训练与评测，但要先有 13 的标注数据）。

## 上一轮恢复记录（2026-10-15，`feat/evidence-browse`）

- 分支 `feat/evidence-browse`，基于 `feat/vector-search` @ `40aad90`（肖天奕 10-14 的页面接入与 Windows 验证），草稿 PR #4（base `feat/vector-search`）。
- **本轮完成**："下一步"第 8 项证据浏览（`72f36ac`）、第 9 项"放宽匹配"+ A05–A10 验收测试（`da23ba5`）、第 12 项批量导入（之后的提交）。
  - 证据浏览：新增 `screenrecall/evidence.py`（时间线、前后关系、缺口归属）、`app.py` 详情页"前后浏览"区、`tests/test_evidence.py`（真实 Recorder 生成数据，8 条）、`tests/test_page.py`（Streamlit AppTest + 假 winapi，**第一批提交进仓库的页面测试**）。
  - 放宽匹配：`search.search(..., relaxed=True)` / `retrieval.run(..., relaxed=True)`；`textutil.squeeze` 去空白并保留到原文的位置映射；页面在严格规则下引号原串无匹配时显示规则说明和"放宽匹配"按钮，放宽后显示规则与"恢复严格匹配"。`tests/test_acceptance.py` 按方案 8.3 编号写了 A05–A10。
  - 批量导入：新增 `screenrecall/importer.py`（`import_batch()` 与命令行 `python -m screenrecall.importer <文件或目录> [--label] [--captured "YYYY-MM-DD HH:MM"]`）、数据库 v4（`import_batches`、`import_items`）、页面"批量导入截图"面板；导入的截图走与实时截图相同的 WebP 落盘 → OCR → 向量流水线，每批一个 `source_type='import'` 的录制段；证据浏览对导入批次按文件顺序排列并说明不是连续屏幕历史。`tests/test_importer.py` 8 条。
- **恢复（macOS）**：`git switch feat/evidence-browse && git pull --ff-only`，`source .venv/bin/activate`，`python -m screenrecall.models` 应输出"模型齐全"，`python -m pytest tests` 期望 105 passed。若 pytest 报 `sqlite3.OperationalError: unable to open database file`（临时目录不可写的沙箱环境里出现过），加 `--basetemp=data/pytest`（`data/` 已被忽略）。
- **下一步（当时）**：第 10 项要先由团队定"删除整幅画面时收藏怎么处理"（10-16 已定，见"待团队拍板"2）；第 7 项资源测量、第 13 项受控数据采集要在 Windows 上做；第 11 项分类依赖第 13 项的标注数据。Mac 上暂时没有不依赖决定或数据的大项，可先补第 7 项的可提交测量脚本（按真实子进程统计内存、复制数据扩库测 P95），再拿到 Windows 上跑。

## 上一轮恢复记录（2026-10-14 收尾时的状态）

- 分支 `feat/vector-search`，草稿 PR #3（base `feat/capture-cadence`）。本轮只改了 `app.py`、`PROGRESS.md`、`README.md`，已提交推送；没有改后端模块。
- **本轮完成**：页面接入（"下一步"第 6 项）；采集节奏与向量功能的 Windows 实机验证（第 1 项的大部分、"待验证" 1–7）。
- **未完成**：30 分钟自动退出与快捷键被占用两个场景未实机确认；资源测量（第 7 项）只在 12 帧小库上测过查询耗时，内存没有测出有效数字（见"验证记录"）。
- **后台进程**：没有残留的 daemon、Streamlit 或下载进程；测试数据和临时脚本已删除，未提交。

页面接入怎么做的（给下一位改页面时参考）：

- `build_engines(mode)` 只加载当前模式的编码器：文字模式加载 MiniLM，画面模式加载多语言 CLIP 文本；两者都用 `st.cache_resource` 按 model_id 缓存，同时缓存对应的 `TextVectorIndex` / `ImageVectorIndex`，它们在 `retrieval.run` 里按 `meta.index_revision` 自行刷新。
- 只有引号原串时不加载模型；模型未下载、配置关闭或加载失败时把原因写进 `Engines.unavailable_reason`，由 `retrieval` 退回词法（文字模式）或提示不可用（画面模式，不自动切换）。`models.available` 的结果缓存 60 秒，下载完成后不用重启页面。
- 结果卡片显示 `sources`（词法 / 语义 / 画面）；`resp.notes` 用 warning 显示；画面模式不显示 OCR 摘要。详情页：橙框 = 原词命中行，蓝框 = 语义贡献块中不与橙框重复的行；画面模式不画框。

恢复步骤（Windows）：

```powershell
git fetch origin; git switch feat/vector-search; git pull --ff-only
# 首次：py -3.12 -m venv .venv（或用 uv 的 Python 3.12）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m screenrecall.models      # 应输出"模型齐全"，否则加 --download
.\.venv\Scripts\python.exe -m pytest tests             # 期望 71 passed（含 2 条真实模型）
```

下一步从"下一步"第 7 项（资源测量）或第 8 项（证据浏览）开始，二者互不依赖。

## 开发环境

### Windows（运行与实机验证）

建议在仓库内单独建 `.venv`（已在 `.gitignore`），不要装进其他课程共用的环境：本分支锁定 `torch==2.14.1`，会替换已有的 GPU 版 torch。

```powershell
git fetch origin
git switch feat/vector-search
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt   # 约 1 GB，torch 为 CPU 版
.\.venv\Scripts\python.exe -m screenrecall.models --download   # 联网一次，约 1.5 GB
.\.venv\Scripts\python.exe -m screenrecall.daemon --check      # 9 项环境检查应全部 [ OK ]
.\.venv\Scripts\python.exe -m screenrecall.daemon              # 终端 1
.\.venv\Scripts\streamlit.exe run app.py                       # 终端 2，http://127.0.0.1:8501
.\.venv\Scripts\python.exe -m pytest tests
```

国内网络下载注意：

- git、pip 和 Hugging Face 都不读 Windows 系统代理。用 Clash 等代理时，要给 git 配 `http.proxy`，下载模型前设置 `$env:HTTPS_PROXY = "http://127.0.0.1:<端口>"`。
- 模型下载若报 `CAS Client Error ... error decoding response body`，是 Hugging Face 的 Xet 传输经代理失败。先设 `$env:HF_HUB_DISABLE_XET = "1"` 再重试，已下载的文件会续传。
- 冷启动慢：首次在页面做语义或画面查询时，需要导入 torch 并加载模型，本机实测约 50–100 秒，之后同一页面进程内不再加载。

### macOS（开发与单元测试）

后台和页面依赖 Windows 窗口 API（`winapi.py` 在非 Windows 上直接报错），**在 Mac 上不能运行录制和页面**，也不打算做成 macOS 产品。Mac 上能做的是：写代码、跑 `tests/`（假窗口替身 + 注入时钟 + 真实 SQLite/WebP），以及不依赖窗口 API 的模块（向量、检索、分类、评测脚本）；导入截图后可用 `python -m screenrecall.indexer --data-dir <目录>` 在 Mac 上跑真实 OCR 与向量（需额外安装 `rapidocr-onnxruntime==1.4.4 onnxruntime==1.20.1 opencv-python==5.0.0.93`，与 `requirements.txt` 同版本）。

系统自带的 `python3` 是 3.9，缺 `tomllib`，需要另装 Python 3.12，例如用 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # 安装 uv（一次）
cd ScreenRecall
uv venv --python 3.12 .venv                        # .venv 已在 .gitignore
source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest tests
```

## 已完成

### P0（分支 `feat/p0-closed-loop`）

| 模块 | 内容 | 文件 |
| --- | --- | --- |
| 后台入口 | 环境检查（FTS5 trigram、WebP、RapidOCR 冒烟、DPI、锁屏）、单实例锁、中断任务恢复、启动文件核查；录制默认关闭 | `screenrecall/daemon.py`, `envcheck.py` |
| 录制控制 | 页面写"期望状态 + 请求序号"，后台应用后写 ACK 与心跳；页面在 ACK 前显示"处理中"；暂停与在途提交串行化，ACK 后不再接受新画面 | `control.py`, `capture.py` |
| 采集 | Per-Monitor V2 DPI 感知；PrintWindow 捕获可见边框；DPI 不感知程序按逻辑尺寸截取；锁屏/最小化/捕获失败记为缺口；目标窗口关闭自动暂停 | `winapi.py`, `capture.py` |
| 复用与出现 | 原始像素完全一致 + 同窗口/进程/尺寸才复用；连续相同只延长出现；A→B→A 复用 frame 并新增出现；最近 4 张缓存 | `capture.py` |
| 存储 | WebP q92 原分辨率；临时文件 → fsync → 解码校验 → 原子改名 → 短事务登记；未登记文件移到 `cache/orphans` | `storage.py` |
| OCR | RapidOCR（PP-OCRv4）识别已保存原图；按捕获顺序领取；写回前校验执行标识与版本；幂等重写；有界重试与退避 | `ocr.py` |
| 检索 | 引号原串 AND 过滤（引号外只排序）；≥3 字符走 FTS5 trigram，更短走 `instr()` 扫描；`%`、`_` 按字面；默认忽略大小写；日期（UTC 半开区间）与应用筛选作用于出现记录；帧级排序 | `search.py` |
| 页面 | 状态每 2 秒刷新；窗口选择；结果列表；原图适应宽度/100%、命中行高亮、OCR 原文、出现记录表 | `app.py` |
| 数据结构 v1 | frames / occurrences / sessions / gaps / frame_text / ocr_lines / ocr_fts / index_jobs / control / meta | `db.py` |

### P1 采集节奏（分支 `feat/capture-cadence`）

| 能力 | 规则（方案 4.1、4.2） | 文件 |
| --- | --- | --- |
| 等稳 | 相邻两次观察（同窗口、同尺寸）变化像素比例 < `stable_change_ratio`（1%）即保存；大变化先等一个间隔；持续变化从等待起点满 10 秒后的首个采样点保存，期间不重置起点。单个数字变化低于阈值同样作为新画面保存（A01） | `cadence.py` |
| 首次直接捕获 | 开启、恢复、切换目标后第一次观察直接保存；锁屏等缺口之后只重建基线，按等稳处理 | `cadence.py` |
| 中断标记 | 观察到变化但未保存（等稳/限频）时写 `gaps.reason = 'unsaved_change'`，`detail` 为 `{"prev_occurrence_id", "mode"}`，并中断当前出现；不存候选图像、不建 OCR 待办。符合 4.2 表：A→B(未存)→A 得到 1 个内容、2 次出现、1 个中断 | `capture.py` |
| 排错模式 | 每 1 秒检查、不等稳；距上次自动保存 < 2 秒时只保留最新候选，允许时补存；满 30 分钟或 OCR 待办 ≥ `debug_exit_backlog`（50）自动退回普通；再次请求不延长到期时间；暂停后回到普通 | `cadence.py`, `capture.py` |
| 补拍 | 页面「立即补拍」或全局快捷键（默认 `Ctrl+Alt+S`，`snapshot_hotkey`）；内容相同则复用图片并新增出现，`occurrences.source_type = 'manual'`；不受自动间隔限制，仍受暂停、锁屏、排除规则限制；结果显示在侧栏 | `hotkey.py`, `winapi.py`, `capture.py`, `daemon.py` |
| 允许/排除规则 | `[rules]`：允许程序（空 = 不限）、排除程序、排除标题关键词；排除优先。开始录制时拒绝；录制中标题命中（如切到 InPrivate）记 `excluded` 缺口、不截图 | `rules.py` |
| 页面 | 普通/排错模式状态、排错倒计时与「退出排错模式」、补拍结果、快捷键状态、窗口列表标注「不可录制」、规则摘要、出现记录「来源」列 | `app.py` |
| 数据结构 v2 | 改为逐版本增量迁移（`db._MIGRATIONS`）；`control` 新增 desired_mode / actual_mode / mode_until_ms / mode_reason / snapshot_* / hotkey_status；`frames.capture_meta` 增加 `mode` 与 `trigger`（auto / held / manual） | `db.py`, `control.py` |
| 测试 | `tests/`：假 `winapi` 替身 + 注入单调时钟 + 真实 SQLite/WebP，任何平台可跑 | `tests/` |

### P1 向量基础设施（分支 `feat/vector-search`，schema v3）

| 能力 | 规则（方案 3.1、3.3、3.7、5.1；提案 4.2、5.1） | 文件 |
| --- | --- | --- |
| 数据结构 v3 | `text_blocks`（共享文本块，`block_key` = 规范化版本 + 分块版本 + 规范化文本）、`frame_blocks`（每帧各自的块顺序、行范围、原文范围、坐标）、`embeddings`（目标、分支、`model_id`=repo@revision、预处理版本、维度、float32 BLOB，唯一键防止混用）、`frames.text_embed_status / image_embed_status` | `db.py` |
| 规范化与分块 | NFC + 去行尾空白（不动行内空白和大小写）；按行序、垂直间距（> 1.5 倍行高中位数换块）和真实 tokenizer 长度（含特殊 token，≤128）切块，超长换块时 1 行重叠，单行超长按字符切 | `vectors.py` |
| 待办登记 | OCR 成功：有文字登记 `text_embed` + `image_embed`，无文字只登记 `image_embed` 并标 `skipped`；OCR 最终失败但原图可用：仍登记 `image_embed`；待办 `created_at_ms` = 抓拍时间（延迟从抓拍算起）；v2→v3 迁移为已有帧补登记 | `ocr.py`, `vectors.py`, `db.py` |
| 统计 | `index_stats` 分阶段：OCR 待办（文字可搜）与文本/全图向量待办分开计数 | `db.py` |
| 模型 | 提案 4.1 固定组合，revision 锁定在 `config.default.toml [models]`；默认离线加载；`python -m screenrecall.models --download` 只下载 sentence-transformers 实际加载的文件，`python -m screenrecall.models` 检查是否齐全；后台与页面各自 `threads` 个 torch 线程 | `models.py` |
| 向量 worker | 后台 `embed` 线程：最老待办优先；OCR 待办 ≥ `ocr_high_water`（50）时让路；文本按真实 tokenizer 分块，只为缺向量的共享块编码，重写本帧块关联并回收失去引用的旧块；全图保持比例灰边补成正方形后编码；写回校验执行标识、版本、图片状态；有界重试；一路失败不影响另一路；模型缺失时后台照常运行，向量待办保留 | `embed.py`, `daemon.py`, `envcheck.py` |
| 文字模式 | 日期/应用筛选 + 引号 AND 得到共同候选；只有引号时按时间、不调模型；词法分支取有片段命中的帧，语义分支每帧取最高块、合并到帧后 Top-50；k=60 等权 RRF，同分按最新时间再帧 ID；语义命中给出贡献块的 OCR 行（高亮用）；向量未完成的帧照常参与词法并提示；模型不可用时退回词法并说明 | `retrieval.py`, `search.py` |
| 画面模式 | 多语言 CLIP 文本编码描述，与 CLIP 图像向量比余弦；只检索原图可用且已有向量的帧；不可用时明确提示，不自动切换到文字模式 | `retrieval.py` |
| 向量矩阵 | 从 SQLite BLOB 读入 NumPy，按 `meta.index_revision` 变化重载，短读快照内同时读矩阵与帧-块关联 | `retrieval.py` |

## 验证记录

### 已验证

- P0，2026-10-13，Windows 11，150% 缩放，Python 3.12.13：环境检查 8/8；端到端 33/33（A→B→A、ACK 0.15–0.35 秒、暂停后无新增、短词 16/18、大小写、`%` 字面、不成对引号、日期与应用筛选、重启后默认关闭）；页面 AppTest 13/13。测试脚本是临时的，未提交。
- 采集节奏，2026-10-13，macOS（arm64）+ Python 3.12，**模拟测试，不等于实机通过**：`python -m pytest tests` 41/41。覆盖：A01 小变化保存、A02/方案 4.2 表、A03 暂停作废候选、A04 排错间隔恰为 2 秒、等稳与 10 秒到期、锁屏缺口、排除规则（开始拒绝/录制中缺口）、排错补存候选、30 分钟与积压退出、到期不可续期、补拍复用/拒绝/去重、快捷键写法解析、v1→v2 迁移。另做了 `py_compile` 与 pyflakes 检查（含 `app.py`、`daemon.py`、`winapi.py`，只检查语法和未定义名，不代表能在 Windows 上正确运行）。

- 向量与检索，2026-10-13，macOS（arm64）+ Python 3.12 + torch 2.14.1 + sentence-transformers 6.1.0：`python -m pytest tests` 71/71（含 2 条真实模型冒烟：中文查询"同事推荐的餐厅"经 MiniLM 找到英文餐厅文字且排第一；CLIP 画面模式区分深色图与白色页面）。其余检索规则用确定性替身编码器验证：RRF 公式、两路融合、引号过滤作用于两路、只有引号不调模型、模型缺失退回词法、向量未完成仍参与词法、日期筛选限制语义分支、画面模式排除过期原图且不切换模式、修订号变化后重载矩阵、共享文本只编码一次、OCR 积压时向量让路、推理中删除不写回、一路失败另一路照常。

- 采集节奏 + 向量 + 页面，**2026-10-14，Windows 11 实机**（150% 缩放，Python 3.12.13，torch 2.14.1+cpu，sentence-transformers 6.1.0）：
  - `pytest tests` 71/71，含 2 条真实模型测试；环境检查 9/9；pyflakes 无告警。
  - 实机端到端 35/35。测试窗口是 tkinter 窗口，脚本按时间表切换文字、标题和画面；后台是真实 daemon 子进程。结果：
    - P0 的 v1 库自动升到 v3，旧数据可原词搜到，迁移补登了向量待办。启动日志里热键注册成功。
    - 等稳：开启后首帧直接保存；单次变化约 4 秒后保存；持续变化 22 秒内只存 1 帧（8.5 秒处），并留下 2 条 `unsaved_change` 标记；变化停下后保存了最终画面。
    - 排错模式：ACK 时剩余 30.0 分钟；9 秒存了 4 帧，相邻间隔 2.0、2.01、2.0 秒；退出立即生效。
    - 补拍：页面补拍和 `Ctrl+Alt+S`（用 `keybd_event` 模拟按键）各新增 1 条补拍出现。
    - 排除规则：录制中标题变成含 "InPrivate" 时记 `excluded` 缺口、帧数不变；含 InPrivate 的窗口无法开始录制。
    - 向量：12 帧的 OCR 和两类向量 21 秒内全部完成，0 失败；日志各有 12 条 `text_embed done` / `image_embed done`。
    - 检索：中文"同事推荐的餐厅"经语义分支找到英文餐厅画面，排第一；画面模式"深色背景 雪山大图"把雪山画面排第一；纯引号查询按时间排序；引号加描述走 RRF，结果都含原串；迁移来的旧帧也能被语义搜到。
    - Ctrl+Break 后 2.2 秒退出，热键线程没有残留。
  - 页面 AppTest 16/16：模式切换、语义结果与"命中分支"、蓝框说明、画面模式结果和详情（不显示 OCR 摘要、不标注局部）、纯引号查询。
  - 耗时，只作参考，不是方案 7.3 的验收：12 帧库、后台空闲时，热查询文字 P50 17 ms，画面 P50 24 ms。新进程冷加载：import torch 约 50 秒，MiniLM 11 秒，多语言 CLIP 文本 4 秒；页面首次语义查询约 99 秒。
  - 没测出有效内存：Windows 上 venv 的 `python.exe` 只是启动器，按启动器 PID 读到的工作集不是真实进程的。测内存要按子进程 PID 统计。
  - 测试脚本临时编写，未提交。

- 证据浏览，**2026-10-15，macOS**（arm64，Python 3.12.15，Streamlit 1.65.0）：`python -m pytest tests` 83/83（原 71 + 新增 12），pyflakes 无告警。
  - `test_evidence.py` 8 条，数据由真实 Recorder（假窗口 + 注入时钟）生成：段内上一条/下一条；A→B（未保存）→A 时两次 A 出现之间提示"画面有变化但未保存"；锁屏缺口；暂停后新段不与旧段拼接、段结束原因"用户暂停"；段尾未结束的缺口；原图过期的出现仍在时间线、已删除画面被跳过；后台异常退出留下的未结束段；同类缺口合并计数。
  - `test_page.py` 4 条（Streamlit AppTest 驱动真实 `app.py`，引号原串查询，不加载模型）：打开结果后显示"第 2 / 3 次出现"和前后缺口提示；下一条 → 回到搜索结果；与上一条并排；第一条没有"上一条"按钮。
  - 均为 Mac 上的逻辑与页面测试，**不等于 Windows 实机通过**。

- 放宽匹配与验收矩阵，**2026-10-15，macOS**：`python -m pytest tests` 96/96（再新增 13 条），pyflakes 无告警。
  - `test_acceptance.py` 12 条，按方案 8.3 编号：A05 两字中文 / 数字 16 / 长短混合，短片段不丢且顺序可重复，`%`、`_` 按字面；A06 两段引号 AND + 日期条件、区分大小写、无匹配不自动放宽、放宽后忽略大小写与空白（可跨 OCR 行，两行都高亮）、放宽后仍保留日期筛选与 AND、从基础筛选重新扫描、文字模式放宽后仍走 RRF 并显示规则、只有空白的引号不能放宽；A07 一帧多块多次出现只占一位；A08 OCR 异常可见、不阻塞下一帧、与"确实没有文字"区分、仍登记全图任务；A09 中断任务恢复后续做，既有索引照常可查，没有重复结果；A10 删除后词法、语义、画面三路都不再返回。
  - 做过一次反向检查：临时关掉放宽分支，3 条放宽测试会失败，说明测试确实覆盖到这段代码。
  - `test_page.py` 新增 1 条：只有引号无匹配时才出现"放宽匹配"，点击后才放宽，可恢复严格匹配，新查询回到严格规则。
  - A01–A04 由 `test_recorder.py` 覆盖；A11、A12 等第 10 项实现；A13（断网、100/125/150% 缩放下高亮对齐）只能在 Windows 实机做。

- 批量导入，**2026-10-15，macOS**：`python -m pytest tests` 105/105（再新增 9 条），pyflakes 无告警。
  - `test_importer.py` 8 条：PNG/JPEG/WebP 导入成功，损坏文件、`.txt`、扩展名为 `.png` 的 GIF、8×8 小图分别失败且不阻塞后续；落盘为 WebP 并登记 OCR 待办；未给时间时标"时间未知"、给了时间时出现记录用该时间；同一批次重交时已成功的跳过、因空间不足失败的重试成功、第三次全部跳过；批次内重复内容跳过，其他批次照常导入；导入后经替身 OCR 可被原串搜到；导入批次时间线按文件顺序而不是时间；已有库迁移到 v4；命令行按文件名导入目录。
  - `test_page.py` 新增 1 条：搜索结果与详情显示"捕获时间未知"，前后浏览说明"导入批次……不是连续的屏幕历史"。**页面上传控件本身没有自动化测试**（Streamlit AppTest 不能操作 `file_uploader`）。

- 收藏 / 删除 / 到期 / 配额，**2026-10-16，macOS**（arm64，Python 3.12.15）：`python -m pytest tests --basetemp=data/pytest` 126/126（再新增 21 条），pyflakes 无告警。**都是逻辑测试**：采集用假窗口，OCR 与向量用替身，时间用注入的 `now_ms`，磁盘占用与剩余空间用注入函数，不等于 Windows 实机通过。
  - `test_lifecycle.py` 18 条：删除本次记录时共享画面保留到最后一个引用；共享文本块在一帧删除后仍为另一帧提供语义命中、两帧都删后回收；删除整幅画面先列出出现记录与收藏，确认后多出的收藏导致拒绝；删除被收藏的出现只删它自己的收藏；收藏外键挡住绕过流程的直接删除；A11（30 天原图过期后文字仍可搜、画面模式不返回；90 天记录删除；收藏保护；取消收藏后按普通规则）；任一出现仍在 30 天内时原图保留；OCR 未完成的帧不被到期或配额淘汰；A12（配额先淘汰最旧非收藏原图、保留收藏；剩余受收藏保护时暂停并说明；`cache/orphans` 不当缓存删；删记录没有缩小占用时只删一批就停；磁盘剩余不足同样触发）；维护报告写入 `meta` 供页面读取；空间不足暂停后拒绝开启录制、恢复后可开启；OCR、文本向量推理中途删除后写回被丢弃；全图向量推理中途原图过期后写回被丢弃；删除后向量矩阵按修订号重载；采集线程不延长已删除的当前记录、画面不变时不重存，画面变化或补拍后恢复。
  - `test_page.py` 新增 3 条 AppTest：收藏后"删除此画面的全部记录"写明"全部 1 次出现记录"和"1 个收藏"，确认前不删、确认后结果消失；"删除本次记录"取消后不删；"我的收藏"在没有搜索时也能打开详情。
  - 变异检查：去掉引用检查、去掉到期时的收藏保护、去掉版本号 +1、去掉删除后的修订号 +1、去掉"删记录无效就停"，每一处都有测试变红；检查后代码已恢复。

- 标注 / 划分 / 分类流程，**2026-10-16，macOS**：`tests/test_classify.py` 9 条 + 页面 1 条，**全部是合成数据**（纯色加噪声的假图、替身编码器的平均颜色向量，类别天然可分），只证明流程：标注校验（未知类别、缺复核、复核改动无理由）、同任务与同文字画面强制同组、整组同去同留、缺类与组数提醒、比例必须显式给出；超参数只在 val 上选、test 有分类器与两个基线、零分母记 0、无样本类别不进分母；模型存取后预测一致；预测写库、类别筛选只在主动选择时生效、删除画面时标签一起删；向量 worker 只在特征模型一致时写预测；标注在划分后被改动时拒绝训练。页面：卡片显示"类别（模型预测）"或"待分类"，筛选默认关闭。**没有任何真实分类结果。**
- 检索评测脚本，**2026-10-16，macOS**：`tests/test_evaluate.py` 4 条，替身编码器上验证：Hit@5 / MRR@10 定义（第 6 名不算 Hit@5、第 11 名 MRR 为 0）；五组实验共用候选；只有引号的查询五组走同一产品路径；无命中、引号硬过滤为空、模型不可用的查询都保留并计 0，分母不变；按 split / 场景 / 语言汇总；查询设计检查（40 条、每侧每场景 5 条、中英各 20、无答案提示）。**没有任何实验结果。**
- 测量脚本，**2026-10-16，macOS**：`tests/test_bench.py` 6 条——最近秩百分位；`expand` 扩到 20 帧后每帧有独立出现、OCR、FTS、文本块与带噪声的全图向量，图片文件齐全，源库不变，目标非空时拒绝；`bench_query` 分开统计冷 / 热、编码 / 读库 + 排序；`readiness` 计入未完成待办；`mem` 能按命令行找到进程并采样 RSS（沙箱里取不到 USS 时只报 RSS）；命令行 `readiness` 可运行。另在空库上用真实 MiniLM 和多语言 CLIP 跑通了 `bench query`。**这些只证明脚本能跑，不是测量结果。**

- 证据浏览 / 批量导入 / 收藏删除到期配额，**2026-10-16，Windows 11 实机**（150% 缩放，Python 3.12.13，torch 2.14.1+cpu，sentence-transformers 6.1.0，仓库内 `.venv`）：
  - `pytest tests` **132/132**（含 2 条真实模型），环境检查 9/9，pyflakes 无告警。此前 `test_bench.py::test_mem_samples_process_tree` 在 Windows 上会失败——不是产品 bug，是测试断言没考虑 venv 的 `python.exe` 启动器会再 spawn 一个真解释器（两者命令行都含标记、都被 `bench` 正确采集）。已把断言改成"根进程在采样结果里、且采到的都属于探针进程树"，`bench.py` 本身未改。
  - #4+#5 实机端到端 **19/19**，用真实 RapidOCR + 真实 MiniLM/CLIP + 真实 SQLite/WebP，直接调各模块公共接口：
    - 批量导入：3 张正常图成功、1 张损坏文件失败不阻塞；同一批次重交被幂等拦截；未给时间的帧标"时间未知"；导入帧走完整 OCR + 向量流水线、0 失败；导入内容能被原词搜到。
    - 证据浏览：导入批次形成一个 `source_type='import'` 的录制段、含 3 次出现；中间出现能取到上一条和下一条。
    - 收藏：加收藏返回 True，重复加幂等（不产生第二条），查询命中。
    - 删除本次出现：记录消失，帧唯一引用时帧被回收、图片删除，之后原词搜不到。
    - 删除整幅画面：先给出计划（含受影响收藏），再按计划删除、连带收藏。
    - 配额：能统计占用；注入极小配额时按"到期 → 缓存 → 最旧非收藏原图 → 最旧非收藏记录"清理，收藏被保护。
  - 测试脚本临时编写，未提交。
  - **仍未做**：真实浏览器里查看页面排版（前后浏览、收藏/删除确认、上传控件都只有 AppTest 逻辑层）；真实资源测量数字（bench 在 Windows 跑通单元测试，但没在底库上跑出 P95 / 内存）。

### 待验证（需要 Windows 实机）

1. 排错模式满 30 分钟自动退出：可在 `config.local.toml` 把 `debug_max_minutes` 改成 1 来验证。积压 ≥ 50 时提前退出。
2. 快捷键被其他程序占用时，侧栏提示注册失败，页面按钮仍可补拍。
3. 真实应用的节奏：记事本连续打字、终端光标闪烁、浏览器滚动和动画下，1% 阈值与 `unsaved_change` 的数量是否合适。本轮只用了 tkinter 测试窗口。
4. 真实 Edge InPrivate 窗口：本轮是让测试窗口的标题包含 InPrivate 来模拟的。
5. 在浏览器里看页面的实际排版和交互：侧栏的排错/补拍按钮、倒计时刷新。AppTest 只验证了逻辑，没有点这些侧栏按钮。
6. 实机 1,500–2,000 帧底库下：后台 OCR + 向量的吞吐与积压、两进程合计内存（按真实子进程统计）、文字/画面热查询 P95，按方案 7.3 在指定测试机上测。
7. 证据浏览（`feat/evidence-browse`）：用真实录制数据在浏览器里打开结果 → 前后浏览、并排两张原图的排版与清晰度；跨锁屏、暂停和 `unsaved_change` 时提示是否正确；`unsaved_change` 很多时提示文字是否过长。
8. 放宽匹配：在真实 OCR 结果上试一条因空格或换行没搜到的原串（如错误码被 OCR 拆成两段），点"放宽匹配"后能找到、原图上两行都标出。
9. A13：模型下载后断网，文字/画面查询仍可用；单屏 100%、125%、150% 缩放下窗口边界与 OCR 高亮对齐。
10. 批量导入：在浏览器里用"批量导入截图"面板上传一批真实截图（含一张损坏文件），逐项结果正确、后台 OCR 与向量补算完成后可搜到；daemon 正在录制时导入，两边同时写库不报 `database is locked`；后台为旧版本、库仍是 v3 时由页面导入触发升级到 v4。
11. 收藏 / 删除 / 到期 / 配额（`feat/lifecycle`）：浏览器里收藏、取消收藏、两种删除和"按时间范围删除"（预览数量与实际删除一致、默认保留收藏）的确认文案与结果；录制进行中删除当前画面的记录后，后台不报错、画面不变时不重新存入；删除后图片文件确实从 `data/frames` 消失（Windows 上文件被页面或杀毒软件占用时会留下残留，下次后台启动时清除，需确认）；在独立测试库的 `config.toml` 里把 `quota_gib` 调小（见"Windows 交接步骤"3）验证按顺序清理、空间不足时暂停采集并在侧栏说明原因、调回后可重新开启；`measure_usage` 在上万张图片下的耗时；把系统时间或测试数据的时间往前调，验证 30/90 天到期。

12. 第 7 项资源测量（用 `python -m screenrecall.bench`，在指定测试机上）：
    - 先录一段真实数据（或用第 13 项的受控数据），`python -m screenrecall.bench expand --src data --dst data-bench --frames 2000`；复制数据只用于资源 / 速度测量。
    - 后台空闲与忙碌（正在录制 + OCR 积压）两种情况下各跑 `bench query --data-dir data-bench --mode text` 和 `--mode image`，记录冷 / 热 P95、编码与读库 + 排序分项。
    - 同时启动后台与页面（各自做过一次语义查询），`bench mem --duration 600` 记录两进程合计平均 / P95 CPU、峰值与常驻内存；注意 Windows venv 的 `python.exe` 是启动器，脚本已带上子进程。
    - 实时录制 30 分钟后 `bench readiness --data-dir data`，对照方案 7.3（文字就绪 P95 ≤15 秒，语义 / 画面就绪 P95 ≤30 秒），未完成与失败数一并报告。

13. 分类（`feat/lifecycle` @ `94657e1`）：受控数据标注完成后，按 `datasets/README.md` 跑 export → check → split → train，报告与模型放 `results/classifier/<名称>/`；然后在 `config.local.toml` 配 `[classify] model_dir`，重启后台，检查新画面自动得到标签、页面卡片显示"类别（模型预测）"、类别筛选只在主动选择时生效。
14. 五组检索实验（`dc9a355`）：查询真值冻结后按 `datasets/README.md` 跑 dev、再只跑一次 blind，结果放 `results/retrieval/<日期>-<split>/`。实验只读数据库，可以把录好的 `data\` 拷到 Mac 上跑。

## 已知问题

- 采集节奏：每次"变化后等稳"都会写一条 `unsaved_change` 缺口，正常使用时行数会较多；光标闪烁的终端会在两张画面之间来回复用并产生大量出现记录。均符合方案字面规则，待实机数据决定是否合并短缺口或调整阈值。
- 排错模式在少见时序下（候选在限频中保留，下一次仍未到允许时间而画面已回到原内容）会把同一画面的出现拆成两段，时间仍正确。
- Mac 上无法运行后台与页面（设计如此，见"开发环境"）。
- 页面进程首次语义或画面查询需要导入 torch、加载模型，本机约 50–100 秒，期间页面显示"正在加载…"。后台与页面各持有一份模型，内存是两份。
- 每个 OCR 文字框作为一行、以换行连接，跨两个框的原串严格匹配搜不到（可点"放宽匹配"忽略空白后找到）；OCR 有误读（实测 `under_score` → `under score`）。
- 导入的截图在没给捕获时间时以导入时间存档并标"时间未知"，日期筛选对它们按导入时间生效；同一批次内所有截图共用一个用户给定的时间。
- 放宽匹配在日期/应用筛选后的全部 OCR 文字上逐帧扫描，没有用全文索引；1,500–2,000 帧下的耗时未测。放宽且有引号外文字时，词法分支按命中片段数排序（不用 bm25）。
- 部分硬件加速窗口 PrintWindow 可能取不到内容，仅前台时退回屏幕拷贝，否则记缺口。
- DPI 不感知程序按逻辑分辨率保存，比屏幕显示小。
- WebP q92、1% 阈值、OCR 线程数、重试次数、积压 50 等是初值，未在固定测试机测量冻结。
- 页面无登录鉴权，只绑定 127.0.0.1。
- 删除数据库记录不会立即缩小数据库文件（没有开启自动 VACUUM）；配额按实际文件占用判断，"提前清理最旧非收藏记录"通常释放不了多少空间，没有效果时只删一批就停并暂停采集。是否加维护时的 VACUUM 待实机测量后定。
- 维护每 300 秒执行一次，两次之间靠 256 MiB 预留余量兜底，采集线程写图前不单独检查磁盘；`measure_usage` 每次遍历整个数据目录，大库耗时未测。
- 用户删除了正在录制的当前画面的记录后，同一画面在变化前不再记录；若用户希望立即重新记下来，需要点"立即补拍"。
- `bench expand` 复制帧共用源帧的录制段 ID，时间整体后移；复制帧的文字与源帧相同，词法候选会成倍增加，这正是压测想要的，但不能用于准确率。`bench mem` 的 RSS 两进程相加会重复计入共享库，合计以 USS 为准（取不到 USS 时会标 null）。

## 下一步（按依赖排序）

标"Mac 可做"的可以在 macOS 上开发和做单元测试；标"需 Windows"的必须实机完成。

1. ~~采集节奏实机验证~~ **主要场景已通过**（2026-10-14，见"验证记录"）。剩下的是"待验证" 1–5，可以与后续开发并行。通过后草稿 PR 转 Ready。
2. **合并 PR #1，再合采集节奏 PR**：按"分支依赖与合并顺序"。依赖：1。
3. ~~向量基础设施 schema v3~~ **已完成**（Mac 可做）：`text_blocks`（共享文本块，复用键 = 文本 + 规范化版本 + 模型版本）、帧-块关联（行号、坐标）、`embeddings`（目标 ID、分支、模型/预处理版本、维度、数据类型、向量）；OCR 完成后登记 `text_embed`、`image_embed` 待办；写回校验 `worker_token` 与 `record_version` 并 `bump_revision`。方案 3.3、3.7、5.1。依赖：2（迁移接在 v2 之后，追加到 `db._MIGRATIONS`，不要改 v2）。
4. ~~文本语义分支~~ **后端已完成**（Mac 可做）：MiniLM（paraphrase-multilingual-MiniLM-L12-v2）按真实分词器分块（≤128 token，含特殊 token）、帧级取最高块分数、Top-50 帧；文字模式两路 RRF（k=60，同分按最新时间再帧 ID）。方案 5.1–5.3。依赖：3。
5. ~~全图分支与画面模式~~ **后端已完成**（Mac 可做）：clip-ViT-B-32 图像向量（保持比例补边）、clip-ViT-B-32-multilingual-v1 文本查询；只检索原图可用的帧。依赖：3。
6. ~~页面查询缓存与模式切换~~ **已完成**（2026-10-14，Windows 实机 + AppTest）。
7. **两进程合计内存与延迟测量**（需 Windows 测试机）：方案 7.3。**测量脚本已完成**（2026-10-16，`screenrecall/bench.py`）；剩下在测试机上按"待验证"12 运行并记录、冻结预算。复制数据只能用于资源压测，不能用于准确率评估。依赖：6。
8. ~~证据浏览~~ **已完成**（2026-10-15，`feat/evidence-browse`，Mac 逻辑 + AppTest；Windows 待查看，见"待验证"7）：只在选中出现的录制段内前后浏览，跨缺口和段边界提示，不拼接其他录制段；两图并排不算差异；可切到同帧的另一次出现。
9. ~~"放宽匹配"按钮与 A05–A13 自动化测试~~ **已完成可在 Mac 上做的部分**（2026-10-15，`feat/evidence-browse`）：放宽按钮 + A05–A10。A11、A12 随第 10 项补；A13 见"待验证"9。
10. ~~收藏 / 删除 / 配额 / 30·90 天到期~~ **已完成**（2026-10-16，`feat/lifecycle`，Mac 逻辑 + AppTest；Windows 待验证，见"待验证"11）。按时间范围删除 10-16 补上（`ce66848`）。
11. **分类**：~~训练与评测流程~~ **代码完成**（2026-10-16，`94657e1`，合成数据流程测试）：标注清单导出与校验、按来源组划分、LR + 两个基线、Macro F1 / 逐类 / 混淆矩阵、预测写库与页面筛选。依赖：13。
    - **初步结果 cao-v0（2026-10-21，只有曹贺乔一人的数据，不是最终结果）**：1,033 帧人工验收帧，按 40 个任务分组划分 0.6/0.2/0.2（`datasets/cao-v0/split.json`，种子 0）。原划分算法只按总帧数分配，chat 大组全部落进 train、val/test 缺 chat，已改为按组多数类分层（`dataset.grouped_split`，新增测试）。
      - CLIP 图像向量 + LR，val 选超参（C=0.1，val Macro F1 0.854）；**test（8 个任务、209 帧）Macro F1 1.000**；基线：多数类 0.113、只看应用名 0.573。报告 `results/classifier/cao-v0/report.json`。
      - 解读：test 恰好是界面差异大的任务（Numbers/Excel 表格、Terminal、Element 聊天），val 里 web 与 table_doc 混淆 10 张（arXiv 表格页、x-rates 汇率页）。只有一台电脑、一个人、聊天只有 Element 一个应用，**test 1.000 偏乐观，不能当最终结论**；最终结果要等合并肖天奕的 Windows 数据后重新划分，并且只跑一次 test。
12. ~~批量导入~~ **已完成**（2026-10-15，`feat/evidence-browse`；Windows 待验证，见"待验证"10）：PNG/JPEG/WebP，导入批次来源、时间未知标注、批次幂等。方案 6.8。依赖：3。
13. **受控数据采集与标注、40 条查询真值**：规程与工具已完成（2026-10-17，`feat/dataset-collection`）。两人都用系统截图 + `collect import`，不需要 Windows 实时录制。顺序：小批试采（`datasets/COLLECTION.md` 第 8 节，每人每场景 10 张）→ 确认类别表、划分比例和肖天奕的任务表 → 各采约 1,000 有效帧 → 互相复核 → 写并冻结 40 条查询。Mac 与 Windows 都可做；建库统一在一台机器上（当前约定曹贺乔的 Mac，`data-dataset/`）。
    - **曹贺乔进度（2026-10-21，`feat/dataset-collection` @ `8dae7dd` 之后）**：原始 1,144 张，清单剔除 274 张，真实来源**候选** 838 张（code 225 / chat 246 / web 231 / table 136），另有 30 张 mock_ui 只作开发测试。**人工验收 0 张**——候选数不是正式有效帧。`collect check data-dataset/raw` 0 错误，完全重复 0，跨任务近重复 0 对，任务内近重复 296 对待人工逐对判定；候选全部导入 `data-dataset/screenrecall.sqlite3`，OCR / 文字向量 / 画面向量 868 帧 0 失败（含 mock）。报告 `data-dataset/reports/check-cao-20261021-batch3.txt`，标注表 `data-dataset/labels-cao.csv`（类别全空）。
    - chat 用本机 Matrix（Synapse，只监听 127.0.0.1:18008，不联邦）+ 官方 Element Web 1.12.32，内容全部合成。10-20 采的 250 张全被 Element "Introducing Sections" 介绍气泡遮挡，已整批标 drop（原图保留）并于 10-21 关掉气泡后重采；4 张私聊开头画面跨任务近重复按规则各删一张。界面全是同一应用，标注与划分时注意。
    - **第 4 批（2026-10-21 10:28）后**：原始 1,268 张，清单剔除 279 张，真实来源候选 **957** 张（code 250 / chat 246 / web 231 / table 230），人工验收 0。check 0 错误、跨任务近重复 0 对、任务内 322 对待判；OCR/向量 987 帧 0 失败。报告 `data-dataset/reports/check-cao-20261021-batch4.txt`。
      - 新增：table-04/05 预览 PDF（分 3 轮各 12 个打开，`open-batch-3b-{1,2,3}.sh`）；table-06 diagrams.net 只读查看器显示 5 张虚构流程图 × 5 种变化（深浅色 / 半屏 / 缩放，近重复交人工）；table-08 arXiv HTML 版 5 篇论文各 5 张结果表（原定预览，改浏览器）；code-03 本机 Jupyter（127.0.0.1:18888，随机令牌，`datasets/scenes/jupyter_notebooks.py` 真实内核执行，traceback 里有本机路径，采完已停）。
      - table-09 时刻表只剩 8 张候选：FlightAware 苏黎世页超时，SBS 首末班车页已下线（5 张 Page Not Found 已剔除），剩下的 FlightAware 帧主要是地图、KTMB 帧主要是介绍图，**是否算时刻表要人工判断**；需要换时刻表来源补采约 17 张。
      - `cao-code-01-031` 的 OCR 里出现任务编号，标注时判断。
    - **第 5 批（2026-10-21 11:25）后**：原始 1,343 张，剔除 279，真实来源候选 **1,034** 张（code 250 / chat 306 / web 231 / table 247），人工验收 0。check 0 错误、跨任务 0 对、任务内 322 对待判；OCR/向量 1,064 帧 0 失败。报告 `data-dataset/reports/check-cao-20261021-batch5.txt`。
      - chat 每任务追加一个新话题房间 6 张（`datasets/scenes/chat_content_c.py`，`capture_chat_element.py --extra`），共 60 张，对话内容全新、不复用原对话；没有把 10-20 被气泡遮挡的旧图加回（它们与重采图是同一脚本同一画面，只多了遮挡）。
      - table-09 改用 seat61 页面里的真实列车时刻表（马来西亚 / 越南南北行，锚点定位）补 17 张；seat61 的 load 事件常超时，计划里改等 DOMContentLoaded（`wait_until`）。部分帧底部有站内广告条，交人工判断。早先 FlightAware / KTMB 的 8 张是否算时刻表仍待人工判断。
    - **人工标注与验收（2026-10-21 17:00 后）**：曹贺乔初标、肖天奕复核，`labels-cao.csv` 1,064 行全部有 label / review_label / final_label；复核改动 0 处（第一版按任务统一标，10-21 已改为按每张图重标，table-08 现为 table_doc 15 / web 10）。
      - 类别表冻结为 `datasets/labelset.json`（2026-10-21）。
      - 任务内近重复 322 对：**由 kiro-ai 判定**（`near_dups.csv` 的 reviewer 写的是 `kiro-ai`，不是人工），依据是逐对比较 OCR 行差异（模糊匹配排除 OCR 噪声）：316 对两张图的 OCR 文字有实际不同（不同报错、日期、数值、文件），写 keep_both + 具体差异；看图确认后剔除 2 张——`cao-code-04-024`（只多 `--tb=long`，输出与 006 完全相同）、`cao-table-01-003`（仿界面，只差选中单元格）。这 2 张已从数据集库删除，标注行移到 `labels-cao-dropped.csv`。备份在 `data-dataset/backups/2026-10-21-before-neardup/`。注意：OCR 不同不等于画面多样，table-01（Numbers 同版式小表）、table-10（x-rates 不同日期汇率）画面高度相似，建议人工抽查。
      - `collect check`：原始 1,343，剔除 281，保留 1,062（含 mock_ui 29），0 错误，任务内近重复待判 0 对，跨任务 0 对；报告 `data-dataset/reports/check-cao-20261021-final.txt`。
      - **人工验收（双人复核、非仿界面）1,033 帧**：code 249 / chat 306 / web 265 / table_doc 213（类别是人工标签，不是场景）。名单 `data-dataset/accepted-cao.txt`（`dataset accepted --raw` 生成，已加近重复与剔除门禁）。
      - 下一步：分组划分（建议 0.6/0.2/0.2）→ 分类训练评测；写曹负责的 20 条查询；合并肖天奕的 `xiao/`。
    - **曹负责的 20 条查询（2026-10-21，草稿，`review_status=draft`）**：`data-dataset/review/queries-cao-draft.json`（被 git 忽略，含 frame_id），槽位与查询文字同步写进 `datasets/collection/query_slots.csv`（status=draft）。由 kiro-ai 代 cao 起草，**需要 cao 确认、肖天奕复核后改成 approved 才能正式出分**。
      - 相关帧只来自采集记录（清单 facts 标记、每张图打开的文件 / 网址）和看图确认，**没有跑任何检索**；盲测 10 条没有在页面或命令行里搜过。dev 只对应 01-05 号任务，blind 只对应 06-10 号。
      - 看图补了 3 张采集时漏标的相关帧（code-02-004、code-10-007/008）；每条的 `review_flag_maybe_relevant` 列出 OCR 里含关键字、但看图判定不相关的帧，给复核人核对。
      - `evaluate --check-only` 格式通过；每侧每场景不足 5 条是因为肖天奕的 20 条还没写。
14. **五组检索实验、资源与存储测量、报告与演示**：方案 6.2、6.3、8.3。**评测脚本已完成**（`dc9a355`，`python -m screenrecall.evaluate`，用法见 `datasets/README.md`）、测量脚本已完成（`0fd523d`）；剩下：40 条查询真值（依赖 13）→ dev 调规则 → 冻结后只跑一次 blind → 报告、Slides、视频。依赖：13。

## 待团队拍板

0. **按时间范围删除时收藏怎么处理（10-16 我先按保守做法实现，请确认）**：默认保留范围内被收藏的记录；只有勾选"同时删除范围内的收藏"并在预览里看到数量后才删。范围与搜索的日期筛选一致，按记录的首次到最近观察与范围有交集计算。

1. 空输入：现按方案 5.2 提示"请输入要查找的文字"（旧版是展示最近记录）。
2. ~~删除整幅画面时收藏怎么处理~~ **已定（2026-10-16）**：用户明确选择"删除此画面的全部记录"时，界面说明会删除哪些出现记录及关联收藏，确认后执行；到期或配额触发的自动清理保护收藏；删除单次出现记录时不误删仍被其他记录引用的共享内容。
3. 页面文案目前是中文，演示视频若需英文需替换。
4. `unsaved_change` 缺口是否在实机数据后合并短间隔（见"已知问题"）。
6. **真实聊天界面从哪来（2026-10-18 起；曹贺乔 10-20 定为"模拟即可"，已用本机 Matrix + Element 合成对话采完曹的 250 张，肖天奕那边仍待定）**：正式数据集要真实应用界面，chat 场景目前只有 mock_ui。需要两人定：用哪个真实聊天应用的网页版（如 Slack / Discord 免费工作区），由谁注册测试账号、在 Playwright 独立窗口里登录一次；聊天内容仍是合成的、只在测试工作区里发，不发给任何真人。
5. **数据集（试采后两人确认）**：~~类别表~~ **已冻结（2026-10-21，`datasets/labelset.json`）**；第 5.3 节混合画面规则；分类划分比例（建议 `0.6,0.2,0.2`）；肖天奕的 40 个任务（`tasks.csv` 中 `status=draft`）；共享网盘位置；建库由谁做。

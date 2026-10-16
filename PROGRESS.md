# 进度与交接

依据《ScreenMemory_Brain_详细实现方案_独立版》（下称"方案"）。首期运行目标是 **Windows 10/11**；开发机可以是 macOS 或 Windows（见"开发环境"）。

协作方式：两人**轮流接力**开发整个项目，不按方案 8.1 的旧分工划范围。每轮结束更新本文件的"交接记录"和"下一步"，下一位从这里接着做。

## 一句话状态

- P0 最小闭环：已完成，Windows 实机通过（PR #1，尚未合入 `main`）。
- P1 采集节奏：等稳、排错模式、补拍、允许/排除规则、未保存变化的中断标记已实现（分支 `feat/capture-cadence`）。2026-10-13 晚在 **Windows 11 实机**跑通主要场景（见"验证记录"），30 分钟自动退出、快捷键被占用两项仍待实机确认。
- P1 向量基础设施、向量 worker、文字模式（词法 + 语义 RRF）、画面模式（多语言 CLIP）：已实现（分支 `feat/vector-search`），Windows 实机端到端通过。
- P1 页面接入：**已完成**。`app.py` 有"文字 / 画面"切换，查询模型按需加载并用 `st.cache_resource` 缓存，侧栏显示语义 / 全图向量待办，结果标注命中分支，详情页蓝框标语义贡献块。
- P1 证据浏览（前后浏览 + 并排）：**已实现**（分支 `feat/evidence-browse`，2026-10-15）。Mac 上逻辑测试 8/8、页面 AppTest 4/4 通过；Windows 浏览器里的排版与真实录制数据**待实机查看**。
- P1 "放宽匹配"按钮与 A05–A10 自动化验收：**已实现**（同一分支，2026-10-15）。Mac 上 13 条新测试通过（A05–A10 共 12 条 + 页面 1 条）；A11、A12 等第 10 项，A13 需 Windows 实机。
- P1 批量导入：**已实现**（同一分支，2026-10-15）。PNG/JPEG/WebP 逐文件校验、时间未知标注、批次幂等与失败重试、页面"批量导入截图"面板与命令行；数据库升到 **v4**。Mac 上 9 条新测试通过；Windows 待实机。
- P1 收藏 / 删除 / 到期 / 配额：**已实现**（分支 `feat/lifecycle`，2026-10-16，数据库 **v5**）。规则按主方案 5.2、5.4 + 团队 10-16 决定（见"待团队拍板"2）。Mac 上新增 21 条测试通过（含 A11、A12 可在 Mac 验证的部分、删除中后台写回、共享内容引用、缓存刷新）；Windows 待实机。
- P1 其余（资源测量、分类、评测）：未开始，见"下一步"。

## 交接记录（最新在上）

| 日期 | 分支 | 基于 | 本轮提交 / PR | 内容 | 验证状态 |
| --- | --- | --- | --- | --- | --- |
| 2026-10-16 | `feat/lifecycle`（新分支） | `feat/evidence-browse` @ `c35ae9e` | 本轮提交（见 `git log`）；草稿 PR #5（base `feat/evidence-browse`） | 收藏、删除本次记录 / 删除此画面的全部记录（先说明后确认）、30/90 天到期、配额与空间不足暂停（schema v5） | macOS：pytest 126/126（含 2 条真实模型）、pyflakes 无告警、5 处变异检查均被测试发现；Windows 未测 |
| 2026-10-15 | `feat/evidence-browse`（新分支） | `feat/vector-search` @ `40aad90` | `72f36ac`（证据浏览）、`da23ba5`（放宽匹配 + A05–A10）、批量导入提交（见 `git log`）；草稿 PR #4（base `feat/vector-search`） | 证据浏览：同一录制段内前后浏览、缺口/段边界提示、两图并排、切换到同帧的其他出现；"放宽匹配"按钮；A05–A10 自动化验收测试；批量导入（schema v4） | macOS：pytest 105/105（含 2 条真实模型）、pyflakes 无告警；Windows 未测 |
| 2026-10-14 | `feat/vector-search`（同一分支接力） | `95f4995` | 本轮提交（见 `git log`）；仍在草稿 PR #3 | 页面接入文字/画面模式；采集节奏与向量功能 Windows 实机验证 | Windows 11：pytest 71/71（含真实模型）、环境检查 9/9、实机端到端 35/35、页面 AppTest 16/16 |
| 2026-10-13 | `feat/vector-search` | `feat/capture-cadence` @ `244c19f` | `fad7ab7`（v3 基础设施）+ 本轮收尾提交（见 `git log`）；草稿 PR #3（base `feat/capture-cadence`） | 向量基础设施、向量 worker、文字/画面检索后端；**页面未接入（WIP）** | macOS 71/71（含 2 条真实模型冒烟）；Windows 未测 |
| 2026-10-13 | `feat/capture-cadence` | `feat/p0-closed-loop` @ `7bfe972` | `e0739a2`；草稿 PR #2（base `feat/p0-closed-loop`） | 采集节奏（P1） | macOS 单元测试 41/41；Windows 实机待验证 |
| 2026-10-13 | `feat/p0-closed-loop` | `main` @ `1d01dbe` | `7bfe972`；PR #1（base `main`，未合并） | P0 最小闭环 | Windows 11 实机通过 |

### 分支依赖与合并顺序

```
main (1d01dbe) ── feat/p0-closed-loop (7bfe972, PR #1) ── feat/capture-cadence (d325790, 草稿 PR #2) ── feat/vector-search (40aad90, 草稿 PR #3) ── feat/evidence-browse (c35ae9e, 草稿 PR #4) ── feat/lifecycle (草稿 PR #5)
```

截至 2026-10-16：PR #1–#4 都没有合并。`feat/lifecycle` 改动了 #4 引入的页面和导入、证据模块，所以从 `feat/evidence-browse` 的最新提交建，PR base 选 `feat/evidence-browse`，排在 #4 之后合并。

截至 2026-10-15：PR #1–#3 都没有合并，`main` 仍是 `1d01dbe`。新分支 `feat/evidence-browse` 依赖页面接入（`app.py` 在 PR #3 里改过），所以从 `feat/vector-search` 的最新提交建，PR base 选 `feat/vector-search`；按 #1 → #2 → #3 → #4 顺序合并，前一个合并后把下一个的 base 改成 `main`。

1. 先合 PR #1 到 `main`。**请用 "Create a merge commit"，不要 Squash**：Squash 会改写 P0 的提交，`feat/capture-cadence` 再合时会重复出现 P0 的全部改动。
2. PR #1 合并后，把采集节奏 PR 的 base 改成 `main`（PR 页面标题旁 Edit → base）。若 PR #1 已被 Squash，则在本地 `git fetch origin && git rebase --onto origin/main 7bfe972 feat/capture-cadence`，检查无误后用 `git push --force-with-lease origin feat/capture-cadence`（只覆盖自己的功能分支）。
3. 采集节奏在 Windows 实机验证通过后，再把草稿 PR 转为 Ready 并合并。
4. 之后的新任务从最新 `main` 建分支；如果依赖的 PR 还没合，就从那个功能分支建，并在本表写明"基于"哪一个提交。

## 下次恢复（2026-10-16，`feat/lifecycle`）

- 分支 `feat/lifecycle`，基于 `feat/evidence-browse` @ `c35ae9e`，草稿 PR #5（base `feat/evidence-browse`）。远程 10-16 无他人新提交，PR #1–#4 都未合并。
- **本轮完成**："下一步"第 10 项（收藏 / 删除 / 配额 / 到期），A11、A12 中可在 Mac 上验证的部分。新增 `screenrecall/lifecycle.py`、`tests/test_lifecycle.py`；改了 `db.py`（v5 `favorites`）、`config.py` 与 `config.default.toml`（`[retention]`）、`capture.py`（删除当前记录后不延长不重存、空间不足暂停）、`daemon.py`（维护线程）、`evidence.py`（段结束原因"存储空间不足"）、`app.py`（收藏、两种删除确认、我的收藏、侧栏存储状态、"原图已过期"文案）。
- **恢复（macOS）**：`git switch feat/lifecycle && git pull --ff-only`，`source .venv/bin/activate`，`python -m pytest tests --basetemp=data/pytest` 期望 126 passed。
- **升级注意**：新代码第一次启动后台会把库迁到 v5；页面遇到旧结构会提示先重启后台。
- **下一步**：Mac 上按依赖可做的是第 7 项的**测量脚本**（真实子进程内存、扩库到 1,500–2,000 帧测查询 P95，脚本在 Mac 上只做语法与小库冒烟，数字必须在 Windows 测试机上测）；第 11 项分类依赖第 13 项标注数据；第 13 项需要 Windows 录制。Windows 上优先跑"待验证"7–11。

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

后台和页面依赖 Windows 窗口 API（`winapi.py` 在非 Windows 上直接报错），**在 Mac 上不能运行录制和页面**，也不打算做成 macOS 产品。Mac 上能做的是：写代码、跑 `tests/`（假窗口替身 + 注入时钟 + 真实 SQLite/WebP），以及以后不依赖窗口 API 的模块（向量、检索、分类、评测脚本）。

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
11. 收藏 / 删除 / 到期 / 配额（`feat/lifecycle`）：浏览器里收藏、取消收藏、两种删除的确认文案与结果；录制进行中删除当前画面的记录后，后台不报错、画面不变时不重新存入；删除后图片文件确实从 `data/frames` 消失（Windows 上文件被页面或杀毒软件占用时会留下残留，下次后台启动时清除，需确认）；临时把 `quota_gib` 调小（如 0.05）验证按顺序清理、空间不足时暂停采集并在侧栏说明原因、调回后可重新开启；`measure_usage` 在上万张图片下的耗时；把系统时间或测试数据的时间往前调，验证 30/90 天到期。

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
- 按时间范围删除（方案 5.4）只有设计，没有入口。

## 下一步（按依赖排序）

标"Mac 可做"的可以在 macOS 上开发和做单元测试；标"需 Windows"的必须实机完成。

1. ~~采集节奏实机验证~~ **主要场景已通过**（2026-10-14，见"验证记录"）。剩下的是"待验证" 1–5，可以与后续开发并行。通过后草稿 PR 转 Ready。
2. **合并 PR #1，再合采集节奏 PR**：按"分支依赖与合并顺序"。依赖：1。
3. ~~向量基础设施 schema v3~~ **已完成**（Mac 可做）：`text_blocks`（共享文本块，复用键 = 文本 + 规范化版本 + 模型版本）、帧-块关联（行号、坐标）、`embeddings`（目标 ID、分支、模型/预处理版本、维度、数据类型、向量）；OCR 完成后登记 `text_embed`、`image_embed` 待办；写回校验 `worker_token` 与 `record_version` 并 `bump_revision`。方案 3.3、3.7、5.1。依赖：2（迁移接在 v2 之后，追加到 `db._MIGRATIONS`，不要改 v2）。
4. ~~文本语义分支~~ **后端已完成**（Mac 可做）：MiniLM（paraphrase-multilingual-MiniLM-L12-v2）按真实分词器分块（≤128 token，含特殊 token）、帧级取最高块分数、Top-50 帧；文字模式两路 RRF（k=60，同分按最新时间再帧 ID）。方案 5.1–5.3。依赖：3。
5. ~~全图分支与画面模式~~ **后端已完成**（Mac 可做）：clip-ViT-B-32 图像向量（保持比例补边）、clip-ViT-B-32-multilingual-v1 文本查询；只检索原图可用的帧。依赖：3。
6. ~~页面查询缓存与模式切换~~ **已完成**（2026-10-14，Windows 实机 + AppTest）。
7. **两进程合计内存与延迟测量**（需 Windows 测试机）——可与第 9 项并行：方案 7.3。先写一个可提交的测量脚本，统计后台和页面的真实 Python 子进程，再用复制数据扩大到 1,500–2,000 帧测查询 P95。复制数据只能用于资源压测，不能用于准确率评估。依赖：6。
8. ~~证据浏览~~ **已完成**（2026-10-15，`feat/evidence-browse`，Mac 逻辑 + AppTest；Windows 待查看，见"待验证"7）：只在选中出现的录制段内前后浏览，跨缺口和段边界提示，不拼接其他录制段；两图并排不算差异；可切到同帧的另一次出现。
9. ~~"放宽匹配"按钮与 A05–A13 自动化测试~~ **已完成可在 Mac 上做的部分**（2026-10-15，`feat/evidence-browse`）：放宽按钮 + A05–A10。A11、A12 随第 10 项补；A13 见"待验证"9。
10. ~~收藏 / 删除 / 配额 / 30·90 天到期~~ **已完成**（2026-10-16，`feat/lifecycle`，Mac 逻辑 + AppTest；Windows 待验证，见"待验证"11）。未做：按时间范围删除的入口（方案 5.4 提到，首期页面只有两种删除）。
11. **分类**（Mac 可做训练与评测）：全图向量 + Logistic Regression，对照多数类与进程名规则，按任务/批次分组划分。方案 5.3、7.2。依赖：5 和标注数据（13）。
12. ~~批量导入~~ **已完成**（2026-10-15，`feat/evidence-browse`；Windows 待验证，见"待验证"10）：PNG/JPEG/WebP，导入批次来源、时间未知标注、批次幂等。方案 6.8。依赖：3。
13. **受控数据采集与标注、40 条查询真值**（需 Windows 录制）：方案 7.1。依赖：1（采集节奏稳定后再录）。
14. **五组检索实验、资源与存储测量、报告与演示**：方案 7.2、7.3、8.2。依赖：4、5、11、13。

## 待团队拍板

1. 空输入：现按方案 5.2 提示"请输入要查找的文字"（旧版是展示最近记录）。
2. ~~删除整幅画面时收藏怎么处理~~ **已定（2026-10-16）**：用户明确选择"删除此画面的全部记录"时，界面说明会删除哪些出现记录及关联收藏，确认后执行；到期或配额触发的自动清理保护收藏；删除单次出现记录时不误删仍被其他记录引用的共享内容。
3. 页面文案目前是中文，演示视频若需英文需替换。
4. `unsaved_change` 缺口是否在实机数据后合并短间隔（见"已知问题"）。

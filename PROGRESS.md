# 进度与交接

依据《ScreenMemory_Brain_详细实现方案_独立版》（下称"方案"）。首期运行目标是 **Windows 10/11**；开发机可以是 macOS 或 Windows（见"开发环境"）。

协作方式：两人**轮流接力**开发整个项目，不按方案 8.1 的旧分工划范围。每轮结束更新本文件的"交接记录"和"下一步"，下一位从这里接着做。

## 一句话状态

- P0 最小闭环：已完成，Windows 实机通过（PR #1，尚未合入 `main`）。
- P1 采集节奏：等稳、排错模式、补拍、允许/排除规则、未保存变化的中断标记已实现（分支 `feat/capture-cadence`）。只在 macOS 上用替身做过逻辑测试，**Windows 实机待验证**。
- P1 向量基础设施（schema v3、文本块分块与复用、OCR 后登记向量待办）：已实现（分支 `feat/vector-search`），Mac 单元测试通过。
- P1 向量 worker、文字模式（词法 + 语义 RRF）、画面模式（多语言 CLIP）后端：已实现，Mac 上用真实模型做过冒烟测试；**页面尚未接入**（`app.py` 仍只调用词法 `search.search`，没有文字/画面切换），见"下次恢复"。
- P1 其余（语义/全图检索、证据浏览、分类、生命周期、导入、评测）：进行中，见"下一步"。

## 交接记录（最新在上）

| 日期 | 分支 | 基于 | 本轮提交 / PR | 内容 | 验证状态 |
| --- | --- | --- | --- | --- | --- |
| 2026-10-13 | `feat/vector-search` | `feat/capture-cadence` @ `244c19f` | `fad7ab7`（v3 基础设施）+ 本轮收尾提交（见 `git log`）；草稿 PR #3（base `feat/capture-cadence`） | 向量基础设施、向量 worker、文字/画面检索后端；**页面未接入（WIP）** | macOS 71/71（含 2 条真实模型冒烟）；Windows 未测 |
| 2026-10-13 | `feat/capture-cadence` | `feat/p0-closed-loop` @ `7bfe972` | `e0739a2`；草稿 PR #2（base `feat/p0-closed-loop`） | 采集节奏（P1） | macOS 单元测试 41/41；Windows 实机待验证 |
| 2026-10-13 | `feat/p0-closed-loop` | `main` @ `1d01dbe` | `7bfe972`；PR #1（base `main`，未合并） | P0 最小闭环 | Windows 11 实机通过 |

### 分支依赖与合并顺序

```
main (1d01dbe) ── feat/p0-closed-loop (7bfe972, PR #1) ── feat/capture-cadence (244c19f, 草稿 PR #2) ── feat/vector-search (fad7ab7 + 收尾提交, 草稿 PR #3)
```

1. 先合 PR #1 到 `main`。**请用 "Create a merge commit"，不要 Squash**：Squash 会改写 P0 的提交，`feat/capture-cadence` 再合时会重复出现 P0 的全部改动。
2. PR #1 合并后，把采集节奏 PR 的 base 改成 `main`（PR 页面标题旁 Edit → base）。若 PR #1 已被 Squash，则在本地 `git fetch origin && git rebase --onto origin/main 7bfe972 feat/capture-cadence`，检查无误后用 `git push --force-with-lease origin feat/capture-cadence`（只覆盖自己的功能分支）。
3. 采集节奏在 Windows 实机验证通过后，再把草稿 PR 转为 Ready 并合并。
4. 之后的新任务从最新 `main` 建分支；如果依赖的 PR 还没合，就从那个功能分支建，并在本表写明"基于"哪一个提交。

## 下次恢复（2026-10-13 收尾时的状态）

本轮在用户要求下停止，没有开始新功能。当前状态：

- 分支 `feat/vector-search`，草稿 PR #3（base `feat/capture-cadence`），所有源码已提交推送；README 已按分支实际内容更新（`feat/capture-cadence` 的 README 提交 `d325790` 已合并进本分支 `fd0dcca`，本分支再单独改写 README）；本地只有被忽略的 `.venv/`、`data/`（不提交）。
- **已完成并测试**：schema v3、分块与共享块、OCR 后登记向量待办、向量 worker（`embed.py`）、检索编排（`retrieval.py`：文字模式 RRF、画面模式）。
- **未完成（WIP）**：页面接入。`app.py` 还没有"文字 / 画面"切换，没有用 `st.cache_resource` 加载查询模型，结果卡片也没有展示 `sources`、`semantic_lines` 高亮和 `notes`（索引覆盖说明）。后端接口已就绪：`retrieval.run(conn, parsed, filters, mode=..., engines=retrieval.Engines(...))`。
- **模型状态**：三套模型已按锁定 revision 下载到本机 `~/.cache/huggingface/hub`（只下载了 sentence-transformers 实际加载的文件，见 `models.ALLOW_PATTERNS`）；`python -m screenrecall.models` 输出"模型齐全"；离线加载正常（MiniLM 384 维、max_seq_length 128；CLIP 图像/多语言文本均 512 维）。本轮遇到的问题是"检查函数把部分快照判为不完整"导致真实模型测试被跳过，已通过让检查与下载使用同一 `ALLOW_PATTERNS` 修复，之后 2 条真实模型测试通过。没有其他未解决的模型问题。
- **测试结果**（macOS，`.venv` Python 3.12.15）：`python -m pytest tests` → 71 通过、0 失败、0 跳过（未下载模型的机器上 2 条真实模型测试会自动跳过）。pyflakes 无告警。均为模拟/单元测试，不代表 Windows 实机通过。
- **后台进程**：本轮启动的模型下载进程已自行结束（日志 `data/logs/model_download.log` 末尾为 `ALL DONE`）；没有启动后台 daemon、Streamlit 或常驻推理进程。

恢复步骤（macOS）：

```bash
cd "Practice Module 2/ScreenRecall"
git fetch origin && git switch feat/vector-search && git pull --ff-only
source .venv/bin/activate              # 不存在则按"开发环境 → macOS"重建，再：
# uv pip install -r requirements-dev.txt torch==2.14.1 transformers==5.19.0 sentence-transformers==6.1.0 huggingface-hub==1.33.0 scikit-learn==1.9.1 streamlit==1.65.0 pyflakes==3.4.0
# （Mac 上未安装 rapidocr/opencv，OCR 由测试替身代替）
python -m screenrecall.models          # 应输出"模型齐全"；否则 python -m screenrecall.models --download
python -m pytest tests                 # 期望 71 passed
```

接着做"下一步"第 6 项（页面接入）：在 `app.py` 加模式切换，用 `st.cache_resource` 按 `model_id` 缓存 `models.StText`（MiniLM、多语言 CLIP 文本）与 `retrieval.TextVectorIndex` / `ImageVectorIndex`，调用 `retrieval.run`；侧栏增加文本/全图向量待办数与最老年龄（`db.index_stats` 已提供 `text_backlog`、`image_backlog` 等）；用 Streamlit AppTest + 假 winapi 在 Mac 上测页面。完成后在 PR #3 勾选阶段 4。

## 开发环境

### Windows（运行与实机验证）

```powershell
git fetch origin
git switch feat/capture-cadence
pip install -r requirements.txt          # Python 3.12
python -m screenrecall.daemon --check    # 8 项环境检查应全部 [ OK ]
python -m screenrecall.daemon            # 终端 1
streamlit run app.py                     # 终端 2，http://127.0.0.1:8501
python -m pip install -r requirements-dev.txt; python -m pytest tests
```

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

### 待验证（需要 Windows 实机）

1. `python -m screenrecall.daemon --check` 仍 8/8；启动日志出现 `hotkey registered Ctrl+Alt+S`；已有的 v1 数据库能自动升级到 v2 且旧数据可搜。
2. 录记事本：打一个字 → 3 秒内存 1 张；持续打字 → 约 10 秒存 1 张；停下 → 下一次采样存最终画面。记录 1% 阈值在真实窗口（光标闪烁、滚动、动画）下是否合适。
3. 排错模式：快速切换内容 → 日志 `trigger=auto/held` 间隔 ≥ 2 秒；侧栏倒计时走动；「退出排错模式」立即生效；30 分钟到时自动退出（可临时把 `debug_max_minutes` 改成 1 验证）。
4. 按 `Ctrl+Alt+S` → 侧栏显示补拍结果，出现记录「来源」为补拍；快捷键被占用时侧栏提示注册失败、页面按钮仍可补拍；后台退出时热键线程正常结束（Ctrl+Break 后进程不残留）。
5. Edge InPrivate 窗口不能开始录制；录制普通窗口时标题变成含关键词 → 侧栏提示排除、`gaps` 有 `excluded`、不产生截图。
6. 页面新按钮在 Streamlit 1.65 下显示和交互正常（fragment 内按钮、倒计时刷新）。
7. Windows 上安装 `requirements.txt` 中的 torch / sentence-transformers 版本、`python -m screenrecall.models --download` 后离线启动，环境检查"向量模型"为 OK，日志出现 `text_embed done` / `image_embed done`。
8. 实机 1,500–2,000 帧底库下：后台 OCR + 向量的吞吐与积压、两进程合计内存、文字/画面热查询 P95（方案 7.3，必须在指定测试机上测）。

## 已知问题

- 采集节奏：每次"变化后等稳"都会写一条 `unsaved_change` 缺口，正常使用时行数会较多；光标闪烁的终端会在两张画面之间来回复用并产生大量出现记录。均符合方案字面规则，待实机数据决定是否合并短缺口或调整阈值。
- 排错模式在少见时序下（候选在限频中保留，下一次仍未到允许时间而画面已回到原内容）会把同一画面的出现拆成两段，时间仍正确。
- 快捷键注册与消息循环只做了语法检查，未在 Windows 运行过。
- Mac 上无法运行后台与页面（设计如此，见"开发环境"）。
- 每个 OCR 文字框作为一行、以换行连接，跨两个框的原串搜不到；OCR 有误读（实测 `under_score` → `under score`）。
- 部分硬件加速窗口 PrintWindow 可能取不到内容，仅前台时退回屏幕拷贝，否则记缺口。
- DPI 不感知程序按逻辑分辨率保存，比屏幕显示小。
- WebP q92、1% 阈值、OCR 线程数、重试次数、积压 50 等是初值，未在固定测试机测量冻结。
- 页面无登录鉴权，只绑定 127.0.0.1。

## 下一步（按依赖排序）

标"Mac 可做"的可以在 macOS 上开发和做单元测试；标"需 Windows"的必须实机完成。

1. **采集节奏实机验证**（需 Windows）：按上面"待验证"1–6 执行，记录结果；通过后草稿 PR 转 Ready。依赖：无。
2. **合并 PR #1，再合采集节奏 PR**：按"分支依赖与合并顺序"。依赖：1。
3. ~~向量基础设施 schema v3~~ **已完成**（Mac 可做）：`text_blocks`（共享文本块，复用键 = 文本 + 规范化版本 + 模型版本）、帧-块关联（行号、坐标）、`embeddings`（目标 ID、分支、模型/预处理版本、维度、数据类型、向量）；OCR 完成后登记 `text_embed`、`image_embed` 待办；写回校验 `worker_token` 与 `record_version` 并 `bump_revision`。方案 3.3、3.7、5.1。依赖：2（迁移接在 v2 之后，追加到 `db._MIGRATIONS`，不要改 v2）。
4. ~~文本语义分支~~ **后端已完成**（Mac 可做）：MiniLM（paraphrase-multilingual-MiniLM-L12-v2）按真实分词器分块（≤128 token，含特殊 token）、帧级取最高块分数、Top-50 帧；文字模式两路 RRF（k=60，同分按最新时间再帧 ID）。方案 5.1–5.3。依赖：3。
5. ~~全图分支与画面模式~~ **后端已完成**（Mac 可做）：clip-ViT-B-32 图像向量（保持比例补边）、clip-ViT-B-32-multilingual-v1 文本查询；只检索原图可用的帧。依赖：3。
6. **页面查询缓存与模式切换**（Mac 写代码，需 Windows 联调）——**下次从这里开始**：`st.cache_resource` 按模型版本缓存、按 `meta.index_revision` 刷新；文字/画面两种模式；向量未就绪时显示索引状态。依赖：4、5。
7. **两进程合计内存与延迟测量**（需 Windows 测试机）：方案 7.3。依赖：6。
8. **证据浏览**（Mac 可做逻辑与测试）：沿所选出现的 session 前后浏览，遇 `gaps`（含 `unsaved_change`、`excluded`）提示；两图并排。方案 6.1。依赖：2（与 3–7 可并行）。
9. **"放宽匹配"按钮与 A05–A13 自动化测试**（Mac 可做）：方案 5.2、8.3。依赖：2。
10. **收藏 / 删除 / 配额 / 30·90 天到期**（Mac 可做逻辑）：逻辑失效先于文件回收、共享文本块引用计数、删除中推理不复活。方案 3.2、6.4–6.7。依赖：3（共享块与向量要一起回收）。
11. **分类**（Mac 可做训练与评测）：全图向量 + Logistic Regression，对照多数类与进程名规则，按任务/批次分组划分。方案 5.3、7.2。依赖：5 和标注数据（13）。
12. **批量导入**（Mac 可做）：PNG/JPEG/WebP，导入批次来源、时间未知标注、批次幂等。方案 6.8。依赖：3。
13. **受控数据采集与标注、40 条查询真值**（需 Windows 录制）：方案 7.1。依赖：1（采集节奏稳定后再录）。
14. **五组检索实验、资源与存储测量、报告与演示**：方案 7.2、7.3、8.2。依赖：4、5、11、13。

## 待团队拍板

1. 空输入：现按方案 5.2 提示"请输入要查找的文字"（旧版是展示最近记录）。
2. 删除整幅画面时收藏怎么处理：方案为影响全部相关收藏；旧版默认排除收藏。做第 10 项前要定。
3. 页面文案目前是中文，演示视频若需英文需替换。
4. `unsaved_change` 缺口是否在实机数据后合并短间隔（见"已知问题"）。

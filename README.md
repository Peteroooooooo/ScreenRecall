# ScreenRecall

轻量本地屏幕记忆系统（NUS-ISS PRS Practice Module，Group 9）。用原词、语义或大致画面描述找回保存过的屏幕，并回到原图核对来源与时间。产品首期目标是 Windows 10/11 单用户、本地 CPU 推理。实施依据：《ScreenMemory_Brain_详细实现方案_独立版》与 `readable_Proposal.md`。

## 当前分支状态：`feat/dataset-collection`

| 项目 | 状态 |
| --- | --- |
| 基于 | `feat/lifecycle` @ `1e124b5`（草稿 PR #5）← `feat/evidence-browse`（草稿 PR #4）← `feat/vector-search`（草稿 PR #3）← `feat/capture-cadence`（草稿 PR #2）← `main`（已含 P0，PR #1 于 10-16 合入） |
| PR | 草稿 [PR #6](https://github.com/Peteroooooooo/ScreenRecall/pull/6)，base `feat/lifecycle`；合并顺序 #2 → #3 → #4 → #5 → 本 PR，详见 [PROGRESS.md](PROGRESS.md) |
| 本分支新增 | 受控数据集的**采集与标注规程**（[datasets/COLLECTION.md](datasets/COLLECTION.md)）、80 个任务的任务表、40 条查询的分配表、清单 / 事实 / 标注模板；工具 `python -m screenrecall.collect`（清单、检查、导入、标注表、事实→相关帧）和 `python -m screenrecall.indexer`（不采集、只跑 OCR 与向量，Mac 上可用）；导入支持逐张的应用、窗口标题和捕获时间 |
| 测试 | **macOS**：`pytest tests` 169/169（继承 151 + 新增 18）；新增中 1 条用真实 RapidOCR，其余是逻辑测试；另在 Mac 上用真实 OCR + 真实向量模型跑通了一遍 manifest → check → import → indexer → labels 命令行流程（4 张合成文字图，只验证流程） |
| 数据 | **还没有采集任何真实数据**，没有标注、分类结果或检索结果 |
| Windows 实机 | 本分支新增内容未在 Windows 上运行；`collect` / `indexer` 不依赖窗口 API，预期可用，**待验证** |

## 本分支新增内容（受控数据采集与标注，方案 6.1、6.2、6.4）

| 能力 | 做了什么 | 代码 / 文件 | 验证 |
| --- | --- | --- | --- |
| 规程 | 两人共用的采集方式、目录与命名、有效帧标准、去重与补采、标注字段与类别规则、分组划分、40 条查询与盲测隔离、合并流程、小批试采步骤 | `datasets/COLLECTION.md` | 文档 |
| 任务表与查询分配 | 每人每场景 10 个任务 × 约 25 帧；查询每场景 10 条、开发 / 盲测各 5、按场景交替中英 2/3 与 3/2，每人 20 条且中英各 10 | `datasets/collection/tasks.csv`、`query_slots.csv` | 分配数量用脚本核对过 |
| 采集清单 | `collect manifest <任务文件夹>`：逐张一行，自动填编号、指纹、尺寸；捕获时间只从 macOS / Windows 截图工具写进文件名的时间取，取不到记 `unknown`；重复运行保留人工填写的列 | `screenrecall/collect.py` | Mac 测试 |
| 检查 | `collect check <根目录>`：必填字段、类别与语言取值、事实编号、短边 ≥500、像素完全相同、跨人跨任务近重复（差分哈希初筛 + 缩略图逐像素确认）、任务内近重复要写变化说明、每任务 ≤40、各采集者 / 场景数量 | `collect.py` | Mac 测试；近重复阈值在合成文字画面上核对过（不同文字不算、只改一处算），**未在真实截图上校准** |
| 导入 | `collect import`：检查不过就拒绝；每个任务一个导入批次，带清单里的应用、窗口标题和捕获时间；已导入的按指纹跳过，补采后不会重复入库 | `collect.py`、`importer.py`（新增逐文件 `FileMeta`） | Mac 测试 |
| 只建索引 | `python -m screenrecall.indexer --data-dir DIR`：复用后台同一套 OCR / 向量 worker，不启动采集，跑完退出并报告剩余和失败数 | `screenrecall/indexer.py` | Mac 上真实 RapidOCR 测试 + 真实向量模型命令行跑通 |
| 标注表 | `collect labels`：生成 `dataset.py` 格式的标注 CSV，附采集者、图片编号、原图路径、事实、OCR 字数、时间是否已知；两人各标分表，再合成总表，同一帧填法不一致会报错；复核人不能与标注人相同（`dataset check` 新增） | `collect.py`、`dataset.py` | Mac 测试 |
| 查询相关帧 | `collect facts --queries`：按清单里的事实标记填 `relevant`，已有答案不覆盖 | `collect.py` | Mac 测试 |

## 继承自 `feat/lifecycle`（P1 收藏 / 删除 / 到期 / 配额，方案 5.2、5.4，A11、A12）

`feat/lifecycle` 的新增内容已于 2026-10-16 在 Windows 11 实机验证（肖天奕，提交 `0fd523d`；分类与评测脚本未在 Windows 上验证），详见 PROGRESS「验证记录」。

规则以 `readable_Proposal.md` 5.2、5.4 为准，加团队 2026-10-16 的决定：用户明确选择"删除此画面的全部记录"时，页面先列出将删除的出现记录和其中的收藏，确认后执行；到期和配额触发的自动清理一律保护收藏。

| 能力 | 做了什么 | 代码 | 验证 |
| --- | --- | --- | --- |
| 收藏 | 收藏挂在"某一次出现"上，保护该记录及其画面的原图、文字、特征和来源；结果卡片标"★ 已收藏"，主区"我的收藏"列表可直接打开；取消收藏后按普通规则处理；不重新编码，不能恢复收藏前已过期的原图 | `lifecycle.py`、`app.py`、`db.py`（v5） | Mac 测试 + AppTest |
| 删除本次记录 | 只删这一次出现的时间与来源（及它自己的收藏）；同一画面还有其他出现时图片、OCR、向量保留；最后一个引用消失才回收。确认框写明是哪种情况 | `lifecycle.delete_occurrence` | Mac 测试 + AppTest |
| 删除此画面的全部记录 | 先列出全部出现记录与将一并删除的收藏，确认后执行；确认后又多出未展示的收藏时拒绝并刷新列表 | `lifecycle.plan_frame_delete`、`delete_frame` | Mac 测试 + AppTest |
| 按时间范围删除 | 主区"按时间范围删除"：选日期、时间和可选应用，先预览将删除的出现记录数、将完全删除和仍被范围外记录引用的画面数、范围内收藏数；默认保留收藏，勾选后才删；确认后分批短事务执行 | `lifecycle.plan_range_delete`、`delete_range`、`app.py` | Mac 测试 + AppTest |
| 独立测试库配置 | 数据目录里的 `config.toml` 只作用于这份数据（例如把测试库的配额调小），不影响正式数据；`python -m screenrecall.db` 只读显示结构版本 | `config.py`、`db.py` | Mac 测试 |
| 联动回收 | 同一短事务里：删出现 → 画面标 `deleted` 且版本号 +1 → 取消待办 → 删 FTS、OCR 行、帧-块关联、整帧向量 → 共享文本块只在无引用时回收 → 索引修订号 +1；提交后才删图片文件，残留文件由后台启动时清除 | `lifecycle.py` | Mac 测试（含 5 处变异检查） |
| 删除期间的后台写回 | OCR、文本向量、全图向量在推理中途遇到删除或原图过期时，写回被丢弃，不复活已删数据；采集线程不再延长已删的当前记录，画面不变时也不立刻重新存入（画面变化或手动补拍后恢复） | `ocr.py`/`embed.py`（继承的版本校验）、`capture.py` | Mac 测试 |
| 缓存刷新 | 删除与过期都递增索引修订号，页面下次查询前重载向量矩阵 | `retrieval.py`（继承） | Mac 测试 |
| 到期 | 不再被近 30 天记录或收藏引用的原图过期（删文件，文字与特征保留，详情页显示"原图已过期"，画面模式不再返回）；90 天前的非收藏出现记录删除，画面无引用后回收；OCR 未完成的帧不参与 | `lifecycle.expire` | Mac 测试（注入时间） |
| 配额 | 按数据目录实际文件占用（图片、数据库含 WAL、日志、缓存）与磁盘剩余判断，含 256 MiB 预留余量；依次：清理到期数据 → 可重建缓存（`cache/orphans` 除外）→ 最旧非收藏原图 → 最旧非收藏记录；删记录未使文件变小就停，不删光历史；仍不足时暂停采集并说明原因，期间拒绝开启录制 | `lifecycle.enforce_quota`、`daemon.py`、`capture.py` | Mac 测试（占用与磁盘剩余用注入函数） |
| 测量工具（方案 7.3） | `python -m screenrecall.bench`：`expand` 复制数据目录并用复制数据扩到 1,500–2,000 帧（每份复制有自己的文本块和加了少量噪声的向量，矩阵规模随帧数增长；只用于资源 / 速度测量）；`query` 测文字 / 画面的冷查询与热查询，分开记录查询编码和读库 + 排序的 P50 / P95；`readiness` 统计抓拍到可搜索 / 语义就绪 / 全图就绪的延迟并列出未完成、失败、取消数；`mem` 按命令行找到后台与页面并带上全部子进程，采样 CPU（单核与整机两种口径）、RSS 与 USS | `bench.py` | Mac 冒烟：统计口径与扩库结构测试，真实模型下空库查询能跑；**没有测量结论** |
| 标注与分组划分（6.1） | `python -m screenrecall.dataset`：导出标注清单 CSV、按冻结类别表校验（复核结论、改动理由）、按来源组划分（同任务 / 录制段、同像素、同 OCR 文字强制同组，比例必须显式给出）；流程见 `datasets/README.md` | `dataset.py` | 合成数据流程测试 |
| 内容分类器（6.4） | `python -m screenrecall.classify train`：在冻结的全图向量上训练 LR，只在 val 上选超参数，test 评一次，同时报告多数类和进程名规则基线（Macro F1、逐类、混淆矩阵）；产物是 npz + JSON；配置 `[classify] model_dir` 后向量 worker 给新画面写预测，页面卡片显示"类别（模型预测）"，类别筛选需用户主动选择 | `classify.py`、`embed.py`、`search.py`、`app.py`、`db.py`（v6） | 合成数据流程测试 + AppTest；**没有真实标注，没有分类结果** |
| 五组检索实验（6.2） | `python -m screenrecall.evaluate`：Exp 1 词法、Exp 2 语义、Exp 3 全图、Exp 4 文字模式 RRF、Exp 5 三路 RRF（仅离线），同一候选与筛选；Hit@5、MRR@10 按整体 / 场景 / 语言给命中数 / 查询数，逐条结果保留失败；先 dev、冻结后只跑一次 blind | `evaluate.py` | 替身编码器流程测试；**没有查询真值和实验结果** |
| 状态显示 | 侧栏显示运行数据 / 配额、磁盘剩余、收藏数、保留天数、上次维护时间；空间不足时显示暂停原因 | `app.py` | 代码已实现，侧栏未单独测试 |

| 能力 | 做了什么 | 代码 | 验证 |
| --- | --- | --- | --- |
| 录制段时间线 | 以用户选中的那次出现为中心，只取它所在录制段的出现，按时间排序；原图过期的出现保留并说明，已删除画面跳过 | `screenrecall/evidence.py` | Mac 测试（真实 Recorder 生成数据） |
| 缺口与段边界提示 | 相邻两次出现之间的锁屏、最小化、取不到画面、排除规则、`unsaved_change` 等缺口逐一提示（同类合并计数），说明不能当成连续录像；到段首/段尾时显示段的结束原因（暂停、切换窗口、后台退出、异常未结束……），不自动跳到别的录制段 | `evidence.py`、`app.py` | Mac 测试 |
| 前后浏览 | 详情页"上一条 / 下一条 / 回到搜索结果"；换到别的画面后不再沿用搜索结果的语义高亮，原词高亮照常 | `app.py` | AppTest |
| 并排比较 | 选"上一条"或"下一条"与当前并排两张原图，不计算文字或表格差异 | `app.py` | AppTest |
| 切换出现 | 同一画面出现过多次时，可选另一次出现，前后浏览随之换到那次出现的录制段 | `app.py` | 代码已实现，未单独测试 |
| 放宽匹配 | 严格规则下引号原串无匹配时，页面显示规则说明和"放宽匹配"按钮，**只在用户点击后**忽略大小写和全部空白（含 OCR 行间换行）；从日期/应用筛选后的全部文字重新扫描，所有原串仍为 AND；命中跨行时各行都高亮；可"恢复严格匹配" | `search.py`、`retrieval.py`、`textutil.py`、`app.py` | Mac 测试 + AppTest |
| 验收测试 | `tests/test_acceptance.py` 按方案 8.3 编号覆盖 A05 短词、A06 引号与放宽、A07 帧级排名、A08 OCR 失败、A09 重启恢复、A10 删除后不再返回 | `tests/` | 替身 OCR / 编码器下的逻辑验证，不等于实机验收 |
| 批量导入 | 页面"批量导入截图"面板或 `python -m screenrecall.importer <文件或目录>`：只收 PNG/JPEG/WebP 静态图（按实际格式判断）；逐文件校验解码、尺寸和磁盘余量（写入后至少剩 2 GiB），逐项给出成功/失败/跳过原因，坏文件不阻塞后续；通过后走与实时截图相同的 WebP 落盘 → OCR → 向量流水线 | `importer.py`、`app.py` | Mac 测试；上传控件未自动化测试 |
| 导入时间与批次 | 捕获时间只用用户给的值，否则记导入时间并显示"捕获时间未知"，不用文件修改时间；每批一个导入录制段，前后浏览按文件顺序并说明不是连续屏幕历史；批次指纹去重，重交同一批次只重试失败的文件 | `importer.py`、`evidence.py`、`db.py`（v4：`import_batches`、`import_items`） | Mac 测试 + AppTest |

## 累计功能

| 来源 | 能力 | 状态 |
| --- | --- | --- |
| 继承 P0（`feat/p0-closed-loop`） | 指定窗口开启/暂停与 ACK、定时采样与精确复用、内容与出现分开存、缺口、WebP 原子落盘、RapidOCR 与持久待办、引号原串/短词/FTS5 词法检索、日期/应用筛选、原图查看与命中行高亮 | 已实现，Windows 11 实机通过 |
| 继承采集节奏（`feat/capture-cadence`） | 等稳保存、`unsaved_change` 中断标记、排错模式、页面/快捷键补拍、允许/排除规则、数据库 v2 | 已实现；Windows 实机主要场景通过（30 分钟自动退出、快捷键被占用待验证） |
| 继承向量检索（`feat/vector-search`） | 数据库 v3、文本块与向量、向量任务处理、文字模式（词法 + 语义 RRF）、画面模式（多语言 CLIP）、页面文字/画面切换与模型缓存 | 已实现；真实模型测试通过（Mac 与 Windows）；Windows 实机端到端通过（肖天奕 10-14 记录） |
| 继承证据浏览（`feat/evidence-browse`） | 证据浏览与并排、放宽匹配、A05–A10 自动化验收、批量导入、数据库 v4 | 已实现；Windows 11 实机验证通过（10-16，肖天奕） |
| 继承 `feat/lifecycle` | 收藏、三种删除、30/90 天到期、配额与空间不足暂停、A11/A12 可在 Mac 上验证的部分、数据库 v5/v6；测量工具；标注 / 划分工具与分类器流程；检索评测脚本 | 已实现；收藏删除到期配额 Windows 实机通过（10-16）；分类与评测只用合成数据 / 替身编码器验证流程；**没有测量数字、分类结果和检索实验结果** |
| 本分支新增 | 受控数据采集与标注规程、任务表与查询分配、`collect` 与 `indexer` 工具 | 已实现；Mac 测试通过；**尚无真实数据** |

还没有开始：真实数据采集与标注（规程和工具已就绪，见 `datasets/COLLECTION.md`）、40 条查询真值、分类与五组检索实验的真实运行和报告、维护页人工重试、资源测量的实际运行（脚本已写，未在测试机上跑）、A13 实机核验。顺序见 PROGRESS.md「下一步」。

## 运行

### Windows（产品运行）

需要 Python 3.12，建议在仓库内单独建 `.venv`。国内网络、代理和冷启动时间的注意事项见 PROGRESS.md「开发环境 → Windows」。

```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m screenrecall.models --download   # 联网一次，下载锁定版本的三套模型

# 终端 1（仓库根目录）：后台。只做环境检查可加 --check（"向量模型"一项应为 OK）
python -m screenrecall.daemon

# 终端 2（仓库根目录）：页面，打开 http://127.0.0.1:8501
streamlit run app.py
```

后台会在 OCR 之后补算向量。页面里选"文字"或"画面"模式搜索；首次语义或画面查询要加载模型，本机约 1–2 分钟。

### macOS（开发与单元测试）

后台和页面依赖 Windows 窗口 API，Mac 上不能运行录制和页面；可以跑测试（含真实模型冒烟）：

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt torch==2.14.1 transformers==5.19.0 sentence-transformers==6.1.0 huggingface-hub==1.33.0 scikit-learn==1.9.1
python -m screenrecall.models --download   # 可选：不下载则 2 条真实模型测试自动跳过
python -m pytest tests --basetemp=data/pytest   # 本机默认临时目录建不了 SQLite 时加 --basetemp
# 在 Mac 上导入截图并跑真实 OCR（数据集准备，见 datasets/COLLECTION.md）还需要：
uv pip install rapidocr-onnxruntime==1.4.4 onnxruntime==1.20.1 opencv-python==5.0.0.93
python -m screenrecall.indexer --data-dir data-dataset
```

## 配置与数据

- 默认配置在 `config.default.toml`；本机覆盖写到 `config.local.toml`（不提交），只写要改的键。向量检索分支（继承）新增 `[models]`（模型与 revision、线程数）、`[index]`（OCR 高水位、向量重试）、`[search]`（RRF k、每路 Top-N）。本分支新增 `[retention]`：`image_days`、`record_days`、`quota_gib`、`min_free_gib`、`reserve_mib`、`maintenance_interval_seconds`。
- 运行数据默认在 `data/`（不提交）：`frames/` 图片、`screenrecall.sqlite3`、`logs/`、`cache/`。也可用环境变量 `SCREENRECALL_DATA_DIR` 指定。模型缓存在 Hugging Face 默认目录，不在仓库内。
- 页面只绑定 127.0.0.1，没有登录鉴权；日志不写 OCR 全文。只录制受控测试内容，不要录真实密钥和私人聊天。

## 代码结构

```
app.py                    Streamlit 页面：录制控制、文字 / 画面两种搜索、原图核验、前后浏览与并排
screenrecall/
  daemon.py               后台入口：环境检查、单实例锁、恢复、采集/OCR/向量线程与控制循环
  capture.py              采样、等稳/排错节奏、精确复用、出现记录、缺口与中断标记、补拍
  cadence.py              记录节奏（纯逻辑）
  rules.py                允许/排除规则
  hotkey.py               全局快捷键补拍
  winapi.py               窗口枚举、PrintWindow 捕获、DPI、锁屏检测、热键消息循环（ctypes）
  storage.py              WebP 编码、原子落盘、启动核查
  ocr.py                  RapidOCR 封装与 OCR 待办处理；完成后登记向量任务
  vectors.py              规范化、分块、文本块复用、向量 BLOB
  models.py               锁定版本的模型与编码器、下载/检查
  embed.py                向量任务处理 worker
  retrieval.py            文字模式 RRF、画面模式、向量矩阵
  evidence.py             录制段时间线、前后关系与缺口提示
  importer.py             批量导入 PNG/JPEG/WebP、批次幂等、命令行
  lifecycle.py            收藏、删除、到期、配额与定时维护（继承 feat/lifecycle）
  bench.py                资源与速度测量工具（继承 feat/lifecycle）
  dataset.py              标注清单、校验与按来源组划分（继承 feat/lifecycle；本分支加了复核人≠标注人检查）
  classify.py             LR 内容分类器训练、基线、评测与预测（继承 feat/lifecycle）
  evaluate.py             五组检索实验与 Hit@5 / MRR@10（继承 feat/lifecycle）
  collect.py              数据集采集清单、检查、导入、标注表、事实→相关帧（本分支新增）
  indexer.py              不采集、只跑 OCR 与向量待办（本分支新增，Mac 可用）
  search.py               查询解析、词法检索、排序、原图详情
  control.py              页面 ↔ 后台控制记录
  db.py                   表结构、逐版本迁移（v6）、连接与事务
  textutil.py, timeutil.py, config.py, envcheck.py
tests/                    pytest；假窗口替身与替身编码器，任意平台可跑；真实模型测试需本机已下载
```

## 已知限制

- 收藏、删除、到期、配额只在 Mac 上用假窗口、替身 OCR / 编码器和注入的磁盘测量验证过；真实 10 GiB 配额下的维护耗时、`measure_usage` 遍历大量图片的开销、Windows 上删除被占用文件的行为都待实机验证。
- 删除数据库记录不会立即缩小数据库文件（没有自动 VACUUM）；配额只按实际文件占用判断，所以"提前清理记录"一步通常释放不了多少空间，没有效果时会停下并暂停采集。
- 维护每 300 秒一次，两次之间靠 256 MiB 预留余量兜底；采集线程写图前不单独检查磁盘。
- 删除是应用数据联动删除，不保证存储介质级不可恢复擦除；更新到 v6 后要先重启后台完成迁移，页面遇到旧结构会提示。

- 证据浏览只在 Mac 上用假窗口数据和 AppTest 验证过，浏览器里的排版与真实录制数据待 Windows 实机查看；`unsaved_change` 多时提示会合并计数。
- Windows 实机只用 tkinter 测试窗口验证过，记事本、终端、浏览器等真实应用下的节奏尚待验证。
- 页面进程首次语义或画面查询要导入 torch 并加载模型，约 1–2 分钟；后台与页面各持有一份模型。
- 资源与性能（两进程合计内存、OCR + 向量吞吐、查询 P95）尚未在指定测试机上测量；首期不设"无答案"阈值，RRF 与余弦分数不是正确率。
- 模型换版时（repo 或 revision 改变）需要重新计算对应特征；`frames.*_embed_status` 只反映当前配置的模型。
- 放宽匹配逐帧扫描筛选范围内的全部文字，大库下耗时未测。
- 没给捕获时间的导入截图按导入时间存档并标"时间未知"，日期筛选对它们按导入时间生效。
- 继承的限制：跨两个 OCR 文字框的原串严格匹配搜不到（放宽后可找到）；部分硬件加速窗口可能取不到内容；DPI 不感知程序按逻辑分辨率保存；`unsaved_change` 缺口较多；1% 阈值、WebP q92、OCR/向量高水位等是待冻结的初值。

更详细的完成度总表（代码 / 模拟测试 / 真实模型测试 / Windows 实机分开列）、Windows 交接步骤、待验证清单见 [PROGRESS.md](PROGRESS.md)。

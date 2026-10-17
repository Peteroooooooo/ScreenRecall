# 受控数据集：采集与标注规程（两人共用）

依据 `readable_Proposal.md` 6.1、6.2、6.4。目标：曹贺乔（`cao`，macOS）、肖天奕（`xiao`，Windows）各约 1,000 个**有效帧**，合计约 2,000（方案要求 1,500–2,000）。本文件是两人共同遵守的规则；类别表与查询分配在试采后由两人确认并冻结，冻结日期记入 PROGRESS。

## 1. 采集方式与能支持的实验

| | 说明 |
| --- | --- |
| 数据集统一采集方式 | 两人都用**系统截图工具**截取真实运行的应用窗口或整屏，保存原始 PNG，再用 `collect` 批量导入同一个数据库。macOS 用 ⌘⇧4 + 空格（单窗口，按住 Option 去阴影）或 ⌘⇧3（整屏）；Windows 用 Win+Shift+S 或截图工具（开启自动保存）。 |
| 为什么不用实时录制 | 采集后台 `daemon` 依赖 Windows API，在 Mac 上不能运行；统一走"截图 + 导入"后，两台机器的数据格式、元数据和去重方式完全一致。 |
| 必须保留的信息 | 原图文件（不改名、不裁剪、不压缩）、采集者、设备、任务编号、场景、应用名、窗口标题（看得见就填）、语言、事实编号；捕获时间只取截图工具写进文件名的时间或当场手填，取不到就是 `unknown`。 |
| 能支持 | 分类器训练与评测（6.4）、五组检索实验 Exp 1–5（6.2）、40 条查询真值、证据浏览与收藏删除的演示数据；资源测量时作为 1,500–2,000 帧底库（复制扩充的部分只能测速度）。 |
| 不能支持，仍需 Windows 实机 | 捕获覆盖实验（要已知事件时间表 + 实时录制）、文字/语义/画面就绪延迟、后台 CPU/内存、稳态吞吐、排错模式、断网与不同缩放下的截图/高亮对齐。截图导入的帧没有"连续录制段"，不能用来证明采集节奏。 |

## 2. 目录与命名

数据集根目录（截图和清单都在这里，**不进 git**，用两人共享的网盘同步；SQLite 数据库不要放在网盘里）：

```
ScreenRecall-dataset/
  tasks.csv                     # 共同任务表（从 datasets/collection/tasks.csv 复制）
  cao/
    facts.csv                   # 我的事实表（每人一份，避免同时改一个文件）
    cao-code-01/                # 一个任务一个文件夹
      截屏2026-10-18 10.02.31.png   # 原始截图，保留系统文件名
      manifest.csv              # python -m screenrecall.collect manifest 生成，再人工补列
  xiao/
    facts.csv
    xiao-code-01/ ...
```

- 任务编号：`采集者-场景-两位序号`，场景只能是 `code`（报错与参数）、`chat`（中英文交流）、`web`（视觉网页）、`table`（商品/订单/表格数字）。补采的新任务顺延编号（如 `cao-code-11`）并补进 `tasks.csv`。
- 图片编号 `image_id`：`任务编号-三位序号`，由 `manifest` 命令分配，之后不重排。
- 事实编号 `fact_id`：`任务编号#序号`，如 `cao-code-01#1`，指一条你**故意放进画面、以后要用查询找回**的信息（报错原文、参数、餐厅名、订单号、页面外观）。在 `facts.csv` 写清内容，在 manifest 的 `facts` 列标出哪些截图里看得见它（多个用 `;` 分隔）。
- 设备编号：`cao-mbp`、`xiao-win`；换机器就换编号。

## 3. 任务与数量

`datasets/collection/tasks.csv` 列出 80 个任务：每人每场景 10 个任务 × 约 25 帧 = 250 帧，四场景共 1,000。肖天奕那 40 行是草稿（`status=draft`），由他按自己的环境改应用和内容，原则是**不和对方用同一主题**：

- 报错：我以 Python / ML 工具链和 macOS 终端为主；肖天奕以 Windows 系统错误、PowerShell、Node / Java / C++ 为主。
- 聊天：话题不重复（我：API 文档、聚餐、论文、组会、旅行；他：租房、周报、游戏、社团、外卖等）；两人可以在同一测试群里互相发消息，但**同一段对话只能由一个人截图**。
- 网页：网站类别不重复（我：户外、科技产品、食谱、博物馆、新闻……；他：游戏、汽车、酒店、时尚、音乐节……）。
- 表格：我用 Numbers / Google Sheets / Preview；他用 Excel / Word / PowerPoint。所有订单、账单、聊天、预算都用合成数据。

每个任务内要有变化：至少 8 种不同内容状态（新消息、新报错、不同参数、不同页面、滚到新内容），并至少用 2 种显示变化（浅/深色、窗口全屏/半屏、缩放 100%/125%、字号）。单个任务的有效帧不超过 40。建议每人多采约 10% 作为剔除余量。

`query_pool` 列：每人每场景 01–05 号任务供开发查询，06–10 号供盲测查询（见第 6 节）。

## 4. 什么算有效帧

1,000 帧按清洗后的有效帧计：清单 `keep=yes`、`collect check` 无错误、导入成功、OCR 未失败、标注表里有复核后的最终类别。

1. **真实截图**：在真实运行的应用里截取。内容可以受控构造（测试账号、两人合成聊天、自制订单/账单 PDF、脚本故意触发的报错、公开网页）。**不允许**：复制或改名已有截图、另存成其他格式、裁剪/旋转/调色后当新图、截图的截图、AI 生成或手绘的示意图、从网上下载的截图。
2. **清晰**：原尺寸 PNG；短边不少于 500 像素（工具检查）；100% 显示下文字能看清；没有滚动中的拖影、半加载页面、挡住主要内容的弹窗（弹窗本身是内容时除外）。文字类帧 `ocr_chars` 很小（如少于 10）时人工看一眼是否有效。
3. **内容对题**：画面主要区域属于该任务场景；不出现真实个人信息、真实密码/密钥/证件号。
4. **不重复**：
   - 像素完全相同：工具报错，只留一张；
   - 跨任务、跨人的近重复：工具报错，必须删掉一张（`keep=drop`，`drop_reason` 写 `near-dup of <image_id>`）；
   - 同任务内近重复：只有内容发生有意义的变化（如 `batch_size=16→18`、多了一条消息）才保留，并在 `note` 写明变化；只有光标闪烁或鼠标移动的就删。
5. **信息完整**：必填列齐全；时间取不到就 `time_source=unknown` 且 `captured_local` 留空。

被剔除的帧不删文件，在清单里 `keep=drop` 并写原因。补采时回到原任务截**新画面**，或开新任务；不能复制旧图凑数。

## 5. 清单与标注字段

### 5.1 采集清单 `manifest.csv`（每个任务文件夹一份，示例见 `datasets/collection/manifest.example.csv`）

| 列 | 谁填 | 说明 |
| --- | --- | --- |
| image_id, file, sha, width, height | 工具 | 编号、原文件名、内容指纹、尺寸 |
| collector, task_id, scenario | 工具 | 由文件夹名得出 |
| captured_local, time_source | 工具 / 人 | 文件名里有截图时间就 `filename`；当场记下的写 `manual`；否则 `unknown` 并留空 |
| device | 人（可用 `--device`） | 设备编号 |
| app | 人（可用 `--app` 先填默认值） | 统一用官方英文名：Terminal、Visual Studio Code、Google Chrome、Safari、Microsoft Edge、WeChat、Slack、Microsoft Excel、Numbers、Preview…… 两台机器同一应用写同一个名字 |
| window_title | 人，可空 | 标题栏看得见的文字 |
| language | 人 | 画面主要文字语言：`zh`、`en`、`mixed` |
| facts | 人 | 这张图里看得见的事实编号 |
| keep, drop_reason | 人 | `yes` / `drop`；drop 必须写原因 |
| note | 人 | 任务内近重复的变化说明、其他备注 |

### 5.2 事实表 `facts.csv`（每人一份，模板 `datasets/collection/facts.template.csv`）

`fact_id, task_id, kind（error / param / message / number / look）, content, language, note`。外观类（`look`）写看得见的特征，如"深色导航栏 + 雪山湖泊大图 + 白色大标题"。

### 5.3 标注表（由 `collect labels` 生成，示例 `datasets/collection/labels.example.csv`）

前半是 `dataset.py` 的标准列（`frame_id … change_reason`），后面附 `collector, image_id, source_file, facts, ocr_chars, time_known`。`task_id / scenario / language / app` 从清单带过来，**要改就改清单再重新生成**。人工只填：

- 采集者本人：`label`、`annotator`；
- 另一个人复核：`review_label`、`reviewer`（不能和 annotator 相同，`dataset check` 会拒绝）；
- 有分歧时两人商量后写 `final_label`，复核改了类别必须写 `change_reason`。

类别表（试采后冻结；草稿同 `datasets/labelset.example.json`）：`code`、`chat`、`web`、`table_doc`。**类别按画面主要可见内容标，不按场景或应用名标**：

1. `code`：源代码、终端/命令行输出、报错栈、调试面板、Notebook 代码单元（浏览器里的 GitHub 代码页、Jupyter 也算）。
2. `chat`：即时通讯界面，以消息列表为主（含网页版聊天、会议聊天）；消息里的代码块不超过消息区一半时仍是 chat。
3. `table_doc`：电子表格、参数表、订单/账单/通知、PDF 文档、流程图；网页里参数表或时刻表占主要区域时也算。
4. `web`：其余以网页排版和图片为主的页面。
5. 两类各占约一半时，按"截图时要找回的东西"定，并在 `change_reason` 或清单 `note` 写理由；新遇到的情况两人商量后补进本节的例外清单。

所以场景和类别**多数一致但不必一致**：`table` 场景的商品参数页可能是 `table_doc` 也可能是 `web`，`code` 场景的 Stack Overflow 页面可能是 `web`。工具不会用场景自动填类别；以后若加自动建议，也只能当参考，不能直接写进 `label` 或当人工真值。

## 6. 数据划分与查询集

**分类器划分**：`python -m screenrecall.dataset split` 按来源组整体分配——同一 `task_id` 一组，像素相同或 OCR 全文相同的帧强制并组；跨任务近重复已在采集检查时删除。80 个任务约 80 组，比例由两人确认后写进命令（建议 `0.6,0.2,0.2`，待确认）。划分后再改标注会被拒绝，需要重新划分。

**40 条主查询**（`datasets/collection/query_slots.csv`，已按方案排好）：

- 每场景 10 条，开发 / 盲测各 5；按场景交替中英 2/3 与 3/2，开发和盲测各为中英 10/10。
- 每人 20 条（每场景 5 条，开发 10、盲测 10，中英各 10）；另一人复核。
- 开发查询只针对 01–05 号任务的事实，盲测查询只针对 06–10 号任务的事实，两部分任务不交叉。
- 默认模式：`web` 用 `image`（画面），其余用 `text`；冻结前可改，但要事先写定，不能看了结果再换。

**怎么写一条查询**：在截完目标任务时就写（还没跑过任何检索），在 `queries.json` 里填 `id`（用 slot 名）、`split`、`scenario`、`language`、`mode`、`query`、可选的 `start/end/apps/case_sensitive`、`facts`（目标事实编号）和 `rationale`（判定依据）。然后 `collect facts --queries` 根据清单里的事实标记自动填 `relevant`。复核人打开这些原图核对；发现别的截图里也看得见该事实，就在那张图的清单 `facts` 列补标，再重新生成。**不按某个检索器搜不搜得到来决定相关帧。**

**盲测隔离**：

1. 两人的 40 条查询合并成一个 `queries.json`，`evaluate --check-only` 通过、复核完成后冻结，在 PROGRESS 记录日期和文件指纹（结果文件里也会写指纹）。
2. 开发阶段只跑 `--split dev`；任何人都**不要在页面里试搜盲测查询**，也不拿 06–10 号任务调参数。
3. 规则、模型、参数冻结（PROGRESS 记提交号）后，`--split blind` 只跑一次，结果原样提交；失败如实报告，不改查询、不补引号、不临时启用放宽匹配、不重跑。

## 7. 合并与去重流程

由一个人（曹贺乔，Mac）在共享根目录上统一建库，库只建一份：

```bash
cd "/Users/mike/Documents/NUS/Practice Module 2/ScreenRecall"
R=~/ScreenRecall-dataset          # 共享根目录（按实际路径改）
.venv/bin/python -m screenrecall.collect manifest "$R"/cao/cao-code-01 --device cao-mbp   # 每个任务截完都跑一次
.venv/bin/python -m screenrecall.collect check "$R"                  # 全部清单：字段、清晰度、重复、近重复（跨人）
.venv/bin/python -m screenrecall.collect import "$R" --data-dir data-dataset   # 检查不过会拒绝导入；已导入的自动跳过
.venv/bin/python -m screenrecall.indexer --data-dir data-dataset     # Mac 上跑 OCR 和向量，跑完退出
.venv/bin/python -m screenrecall.collect labels "$R" --data-dir data-dataset --collector cao --out "$R"/labels-cao.csv
.venv/bin/python -m screenrecall.collect labels "$R" --data-dir data-dataset --collector xiao --out "$R"/labels-xiao.csv
# 两人标注、互相复核后合成总表（同一帧两份表填得不一样会报错）
.venv/bin/python -m screenrecall.collect labels "$R" --data-dir data-dataset --out datasets/v1/labels.csv \
    --keep-from "$R"/labels-cao.csv "$R"/labels-xiao.csv
.venv/bin/python -m screenrecall.dataset --data-dir data-dataset check --labels datasets/v1/labels.csv --labelset datasets/v1/labelset.json
```

- 肖天奕只需把 `xiao/` 文件夹（截图 + manifest + facts.csv）同步到共享根目录，自己在 Windows 上也可以跑 `manifest` 和 `check`（命令相同，用 `.\.venv\Scripts\python.exe`）。
- `data-dataset/` 在仓库里被 git 忽略。开始标注后**不要重建数据库**（frame_id 跟着这份库走）；每次导入前先备份整个目录。需要在 Windows 上做资源测量或演示时，拷贝整个 `data-dataset/` 过去。
- 不提交截图和真实聊天内容；`datasets/v1/` 里只提交类别表、标注 CSV、划分清单和 `queries.json`。

## 8. 小批试采（每场景约 10 张）

1. 建根目录和 `cao/` 下 4 个任务文件夹：`cao-code-01`、`cao-chat-01`、`cao-web-01`、`cao-table-01`；把 `tasks.csv` 复制到根目录，`facts.template.csv` 复制成 `cao/facts.csv` 并删掉示例行。
2. macOS 截图设置：⌘⇧5 → 选项 → 存储位置选当前任务文件夹；文件名保持系统默认（带时间）。
3. 每个任务截 10 张，按第 3、4 节要求有变化；边截边在 `facts.csv` 记下故意放进去的事实。
4. 运行 `manifest`，补 `app / language / facts / window_title`；运行 `check` 直到没有错误。
5. `import` → `indexer` → `labels --collector cao`；用 Numbers/Excel 打开标注表，对照 `source_file` 看图填 `label`、`annotator=cao`，记一下 40 张花了多少时间。
6. 请肖天奕复核这 40 张（填 `review_label`、`reviewer=xiao`），统计分歧，修订第 5.3 节规则；两人确认后冻结类别表，写进 PROGRESS。
7. 每个场景按第 6 节试写 1 条开发查询，跑 `collect facts --queries` 和 `evaluate --check-only`，确认流程通。

通过标准：`check` 0 错误；40 张全部导入、OCR 与向量 0 失败；标注表能正常打开、中文不乱码；复核分歧都有结论。试采合格的帧可以保留进正式数据集，之后每个任务按 25 帧补齐。

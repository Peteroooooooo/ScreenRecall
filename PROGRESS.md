# 进度与交接

更新：2026-10-13 · 分支 `feat/p0-closed-loop` · 依据《ScreenMemory_Brain_详细实现方案_独立版》（下称"方案"）

## 一句话状态

方案第一阶段（P0 最小闭环）已完成并实测通过：指定窗口可开启/暂停，每 3 秒采样，完全重复跳过或复用，存图 → OCR → 原串/短词搜索 → 回到清晰原图。P1 尚未开始。

## 接手步骤

```powershell
git fetch origin
git switch feat/p0-closed-loop      # 若已合入 main，则 git switch main; git pull --ff-only
pip install -r requirements.txt     # Python 3.12，Windows
python -m screenrecall.daemon --check   # 8 项环境检查应全部 [ OK ]
python -m screenrecall.daemon       # 终端 1
streamlit run app.py                # 终端 2，http://127.0.0.1:8501
```

开始新任务前按《github协作指南》：从最新 `main` 建任务分支，完成后提 PR。

## 已完成（P0）

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
| 数据结构 | frames / occurrences / sessions / gaps / frame_text / ocr_lines / ocr_fts / index_jobs / control / meta，结构版本 1 | `db.py` |

## 验证记录（2026-10-13，Windows 11，150% 缩放，Python 3.12.13）

- 环境检查 8/8 通过。
- 端到端 33/33 通过：测试窗口 A→B→A 录约 25 秒，存 2 张、出现 3 次、第 3 次复用 A；开启/暂停 ACK 约 0.15–0.35 秒；暂停后 4 秒内无新增记录；OCR 清空无失败；短词 16/18 只命中各自画面；大小写、`%` 字面、不成对引号、空输入、日期与应用筛选正确；Ctrl+Break 正常退出，重启后默认关闭且旧数据可搜。
- 页面无头测试（Streamlit AppTest）13/13 通过：首屏、搜索、打开原图、100% 视图、错误提示。
- 测试脚本是临时的，未提交。方案 8.3 的 A01–A13 尚未写成正式自动化测试。

## 下一步（按方案 8.1 分工与 8.2 阶段）

第二阶段（原计划 10/7–13，已落后）：

1. **曹和乔：采集节奏补全**。等稳（变化 < 1% 保存、持续变化 10 秒到期保存）、排错模式（1 秒检查、至少 2 秒保存、30 分钟退出）、快捷键补拍、允许/排除规则。改 `capture.py` 的 `_sample`；等稳会引入"未接受候选"，被替换的候选要按方案 4.2 记中断标记。
2. **饶立腾：语义与全图检索**。
   - MiniLM 文本分块与向量（复用键：文本 + 规范化版本 + 模型版本）。
   - CLIP 全图向量，文字模式两路 RRF（k=60，每路 Top-50 帧级合并）。
   - 画面模式。
   - 页面查询模型缓存（`st.cache_resource`，按 `meta.index_revision` 刷新）。
   - 7–8 日应完成两进程合计内存测量，尚未做。
3. **肖天奕：证据页与分类**。前后浏览（沿 session，遇 gaps 提示）、并排比较、分类（全图向量 + LR，对照多数类与进程名规则）。

接入向量时的约定：

- 在 `db.py` 加 schema v2 迁移，新增 embeddings、text_blocks 等表。
- `index_jobs.stage` 已不限制取值，可直接新增 `text_embed`、`image_embed`。
- 写回同样校验 `worker_token` 与 `record_version`，同时 `bump_revision`。
- OCR 完成后由 OCR 线程登记向量待办，或新建 worker。

第三阶段：收藏/删除/配额/30·90 天到期（删除须逻辑失效先于文件回收）、批量导入、受控数据采集与标注、40 条查询真值、五组实验、资源与存储测量。

## 待团队拍板

1. 空输入：现按方案 5.2 提示"请输入要查找的文字"（旧版是展示最近记录）。
2. 删除整幅画面时收藏怎么处理：方案为影响全部相关收藏；旧版默认排除收藏。P1 做删除前要定。
3. 页面文案目前是中文，演示视频若需英文需替换。

## 已知限制

- 每个 OCR 文字框作为一行、以换行连接，跨两个框的原串搜不到；OCR 有误读（实测 `under_score` → `under score`）。
- 部分硬件加速窗口 PrintWindow 可能取不到内容，仅前台时退回屏幕拷贝，否则记缺口。
- DPI 不感知程序按逻辑分辨率保存，比屏幕显示小。
- WebP q92、OCR 线程数、重试次数等是初值，未在固定测试机测量冻结。
- 页面无登录鉴权，只绑定 127.0.0.1。

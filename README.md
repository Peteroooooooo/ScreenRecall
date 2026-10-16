# ScreenRecall

轻量本地屏幕记忆系统（NUS-ISS PRS Practice Module，Group 9）。用原词找回保存过的屏幕，并回到原图核对来源与时间。产品首期目标是 Windows 10/11 单用户、本地 CPU 推理。实施依据：《ScreenMemory_Brain_详细实现方案_独立版》与 `readable_Proposal.md`。

## 当前分支状态：`feat/capture-cadence`

| 项目 | 状态 |
| --- | --- |
| 基于 | `feat/p0-closed-loop` @ `7bfe972`（同学的 P0 最小闭环，PR #1，尚未合入 `main`） |
| 本分支提交 | `e0739a2`（采集节奏代码）、`244c19f`（交接记录）及本 README 更新 |
| PR | 草稿 PR #2，base `feat/p0-closed-loop`；合并顺序 #1 → #2，详见 [PROGRESS.md](PROGRESS.md) |
| 代码 | 本分支新增内容**已实现** |
| 测试 | **只做过 macOS 模拟测试**：`python -m pytest tests` 41/41 通过（假窗口替身 + 注入时钟 + 真实 SQLite/WebP） |
| Windows 实机 | **待验证**（真实窗口节奏与 1% 阈值、快捷键注册、页面新按钮、InPrivate 排除、旧库升级，见 PROGRESS.md「待验证」1–6） |

## 本分支新增内容（P1 采集节奏，方案 4.1、4.2）

| 能力 | 做了什么 | 代码 | 验证 |
| --- | --- | --- | --- |
| 等稳保存 | 相邻两次观察变化 < 1% 即保存（单个数字变化也保存，不被"小变化"过滤）；大变化等一个采样间隔确认稳定；持续变化从等待起点满 10 秒后的首个采样点保存；开启/恢复/切换目标后直接捕获 | `screenrecall/cadence.py`、`capture.py` | Mac 模拟测试通过；Windows 待验证 |
| 中断标记 | 看到变化但未保存（等稳或限频中）时写 `gaps.reason = 'unsaved_change'`，中断当前出现记录，不保存候选图像、不建 OCR 待办（A→B 未存→A：1 个内容、2 次出现、1 个中断） | `capture.py` | Mac 模拟测试通过 |
| 排错模式 | 每 1 秒检查、不等稳；自动保存至少间隔 2 秒，限频期间保留最新候选并在允许时补存；满 30 分钟或 OCR 待办 ≥ 50 自动退回普通模式；页面倒计时与「退出排错模式」 | `cadence.py`、`capture.py`、`app.py` | Mac 模拟测试通过；页面与实机待验证 |
| 手动补拍 | 页面「立即补拍」按钮 + 全局快捷键（默认 `Ctrl+Alt+S`）；内容相同则复用图片并新增一次出现（`source_type = 'manual'`）；不受自动间隔限制，仍受暂停、锁屏、排除规则限制 | `hotkey.py`、`winapi.py`、`capture.py`、`daemon.py` | 补拍逻辑 Mac 模拟测试通过；**快捷键注册只做过语法检查**，Windows 待验证 |
| 允许 / 排除规则 | `config.*.toml` 的 `[rules]`：允许程序（空 = 不限）、排除程序、排除标题关键词，排除优先；开始录制时拒绝，录制中标题命中记 `excluded` 缺口、不截图 | `rules.py`、`capture.py`、`app.py` | Mac 模拟测试通过；InPrivate 实机待验证 |
| 数据结构 v2 | 改为逐版本迁移（`db._MIGRATIONS`）；`control` 表新增记录模式、排错到期、补拍请求与结果、快捷键状态；`frames.capture_meta` 记录 `mode` 与 `trigger` | `db.py`、`control.py` | v1→v2 迁移 Mac 测试通过 |
| 测试 | `tests/`：假 `winapi` 替身，任意平台可跑 | `tests/` | 41/41（macOS） |

## 累计功能

继承自 P0（同学实现，Windows 11 实机验证通过，见 PROGRESS.md）：

| 能力 | 方案章节 |
| --- | --- |
| 指定窗口开启/暂停，页面等待后台 ACK，心跳与"后台不可达"提示 | 2.3、4.1 |
| 定时采样；原始像素完全一致时跳过或复用（A→B→A 复用内容、新增出现） | 4.1、4.2 |
| 内容（frame）与出现（occurrence）分开存；锁屏、最小化、捕获失败记为缺口 | 3.1、3.2 |
| 原分辨率 WebP q92；临时文件 → 校验 → 原子改名 → 短事务登记；启动核查 | 3.5、6.5 |
| RapidOCR 识别已保存原图；持久待办、版本校验写回、有界重试、重启恢复 | 4.3、5.1 |
| 引号原串（AND）、短词子串扫描、FTS5 trigram、区分大小写、日期/应用筛选 | 5.2 |
| 原图查看：适应宽度 / 100% 像素、命中 OCR 行高亮、OCR 原文、出现记录 | 6.1、6.2 |

本分支新增：上表"本分支新增内容"。

本分支**没有**：双语语义检索与 RRF、全图检索（在后续分支 `feat/vector-search` 开发中）、分类、前后浏览与并排、收藏/删除/配额/到期、批量导入、放宽匹配按钮、维护页人工重试。

## 运行

### Windows（产品运行）

需要 Python 3.12。

```powershell
pip install -r requirements.txt

# 终端 1（仓库根目录）：后台。只做环境检查可加 --check
python -m screenrecall.daemon

# 终端 2（仓库根目录）：页面，打开 http://127.0.0.1:8501
streamlit run app.py
```

使用：侧栏选窗口 → 开始录制 → 等侧栏显示"录制中" → 在主区搜索 → 查看原图。需要时点「排错模式 30 分钟」或「立即补拍」，也可按 `Ctrl+Alt+S` 补拍。关闭页面不会暂停后台；后台重启后录制默认关闭。

### macOS（开发与单元测试）

后台和页面依赖 Windows 窗口 API，Mac 上不能运行录制和页面；可以跑单元测试：

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest tests
```

## 配置与数据

- 默认配置在 `config.default.toml`；本机覆盖写到 `config.local.toml`（不提交），只写要改的键。本分支新增 `[capture]` 的等稳/排错/快捷键参数和 `[rules]` 允许/排除规则，标"初值"的参数待实机测量后冻结。
- 运行数据默认在 `data/`（不提交）：`frames/` 图片、`screenrecall.sqlite3`、`logs/daemon.log`、`cache/`。也可用环境变量 `SCREENRECALL_DATA_DIR` 指定。
- 页面只绑定 127.0.0.1，没有登录鉴权；日志不写 OCR 全文。只录制受控测试内容，不要录真实密钥和私人聊天。

## 代码结构

```
app.py                    Streamlit 页面
screenrecall/
  daemon.py               后台入口：环境检查、单实例锁、恢复、线程与控制循环
  capture.py              采样、等稳/排错节奏、精确复用、出现记录、缺口与中断标记、补拍
  cadence.py              记录节奏：等稳、到期保存、排错限频（纯逻辑，本分支新增）
  rules.py                允许/排除规则（本分支新增）
  hotkey.py               全局快捷键补拍（本分支新增）
  winapi.py               窗口枚举、PrintWindow 捕获、DPI、锁屏检测、热键消息循环（ctypes）
  storage.py              WebP 编码、原子落盘、启动核查
  ocr.py                  RapidOCR 封装与 OCR 待办处理
  search.py               查询解析、词法检索、排序、原图详情
  control.py              页面 ↔ 后台控制记录（请求序号 / ACK / 心跳 / 模式 / 补拍）
  db.py                   表结构、逐版本迁移（v2）、连接与事务
  textutil.py, timeutil.py, config.py, envcheck.py
tests/                    pytest；假窗口替身，可在任意平台运行（本分支新增）
```

## 已知限制

- 本分支新增功能只在 macOS 上做过模拟测试，**不等于 Windows 实机通过**；快捷键代码未在 Windows 上运行过。
- 每次"变化后等稳"都会写一条 `unsaved_change` 缺口，日常使用行数较多；光标闪烁的终端会在两张画面间反复复用、产生较多出现记录。待实机数据决定是否合并短缺口或调整阈值。
- 每个 OCR 文字框是一"行"，以换行连接；跨两个文字框的原串搜不到。OCR 可能误读（实测 `under_score` 读成 `under score`），原串命中只说明 OCR 文字满足条件。
- PrintWindow 对部分硬件加速窗口可能取不到内容；此时仅当窗口在前台时退回屏幕拷贝，否则记为缺口。
- DPI 不感知的程序（如默认 tkinter）按其逻辑分辨率保存，比屏幕上看到的放大画面小。
- 性能数字（索引就绪、查询耗时、资源占用）与 1% 阈值、WebP q92 等初值尚未在固定测试机上测量。

更详细的验证记录、待验证清单和下一步见 [PROGRESS.md](PROGRESS.md)。

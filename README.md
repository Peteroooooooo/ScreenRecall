# ScreenRecall

轻量本地屏幕记忆系统（NUS-ISS PRS Practice Module，Group 9）。用原词、语义或大致画面描述找回保存过的屏幕，并回到原图核对来源与时间。产品首期目标是 Windows 10/11 单用户、本地 CPU 推理。实施依据：《ScreenMemory_Brain_详细实现方案_独立版》与 `readable_Proposal.md`。

## 当前分支状态：`feat/evidence-browse`

| 项目 | 状态 |
| --- | --- |
| 基于 | `feat/vector-search` @ `40aad90`（向量检索 + 页面接入，草稿 PR #3）← `feat/capture-cadence`（草稿 PR #2）← `feat/p0-closed-loop`（P0，PR #1）；以上都**尚未合入 `main`** |
| PR | 草稿 PR #4，base `feat/vector-search`；合并顺序 #1 → #2 → #3 → #4，详见 [PROGRESS.md](PROGRESS.md) |
| 本分支新增 | **已实现**：证据浏览（同一录制段内前后浏览、缺口与段边界提示、两图并排、切到同帧的另一次出现） |
| 测试 | **macOS**：`pytest tests` 83/83（继承 71 + 新增 12），含 2 条真实模型测试；新增 12 条是 Mac 上的逻辑测试与 Streamlit AppTest |
| Windows 实机 | **证据浏览待验证**（浏览器里的排版、真实录制数据上的缺口提示）；继承功能的实机状态见下表 |

## 本分支新增内容（P1 证据浏览，方案 5.2、6.1）

| 能力 | 做了什么 | 代码 | 验证 |
| --- | --- | --- | --- |
| 录制段时间线 | 以用户选中的那次出现为中心，只取它所在录制段的出现，按时间排序；原图过期的出现保留并说明，已删除画面跳过 | `screenrecall/evidence.py` | Mac 测试（真实 Recorder 生成数据） |
| 缺口与段边界提示 | 相邻两次出现之间的锁屏、最小化、取不到画面、排除规则、`unsaved_change` 等缺口逐一提示（同类合并计数），说明不能当成连续录像；到段首/段尾时显示段的结束原因（暂停、切换窗口、后台退出、异常未结束……），不自动跳到别的录制段 | `evidence.py`、`app.py` | Mac 测试 |
| 前后浏览 | 详情页"上一条 / 下一条 / 回到搜索结果"；换到别的画面后不再沿用搜索结果的语义高亮，原词高亮照常 | `app.py` | AppTest |
| 并排比较 | 选"上一条"或"下一条"与当前并排两张原图，不计算文字或表格差异 | `app.py` | AppTest |
| 切换出现 | 同一画面出现过多次时，可选另一次出现，前后浏览随之换到那次出现的录制段 | `app.py` | 代码已实现，未单独测试 |

## 累计功能

| 来源 | 能力 | 状态 |
| --- | --- | --- |
| 继承 P0（`feat/p0-closed-loop`） | 指定窗口开启/暂停与 ACK、定时采样与精确复用、内容与出现分开存、缺口、WebP 原子落盘、RapidOCR 与持久待办、引号原串/短词/FTS5 词法检索、日期/应用筛选、原图查看与命中行高亮 | 已实现，Windows 11 实机通过 |
| 继承采集节奏（`feat/capture-cadence`） | 等稳保存、`unsaved_change` 中断标记、排错模式、页面/快捷键补拍、允许/排除规则、数据库 v2 | 已实现；Windows 实机主要场景通过（30 分钟自动退出、快捷键被占用待验证） |
| 继承向量检索（`feat/vector-search`） | 数据库 v3、文本块与向量、向量任务处理、文字模式（词法 + 语义 RRF）、画面模式（多语言 CLIP）、页面文字/画面切换与模型缓存 | 已实现；真实模型测试通过（Mac 与 Windows）；Windows 实机端到端通过（肖天奕 10-14 记录） |
| 本分支新增 | 证据浏览与并排 | 已实现；Mac 测试通过；Windows 实机待验证 |

还没有开始：放宽匹配按钮与 A05–A13 自动化测试、收藏/删除/配额/到期、分类、批量导入、维护页人工重试、资源测量、评测实验。顺序见 PROGRESS.md「下一步」。

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
python -m pytest tests
```

## 配置与数据

- 默认配置在 `config.default.toml`；本机覆盖写到 `config.local.toml`（不提交），只写要改的键。向量检索分支（继承）新增 `[models]`（模型与 revision、线程数）、`[index]`（OCR 高水位、向量重试）、`[search]`（RRF k、每路 Top-N）。
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
  evidence.py             录制段时间线、前后关系与缺口提示（本分支新增）
  search.py               查询解析、词法检索、排序、原图详情
  control.py              页面 ↔ 后台控制记录
  db.py                   表结构、逐版本迁移（v3）、连接与事务
  textutil.py, timeutil.py, config.py, envcheck.py
tests/                    pytest；假窗口替身与替身编码器，任意平台可跑；真实模型测试需本机已下载
```

## 已知限制

- 证据浏览只在 Mac 上用假窗口数据和 AppTest 验证过，浏览器里的排版与真实录制数据待 Windows 实机查看；`unsaved_change` 多时提示会合并计数。
- Windows 实机只用 tkinter 测试窗口验证过，记事本、终端、浏览器等真实应用下的节奏尚待验证。
- 页面进程首次语义或画面查询要导入 torch 并加载模型，约 1–2 分钟；后台与页面各持有一份模型。
- 资源与性能（两进程合计内存、OCR + 向量吞吐、查询 P95）尚未在指定测试机上测量；首期不设"无答案"阈值，RRF 与余弦分数不是正确率。
- 模型换版时（repo 或 revision 改变）需要重新计算对应特征；`frames.*_embed_status` 只反映当前配置的模型。
- 继承的限制：跨两个 OCR 文字框的原串搜不到；部分硬件加速窗口可能取不到内容；DPI 不感知程序按逻辑分辨率保存；`unsaved_change` 缺口较多；1% 阈值、WebP q92、OCR/向量高水位等是待冻结的初值。

更详细的验证记录、待验证清单和下次恢复步骤见 [PROGRESS.md](PROGRESS.md)。

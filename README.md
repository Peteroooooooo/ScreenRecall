# ScreenRecall

轻量本地屏幕记忆系统（NUS-ISS PRS Practice Module，Group 9）。用原词、语义或大致画面描述找回保存过的屏幕，并回到原图核对来源与时间。产品首期目标是 Windows 10/11 单用户、本地 CPU 推理。实施依据：《ScreenMemory_Brain_详细实现方案_独立版》与 `readable_Proposal.md`。

## 当前分支状态：`feat/vector-search`

| 项目 | 状态 |
| --- | --- |
| 基于 | `feat/capture-cadence`（采集节奏，草稿 PR #2）←`feat/p0-closed-loop`（同学的 P0，PR #1，尚未合入 `main`） |
| 本分支提交 | `fad7ab7`（向量基础设施 v3）、`95e1176`（向量任务与检索后端）、页面接入与 Windows 验证（2026-10-14） |
| PR | 草稿 PR #3，base `feat/capture-cadence`；合并顺序 #1 → #2 → #3，详见 [PROGRESS.md](PROGRESS.md) |
| 后端 | **已实现**：数据库 v3、文本块与向量、向量任务处理、文字模式（词法 + 语义 RRF）与画面模式检索 |
| 页面 | **已接入**：文字 / 画面模式切换，查询模型按需加载并缓存，侧栏显示向量待办，结果标注命中分支，详情页蓝框标语义贡献块 |
| 测试 | Windows 11 与 macOS：`pytest tests` 71/71，含 2 条真实模型测试。Windows 实机端到端 35/35，页面 AppTest 16/16，测试脚本未提交 |
| Windows 实机 | **主要场景已通过**；30 分钟自动退出、快捷键被占用、真实应用下的节奏、1,500–2,000 帧资源测量仍待验证（PROGRESS.md「待验证」） |

## 本分支新增内容（P1 向量与检索后端）

| 能力 | 做了什么 | 代码 | 验证 |
| --- | --- | --- | --- |
| 数据库 v3 | 新增 `text_blocks`（共享文本块）、`frame_blocks`（每帧各自的块顺序、行范围、原文范围、坐标）、`embeddings`（模型 repo@revision、预处理版本、维度、float32 BLOB，唯一键防止新旧模型混用）、`frames.text_embed_status / image_embed_status`；v2→v3 迁移为已有帧补登记向量任务 | `screenrecall/db.py` | Mac 测试通过 |
| 文本块 | NFC + 去行尾空白（不动行内空白与大小写）；按 OCR 行序、垂直间距和**真实 tokenizer** 长度（含特殊 token，≤128）切块，超长换块时 1 行重叠；相同文字只存一份、只编码一次，坐标按帧分别保存 | `vectors.py` | Mac 测试通过 |
| 向量任务登记 | OCR 成功：有文字登记文本 + 全图任务，无文字只登记全图；OCR 最终失败但原图可用仍登记全图；任务年龄从抓拍算起 | `ocr.py`、`vectors.py` | Mac 测试通过 |
| 向量任务处理 | 后台 `embed` 线程：最老任务优先；OCR 待办 ≥ 50 时让路；写回前校验执行标识、版本和图片状态（删除中推理不复活）；有界重试；一路失败不影响另一路；模型缺失时后台照常运行、任务保留 | `embed.py`、`daemon.py`、`envcheck.py` | Mac 测试通过；Windows 实机 12 帧全部完成、0 失败 |
| 页面接入 | 文字 / 画面切换；`st.cache_resource` 按模型版本缓存查询编码器与向量矩阵，只加载当前模式需要的模型；侧栏显示语义 / 全图向量待办；结果显示命中分支（词法 / 语义 / 画面）与索引覆盖说明；详情页橙框标原词、蓝框标语义贡献块，画面模式不画框 | `app.py` | Windows 实机 + AppTest 16/16 |
| 模型 | 提案 4.1 的固定组合，revision 锁定：MiniLM（384 维文本）、CLIP ViT-B/32（512 维全图，保持比例补边）、多语言 CLIP（画面描述查询）；默认离线加载；`python -m screenrecall.models --download` / 检查是否齐全 | `models.py`、`config.default.toml [models]` | Mac 上已下载并离线加载 |
| 文字模式检索 | 日期/应用筛选 + 引号原串 AND 得到共同候选；只有引号时按时间、不调模型；语义分支每帧取最高块、合并到帧后 Top-50；k=60 等权 RRF；返回语义贡献块的行号供高亮；向量未完成的帧照常参与词法并给出说明；模型不可用时退回词法 | `retrieval.py`、`search.py` | 规则用替身验证；真实 MiniLM 冒烟：中文"同事推荐的餐厅"找到英文餐厅文字并排第一 |
| 画面模式检索 | 多语言 CLIP 描述 vs 整图余弦；只检索原图可用且已有全图向量的帧；不可用时明确提示，不自动切到文字模式 | `retrieval.py` | 规则用替身验证；真实 CLIP 冒烟：区分深色图与白色页面 |
| 向量矩阵 | 从 SQLite 读入 NumPy，按 `meta.index_revision` 变化重载 | `retrieval.py` | Mac 测试通过 |

## 累计功能

| 来源 | 能力 | 状态 |
| --- | --- | --- |
| 继承 P0（同学实现） | 指定窗口开启/暂停与 ACK、定时采样与精确复用、内容与出现分开存、缺口、WebP 原子落盘、RapidOCR 与持久待办、引号原串/短词/FTS5 词法检索、日期/应用筛选、原图查看与命中行高亮 | 已实现，Windows 11 实机验证通过 |
| 继承采集节奏（`feat/capture-cadence`） | 等稳保存、`unsaved_change` 中断标记、排错模式（1 秒检查、2 秒限频、30 分钟退出）、页面/快捷键补拍、允许/排除规则、数据库 v2 | 已实现；Windows 实机主要场景通过 |
| 本分支新增 | 数据库 v3、文本块与向量、向量任务处理、文字模式 RRF 与画面模式、页面接入 | 已实现；Windows 实机通过 |

还没有开始：分类、前后浏览与并排、收藏/删除/配额/到期、批量导入、放宽匹配按钮、维护页人工重试、评测实验。顺序见 PROGRESS.md「下一步」。

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

- 默认配置在 `config.default.toml`；本机覆盖写到 `config.local.toml`（不提交），只写要改的键。本分支新增 `[models]`（模型与 revision、线程数）、`[index]`（OCR 高水位、向量重试）、`[search]`（RRF k、每路 Top-N）。
- 运行数据默认在 `data/`（不提交）：`frames/` 图片、`screenrecall.sqlite3`、`logs/`、`cache/`。也可用环境变量 `SCREENRECALL_DATA_DIR` 指定。模型缓存在 Hugging Face 默认目录，不在仓库内。
- 页面只绑定 127.0.0.1，没有登录鉴权；日志不写 OCR 全文。只录制受控测试内容，不要录真实密钥和私人聊天。

## 代码结构

```
app.py                    Streamlit 页面：录制控制、文字 / 画面两种搜索、原图核验
screenrecall/
  daemon.py               后台入口：环境检查、单实例锁、恢复、采集/OCR/向量线程与控制循环
  capture.py              采样、等稳/排错节奏、精确复用、出现记录、缺口与中断标记、补拍
  cadence.py              记录节奏（纯逻辑）
  rules.py                允许/排除规则
  hotkey.py               全局快捷键补拍
  winapi.py               窗口枚举、PrintWindow 捕获、DPI、锁屏检测、热键消息循环（ctypes）
  storage.py              WebP 编码、原子落盘、启动核查
  ocr.py                  RapidOCR 封装与 OCR 待办处理；完成后登记向量任务
  vectors.py              规范化、分块、文本块复用、向量 BLOB（本分支新增）
  models.py               锁定版本的模型与编码器、下载/检查（本分支新增）
  embed.py                向量任务处理 worker（本分支新增）
  retrieval.py            文字模式 RRF、画面模式、向量矩阵（本分支新增）
  search.py               查询解析、词法检索、排序、原图详情
  control.py              页面 ↔ 后台控制记录
  db.py                   表结构、逐版本迁移（v3）、连接与事务
  textutil.py, timeutil.py, config.py, envcheck.py
tests/                    pytest；假窗口替身与替身编码器，任意平台可跑；真实模型测试需本机已下载
```

## 已知限制

- Windows 实机只用 tkinter 测试窗口验证过，记事本、终端、浏览器等真实应用下的节奏尚待验证。
- 页面进程首次语义或画面查询要导入 torch 并加载模型，约 1–2 分钟；后台与页面各持有一份模型。
- 资源与性能（两进程合计内存、OCR + 向量吞吐、查询 P95）尚未在指定测试机上测量；首期不设"无答案"阈值，RRF 与余弦分数不是正确率。
- 模型换版时（repo 或 revision 改变）需要重新计算对应特征；`frames.*_embed_status` 只反映当前配置的模型。
- 继承的限制：跨两个 OCR 文字框的原串搜不到；部分硬件加速窗口可能取不到内容；DPI 不感知程序按逻辑分辨率保存；`unsaved_change` 缺口较多；1% 阈值、WebP q92、OCR/向量高水位等是待冻结的初值。

更详细的验证记录、待验证清单和下次恢复步骤见 [PROGRESS.md](PROGRESS.md)。

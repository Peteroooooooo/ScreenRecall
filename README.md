# ScreenRecall

轻量本地屏幕记忆系统（NUS-ISS PRS Practice Module，Group 9）。用原词找回保存过的屏幕，并回到原图核对来源与时间。Windows 10/11 单用户、本地 CPU 推理。实施依据：《ScreenMemory_Brain_详细实现方案_独立版》。

## 当前进度：P0 最小闭环

接手、验证记录与下一步分工见 [PROGRESS.md](PROGRESS.md)。

| 能力 | 状态 | 方案章节 |
| --- | --- | --- |
| 指定窗口开启/暂停，页面等待后台 ACK，心跳与"后台不可达"提示 | 已实现 | 2.3、4.1 |
| 每 3 秒采样；原始像素完全一致时跳过或复用（A→B→A 复用内容、新增出现） | 已实现 | 4.1、4.2 |
| 内容（frame）与出现（occurrence）分开存；锁屏、最小化、捕获失败记为缺口 | 已实现 | 3.1、3.2 |
| 原分辨率 WebP q92；临时文件 → 校验 → 原子改名 → 短事务登记；启动核查 | 已实现 | 3.5、6.5 |
| RapidOCR 识别已保存原图；持久待办、版本校验写回、有界重试、重启恢复 | 已实现 | 4.3、5.1 |
| 引号原串（AND）、短词子串扫描、FTS5 trigram、区分大小写、日期/应用筛选 | 已实现 | 5.2 |
| 原图查看：适应宽度 / 100% 像素、命中 OCR 行高亮、OCR 原文、出现记录 | 已实现 | 6.1、6.2 |

尚未实现（P1）：等稳与排错模式、允许/排除规则、双语语义检索与 RRF、全图检索、分类、前后浏览与并排、收藏/删除/配额/到期、批量导入、放宽匹配按钮、维护页的人工重试。

## 运行

需要 Python 3.12（Windows）。

```powershell
pip install -r requirements.txt

# 终端 1（仓库根目录）：后台。只做环境检查可加 --check
python -m screenrecall.daemon

# 终端 2（仓库根目录）：页面，打开 http://127.0.0.1:8501
streamlit run app.py
```

使用：侧栏选窗口 → 开始录制 → 等侧栏显示"录制中" → 在主区搜索 → 查看原图。关闭页面不会暂停后台；后台重启后录制默认关闭。

## 配置与数据

- 默认配置在 `config.default.toml`；本机覆盖写到 `config.local.toml`（不提交），只写要改的键。
- 运行数据默认在 `data/`（不提交）：`frames/` 图片、`screenrecall.sqlite3`、`logs/daemon.log`、`cache/`。也可用环境变量 `SCREENRECALL_DATA_DIR` 指定。
- 页面只绑定 127.0.0.1，没有登录鉴权；日志不写 OCR 全文。只录制受控测试内容，不要录真实密钥和私人聊天。

## 代码结构

```
app.py                    Streamlit 页面
screenrecall/
  daemon.py               后台入口：环境检查、单实例锁、恢复、线程与控制循环
  capture.py              采样、精确复用、出现记录、缺口、接受提交
  winapi.py               窗口枚举、PrintWindow 捕获、DPI、锁屏检测（ctypes）
  storage.py              WebP 编码、原子落盘、启动核查
  ocr.py                  RapidOCR 封装与 OCR 待办处理
  search.py               查询解析、词法检索、排序、原图详情
  control.py              页面 ↔ 后台控制记录（请求序号 / ACK / 心跳）
  db.py                   表结构、连接与事务
  textutil.py, timeutil.py, config.py, envcheck.py
```

## 已知限制

- 每个 OCR 文字框是一"行"，以换行连接；跨两个文字框的原串搜不到。OCR 可能误读（实测 `under_score` 读成 `under score`），原串命中只说明 OCR 文字满足条件。
- PrintWindow 对部分硬件加速窗口可能取不到内容；此时仅当窗口在前台时退回屏幕拷贝，否则记为缺口。
- DPI 不感知的程序（如默认 tkinter）按其逻辑分辨率保存，比屏幕上看到的放大画面小。
- 性能数字（索引就绪、查询耗时、资源占用）尚未在固定测试机上测量。

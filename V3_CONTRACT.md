# ScreenRecall v3 实现约定（5 个单元并行 + 集成）

方案原文：`D:\Desktop\Projects\Individual Projects - Github repository\ScreenRecall\docs\walkthrough-v3.html`（已定稿）。
一句话规则：**能读成字的就存字，读不成字的就存截图。** 本文件只定"谁写什么、接口长什么样"；规则细节以方案为准，冲突时以方案为准。

工作区：`D:\Desktop\Projects\Individual Projects - Github repository\sr-v2`（分支 `feat/recall-v3`，不提交、不推送、不换分支）。
下文路径都相对这个目录。Python：`.venv\Scripts\python.exe`（Python 3.12）。

---

## 0. 所有单元都要遵守

- **只改自己名下的文件**（§6）。需要别人名下的东西：按本约定的接口写，测试里用假的（fake）顶上。别的单元的模块可能还没写好：
  **跨单元的导入一律写在函数里（懒导入）或由构造参数注入**，你的测试在别的单元文件不存在时也必须能过。
- **v3 代码不许导入 `screenrecall.v2.*`、`screenrecall.store.writer/queries/schema/delete/gate/aliases/bench`、`screenrecall.rec.core`、
  `screenrecall.rec.tracker`、`screenrecall.text.clipbatch`**：这些在集成时删掉（§7）。要用里面的小工具就复制过来（改成 v3 的写法）。
- 冻结文件（架构给的，谁都不改；确实要改先在交付说明里写清楚，由集成统一改）：
  `screenrecall/v3/__init__.py`、`screenrecall/v3/config.py`、`screenrecall/v3/types.py`、本文件。
  `screenrecall/v3/stdin_eof.py` 是 `v2/stdin_eof.py` 的原样复制，归 capture 单元。
- 通用规则：所有软件一套规则，**不许按软件名写分支**（软件名只能作为配置数据出现，如默认浏览器名单）；简单、不重复；
  中文 docstring / 注释，密度和仓库现有代码一致；不加第三方依赖（JPEG 用 Pillow 或 opencv，都已在 requirements.txt）。
- 真实数据：**不许打开、查询或打印** `data-v2`、`data-dataset` 和任何已有的 `.sqlite3`；v3 默认数据目录是 `data-v3`，测试只用临时目录。
  不跑盲测评测查询。不打印 `.env` 的值（只能提键名 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`）。
- 真实窗口测试只碰自己开的窗口（临时 profile 的 Edge + 自己写的本地 HTML，或自己起的 Tk / 控制台窗口），用完关掉；
  录制进程用测试隔离参数（§5）只录这些窗口。报告真实结果时只报数量。
- 测试命令（每个单元用自己的 basetemp，免得并行时互相踩）：
  `.venv\Scripts\python.exe -m pytest tests\test_v3_<unit>*.py -q -p no:cacheprovider --basetemp=$env:TEMP\sr-v3-<unit>`
  前端：在 `web` 目录 `npm run typecheck`、`npx vitest run`（web 单元）。
- PowerShell 7：文件 cmdlet 用 `-LiteralPath`；复杂脚本写临时 `.ps1` 用 `pwsh -NoLogo -NoProfile -NonInteractive -File` 跑，跑完删掉。
  自己建的临时文件都要清理。不写 `Co-Authored-By`。

---

## 1. 包结构

```
screenrecall/
  v3/
    __init__.py        [冻结]
    config.py          [冻结] V3Config / load_v3 / build / DB_NAME / TIERS（§4）
    types.py           [冻结] 单元之间的数据类型（§3.1）
    stdin_eof.py       [capture] 子进程看 stdin EOF（v2 原样复制）
    recorder.py        [capture] 录制子进程入口（§5）
    loop.py            [capture] 捕获循环：前台、名单三档、会话池、拍的时机、灰色时段、走开
    trigger.py         [capture] 每个窗口"什么时候拍"（纯逻辑）
    clipboard.py       [capture] 剪贴板监听
    pipeline.py        [capture] 处理线程：读字 → 记出处 → 存字 → 留截图 → 时间线
    control.py         [capture] 录制进程里读 control / 名单 / 不录规则、写心跳
    read.py            [text]    ② 读字：UIA 正文 + 不够时 OCR，合并去重
    source.py          [text]    ③ 记出处：强 / 弱标签、网址清理、无痕 / 内部页 / 自己的页面 / 不录网站
    shots.py           [text]    ⑤ 截图：JPEG 编码、全黑判断、留图策略 ShotPolicy
    store/__init__.py  [store]
    store/schema.py    [store]   表结构、连接
    store/writer.py    [store]   Store：所有写库
    store/queries.py   [store]   Queries：所有只读查询（网页、MCP、空闲整理共用）
    demo.py            [store]   演示数据：方案"实际跑一遍"那 15 分钟
    web.py             [web]     /api/v3/* 路由
    worker.py          [worker]  空闲整理子进程
    names.py           [worker]  可选：大模型起名字、每小时一句话
    vectors.py         [worker]  MiniLM 文字向量 + 按意思搜（默认开）
    mcp_queries.py     [worker]  app/mcp_server.Queries 的 v3 实现
    controller.py      [worker]  托盘控制（暂停 / 继续 / 删最近 / 状态）+ Agent 策略
```

**原样复用（不改）**：`rec/clock.py`、`rec/frames.py`、`rec/motion.py`（只用 `Fp`、`tile_changes`、`MotionMask`）、`rec/volatile.py`、
`rec/sessions.py`、`rec/wgc.py`、`rec/win32.py`、`rec/windows.py`（`RealWindows`、`ForegroundWatcher`、`FakeWindows`、`is_shell`、`locked`；
2026-10-29 起 `foreground()` 经 `popup_owner`：无标题、同进程、有所有者的小窗（查找栏这类）在前台时返回主窗口）、
`text/ocr2.py`（`OcrEngine`）、`text/encoder.py`、`store/fts/docs.py`（`norm`、`fold`、`index_rows`、`unindex_rows`，tail 一律传 `""`）、
`store/vectors.py`（`VectorStore`）、`app/auth.py`、`app/paths.py`、`app/llm.py`（`LlmClient.from_env`、`ENV_KEYS`、`read_env_file`）、
`app/idle.py`、`app/agents.py`、`app/mcp_shim.py`、`app/autostart.py`、`app/shortcut.py`、`winapi.py`、`timeutil.py`（`utc_ms`）、`webapi.py`（只用来发 `web/dist`）。
**会改的复用文件**：`rec/inputmon.py`（capture：加"打字"判断）、`text/uia.py`（text：加 `doc_url`）、`store/fts/lexical.py`（store：可删掉只服务 v2 表的
`search()` 那半，`plan()` / `split_query()` / `Term` / `Plan` 保留）、`app/mcp_server.py`（worker：工具说明改成 v3、加 `copied`）。

### 三个进程（不变）
- **主进程**（`python -m screenrecall.app`）：托盘、网页（`/api/v3`）、MCP（`/mcp`）、登录；一个 `Store`（写：暂停、名单、设置）+ 一个 `Queries`（读）。
- **录制子进程**（`python -m screenrecall.v3.recorder`，一直运行）：`loop` 线程只看像素和窗口、把事件放进队列；`pipeline` 线程按顺序读字、写库（UIA + OCR 都在这里，OCR tiny 约 0.7 秒）；
  `control` 线程每秒读一次控制行、写心跳；剪贴板监听线程。
- **空闲整理子进程**（`python -m screenrecall.v3.worker`，电脑空闲时才运行，低优先级）：删过期截图、可选的大模型名字 / 每小时一句话、可选向量。
- 进程之间只通过数据库（WAL）交流：主进程写 `control` / `apps` / `excludes`，录制进程每秒读（`control.rev` 变了才重读名单）。

---

## 2. 数据库（`<数据目录>/recall-v3.sqlite3`，store 单元实现）

连接：WAL、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`、`secure_delete=ON`；写操作 `BEGIN IMMEDIATE` 短事务。
`meta` 里记 `schema_version = 1`、`fts_version`（沿用 v2 的做法：`docs` 规则或 Python Unicode 版本变了就重建两张全文表）。
截图文件：`<数据目录>/shots/YYYY-MM-DD/<uuid hex>.jpg`（本机日期），`shots.path` 存相对路径。数据目录用 `app.paths.ensure_private_dir` 建（只有本人能读）。

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 名单：不在表里的进程 = never（不录）。第一次建库时把 cfg.default_record_apps 写成 record
CREATE TABLE apps (
    process  TEXT PRIMARY KEY COLLATE NOCASE,          -- 进程名，如 msedge.exe
    tier     TEXT NOT NULL CHECK (tier IN ('record', 'always', 'never')),
    added_ms INTEGER NOT NULL
);

-- 你设的不录窗口和网站
CREATE TABLE excludes (
    exclude_id INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('host', 'title')),  -- host：网址主机名（含子域）；title：标题关键词（不分大小写）
    value      TEXT NOT NULL,
    added_ms   INTEGER NOT NULL,
    UNIQUE (kind, value)
);

CREATE TABLE control (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    paused_until_ms INTEGER,                 -- NULL = 在录；> 现在 = 暂停到那时；PAUSE_FOREVER_MS = 到手动继续
    rev             INTEGER NOT NULL DEFAULT 0,  -- apps / excludes 每改一次 +1
    agent_enabled   INTEGER NOT NULL DEFAULT 1,
    recorder_pid    INTEGER,
    recorder_state  TEXT,                    -- starting / recording / paused / locked / stopped
    heartbeat_ms    INTEGER,
    last_snap_ms    INTEGER,                 -- 最后一次"拍"（过了跳过检查、处理完）
    current_app     TEXT,                    -- 现在前台在录的窗口；没在录时 NULL
    current_title   TEXT,
    updated_ms      INTEGER
);
-- 建库时 INSERT INTO control (id, updated_ms) VALUES (1, now)

-- ③ 出处：一个网址 / 一个文件 / 一个"软件 + 标题"
CREATE TABLE sources (
    source_id     INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('url', 'file', 'app_title')),
    key           TEXT NOT NULL UNIQUE,      -- SourceLabel.key
    app           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',  -- 最近一次的清理后标题
    title_norm    TEXT NOT NULL DEFAULT '',  -- docs.norm(title)，搜标题用
    url           TEXT,
    path          TEXT,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    shot_id       INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL,  -- 强标签：第一次看时留的那一张
    name          TEXT,                      -- 可选：大模型起的名字
    name_ms       INTEGER
);
CREATE INDEX idx_sources_seen ON sources (last_seen_ms);

-- ④ 字：整个库里每行只存一份（按 norm 去重）
CREATE TABLE lines (
    line_id       INTEGER PRIMARY KEY,
    text          TEXT NOT NULL,             -- 第一次看到时的原文（显示用）
    norm          TEXT NOT NULL UNIQUE,      -- store.fts.docs.norm(text)
    via           TEXT NOT NULL,             -- uia / ocr（第一次）
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL
);
CREATE INDEX idx_lines_seen ON lines (last_seen_ms);

-- 行 ↔ 出处：在哪看到、第一次和最近一次
CREATE TABLE source_lines (
    source_id     INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    line_id       INTEGER NOT NULL REFERENCES lines (line_id) ON DELETE CASCADE,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    PRIMARY KEY (source_id, line_id)
) WITHOUT ROWID;
CREATE INDEX idx_source_lines_line ON source_lines (line_id, last_seen_ms);
CREATE INDEX idx_source_lines_seen ON source_lines (source_id, last_seen_ms);

-- 全文索引：和 v2 同样的两张 contentless 表，rowid = line_id，文档用 docs.index_rows(conn, [(line_id, text, norm, "")])
CREATE VIRTUAL TABLE lines_tri USING fts5 (k, content = '', detail = 'none', tokenize = 'trigram case_sensitive 1');
CREATE VIRTUAL TABLE lines_bi  USING fts5 (k, content = '', detail = 'none', tokenize = 'ascii');
INSERT INTO lines_tri (lines_tri, rank) VALUES ('secure-delete', 1);
INSERT INTO lines_bi (lines_bi, rank) VALUES ('secure-delete', 1);

-- ⑥ 时间线的一条（一次"看"，5 分钟内回到同一个出处合成一条）
CREATE TABLE visits (
    visit_id      INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,          -- 最近一次离开 / 最近一次更新的时间
    open_since_ms INTEGER,                   -- 还开着：这一段从何时开始；NULL = 已离开
    dwell_ms      INTEGER NOT NULL DEFAULT 0,-- 已结束各段的"看了多久"之和（扣掉走开）；后台的一直是 0
    background    INTEGER NOT NULL DEFAULT 0,-- 一直录的窗口在后台
    merged        INTEGER NOT NULL DEFAULT 0,-- 合进来的回访次数
    new_lines     INTEGER NOT NULL DEFAULT 0,-- 这一条里新存了几行
    copied        INTEGER NOT NULL DEFAULT 0,-- 这一条里复制过几次
    shot_id       INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL  -- 这一条自己留下的截图（缩略图）
);
CREATE INDEX idx_visits_start ON visits (start_ms);
CREATE INDEX idx_visits_source ON visits (source_id, end_ms);

-- ① 每拍一次（过了跳过检查的）一行："为什么拍"、新字几行
CREATE TABLE snaps (
    snap_id    INTEGER PRIMARY KEY,
    t_ms       INTEGER NOT NULL,
    source_id  INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    visit_id   INTEGER REFERENCES visits (visit_id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,                -- still / forced / copy
    via        TEXT NOT NULL,                -- uia / ocr / uia+ocr / none / black
    new_lines  INTEGER NOT NULL,
    seen_lines INTEGER NOT NULL,
    shot_id    INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL
);
CREATE INDEX idx_snaps_visit ON snaps (visit_id);
CREATE INDEX idx_snaps_t ON snaps (t_ms);

-- ⑤ 留下的截图（JPEG q80，宽最多 1920）
CREATE TABLE shots (
    shot_id   INTEGER PRIMARY KEY,
    t_ms      INTEGER NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    visit_id  INTEGER REFERENCES visits (visit_id) ON DELETE SET NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('first', 'visit', 'fallback')),
    path      TEXT NOT NULL,                 -- 相对数据目录
    width     INTEGER NOT NULL,
    height    INTEGER NOT NULL,
    bytes     INTEGER NOT NULL
);
CREATE INDEX idx_shots_t ON shots (t_ms);
CREATE INDEX idx_shots_source ON shots (source_id, t_ms);

-- 截图上每行字的框（JPEG 坐标），搜索结果画黄框用
CREATE TABLE shot_lines (
    shot_id INTEGER NOT NULL REFERENCES shots (shot_id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES lines (line_id) ON DELETE CASCADE,
    x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
    PRIMARY KEY (shot_id, line_id)
) WITHOUT ROWID;
CREATE INDEX idx_shot_lines_line ON shot_lines (line_id);

-- 复制过的：复制了什么、在哪复制的、几点
CREATE TABLE clips (
    clip_id   INTEGER PRIMARY KEY,
    t_ms      INTEGER NOT NULL,
    text      TEXT NOT NULL,
    norm      TEXT NOT NULL,                 -- docs.norm(text)，搜索用 instr
    source_id INTEGER REFERENCES sources (source_id) ON DELETE SET NULL,
    visit_id  INTEGER REFERENCES visits (visit_id) ON DELETE SET NULL,
    app       TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_clips_t ON clips (t_ms);

-- 时间线上的灰字：不在名单的软件（带 app，"以后录它"）、暂停、锁屏
CREATE TABLE grey_spans (
    span_id  INTEGER PRIMARY KEY,
    reason   TEXT NOT NULL CHECK (reason IN ('unlisted', 'paused', 'locked')),
    app      TEXT,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER                         -- NULL = 还没结束
);
CREATE INDEX idx_grey_start ON grey_spans (start_ms);

-- 可选：每小时一句话
CREATE TABLE hour_lines (
    hour_ms INTEGER PRIMARY KEY,             -- 本机整点的 UTC 毫秒
    text    TEXT NOT NULL,
    model   TEXT,
    made_ms INTEGER NOT NULL
);
```

跳过的拍（打字、无痕、内部页……）**什么都不写**。DRM 全黑：出处 + 时间线照记，snaps.via = 'black'，不存字、不留图。
本地文件（kind = 'file'）：出处 + 时间线 + 第一次的 1 张截图，**不写 lines / source_lines / shot_lines**。

---

## 3. 接口

### 3.1 共用类型（`screenrecall/v3/types.py`，冻结，直接读源码）
`Box`、`TextLine`、`TextResult`、`SourceLabel`（`.strong`）、`LinesOutcome`、`ShotBox`、`EncodedShot`；
录制进程内事件 `SnapEvent`（含 `changed` 框）、`FocusEvent`、`GreyEvent`、`AwayEvent`、`CopyEvent`；
常量 `REASON_*`、`KIND_*`、`SHOT_*`、`GREY_*`、`PAUSE_FOREVER_MS`。

### 3.2 text 单元（`v3/read.py`、`v3/source.py`、`v3/shots.py`、`text/uia.py`）

```python
# text/uia.py（改）
UiaResult.doc_url: str | None = None; UiaResult.doc_name: str | None = None
#   最大的 Document 元素（ControlType Document）的 Value（ValuePattern），形如 http(s)://… 或 file:///…，和它的名字（网页标题）；
#   取不到时 None。读整棵树之前 ComBackend.doc 先单独 FindAll 一次 Document、取最大的（实测 40–260 ms），整棵树超时（status 'timeout'）
#   时网址照样有；整棵树读完时以树里最大的那个为准。其余行为不变（已有测试要继续过）。

# v3/read.py
class TextReader:
    def __init__(self, cfg: V3Config, *, uia=None, ocr=None, title_of=None): ...   # title_of(hwnd) -> 窗口标题，默认 winapi.window_title
        # uia：有 read(hwnd, timeout_s) -> UiaResult 的对象；None = UiaReader(UiaConfig(timeout_s=cfg.uia_timeout_ms/1000,
        #   max_elements=cfg.uia_max_nodes, comtypes_cache_dir=str(cfg.home / "comtypes_cache")))
        # ocr：有 recognize(bgr) -> list[OcrLine] 的对象，或返回它的零参函数（第一次要用时才建 OcrEngine(cfg.ocr_model, threads=cfg.ocr_threads,
        #   models_dir=cfg.ocr_models_path)）；模型缺失时记一次日志、以后只用 UIA
    def read_text(self, hwnd: int, frame, *, want_ocr=None) -> TextResult:
        """frame 是 rec.frames.Frame（.bgr、.fmap）。
        1. UIA 读一次（≤ uia_timeout_ms、≤ uia_max_nodes；超时不重读）；只要 role == 'text' 的行（正文），密码框的值 uia.py 已经不取；
        2. body_chars < cfg.ocr_min_chars，或 body_chars < cfg.ocr_min_body_ratio × uia_chars → 当场对整帧 OCR；
           OCR 行里落在 UIA chrome_bottom（标签页条 / 工具栏）以上的不要；
        3. 合并：UIA 行在前（用 frame.fmap 换成整帧坐标），OCR 行按 norm 去重（和 UIA 行或彼此 norm 相同、或一方 norm 包含另一方且框重叠的去掉），
           按 (y, x) 排阅读顺序；norm 少于 2 个字的行丢掉；
        4. url = UiaResult.doc_url（超时也有），page_title = doc_name，window_title = 读完系统文字时的窗口标题；
        5. 要 OCR 之前问 want_ocr(到这一步的 TextResult)，False（这一拍反正不存）就不 OCR、ocr_skipped=True。
        从不抛异常（出错时 status='error'、lines 为空）。"""
    def close(self) -> None: ...

# v3/source.py（纯函数，全部可单测）
def clean_url(url: str) -> str            # 去掉 #片段 和 utm_* 参数（其余参数保持原顺序），协议和主机名小写
def url_scheme(url: str | None) -> str    # 'https' / 'file' / 'edge' / 'about' / ''（没有网址）
def url_host(url: str | None) -> str      # 小写主机名；没有时 ''
def file_path_from_url(url: str) -> str | None   # file:///C:/a%20b/x.pdf → C:\a b\x.pdf
def path_in_title(title: str) -> str | None      # 标题里的完整路径：盘符路径 C:\…\x.ext 或 UNC \\server\share\…（取最长的那段）
def clean_title(title: str) -> str        # 去掉未读数（开头 / 结尾的 "(3)"、"[12]"、"（3）"）和转圈符号（⠋⠙⠹… ◐◓◑◒ 等），首尾空白
def label_source(app: str, title: str, url: str | None, page_title: str | None = None) -> SourceLabel
    # 有 http(s) 网址 → kind url（clean_url）；file:/// 网址或标题里有完整路径 → kind file（path 规范化：盘符大写、反斜杠）；
    # 否则 kind app_title，key = f"{app.lower()}\t{clean_title(title)}"；有网址时标题用 page_title（网页自己的标题），没有再用窗口标题
def is_private_title(title: str, keywords: Sequence[str]) -> bool        # 无痕窗口（不分大小写子串）
def title_excluded(title: str, keywords: Sequence[str]) -> bool          # 你设的不录窗口（excludes kind='title'）
def url_skip_reason(url: str | None, *, schemes: Sequence[str], own_origins: Sequence[str],
                    excluded_hosts: Sequence[str], internal_prefixes: Sequence[str] = ()) -> str | None
    # 'internal'：有网址但协议不在 schemes 里（edge:// chrome:// about: …），或以 internal_prefixes 开头（新标签页的 https 页面）；'own'：网址以 own_origins 里任一个开头（ScreenRecall 自己的网页）；
    # 'excluded'：主机名等于或是 excluded_hosts 里某个的子域；否则 None。没有网址 → None

# v3/shots.py
def encode_jpeg(bgr, *, quality: int, max_width: int) -> EncodedShot   # 宽 > max_width 时等比缩小；boxes 为空
def shot_boxes(lines: Sequence[TextLine], scale: float) -> list[tuple[str, Box]]  # (norm, 缩放后的框)
def is_black(bgr) -> bool                  # 几乎全黑（DRM 视频）：均值和标准差都很小
def uncovered_change_ratio(changed: Box | None, frame_shape: tuple[int, int], lines: Sequence[TextLine]) -> float
    # 上次拍之后变了、又没有字盖住的面积 / 整帧面积；changed 为 None 时按整帧算

class ShotPolicy:
    """⑤ 留截图。只看元数据决定；编码由调用方做。状态只在内存（重启就忘，没关系）。"""
    def __init__(self, cfg: V3Config): ...
    def decide(self, *, label: SourceLabel, source_id: int, visit_id: int, source_has_shot: bool, t_ms: int,
               new_lines: int, has_text: bool, uncovered_change: float, black: bool) -> str:
        """返回 'first' / 'fallback' / 'visit' / 'none'：
        - black → 'none'；
        - 强标签且这个出处还没有图 → 'first'（本地文件也是，每个文件 1 张）；
        - new_lines == 0 且 uncovered_change ≥ cfg.fallback_min_area（读不出字的新画面：照片、视频）
          且这个出处上次 fallback 已过 cfg.fallback_shot_ms → 'fallback'（强弱标签都适用）；
        - 弱标签且 new_lines > 0，这一条（visit_id）在这 weak_visit_ms 里还没存图、或这一拍新字比已存的那张多 → 'visit'
          （当场存；replaces 给出要替换掉的旧图）；
        - 其余 'none'。"""
    def replaces(self, visit_id: int, t_ms: int) -> int | None   # 'visit' 要替换的旧图（同一条、同一个 weak_visit_ms 里）；新开一段 None
    def saved_visit(self, visit_id: int, shot_id: int, new_lines: int, t_ms: int) -> None   # 真的存下了 'visit' 图
    def mark_fallback(self, source_id: int, t_ms: int) -> None
    def clear(self) -> None                         # 隐私删除之后忘掉已存的图
```

### 3.3 store 单元（`v3/store/*`）

两个类都**线程安全**（内部一把锁 + 一个连接，`check_same_thread=False`），每个进程按需各开一个。构造时建库 / 迁移（只读的 Queries 不建库，库不存在时
所有查询返回空）。时间参数都是 UTC 毫秒，区间 `[start_ms, end_ms)`。

```python
class Store:
    def __init__(self, cfg: V3Config, *, clock: Callable[[], int] = utc_ms): ...
    def close(self) -> None
    # ---- 建库 / 恢复 ----
    def init_defaults(self, record_apps: Sequence[str]) -> bool   # 第一次（meta 'apps_seeded' 没有）把它们写成 record；返回是否第一次
    def recover(self, now_ms: int) -> dict                         # 录制进程启动时：开着的 visits 在 end_ms 离开、没结束的 grey_spans 在
                                                                   # max(start, heartbeat_ms) 结束；返回 {"crashed": recorder_state 不是 stopped}
    # ---- 出处 / 时间线 ----
    def upsert_source(self, label: SourceLabel, t_ms: int) -> tuple[int, bool]   # (source_id, 已有截图?)；更新 title / last_seen
    def visit_enter(self, source_id: int, t_ms: int, *, background: bool = False) -> int
        # 这个出处最新的一条（前台后台都算）还开着 → 返回它；结束不超过 cfg.merge_ms → 合进它（merged+1）；否则新建。
        # 前台进来：那条变前台条目（background=0，原是后台条目时 dwell 从 0 算）；后台进来合进前台条目：不打开（后台那段不算看了多久）
    def visit_touch(self, visit_id: int, t_ms: int) -> None          # end_ms = max(end_ms, t)（开着的那条每拍一次更新）
    def visit_leave(self, visit_id: int, t_ms: int, away_ms: int = 0) -> None
        # 开着才算：dwell += max(0, t - open_since - away_ms)（background 不加）；end_ms = max(end_ms, t)；open_since = NULL
    def grey_enter(self, reason: str, app: str | None, t_ms: int) -> int   # 同 reason + app 的上一段结束不超过 merge_ms → 重新打开那段
    def grey_leave(self, span_id: int, t_ms: int) -> None
    # ---- 字 / 拍 / 图 / 复制 ----
    def record_lines(self, source_id: int, lines: Sequence[TextLine], t_ms: int, *, visit_id: int | None = None) -> LinesOutcome
        # 按 docs.norm 去重：库里没有的新建（并进全文索引），有的只更新 lines.last_seen；source_lines 新建或更新 last_seen；
        # visits.new_lines += 新行数（给了 visit_id 时）。norm 为空的跳过
    def add_snap(self, *, t_ms: int, source_id: int, visit_id: int | None, reason: str, via: str,
                 new_lines: int, seen_lines: int) -> int
    def save_shot(self, *, source_id: int, visit_id: int | None, snap_id: int | None, t_ms: int, kind: str,
                  shot: EncodedShot, ids_by_norm: dict[str, int]) -> int | None
        # 硬盘剩不到 cfg.min_free_gb（shutil.disk_usage，最多 60 秒查一次）→ 不存，返回 None。
        # 否则写文件（先写临时名再改名）+ shots 行 + shot_lines（shot.boxes 里能按 norm 对上 line_id 的）；
        # snaps.shot_id；kind 'first' → sources.shot_id；visits.shot_id 没有时设成它（'visit' 总是覆盖）
    def add_clip(self, *, t_ms: int, text: str, app: str, title: str, source_id: int | None, visit_id: int | None) -> int
        # visits.copied += 1（给了 visit_id 时）
    # ---- 控制 ----
    def control(self) -> dict                          # control 行
    def set_control(self, **fields) -> None            # 任意 control 字段（paused_until_ms、agent_enabled …），updated_ms 自动
    def heartbeat(self, **fields) -> None              # heartbeat_ms = now，并写给出的字段（recorder_state、last_snap_ms、current_app …）
    def set_app_tier(self, process: str, tier: str) -> None   # tier ∈ TIERS；rev + 1
    def app_tiers(self) -> dict[str, str]              # {进程名小写: tier}
    def add_exclude(self, kind: str, value: str) -> int       # rev + 1
    def remove_exclude(self, exclude_id: int) -> None         # rev + 1
    def excludes(self) -> list[dict]                   # [{exclude_id, kind, value, added_ms}]
    # ---- 维护 ----
    def expire_shots(self, before_ms: int) -> int      # 删 t_ms < before_ms 的截图（文件 + 行）；字留着；返回张数
    def delete_range(self, start_ms: int, end_ms: int) -> dict
        # 隐私删除："最近 N 分钟"等：删这段里开始的 visits / snaps / shots（含文件）/ clips / grey_spans，删 first_seen 落在这段里的 source_lines，
        # 再删掉没有 source_lines 的 lines（同时 docs.unindex_rows）和既没 visits 又没 source_lines 的 sources；返回各表删除数
    def set_source_name(self, source_id: int, name: str) -> None
    def put_hour_line(self, hour_ms: int, text: str, model: str | None) -> None
    def meta_get(self, key: str, default: str | None = None) -> str | None
    def meta_put(self, key: str, value: str) -> None

class Queries:
    def __init__(self, cfg: V3Config, *, clock: Callable[[], int] = utc_ms): ...
    def close(self) -> None
    def control(self) -> dict
    def status(self, now_ms: int | None = None) -> dict             # Status（§3.6）
    def timeline(self, start_ms: int, end_ms: int, *, now_ms: int | None = None, limit: int = 500) -> dict
        # {"from_ms", "to_ms", "items": [Entry | Grey …按 start_ms 从早到晚], "hours": [{"hour_ms", "text"}]}
        # 开着的条目：dwell_ms 算到 now（dwell_ms + now - open_since_ms）、"open": true
    def entry(self, visit_id: int) -> dict | None
        # {"entry": Entry, "lines": [{"line_id", "text", "via", "first_seen_ms", "last_seen_ms", "new": 第一次出现在这一条里}],
        #  "shots": [Shot（不带框）], "clips": [Clip]}；lines = 这个出处在 [start, end] 里看到过的行，按 (first_seen, line_id)，最多 500
    def source(self, source_id: int, *, offset: int = 0, limit: int = 200) -> dict | None
        # {"source": Source, "visits": [Entry…最近 50 条], "lines": [...], "total_lines"}
    def search(self, q: str, *, start_ms: int | None = None, end_ms: int | None = None, app: str | None = None,
               limit: int = 30, per_result: int = 5,
               semantic: Callable[[str, int], list[tuple[int, float]]] | None = None) -> dict
        """全文：lexical.plan(q) → lines_tri / lines_bi 出候选 → instr(norm, 词) 核验（所有词都要有）→ 连 source_lines 按时间、app 筛；
        另外标题（sources.title_norm）和网址（sources.url 小写）里含所有词的出处也算命中（match 里写 'title' / 'url'）。
        合并：同一个出处、命中时间（source_lines.last_seen_ms）相邻不超过 cfg.search_merge_ms 的并成一条结果；结果按最近时间从新到旧。
        每条结果的截图：强标签用 sources.shot_id；弱标签用这个出处里包含命中行最多的那张（同样多取时间最近的），没有就 None；
        boxes 只给命中行。semantic 给了时（可选的按意思搜）：返回的 line_id 也当命中（hit.semantic = true，排在全文命中后面）。
        返回 {"query", "terms": [...], "error": str | None, "total": 结果数, "results": [SearchResult…limit 条], "copied": [Clip…匹配 q 的最多 20 条]}"""
    def copied(self, *, q: str | None = None, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 100) -> list[dict]                       # [Clip]，从新到旧；q 按 norm instr
    def shot(self, shot_id: int) -> dict | None                      # Shot + "source": Source + "boxes"（全部，带 "text"）
    def shot_file(self, shot_id: int) -> Path | None                 # 截图文件的绝对路径（文件不在了也返回 None）
    def line_context(self, line_id: int, *, before: int = 12, after: int = 12) -> dict | None
        # 这一行最近在哪个出处看到的、那个出处的行按 (first_seen, line_id) 排，取前后各若干行：
        # {"source": Source, "lines": [...], "target": 下标, "total"}
    def apps(self) -> dict
        # {"apps": [{"process", "tier", "added_ms"}], "unlisted": [{"app", "last_ms", "total_ms"}…最近 30 天 grey_spans 里的，按 last_ms],
        #  "excludes": [...]}
    # ---- 给空闲整理用 ----
    def unnamed_sources(self, limit: int = 20) -> list[dict]          # [{"source_id", "kind", "app", "title", "url", "path", "sample": [最多 20 行原文]}]
    def hours_without_line(self, before_ms: int, limit: int = 24) -> list[int]   # 有条目、还没写 hour_lines 的本机整点（只要 < before_ms 的完整小时）
    def hour_digest(self, hour_ms: int) -> list[dict]                 # 这一小时里的条目 [{"name", "app", "dwell_ms", "new_lines", "copied"}]
    def lines_after(self, line_id: int, limit: int = 1000) -> list[tuple[int, str, int, int]]   # (line_id, text, last_seen_ms, 最近的 source_id)
```

显示名字（Entry.name / Source.name 为空时）：`name` > 清理后的 `title` > 网址主机名 + 路径 / 文件名 > `app`。

`v3/demo.py`：`python -m screenrecall.v3.demo --data-dir <空目录> [--day YYYY-MM-DD]`，只用 Store 的公开方法写出方案"实际跑一遍"的 6 个时刻
（文章 12 行 + 1 张图、VS Code 灰字、微信 7 行 + 1 张图 + 复制 1 段、回到文章 0 新行；图用 Pillow 画出来的合成图，框对得上）。
数据目录里已有库文件时拒绝运行。网页和 MCP 单元的手动演示用它。

### 3.4 capture 单元（`v3/recorder.py`、`loop.py`、`trigger.py`、`clipboard.py`、`pipeline.py`、`control.py`、`rec/inputmon.py`）

```python
# rec/inputmon.py（改；InputMonitor 和 FakeInput 都加）
last_typing_ms: int | None
    # 最后一次"打字"：按下的不是翻页 / 方向键（NAV_KEYS）、不是修饰键（Shift / Ctrl / Alt / Win），并且当时没按着 Ctrl / Alt / Win
    #（所以 Ctrl+C / Ctrl+V / Ctrl+组合键都不算）。仍然只记时间，不记键值。Raw Input 的抬起（RI_KEY_BREAK）用来跟踪修饰键状态。
FakeInput.typing(t_ms) / FakeInput.key(t_ms, nav=False, typing=True)   # 测试用

# v3/trigger.py
class SnapTrigger:
    """一个窗口"什么时候拍"。只看像素（rec.motion.Fp 的 tile_changes，减去 rec.volatile 的一直在变的小块）。"""
    def __init__(self, cfg: V3Config): ...
    def reset(self, t_ms: int) -> None              # 切进来：下一帧开始计"停下"；changed 记成 None（整幅都算新的）
    def feed(self, frame, t_ms: int) -> None        # 有变化（非一直在变的小块）→ 记变化起点 / 最后变化时间，累积 changed 框
    def tick(self, t_ms: int, *, typing: bool) -> str | None
        # 'still'：有待拍的变化（或刚切进来），且最后一次变化已过 cfg.still_ms；
        # 'forced'：从变化起点连续在变已满 cfg.forced_ms（之后从现在重新计）；
        # typing=True（这个页面 cfg.typing_quiet_ms 内打过字）时先不拍，保留待拍，等不打字了再按上面的规则出
    def snapped(self, t_ms: int) -> Box | None      # 调用方拍了：返回这次的 changed 框并清零
    latest: Frame | None                            # 最新一帧（拍的时候用它）

# v3/clipboard.py
class ClipboardWatcher:
    """消息专用窗口 + AddClipboardFormatListener；WM_CLIPBOARDUPDATE 时读 CF_UNICODETEXT（OpenClipboard 失败重试几次），
    回调 on_copy(text, t_ms)。不是文字（图片、文件）时不回调。"""
    def __init__(self, on_copy: Callable[[str, int], None], *, clock=now_ms): ...
    def start(self) -> bool; def stop(self) -> None

# v3/loop.py
class CaptureLoop:
    """分析线程，节拍约 100 ms（来新帧时提前叫醒）。每一轮：
    1. 暂停（control.paused）/ 锁屏（windows.locked()）→ 关会话、发 GreyEvent paused / locked（开始 / 结束各一次）、FocusEvent(hwnd=0)；
    2. 前台 fg：桌面 / 开始菜单（windows.is_shell）→ FocusEvent(0)，什么都不留；
       进程的 tier == never（不在名单）→ FocusEvent(0) + GreyEvent unlisted(app)（切走时结束）；
       标题是无痕（source.is_private_title）或命中不录窗口（source.title_excluded）→ FocusEvent(0)，什么都不留；
       否则是要录的前台：(hwnd, 标题) 变了且稳定 cfg.title_settle_ms → FocusEvent(hwnd, pid, app, title)，trigger.reset；
    3. 后台：tier == always 的进程的可见顶层窗口（每 2 秒列一次，最多 cfg.max_background_windows 个）也录（background=True）；
    4. rec.sessions.SessionPool 跟上要录的窗口（rec.wgc.open_source，cfg.fps）；取最新帧喂给各自的 SnapTrigger；
    5. 打字：inputs.last_typing_ms 变了 → 记到当前前台的页面键 (hwnd, 标题)；trigger.tick(typing=该页面在 typing_quiet_ms 内打过字)；
    6. tick 出了原因 → SnapEvent(hwnd, pid, app, title, t, reason, frame=trigger.latest, background, changed=trigger.snapped(t))；
    7. 剪贴板回调：前台是要录的窗口 → CopyEvent（文字截到 cfg.clip_max_chars）+ 马上一个 reason='copy' 的 SnapEvent（同样受打字规则限制，
       但 CopyEvent 照发）；
    8. 走开：idle_seconds() ≥ cfg.away_ms 时记下走开起点（现在 - idle）；又有输入时发 AwayEvent(起点, 现在)。
    事件只放进队列（queue.Queue），不碰数据库、不读 UIA。测试注入 windows / inputs / source_factory / clock / window_list，直接调 step(now)。"""
    def __init__(self, cfg: V3Config, out: "queue.Queue", *, windows=None, inputs=None, source_factory=None,
                 clock=None, window_list: Callable[[], list[tuple[int, str, str]]] | None = None): ...
    def set_tiers(self, tiers: dict[str, str]) -> None          # control 线程在 rev 变了时调
    def set_excludes(self, title_keywords: Sequence[str]) -> None
    def set_paused(self, paused: bool) -> None
    def on_copy(self, text: str, t_ms: int) -> None             # 给 ClipboardWatcher
    def step(self, now: int | None = None) -> None
    def start(self) -> None; def stop(self) -> None

# v3/pipeline.py
class Pipeline:
    """处理线程：按顺序处理 loop 的事件。所有写库都在这里（一个 Store）。
    FocusEvent：离开当前前台条目（visit_leave，带这一段累计的走开时间）；
      记下新的前台窗口和切进来的时间（条目等第一次拍、知道出处后才建，用切进来的时间 visit_enter）。
    SnapEvent：
      1. res = reader.read_text(hwnd, frame, want_ocr=…)（反正不存的拍不 OCR）；
      2. 读字时窗口标题和拍时不一样（skip_stale_page）、source.url_skip_reason(url, …, internal_prefixes=cfg.internal_url_prefixes)
         不为 None、带网址的软件读不到网址（skip_no_url）→ 跳过，什么都不写；不带网址的软件的非 http(s) Document 值不当网址；
      3. label = source.label_source(app, title, url, res.page_title)；black = shots.is_black(frame.bgr)；
      4. sid, has_shot = store.upsert_source(label, t)；前台：出处和当前条目的不一样（第一次拍、或同一窗口换了网址）→ 离开旧的、visit_enter(sid, 切进来时间或 t)；
         后台：visit_enter(sid, t, background=True)；visit_touch；
      5. 不是 file、不是 black → out = store.record_lines(sid, res.lines, t, visit_id=…)；
      6. snap_id = store.add_snap(…, via='black' if black else res.via)；store.heartbeat(last_snap_ms=t)；
      7. d = policy.decide(…, uncovered_change=shots.uncovered_change_ratio(ev.changed, frame 形状, res.lines))：
         'first' / 'fallback' / 'visit' → encode_jpeg + shot_boxes → store.save_shot(kind=…, replaces=policy.replaces(…))。
         这个出处没有上一拍的小块指纹（重启后第一拍）且新字 0、见过的字 > 0 时 uncovered_change 记 0（只当参照）。
    GreyEvent → store.grey_enter / grey_leave；AwayEvent → 和当前前台条目重叠的部分累加到"这一段的走开时间"；
    CopyEvent → 这个窗口最近一次的出处（没有就 label_source(app, title, None)）→ store.add_clip(…)。
    own_origins：每 5 秒读一次 <home>/endpoint.json（app.auth.read_endpoint）的端口 → http://127.0.0.1:端口、http://localhost:端口。"""
    def __init__(self, cfg: V3Config, events: "queue.Queue", *, store=None, reader=None, policy=None, clock=None,
                 own_origins: Callable[[], Sequence[str]] | None = None, excluded_hosts: Callable[[], Sequence[str]] | None = None): ...
    def process(self, ev) -> None                 # 测试直接调
    def drain(self) -> int; def start(self) -> None
    def stop(self, timeout_s: float = 3.0) -> None   # 处理完剩下的事件；当前条目离开

# v3/control.py
class ControlLoop:
    """每秒：读 control 行 → loop.set_paused(paused_until_ms 未过期)；rev 变了 → loop.set_tiers(store.app_tiers())、
    set_excludes(title 类) 并刷新 pipeline 的 excluded_hosts；每 2 秒 store.heartbeat(recorder_pid, recorder_state, current_app, current_title)。"""
    def __init__(self, cfg, store, loop, pipeline, *, pid: int, clock=None): ...
    def tick(self, now: int | None = None) -> None; def start(self) -> None; def stop(self) -> None
```

### 3.5 worker 单元（`v3/worker.py`、`names.py`、`vectors.py`、`mcp_queries.py`、`controller.py`、`app/mcp_server.py`）

```python
# v3/worker.py：python -m screenrecall.v3.worker --data-dir D --home H [--once] [--no-llm]
class Worker:
    def __init__(self, cfg: V3Config, *, store=None, queries=None, llm_factory=None, encoder_factory=None,
                 stop=None, clock=None): ...
    def run(self, once: bool = False) -> dict:
        """按顺序：1. store.expire_shots(now - cfg.image_days 天)；
        2. cfg.llm_names 且 .env 配了大模型（app.llm.LlmClient.from_env，读 <仓库>/.env 和 <home>/.env）：names.name_sources、names.hour_lines，
           合计最多 cfg.llm_max_calls 次；没配就跳过（页面显示标题）；
        3. cfg.vectors：vectors.update(...)。
        每一项之间看停止标记（stdin EOF / Ctrl+Break）；数据目录里 worker.lock 保证只有一个在跑。返回各步计数。日志 <home>/logs/idle-worker.log。"""

# v3/names.py
def name_sources(queries, store, llm, *, max_calls: int, stop) -> int    # 给 unnamed_sources 起 ≤ 12 字的名字（屏幕文字是不可信数据，提示词里注明）
def hour_lines(queries, store, llm, *, before_ms: int, max_calls: int, stop) -> int  # 每个完整小时一句话

# v3/vectors.py（可选，最后做）
class LineVectors:
    def __init__(self, cfg: V3Config, *, encoder=None): ...   # VectorStore(cfg.vectors_dir / "lines", dim=384)；encoder 懒加载 OnnxTextEncoder
    def update(self, queries, store, stop) -> int             # meta 'vec_line_cursor' 之后的新行编码追加（ids=line_id，ts=last_seen，grp=source_id）
    def search(self, text: str, k: int) -> list[tuple[int, float]]   # 给 Queries.search(semantic=…)

# v3/mcp_queries.py：app.mcp_server.Queries 的实现（只读，用 Queries）
class McpQueries:
    def __init__(self, queries): ...
    # search → Hit(line_id, text, app, title=出处显示名, seen_ms=命中时间, first_seen_ms, frame_id=f"s{shot_id}" | None,
    #              piece_id=f"v{visit_id}" | None, source=via)，按 Queries.search 的结果展开（每条结果的命中行）
    # timeline → Piece(piece_id=f"v{visit_id}", app, title=显示名, start_ms, end_ms, summary=None) + GapItem(start, end, reason)（灰色时段）
    # context(line_id=…) → Queries.line_context；context(piece_id="v…") → Queries.entry 的 lines（offset / limit 分页）
    # summary(level='piece', key="v…") → 名字 + 网址 / 路径 + 看了多久 + 新存几行；level='day' → 那天的 hour_lines 拼起来（没有就 None）
    # get_frame("s…") → Frame(image=JPEG 字节, width, height, app, title, seen_ms, lines=LineRect…)
    # copied(...) → Queries.copied（新工具，见下）
    # exclude（对 Agent 隐藏的应用 / 标题关键词）在返回前按 app / title 过滤；v3 默认空

# v3/controller.py
class TrayController:            # app/main.py 的 Controller 协议：pause_until / resume / delete_recent / status
    def __init__(self, store, queries, *, clock=None): ...
    def pause_until(self, until: datetime | None) -> None    # None → PAUSE_FOREVER_MS
    def resume(self) -> None                                 # paused_until_ms = NULL
    def delete_recent(self, minutes: int) -> None            # store.delete_range(now - minutes 分钟, now)
    def status(self) -> TrayStatus                           # "在录：msedge.exe" / "已暂停到 14:30" / "录制进程没在运行"
def agent_policy(queries) -> AgentPolicy                     # enabled = control.agent_enabled（每次现读，1 秒缓存）
```

`app/mcp_server.py`（worker 单元改）：保留现有 5 个工具名和参数（search / read / timeline / summary / get_frame），说明文字改成 v3 的意思
（2026-10-29 改名为 list_timeline / get_summary，copied 改为 list_copied：工具名都以读类动词开头，见 mcp_server.py 开头）
（没有"原地改写"，piece = 时间线上的一条，frame = 截图）；加只读工具 **`copied(time_from, time_to, query)`**：复制了什么、在哪复制的、几点。
`Queries` 协议加 `copied(...)`；`FakeQueries` 跟着改；不可信内容的引用规则和大小上限不变。GAP_LABEL 加 `unlisted`："不在名单里的软件（未录）"。

### 3.6 web 单元（`v3/web.py` + `web/src/**`）

```python
@dataclass
class V3Context:
    cfg: V3Config
    store: Store                 # 写：暂停、名单、不录规则、设置
    queries: Queries             # 读
    home: Path
    rotate_token: Callable[[], str] | None = None   # 换令牌，返回新的 Set-Cookie 值
    autostart: Any = None                           # is_enabled() / enable(cmd) / disable()；None = app.autostart
    semantic: Callable[[str, int], list[tuple[int, float]]] | None = None
    open_in_explorer: Callable[[Path], None] | None = None   # None = explorer.exe /select,<路径>
    clock: Callable[[], int] = utc_ms

def routes(ctx: V3Context) -> list[BaseRoute]     # [Mount("/api/v3", routes=[...])]，挂到 server.build_app(extra_routes=…)
```

照搬 v2 web.py 的做法（复制过来，不导入）：GET 不改数据；POST 要 `X-ScreenRecall: 1` 头，body 是 JSON 对象；参数校验错误 400 `{"error": 中文}`；
找不到 404；出错 500 只说"详情见日志"；所有响应 `Cache-Control: no-store`。整个应用外面已经有 `AuthGuard`（登录 Cookie / 令牌），这里不再做认证。

| 方法 路径 | 参数 | 返回 |
|---|---|---|
| GET `/status` | | Status |
| POST `/pause` | `{minutes: 5–60}` | Status |
| POST `/resume` | | Status |
| GET `/timeline` | `day=YYYY-MM-DD`（本机，默认今天）或 `from_ms&to_ms` | `Queries.timeline` |
| GET `/entries/{visit_id}` | | `Queries.entry` |
| GET `/sources/{source_id}` | `offset, limit` | `Queries.source` |
| GET `/search` | `q`（必填）、`from_ms`、`to_ms`、`app`、`limit` | `Queries.search`，每个 hit 加 `spans: [[a, b]]`（原文里命中的字符区间，照 v2 `match_spans`） |
| GET `/copied` | `q, from_ms, to_ms, limit` | `{"items": [Clip]}` |
| GET `/shots/{shot_id}` | | `Queries.shot` |
| GET `/shots/{shot_id}/image` | | `image/jpeg` 原图 |
| GET `/shots/{shot_id}/thumb` | `w`（默认 240，≤ 480） | `image/jpeg` 缩略图（内存里缓存最近 200 张） |
| GET `/apps` | | `Queries.apps` |
| POST `/apps` | `{process, tier}` | `Queries.apps`（"以后录它" = tier record） |
| POST `/excludes` | `{kind: host/title, value}` | `Queries.apps` |
| POST `/excludes/remove` | `{exclude_id}` | `Queries.apps` |
| POST `/sources/{source_id}/reveal` | | `{"ok": true}`：只对 kind=file 且文件存在，用资源管理器选中它（不执行文件） |
| GET `/settings` | | `{"numbers": {cfg 里所有能调的数字}, "data_dir", "llm": {"configured", "model"}, "agent_enabled", "autostart"}`（不返回密钥和地址） |
| POST `/settings` | `{agent_enabled?, autostart?}` | 同 GET |
| POST `/delete` | `{minutes}` 或 `{from_ms, to_ms}` | `Store.delete_range` 的计数 |
| POST `/agent/reset-token` | | `{"ok": true}` + Set-Cookie |

JSON 形状（Queries 返回的也是这些）：

```text
Source        {source_id, kind, strong, app, title, url, path, name, first_seen_ms, last_seen_ms, shot_id}
Entry         {type:"entry", visit_id, source: Source, name, start_ms, end_ms, open, dwell_ms, background, merged,
               new_lines, copied, reasons: ["still"|"forced"|"copy"…去重], thumb: {shot_id, own} | null}
               （own=false：这一条没留图，用的是出处已有的那张——方案里的"已有图"）
Grey          {type:"grey", span_id, reason: "unlisted"|"paused"|"locked", app, start_ms, end_ms | null, tier}
               （tier：app 现在在名单里的档；"以后录它"按钮只在 unlisted 且 tier == "never" 时显示）
Shot          {shot_id, t_ms, kind, width, height, source_id, visit_id, boxes: [{line_id, x, y, w, h, text?}]}
Hit           {line_id, text, via, first_seen_ms, last_seen_ms, semantic}
SearchResult  {source: Source, visit_id | null, start_ms, end_ms, last_seen_ms, match: ["text"|"title"|"url"],
               hits: [Hit…最多 per_result], more_hits, shot: Shot | null（boxes 只有命中行）}
Clip          {clip_id, t_ms, text, app, title, source: Source | null, visit_id}
Status        {state: "recording"|"paused"|"locked"|"stopped", paused_until_ms, paused_forever, recorder_alive
               （心跳 10 秒内）, last_snap_ms, now_ms, current: {app, title} | null,
               background（后台在录的窗口数 = 开着的后台条目）, disk_low, agent_enabled}
```

前端（沿用 `web/` 现有的 React + Tailwind + lucide、外壳、主题、中英文 i18n、`api.ts` 的请求封装和 401 处理；接口换成 `/api/v3`）：
- **顶栏状态**：圆点 + "在录 / 已暂停（到 14:30）/ 录制进程没在运行"、"最后一次拍：N 秒前"（每 2 秒轮询 `/status`，后台放慢）、暂停菜单 5 / 15 / 30 / 60 分钟、继续。
- **时间线**（`/`，按天）：每条写 名字、几点到几点、看了多久（或"还在看"）、存了几行、复制过几段、小图（或"无图" / "已有图"）、为什么拍；
  灰色行"在 XX 里（未录）" + "以后录它"；暂停 / 锁屏也是灰色行；有每小时一句话时插在整点。点一条进 `/entry/:id`。
- **条目页**（`/entry/:id`）：出处（网址可点开，新窗口，只放 http/https；本地文件给"在文件夹中显示"）、这一条的字（新存的标出来）、截图、复制过的。
- **查找**（`/search`）：搜字、标题、网址；结果从新到旧，一个出处 10 分钟内的命中只显示一条；点开看截图，命中行用黄框标出（框坐标按图片显示尺寸缩放）；
  "复制过的"单独一组（复制了什么、在哪、几点）。
- **名单**（`/apps`）：三档（录 / 一直录 / 不录）切换、最近出现过的未录软件一键加入、不录的窗口关键词和网站。
- **设置**（`/settings`）：能调的数字（只读展示，写明在 config 的 [v3] 里改）、大模型有没有配（不显示密钥）、Agent 开关 + 连接说明（复用现有 AgentPage 里调 `/api/app/agent-configs` 的部分）、开机自启、数据目录、删最近 N 分钟。
- 旧页面（Today / Piece / Recorder / Privacy 等只服务 v2 的）删掉或改造；手机宽度可用（16 px 边距、不横向滚动）；不要"AI 味"装饰。

---

## 4. 配置（一节 `[v3]`，`screenrecall/v3/config.py` 的 `V3Config`）

键和默认值以 `config.py` 为准（冻结）。方案"能调的数字"对应：
停下多久算停下 `still_ms=2000` · 一直在动多久强制拍 `forced_ms=10000` · 打完字多久才拍 `typing_quiet_ms=5000` ·
系统文字少于多少就 OCR `ocr_min_chars=100` / `ocr_min_body_ratio=0.30` · 读不出字时多久截一张 `fallback_shot_ms=60000` ·
弱标签多久算新的一次 `weak_visit_ms=600000` · 回到同一个东西多久内合成一条 `merge_ms=300000` · 多久没碰键盘鼠标算走开 `away_ms=300000` ·
搜索结果多久内合成一条 `search_merge_ms=600000` · 截图留几天 `image_days=30`。
另有：`default_record_apps`（默认名单 = 浏览器）、`private_title_keywords`、`record_url_schemes`、`max_background_windows`、`fps`、`title_settle_ms`、
`uia_timeout_ms=1500`、`uia_max_nodes=5000`、`ocr_model`、`ocr_threads`、`ocr_models_dir`、`jpeg_quality=80`、`shot_max_width=1920`、
`fallback_min_area=0.05`、`min_free_gb=5.0`、`clip_max_chars`、`idle_s`、`llm_names`、`llm_max_calls`、`vectors=true`、`semantic_min_score=0.60`、`semantic_min_chars=6`、`port`、`open_browser`、`tray`、`data_dir`。

数据目录：`--data-dir` > `SCREENRECALL_V3_DATA_DIR` > `[v3] data_dir` > `<仓库>/data-v3`。库文件 `recall-v3.sqlite3`。测试用 `config.build({}, data_dir=tmp, home=tmp)`。

集成时写进 `config.default.toml` 的一节（逐键和 `V3Config` 默认值相同、带中文注释）；删掉 `[v2]`、`[v2.*]` 各节。旧版各节（`[storage]` … `[classify]`）给 v1 工具和评测用，保留。

---

## 5. 命令行

```
python -m screenrecall.v3.recorder --data-dir D --home H
    [--only-hwnds 0x1A2B,1234]   # 测试隔离（隐藏参数）：只有这些窗口能当前台 / 后台录，别的窗口在前台当成"没有要录的前台"
    [--only-pids 4321,5678]      # 测试隔离（隐藏参数）：只录这些进程的窗口；和 --only-hwnds 同时给时两个条件都要满足
    [--no-ocr]                   # 不加载 OCR（测试 / 没下载模型时）
    [--exit-after-s 30]          # 测试用：这么多秒后自己正常收尾退出
环境变量 SCREENRECALL_TEST_ONLY_HWNDS / SCREENRECALL_TEST_ONLY_PIDS 和对应参数合并（主进程启动的录制子进程继承环境变量，端到端测试用）。
隔离时：窗口列表、剪贴板（前台不在允许范围就不发 CopyEvent）、灰色时段（不在允许范围的前台不记 unlisted）都只看允许的窗口。
启动：winapi.enable_dpi_awareness() → 先 import screenrecall.text.uia（在任何模块导入 comtypes.client 之前）→ load_v3 → ensure_private_dir(data_dir)
→ Store → recover → init_defaults(cfg.default_record_apps) → control 先 tick 一次 → pipeline / loop / 剪贴板 / control 线程。
退出（stdin EOF、Ctrl+Break / Ctrl+C、--exit-after-s）：loop 停 → pipeline 处理完（当前条目离开）→ heartbeat(recorder_state="stopped") → 关库；
超过 3 秒强行退出。日志 <home>/logs/recorder.log（只记次数、耗时、状态，从不记屏幕文字和标题）。

python -m screenrecall.v3.worker --data-dir D --home H [--once] [--no-llm]
python -m screenrecall.v3.demo --data-dir D [--day YYYY-MM-DD]
python -m screenrecall.app ...   # 主进程，参数不变；子进程模块换成上面两个
```

---

## 6. 文件归属（每个文件只归一个单元）

| 单元 | 新建 / 改的文件 | 测试 |
|---|---|---|
| **store** | `screenrecall/v3/store/__init__.py`、`store/schema.py`、`store/writer.py`、`store/queries.py`、`screenrecall/v3/demo.py`、`screenrecall/store/fts/lexical.py`（可裁）、`screenrecall/store/fts/docs.py`（应不用改） | `tests/test_v3_store*.py`；`tests/test_store_lexical.py`（改成测 v3 或删掉） |
| **capture** | `screenrecall/v3/recorder.py`、`loop.py`、`trigger.py`、`clipboard.py`、`pipeline.py`、`control.py`、`stdin_eof.py`、`screenrecall/rec/inputmon.py` | `tests/test_v3_capture*.py`、`tests_win/test_v3_recorder_win.py`（真实窗口：自己开的 Tk / 临时 profile 的 Edge） |
| **text** | `screenrecall/v3/read.py`、`source.py`、`shots.py`、`screenrecall/text/uia.py` | `tests/test_v3_text*.py`、`tests/test_text_uia.py`（跟着改）、`tests_win/test_uia_win.py`（可选：加 doc_url） |
| **web** | `screenrecall/v3/web.py`、`web/src/**`（含 `__tests__`、`test`）、`web/index.html` | `tests/test_v3_web*.py`、`web/src/__tests__/*` |
| **worker** | `screenrecall/v3/worker.py`、`names.py`、`vectors.py`、`mcp_queries.py`、`controller.py`、`screenrecall/app/mcp_server.py` | `tests/test_v3_worker*.py`、`tests/test_v3_mcp*.py`、`tests/test_app_mcp.py`、`tests/test_app_mcp_formats.py`（改成 v3 或删） |
| **集成** | `config.default.toml`、`screenrecall/app/main.py`、`app/supervisor.py`、`app/server.py`、`app/tray.py`、`app/idle.py`、`requirements*.txt`、`pytest.ini`、`web/package.json`、`web/vite.config.ts`、`web/vitest.config.ts`、`web/tsconfig.json`、`README.md`、`PROGRESS.md`、`tools/agent_eval.py`、§7 的删除、`screenrecall/store/__init__.py`、`screenrecall/rec/__init__.py`、`screenrecall/text/__init__.py`（docstring 改成 v3） | `tests/test_v3_main.py`、`tests/test_v3_stdin_eof.py`（由 `test_v2_stdin_eof.py` 改名改导入）、其余引用 v2 的 `tests/test_app_*.py`、`tests_win/test_app_main_win.py`、`tests_win/test_launcher_win.py` |
| 冻结 | `screenrecall/v3/__init__.py`、`config.py`、`types.py`、`V3_CONTRACT.md` | |

---

## 7. 集成时删掉 / 保留

**删掉（v3 用不到的 v2 东西：拼接 / 块 / 别名、逐段大模型概括、片段、CLIP / 分类器、只为这些服务的适配层）**
- `screenrecall/v2/` 整个目录（adapters、config、demo、rec_control、rec_sink、rec_text、recorder、refs、searchsvc、stdin_eof、summarize_now、web、
  work_clip、work_common、work_frames、work_llm、work_ocr、work_pieces、work_text、worker）。
- `screenrecall/rec/core.py`、`screenrecall/rec/tracker.py`（换成 v3/loop.py + v3/trigger.py）。
- `screenrecall/store/aliases.py`、`bench.py`、`delete.py`、`gate.py`、`queries.py`、`schema.py`、`writer.py`（换成 v3/store；`store/fts/`、`store/vectors.py` 留着）。
- `screenrecall/text/clipbatch.py`（CLIP）。
- 测试：`tests/test_rec_core.py`、`test_rec_tracker.py`、`test_store_aliases.py`、`test_store_bench.py`、`test_store_delete.py`、`test_store_gate.py`、
  `test_store_multiprocess.py`、`test_store_queries.py`、`test_store_review_fixes.py`、`test_store_writer.py`、`test_text_clipbatch.py`、
  `tests/test_v2_*.py`（全部 16 个；`test_v2_stdin_eof.py` 改名成 `test_v3_stdin_eof.py`）、`tests/_v2_worker_fakes.py`；
  `tests_win/bench_rec.py`、`test_rec_win.py`、`test_v2_main_win.py`、`test_v2_quality_win.py`、`test_v2_recorder_win.py`、`test_v2_worker_win.py`。
- 前端里只服务 v2 的页面和测试（web 单元自己删）。
- 删之前 `rg` 一遍确认没有剩下的引用；删完跑全部测试。

**保留（v1 / 评测 / 报告工具，不动）**
- `screenrecall/` 顶层 v1 模块：`evaluate.py`、`dataset.py`、`collect.py`、`bench.py`、`classify.py`、`coverage.py`、`codec_compare.py`、`retry.py`、`importer.py`、
  `lifecycle.py`、`cadence.py`、`capture.py`、`daemon.py`、`db.py`、`embed.py`、`evidence.py`、`hotkey.py`、`i18n.py`、`indexer.py`、`models.py`、`ocr.py`、
  `retrieval.py`、`rules.py`、`search.py`、`storage.py`、`textutil.py`、`timeutil.py`、`vectors.py`、`webapi.py`（主进程用它发 `web/dist`）、`winapi.py`、`config.py`、`control.py`、`envcheck.py`、`app.py`（Streamlit 旧界面）及其测试。
- `rec/` 里 §1 列的复用模块和 `tests/test_rec_motion.py`、`tests/_rec_motion_ref.py`、`tests/_rec_synth.py`、`tests/test_rules_hotkey.py`。
- `text/ocr2.py`、`text/encoder.py`、`text/uia.py` 及测试；`store/fts/`、`store/vectors.py` 及 `tests/test_store_vectors.py`。
- `experiments/`（报告里的实测依据；里面有脚本引用 v2 模块，删 v2 后跑不了，只作记录，不再运行）、`results/`、`report/`、`datasets/`、`models/`、`tools/`（`agent_eval.py` 由集成改成用 v3 demo + McpQueries；改不动就删掉并在 PROGRESS.md 记一句）。

---

## 8. 集成步骤（集成 agent 做）

1. `config.default.toml`：加 `[v3]` 一节（§4），删 `[v2]`、`[v2.*]`。`config.local.toml` 不动（v3 不读它的 `[v2]`）。
2. `app/main.py`：`RECORDER_MODULE = "screenrecall.v3.recorder"`、`WORKER_MODULE = "screenrecall.v3.worker"`；`load_v3` 代替 `load_v2`；
   `_start_store` 改成：`Store(cfg)`（`init_defaults`）+ `Queries(cfg)` + `web.routes(V3Context(...))` + `McpQueries` + `TrayController` + `agent_policy`；
   删掉 `StorageCap`、`SummaryRunner`、`LivePieces` 相关；`isolated_window_list` 改用 v3 recorder 的环境变量名；
   `app_switches` 读 `cfg.tray` / `cfg.open_browser`；空闲阈值 `cfg.idle_s`；`--fake-queries` 照旧走 `mcp_server.FakeQueries`。
   v3 页面挂在 `/api/v3`；`StaticOnly` 继续拦旧版 `/api/*`（注意别把 `/api/v3`、`/api/app` 拦掉：它们是 extra_routes，排在 legacy 前面）。
3. `web/vite.config.ts` 的开发代理端口改成 8611；`npm run build` 生成 `web/dist`。
4. 按 §7 删除；改名 `test_v2_stdin_eof.py`；修引用 v2 的 `tests/test_app_*.py` 和 `tests_win/`；写 `tests/test_v3_main.py`（用临时 home / data_dir、`--no-children`，
   起主进程，打 `/api/v3/status`、`/api/v3/timeline`、`/mcp` 列工具）。
5. 全部测试：`pytest tests`、前端 `npx vitest run`、`npm run typecheck`、`npm run build`。
6. 端到端（真实窗口，只录自己开的）：`v3.demo` 写一个临时数据目录 → 主进程 `--data-dir` 指它、`--no-children` 看页面；
   再用 `SCREENRECALL_TEST_ONLY_PIDS` 起完整主进程 + 录制子进程，开临时 profile 的 Edge（`--user-data-dir=<临时目录> --no-first-run`）打开
   自己起的 `python -m http.server --bind 127.0.0.1 <随机端口>` 上自己写的 HTML（用 http 而不是 file:///：file 出处按规则不存字），停 3 秒、复制一句、
   切到自己开的 Tk 窗口 → 时间线有一条网页（url 出处、存了字、1 张图）+ 一行灰字、复制组有一条；报数量。关掉自己开的窗口和进程，删临时目录。
7. `PROGRESS.md` 加一节"2026-10-28 v3"（做了什么、怎么跑、测了什么、没测什么）。不提交。

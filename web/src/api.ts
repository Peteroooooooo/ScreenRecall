// v2 网页接口（/api/v2/*）的请求封装与类型。时间一律是 UTC 毫秒整数，界面按本机时区显示。
// 会改数据的请求（POST）都带 X-ScreenRecall: 1 头（服务端据此防跨站请求）。
// 任何 /api 请求返回 401 时广播 UNAUTHORIZED_EVENT：外壳换成“请从托盘进入”的整页提示并停止轮询。

export class ApiError extends Error {
  status: number;
  data: Record<string, unknown>;
  constructor(status: number, message: string, data: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export const UNAUTHORIZED_EVENT = "sr-unauthorized";
let unauthorized = false;
export const isUnauthorized = () => unauthorized;
/** 只给测试用：重置 401 状态 */
export const resetUnauthorized = () => {
  unauthorized = false;
};

async function handle<T>(res: Response): Promise<T> {
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (res.status === 401) {
    if (!unauthorized) {
      unauthorized = true;
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
  }
  if (!res.ok) throw new ApiError(res.status, String(data.error ?? `请求失败（${res.status}）`), data);
  return data as T;
}

// 界面语言随请求发给服务端（X-Lang）
let lang = "zh";
export const setApiLang = (l: string) => {
  lang = l;
};

export type Query = Record<string, string | number | null | undefined>;

export function withQuery(path: string, q?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(q ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

/** signal：页面不再等待时（换了条件、卸载、轮询超时）中止请求 */
export function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return fetch(path, { headers: { Accept: "application/json", "X-Lang": lang }, credentials: "same-origin", signal }).then((r) => handle<T>(r));
}

export function post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-ScreenRecall": "1", "X-Lang": lang },
    credentials: "same-origin",
    body: JSON.stringify(body),
    signal,
  }).then((r) => handle<T>(r));
}

// ---------- 类型（与接口约定一致） ----------

export type RecordingState = "recording" | "paused" | "stopped";

export interface Status {
  recording: RecordingState;
  pause_until_ms: number | null;
  recorder_alive: boolean;
  /** 前台窗口；excluded = 前台是排除的窗口或命中了排除规则（这时 app / title 都是 null） */
  current: { app: string | null; title: string | null; excluded?: boolean } | null;
  pending_ocr: number;
  idle_work: "idle" | "running";
  /** 空闲整理还有多少活（只有数量）：待识别区域、还没算向量的行、等着写概括的片段 */
  queue?: { ocr: number; vectors: number; summaries: number };
  agent_enabled: boolean;
  revision: number;
}

export interface Summary {
  text: string;
  status: "ok" | "stale";
  model: string | null;
}

export interface PieceItem {
  kind: "piece";
  piece_id: number;
  start_ms: number;
  end_ms: number;
  app: string;
  title: string;
  task: string | null;
  summary_line: string | null;
  pending_ocr: boolean;
}

export interface GapItem {
  kind: "gap";
  start_ms: number;
  end_ms: number;
  reason: string;
  note: string | null;
}

/** 当天概括：generating 正在马上写；failed 这次没写成；stale 删过部分记录、概括已清空或过时；ok；none 还没有 */
export type DaySummaryState = "generating" | "failed" | "stale" | "ok" | "none";

export interface Day {
  date: string;
  overview: { active_ms: number; apps: { app: string; ms: number }[]; lines_new: number; frames: number };
  summary: Summary | null;
  summary_state?: DaySummaryState;
  /** 能不能点“重新生成”马上写（配置了大模型） */
  summary_refresh?: boolean;
  items: (PieceItem | GapItem)[];
}

export type BlockItem =
  | { kind: "line"; line_id: number; text: string; source: "uia" | "ocr"; first_seen_ms: number; frame_id: string | null }
  | { kind: "skim"; start_ms: number; end_ms: number; distance_px: number }
  | { kind: "pending"; region_id: number; frame_id: string };

export interface FrameRef {
  frame_id: string;
  captured_ms: number;
  width: number;
  height: number;
}

/** 片段概括的情况：ok 已写好；stale 内容被删过、要重写；generating 主进程正在马上写；queued 点过“重新生成”、等空闲时写；
 *  private 不发给大模型（对 Agent 隐藏或排除名单）；failed 没写成；no_text 没有字可写；waiting 还没写 */
export type SummaryState = "ok" | "stale" | "generating" | "queued" | "private" | "failed" | "no_text" | "waiting";

export interface PieceDetail {
  piece: { piece_id: number; start_ms: number; end_ms: number; app: string; title: string; task: string | null };
  summary: Summary | null;
  summary_state: SummaryState;
  /** block_id 为 null：还没归到块里的划过 / 待识别区域 */
  blocks: { block_id: number | null; items: BlockItem[] }[];
  unjoined: boolean;
  frames: FrameRef[];
}

export interface Hit {
  line_id: number;
  text: string;
  spans: [number, number][];
  last_seen_ms: number;
  frame_id: string | null;
}

export interface Group {
  segment_id: number;
  app: string;
  title: string;
  first_seen_ms: number;
  last_seen_ms: number;
  hits_total: number;
  piece_id: number | null;
  /** literal：字面命中（hits 里有高亮）；semantic：只是意思相近（没有字面命中，hits 按相似度排） */
  match: "literal" | "semantic";
  hits: Hit[];
}

/** relevant：字面命中在前、意思更近的在前，再是意思相近的；recent：按最后一次看到的时间 */
export type SearchSort = "relevant" | "recent";

export interface SearchResult {
  query: string;
  sort: SearchSort;
  offset: number;
  total_groups: number;
  groups: Group[];
  /** 后面还有；下一页从 next_offset 开始（同样的条件，顺序不变） */
  has_more: boolean;
  next_offset: number | null;
  /** 这次检索用上了语义（向量）分支 */
  semantic: boolean;
}

export interface TaskItem {
  task: string;
  /** 这个任务的片段数 */
  n: number;
}

export type SearchQuery = { q: string; from?: number; to?: number; app?: string; task?: string; limit?: number; offset?: number; sort?: SearchSort };

export interface FrameDetail {
  frame_id: string;
  captured_ms: number;
  width: number;
  height: number;
  app: string;
  title: string;
  piece_id: number | null;
  lines: { line_id: number; text: string; x0: number; y0: number; x1: number; y1: number }[];
}

export interface Settings {
  /** hwnd：钉住时的窗口句柄（标题会变，录制程序优先按它找）；found：现在能不能找到这个窗口（null = 不知道） */
  pinned: { process: string; title: string; hwnd?: number | null; found?: boolean | null }[];
  excluded_processes: string[];
  excluded_title_keywords: string[];
  hidden_from_agents: { apps: string[]; title_keywords: string[] };
  agent_enabled: boolean;
  idle_minutes: number;
  ocr_model: "tiny" | "small";
  autostart: boolean;
  llm: { configured: boolean; model: string | null; host: string | null };
  data_dir: string;
  /** 保留期：截图 / 文字保留几天（0 = 一直保留），空间上限 GB（0 = 不限） */
  image_days: number;
  text_days: number;
  storage_cap_gb: number;
}

export type SettingsPatch = Partial<Omit<Settings, "llm" | "data_dir">>;

export interface LlmTest {
  ok: boolean;
  model: string | null;
  latency_ms: number | null;
  error?: string;
}

export interface WindowInfo {
  hwnd: number;
  process: string;
  title: string;
}

export interface Storage {
  data_dir: string;
  db_bytes: number;
  frames_bytes: number;
  vectors_bytes: number;
  frames: number;
  lines: number;
  oldest_ms: number | null;
}

export type DeleteRequest = { recent_minutes: number } | { from_ms: number; to_ms: number; app?: string } | { app: string };
export interface DeleteResult {
  deleted: Record<string, number>;
}

export interface AgentConfig {
  name: string;
  file: string;
  json: unknown;
  cli?: string;
}
export interface AgentConfigs {
  configs: Record<string, AgentConfig>;
  mcp_url: string;
}

// ---------- 接口 ----------

const V2 = "/api/v2";

export const api = {
  status: (s?: AbortSignal) => get<Status>(`${V2}/status`, s),
  pause: (body: { minutes: number } | { until: "tomorrow" }) => post<Status>(`${V2}/pause`, body),
  resume: () => post<Status>(`${V2}/resume`, {}),
  day: (date: string | undefined, s?: AbortSignal) => get<Day>(withQuery(`${V2}/day`, { date }), s),
  piece: (id: number, s?: AbortSignal) => get<PieceDetail>(`${V2}/pieces/${id}`, s),
  summarize: (id: number) => post<{ queued: boolean; summary_state: SummaryState }>(`${V2}/pieces/${id}/summarize`, {}),
  summarizeDay: (date: string) => post<{ date: string; summary_state: DaySummaryState }>(`${V2}/day/summarize`, { date }),
  runIdle: () => post<Status>(`${V2}/idle/run`, {}),
  openDataDir: () => post<{ ok: boolean }>(`${V2}/storage/open`, {}),
  search: (q: SearchQuery, s?: AbortSignal) => get<SearchResult>(withQuery(`${V2}/search`, q), s),
  /** 搜索页打开时调用：服务端在后台先加载查询模型（第一次语义检索不用等） */
  searchWarm: (s?: AbortSignal) => get<{ semantic: boolean }>(`${V2}/search/warm`, s),
  tasks: (q: { from?: number; to?: number }, s?: AbortSignal) => get<{ tasks: TaskItem[] }>(withQuery(`${V2}/tasks`, q), s),
  frame: (id: string, s?: AbortSignal) => get<FrameDetail>(`${V2}/frames/${encodeURIComponent(id)}`, s),
  apps: (q: { from?: number; to?: number }, s?: AbortSignal) => get<{ apps: { app: string; n: number }[] }>(withQuery(`${V2}/apps`, q), s),
  settings: (s?: AbortSignal) => get<Settings>(`${V2}/settings`, s),
  saveSettings: (patch: SettingsPatch) => post<Settings>(`${V2}/settings`, patch),
  saveLlm: (body: { base_url: string; model: string; api_key?: string }) => post<Settings>(`${V2}/settings/llm`, body),
  testLlm: () => post<LlmTest>(`${V2}/settings/llm/test`, {}),
  windows: (s?: AbortSignal) => get<{ windows: WindowInfo[] }>(`${V2}/windows`, s),
  storage: (s?: AbortSignal) => get<Storage>(`${V2}/storage`, s),
  remove: (body: DeleteRequest) => post<DeleteResult>(`${V2}/delete`, body),
  resetToken: () => post<{ ok: boolean }>(`${V2}/agent/reset-token`, {}),
  agentConfigs: (s?: AbortSignal) => get<AgentConfigs>("/api/app/agent-configs", s),
};

export const frameImageUrl = (id: string) => `${V2}/frames/${encodeURIComponent(id)}/image`;
export const frameThumbUrl = (id: string, w = 320) => `${V2}/frames/${encodeURIComponent(id)}/thumb?w=${w}`;

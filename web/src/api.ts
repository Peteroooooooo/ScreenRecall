// v3 网页接口（/api/v3/*）的请求封装与类型（形状见 V3_CONTRACT.md §3.6）。时间一律是 UTC 毫秒整数，界面按本机时区显示。
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

// ---------- 类型（与接口约定 §3.6 一致） ----------

export type Tier = "record" | "always" | "never";
export type SourceKind = "url" | "file" | "app_title";
export type SnapReason = "still" | "forced" | "copy";

export interface Source {
  source_id: number;
  kind: SourceKind;
  /** 强标签：网址或文件（原件能打开） */
  strong: boolean;
  app: string;
  title: string;
  url: string | null;
  path: string | null;
  /** 可选：大模型起的名字 */
  name: string | null;
  first_seen_ms: number;
  last_seen_ms: number;
  shot_id: number | null;
}

export interface Entry {
  type: "entry";
  visit_id: number;
  source: Source;
  name: string;
  start_ms: number;
  end_ms: number;
  /** 还开着（还在看） */
  open: boolean;
  dwell_ms: number;
  /** 一直录的窗口在后台 */
  background: boolean;
  merged: number;
  new_lines: number;
  copied: number;
  reasons: SnapReason[];
  /** own=false：这一条没留图，用的是出处已有的那张（“已有图”） */
  thumb: { shot_id: number; own: boolean } | null;
}

export interface Grey {
  type: "grey";
  span_id: number;
  reason: "unlisted" | "paused" | "locked";
  app: string | null;
  start_ms: number;
  end_ms: number | null;
  /** app 现在在名单里的档；“以后录它”只在 unlisted 且 tier == never 时显示 */
  tier: Tier | null;
}

export interface Timeline {
  from_ms: number;
  to_ms: number;
  items: (Entry | Grey)[];
  hours: { hour_ms: number; text: string }[];
}

export interface ShotBox {
  line_id: number;
  x: number;
  y: number;
  w: number;
  h: number;
  text?: string;
}

export interface Shot {
  shot_id: number;
  t_ms: number;
  kind: "first" | "visit" | "fallback";
  width: number;
  height: number;
  source_id: number;
  visit_id: number | null;
  boxes: ShotBox[];
}

export interface ShotDetail extends Shot {
  source: Source;
}

export interface Line {
  line_id: number;
  text: string;
  via: "uia" | "ocr";
  first_seen_ms: number;
  last_seen_ms: number;
  /** 第一次出现在这一条里（新存的） */
  new?: boolean;
}

export interface Clip {
  clip_id: number;
  t_ms: number;
  text: string;
  app: string;
  title: string;
  source: Source | null;
  visit_id: number | null;
}

export interface EntryDetail {
  entry: Entry;
  lines: Line[];
  shots: Shot[];
  clips: Clip[];
}

export interface Hit {
  line_id: number;
  text: string;
  via: "uia" | "ocr";
  first_seen_ms: number;
  last_seen_ms: number;
  semantic: boolean;
  /** 原文里命中的字符区间（按码点计） */
  spans: [number, number][];
}

export interface SearchResult {
  source: Source;
  visit_id: number | null;
  start_ms: number;
  end_ms: number;
  last_seen_ms: number;
  match: ("text" | "title" | "url")[];
  hits: Hit[];
  more_hits: number;
  /** boxes 只有命中行 */
  shot: Shot | null;
}

export interface Search {
  query: string;
  terms: unknown[];
  error: string | null;
  total: number;
  results: SearchResult[];
  copied: Clip[];
}

export type RecState = "recording" | "paused" | "locked" | "stopped";

export interface Status {
  state: RecState;
  paused_until_ms: number | null;
  paused_forever: boolean;
  /** 心跳 10 秒内 */
  recorder_alive: boolean;
  last_snap_ms: number | null;
  now_ms: number;
  current: { app: string; title: string } | null;
  /** 后台在录的（一直录的）窗口数；旧的后端没有这个字段 */
  background?: number;
  disk_low: boolean;
  agent_enabled: boolean;
}

export interface AppsInfo {
  /** installed：默认名单里的浏览器这台电脑上装了没有（没装的收起来）；没有这一项当作 true */
  /** name：exe 的"文件说明"（取不到 = null，用进程名）；icon：有图标可取（appIconUrl） */
  apps: { process: string; tier: Tier; added_ms: number; installed?: boolean; name?: string | null; icon?: boolean }[];
  /** 最近 30 天出现过、不在名单里的软件 */
  unlisted: { app: string; last_ms: number; total_ms: number; name?: string | null; icon?: boolean }[];
  excludes: { exclude_id: number; kind: "host" | "title"; value: string; added_ms: number }[];
}

/** 现在开着窗口的软件；name 是 exe 的文件说明，title 是它第一个窗口的标题；tier = 在名单里的档（不在 = null） */
export interface RunningApp { process: string; name: string; title: string; windows: number; tier: Tier | null; icon?: boolean }

export const appIconUrl = (process: string) => `${V3}/apps/icon?process=${encodeURIComponent(process)}`;

export interface Settings {
  /** config 的 [v3] 里能调的数字（只读展示） */
  numbers: Record<string, number>;
  data_dir: string;
  llm: { configured: boolean; model: string | null };
  agent_enabled: boolean;
  autostart: boolean;
}

export type SettingsPatch = Partial<Pick<Settings, "agent_enabled" | "autostart">>;
export type DeleteRequest = { minutes: number } | { from_ms: number; to_ms: number };

export interface AgentConfig {
  name: string;
  file: string;
  json?: unknown;
  toml?: string;
  cli?: string;
}
export interface AgentConfigs {
  configs: Record<string, AgentConfig>;
  mcp_url: string;
}

export type SearchQuery = { q: string; from_ms?: number; to_ms?: number; app?: string; limit?: number };

// ---------- 接口 ----------

const V3 = "/api/v3";

export const api = {
  status: (s?: AbortSignal) => get<Status>(`${V3}/status`, s),
  pause: (minutes: number) => post<Status>(`${V3}/pause`, { minutes }),
  resume: () => post<Status>(`${V3}/resume`, {}),
  timeline: (day: string | undefined, s?: AbortSignal) => get<Timeline>(withQuery(`${V3}/timeline`, { day }), s),
  entry: (id: number, s?: AbortSignal) => get<EntryDetail>(`${V3}/entries/${id}`, s),
  reveal: (sourceId: number) => post<{ ok: boolean }>(`${V3}/sources/${sourceId}/reveal`, {}),
  search: (q: SearchQuery, s?: AbortSignal) => get<Search>(withQuery(`${V3}/search`, q), s),
  shot: (id: number, s?: AbortSignal) => get<ShotDetail>(`${V3}/shots/${id}`, s),
  apps: (s?: AbortSignal) => get<AppsInfo>(`${V3}/apps`, s),
  setTier: (process: string, tier: Tier) => post<AppsInfo>(`${V3}/apps`, { process, tier }),
  runningApps: (s?: AbortSignal) => get<{ running: RunningApp[] }>(`${V3}/apps/running`, s),
  addExclude: (kind: "host" | "title", value: string) => post<AppsInfo>(`${V3}/excludes`, { kind, value }),
  removeExclude: (exclude_id: number) => post<AppsInfo>(`${V3}/excludes/remove`, { exclude_id }),
  settings: (s?: AbortSignal) => get<Settings>(`${V3}/settings`, s),
  saveSettings: (patch: SettingsPatch) => post<Settings>(`${V3}/settings`, patch),
  remove: (body: DeleteRequest) => post<Record<string, number>>(`${V3}/delete`, body),
  resetToken: () => post<{ ok: boolean }>(`${V3}/agent/reset-token`, {}),
  agentConfigs: (s?: AbortSignal) => get<AgentConfigs>("/api/app/agent-configs", s),
  agents: (s?: AbortSignal) => get<AgentStatus>(`${V3}/agents`, s),
  installAgents: () => post<AgentStatus & { messages: string[] }>(`${V3}/agents/install`, {}),
};

/** Claude Code / Codex 有没有登记 ScreenRecall；claude_cli = 找得到 claude 命令 */
export interface AgentStatus { claude: boolean; codex: boolean; claude_cli: boolean }

export const shotImageUrl = (id: number) => `${V3}/shots/${id}/image`;
export const shotThumbUrl = (id: number, w = 240) => `${V3}/shots/${id}/thumb?w=${w}`;

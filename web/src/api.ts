// 与 screenrecall/webapi.py 对应的请求封装。会改数据的请求都带 X-ScreenRecall 头（服务端据此防跨站请求）。

export class ApiError extends Error {
  status: number;
  data: Record<string, unknown>;
  constructor(status: number, message: string, data: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function handle<T>(res: Response): Promise<T> {
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!res.ok) throw new ApiError(res.status, String(data.error ?? `请求失败（${res.status}）`), data);
  return data as T;
}

// 界面语言随每个请求发给服务端（X-Lang），服务端据此翻译系统提示
let lang = "zh";
export const setApiLang = (l: string) => {
  lang = l;
};

export function get<T>(path: string): Promise<T> {
  return fetch(path, { headers: { Accept: "application/json", "X-Lang": lang } }).then((r) => handle<T>(r));
}

export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ScreenRecall": "1", "X-Lang": lang },
    body: JSON.stringify(body),
  }).then((r) => handle<T>(r));
}

export function postForm<T>(path: string, form: FormData): Promise<T> {
  return fetch(path, { method: "POST", headers: { "X-ScreenRecall": "1", "X-Lang": lang }, body: form }).then((r) => handle<T>(r));
}

export const thumbUrl = (fid: string, w = 480) => `/api/frames/${fid}/thumb?w=${w}`;
export const imageUrl = (fid: string) => `/api/frames/${fid}/image`;
export const downloadUrl = (fid: string) => `/api/frames/${fid}/image?download=1`;

// ---------- 类型（只列前端用到的字段） ----------

export type Quad = [number, number][];
export type Box = { kind: "lex" | "sem"; quad: Quad };

export interface Occurrence {
  occurrence_id: string;
  session_id: string;
  first_seen_ms: number;
  last_seen_ms: number;
  observations: number;
  app: string | null;
  window_title: string | null;
  source_type: string;
  source_label?: string;
  favorited?: boolean;
}

export interface Result {
  frame_id: string;
  width: number;
  height: number;
  image_status: string;
  image_ok: boolean;
  latest: Occurrence;
  occurrences_in_filter: number;
  matched_terms: string[];
  snippet: string[];
  semantic_lines: number[];
  sources: string[];
  boxes: Box[];
  time_unknown: boolean;
  favorited: boolean;
  label: { label: string; score: number } | null;
}

export interface SearchResponse {
  query: { raw: string; quoted: string[]; terms: string[]; error: string | null };
  mode: "text" | "image";
  total: number;
  rank_basis: string;
  rank_basis_label: string;
  message: string | null;
  can_relax: boolean;
  relaxed: boolean;
  relax_rule: string;
  notes: string[];
  results: Result[];
}

export interface Step {
  occurrence_id: string;
  frame_id: string;
  first_seen_ms: number;
  last_seen_ms: number;
  observations: number;
  app: string | null;
  window_title: string | null;
  source_type: string;
  source_label: string;
  image_status: string;
  width: number;
  height: number;
  pos?: number;
  gaps_before?: string;
}

export interface Detail {
  frame_id: string;
  width: number;
  height: number;
  image_status: string;
  image_ok: boolean;
  capture_meta: Record<string, unknown>;
  time_known: boolean;
  text: string;
  lines: { line_no: number; text: string; quad: Quad }[];
  lex_lines: number[];
  sem_lines: number[];
  mode: "text" | "image";
  occurrence: Occurrence;
  occurrences: Occurrence[];
  label: { label: string; score: number } | null;
  context: null | {
    session: {
      is_import: boolean;
      end_label: string;
      started_at_ms: number;
      ended_at_ms: number | null;
      target_title: string | null;
      target_process: string | null;
      is_active: boolean;
    };
    index: number;
    total: number;
    prev: Step | null;
    current: Step;
    next: Step | null;
    gaps_before: string;
    gaps_after: string;
    strip: Step[];
  };
}

export interface Meta {
  db_ok: boolean;
  db_problem?: string;
  apps: string[];
  labels: string[];
  tz_label: string;
  rules: string[];
  notice: string;
  retention: { image_days: number; record_days: number };
  data_dir: string;
}

export interface Status {
  now_ms: number;
  alive: boolean;
  pending: boolean;
  favorites: number;
  control: {
    desired_state: string;
    actual_state: string;
    state_reason: string | null;
    actual_mode: string;
    mode_until_ms: number | null;
    mode_reason: string | null;
    capture_note: string | null;
    snapshot_result: string | null;
    snapshot_at_ms: number | null;
    hotkey_status: string | null;
    last_capture_ms: number | null;
    heartbeat_ms: number | null;
    target_title: string | null;
    target_process: string | null;
  };
  index: Record<string, number | null>;
  maintenance: null | {
    at_ms: number;
    usage_bytes: number;
    free_bytes: number;
    quota_bytes: number;
    paused: boolean;
    reason: string | null;
  };
  retention: { image_days: number; record_days: number };
}

export interface WindowItem {
  hwnd: number;
  title: string;
  process: string;
  width: number;
  height: number;
  allowed: boolean;
  reason: string | null;
}

export interface FailedJob {
  job_id: number;
  frame_id: string;
  stage: string;
  stage_name: string;
  attempts: number;
  last_error: string;
  captured_at_ms: number;
  app: string;
  window_title: string;
  retryable: boolean;
  why_not: string;
}

export interface Failed {
  summary: Record<string, { name: string; retryable: number; not_retryable: number; errors: [string, number][] }>;
  jobs: FailedJob[];
}

export interface Favorite {
  occurrence_id: string;
  frame_id: string;
  first_seen_ms: number;
  last_seen_ms: number;
  app: string | null;
  window_title: string | null;
  image_status: string;
  image_ok: boolean;
  width?: number;
  height?: number;
}

export interface FramePlan {
  frame_id: string;
  favorites: string[];
  image_status: string;
  occurrences: (Occurrence & { favorited: boolean })[];
}

export interface OccurrencePlan {
  occurrence_id: string;
  first_seen_ms: number;
  favorited: boolean;
  other_occurrences: number;
}

export interface RangePlan {
  start_ms: number;
  end_ms: number;
  apps: string[];
  occurrences: number;
  frames_purged: number;
  frames_kept: number;
  favorites: string[];
}

export interface ImportResult {
  batch: {
    batch_id: string;
    time_known: boolean;
    resubmitted: boolean;
    items: { seq: number; name: string; status: "ok" | "failed" | "skipped"; reason: string | null; frame_id: string | null }[];
  };
  ok: number;
  failed: number;
  skipped: number;
}

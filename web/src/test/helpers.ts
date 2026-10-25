// 测试用的 fetch 模拟与固定数据。只模拟前端用到的字段。
import { vi } from "vitest";
import type { Detail, Meta, Result, SearchResponse, Status } from "../api";

export type Call = { method: string; path: string; body: unknown; signal?: AbortSignal | null };
export type Reply = { status?: number; body?: unknown };
type Handler = (call: Call) => Reply | Promise<Reply>;

/** 用 handler 替换全局 fetch；handler 抛错 = 网络错误。返回所有调用记录。 */
export function mockFetch(handler: Handler) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    const raw = init?.body;
    const call: Call = {
      method: init?.method ?? "GET", path, signal: init?.signal,
      body: typeof raw === "string" ? JSON.parse(raw) : raw,
    };
    calls.push(call);
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const r = await handler(call);
    const status = r.status ?? 200;
    return { ok: status >= 200 && status < 300, status, json: async () => r.body ?? {} } as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

/** 一直不返回、直到被中止的请求（模拟服务卡住或慢查询） */
export function hang(signal?: AbortSignal | null): Promise<never> {
  return new Promise((_, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
}

export const meta: Meta = {
  db_ok: true, apps: ["Code.exe", "msedge.exe"], labels: [], tz_label: "UTC+08:00", rules: ["exclude: 1Password"],
  notice: "页面只在本机访问（127.0.0.1），没有登录鉴权。", retention: { image_days: 30, record_days: 90 }, data_dir: "data",
};

export function status(recording = true): Status {
  return {
    now_ms: Date.now(), alive: true, pending: false, favorites: 0,
    control: {
      desired_state: recording ? "recording" : "paused", actual_state: recording ? "recording" : "paused", state_reason: null,
      actual_mode: "normal", mode_until_ms: null, mode_reason: null, capture_note: null, snapshot_result: null,
      snapshot_at_ms: null, hotkey_status: null, last_capture_ms: Date.now(), heartbeat_ms: Date.now(),
      target_title: "README.md - Visual Studio Code", target_process: "Code.exe",
    },
    index: { backlog: 0, backlog_oldest_ms: null, text_backlog: 0, image_backlog: 0, frames: 12, occurrences: 20,
      image_bytes: 1048576, failed: 0, embed_failed: 0, text_oldest_ms: null, image_oldest_ms: null },
    maintenance: null, retention: { image_days: 30, record_days: 90 },
  };
}

export const FID = "0123456789abcdef0123456789abcdef";

export function result(over: Partial<Result> = {}): Result {
  return {
    frame_id: FID, width: 1600, height: 1000, image_status: "ok", image_ok: true,
    latest: { occurrence_id: "occ1", session_id: "s1", first_seen_ms: 1_700_000_000_000, last_seen_ms: 1_700_000_100_000,
      observations: 3, app: "Code.exe", window_title: "README.md - Visual Studio Code", source_type: "live" },
    occurrences_in_filter: 1, matched_terms: [], snippet: ["run pytest -q"], semantic_lines: [], sources: ["lex"],
    boxes: [], time_unknown: false, favorited: false, label: null, ...over,
  };
}

export function searchResponse(results: Result[], over: Partial<SearchResponse> = {}): SearchResponse {
  return {
    query: { raw: "pytest", quoted: [], terms: ["pytest"], error: null }, mode: "text", total: results.length,
    rank_basis: "rrf", rank_basis_label: "词法与语义排名融合（RRF）", message: null, can_relax: false, relaxed: false,
    relax_rule: "", notes: [], results, ...over,
  };
}

export function detail(over: Partial<Detail> = {}): Detail {
  const occ = { occurrence_id: "occ1", session_id: "s1", first_seen_ms: 1_700_000_000_000, last_seen_ms: 1_700_000_100_000,
    observations: 3, app: "Code.exe", window_title: "README.md", source_type: "live", source_label: "自动", favorited: true };
  return {
    frame_id: FID, width: 1600, height: 1000, image_status: "ok", image_ok: true, ocr_status: "done",
    capture_meta: { method: "wgc", dpi: 144 }, time_known: true, text: "", lines: [], lex_lines: [], sem_lines: [],
    mode: "text", occurrence: occ, occurrences: [occ], label: null, context: null, ...over,
  };
}

/** /api/meta、/api/status、/api/models（默认旧服务 404）等外壳请求的默认回答 */
export function shellReply(call: Call): Reply | null {
  if (call.path === "/api/meta") return { body: meta };
  if (call.path === "/api/status") return { body: status() };
  if (call.path === "/api/models") return { status: 404, body: { error: "Not Found" } };
  if (call.path === "/api/windows") return { body: { windows: [] } };
  if (call.path === "/api/failed") return { body: { summary: {}, jobs: [] } };
  return null;
}

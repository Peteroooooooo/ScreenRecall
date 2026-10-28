// 测试用的 fetch 模拟与固定数据。数据形状与 /api/v3 接口约定（V3_CONTRACT.md §3.6）一致，内容都是合成的示例（方案“实际跑一遍”那 15 分钟）。
import { vi } from "vitest";
import type { AgentConfigs, AppsInfo, Entry, EntryDetail, Grey, Search, Settings, ShotDetail, Source, Status, Timeline } from "../api";

export type Call = { method: string; path: string; body: unknown; headers: Record<string, string>; signal?: AbortSignal | null };
export type Reply = { status?: number; body?: unknown };
type Handler = (call: Call) => Reply | null | undefined | Promise<Reply | null | undefined>;

/** 用 handler 替换全局 fetch；handler 抛错 = 网络错误；返回 null 时用 defaultReply。返回所有调用记录。 */
export function mockFetch(handler: Handler = () => null) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    const raw = init?.body;
    const headers = Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>));
    const call: Call = {
      method: init?.method ?? "GET", path, headers, signal: init?.signal,
      body: typeof raw === "string" ? JSON.parse(raw) : raw,
    };
    calls.push(call);
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const r = (await handler(call)) ?? defaultReply(call) ?? { status: 404, body: { error: "没有这个接口" } };
    const status = r.status ?? 200;
    return { ok: status >= 200 && status < 300, status, json: async () => r.body ?? {} } as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

export const pathOf = (p: string) => p.split("?")[0];
export const queryOf = (p: string) => new URLSearchParams(p.split("?")[1] ?? "");

// ---------- 合成数据 ----------

const today0 = () => {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
};
/** 今天 h:m 的毫秒时间 */
export const at = (h: number, m = 0) => today0() + (h * 60 + m) * 60_000;

export function status(over: Partial<Status> = {}): Status {
  const now = Date.now();
  return {
    state: "recording", paused_until_ms: null, paused_forever: false, recorder_alive: true, last_snap_ms: now - 12_000, now_ms: now,
    current: { app: "msedge.exe", title: "Transformer 入门" }, disk_low: false, agent_enabled: true,
    ...over,
  };
}

export function source(over: Partial<Source> = {}): Source {
  return {
    source_id: 1, kind: "url", strong: true, app: "msedge.exe", title: "Transformer 入门", url: "https://blog.example.com/transformer-intro",
    path: null, name: null, first_seen_ms: at(14), last_seen_ms: at(14, 15), shot_id: 1, ...over,
  };
}

export const WECHAT = () => source({ source_id: 2, kind: "app_title", strong: false, app: "Weixin.exe", title: "微信", url: null, shot_id: null });

export function entry(over: Partial<Entry> = {}): Entry {
  return {
    type: "entry", visit_id: 1, source: source(), name: "Transformer 入门", start_ms: at(14), end_ms: at(14, 5), open: false, dwell_ms: 5 * 60_000,
    background: false, merged: 0, new_lines: 12, copied: 0, reasons: ["still"], thumb: { shot_id: 1, own: true }, ...over,
  };
}

export function grey(over: Partial<Grey> = {}): Grey {
  return { type: "grey", span_id: 1, reason: "unlisted", app: "Code.exe", start_ms: at(14, 5), end_ms: at(14, 12), tier: "never", ...over };
}

export function timeline(over: Partial<Timeline> = {}): Timeline {
  return {
    from_ms: at(0), to_ms: at(24),
    items: [
      entry(),
      grey(),
      entry({ visit_id: 2, source: WECHAT(), name: "微信", start_ms: at(14, 12), end_ms: at(14, 15), dwell_ms: 3 * 60_000, new_lines: 7, copied: 1,
        reasons: ["still", "copy"], thumb: { shot_id: 2, own: true } }),
      entry({ visit_id: 3, start_ms: at(14, 15), end_ms: at(14, 16), open: true, dwell_ms: 60_000, new_lines: 0, thumb: { shot_id: 1, own: false } }),
    ],
    hours: [],
    ...over,
  };
}

export function entryDetail(over: Partial<EntryDetail> = {}): EntryDetail {
  return {
    entry: entry(),
    lines: [
      { line_id: 1, text: "计算方式是 Q、K、V 三个矩阵。", via: "uia", first_seen_ms: at(14), last_seen_ms: at(14, 15), new: true },
      { line_id: 2, text: "注意力机制的一段说明", via: "uia", first_seen_ms: at(13), last_seen_ms: at(14), new: false },
    ],
    shots: [{ shot_id: 1, t_ms: at(14), kind: "first", width: 1600, height: 1000, source_id: 1, visit_id: 1, boxes: [] }],
    clips: [],
    ...over,
  };
}

export function search(over: Partial<Search> = {}): Search {
  return {
    query: "矩阵", terms: ["矩阵"], error: null, total: 1,
    results: [{
      source: source(), visit_id: 1, start_ms: at(14), end_ms: at(14, 5), last_seen_ms: at(14, 15), match: ["text"],
      hits: [{ line_id: 1, text: "计算方式是 Q、K、V 三个矩阵。", via: "uia", first_seen_ms: at(14), last_seen_ms: at(14, 15), semantic: false, spans: [[14, 16]] }],
      more_hits: 2,
      shot: { shot_id: 1, t_ms: at(14), kind: "first", width: 1600, height: 1000, source_id: 1, visit_id: 1, boxes: [{ line_id: 1, x: 200, y: 100, w: 800, h: 50 }] },
    }],
    copied: [{ clip_id: 1, t_ms: at(14, 14), text: "老师说报告要加一段局限性分析", app: "Weixin.exe", title: "微信", source: WECHAT(), visit_id: 2 }],
    ...over,
  };
}

export function shotDetail(over: Partial<ShotDetail> = {}): ShotDetail {
  return {
    shot_id: 1, t_ms: at(14), kind: "first", width: 1600, height: 1000, source_id: 1, visit_id: 1, source: source(),
    boxes: [
      { line_id: 1, x: 200, y: 100, w: 800, h: 50, text: "计算方式是 Q、K、V 三个矩阵。" },
      { line_id: 2, x: 200, y: 200, w: 600, h: 50, text: "注意力机制的一段说明" },
    ],
    ...over,
  };
}

export function apps(over: Partial<AppsInfo> = {}): AppsInfo {
  return {
    apps: [{ process: "msedge.exe", tier: "record", added_ms: at(9) }, { process: "chrome.exe", tier: "record", added_ms: at(9) }],
    unlisted: [{ app: "Code.exe", last_ms: at(14, 12), total_ms: 7 * 60_000 }],
    excludes: [{ exclude_id: 1, kind: "host", value: "bank.example.com", added_ms: at(9) }],
    ...over,
  };
}

export function settings(over: Partial<Settings> = {}): Settings {
  return {
    numbers: { still_ms: 2000, forced_ms: 10000, ocr_min_chars: 100, ocr_min_body_ratio: 0.3, merge_ms: 300000, image_days: 30, min_free_gb: 5 },
    data_dir: "C:\\example\\data-v3", llm: { configured: false, model: null }, agent_enabled: true, autostart: false,
    ...over,
  };
}

export function agentConfigs(): AgentConfigs {
  const cmd = { command: "C:\\example\\python.exe", args: ["-m", "screenrecall.app.mcp_shim"] };
  return {
    configs: {
      claude_code: { name: "Claude Code", file: ".mcp.json", json: { mcpServers: { screenrecall: { type: "stdio", ...cmd } } }, cli: "claude mcp add screenrecall -- C:\\example\\python.exe" },
      codex: { name: "Codex", file: "%USERPROFILE%\\.codex\\config.toml", toml: '[mcp_servers.screenrecall]\ncommand = "C:\\\\example\\\\python.exe"\n' },
    },
    mcp_url: "http://127.0.0.1:8611/mcp",
  };
}

/** 各接口的默认回答（测试里没特别处理的请求） */
export function defaultReply(call: Call): Reply | null {
  const p = pathOf(call.path);
  if (p === "/api/v3/status") return { body: status() };
  if (p === "/api/v3/timeline") return { body: timeline() };
  if (/^\/api\/v3\/entries\/\d+$/.test(p)) return { body: entryDetail() };
  if (/^\/api\/v3\/shots\/\d+$/.test(p)) return { body: shotDetail() };
  if (p === "/api/v3/search") return { body: search({ query: queryOf(call.path).get("q") ?? "" }) };
  if (p === "/api/v3/apps" && call.method === "GET") return { body: apps() };
  if (p === "/api/v3/settings" && call.method === "GET") return { body: settings() };
  if (p === "/api/app/agent-configs") return { body: agentConfigs() };
  return null;
}

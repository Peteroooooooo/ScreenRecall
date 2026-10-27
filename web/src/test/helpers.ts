// 测试用的 fetch 模拟与固定数据。数据形状与 /api/v2 接口约定完全一致，内容都是合成的示例。
import { vi } from "vitest";
import type { AgentConfigs, Day, FrameDetail, PieceDetail, SearchResult, Settings, Status, Storage } from "../api";

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

/** 一直不返回、直到被中止的请求（模拟服务卡住或慢查询） */
export function hang(signal?: AbortSignal | null): Promise<never> {
  return new Promise((_, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
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
  return {
    recording: "recording", pause_until_ms: null, recorder_alive: true,
    current: { app: "SampleEditor.exe", title: "示例文档.txt" }, pending_ocr: 2, idle_work: "idle", agent_enabled: true, revision: 1,
    ...over,
  };
}

export function day(over: Partial<Day> = {}): Day {
  return {
    date: "2026-10-27",
    overview: { active_ms: 3 * 3_600_000 + 12 * 60_000, apps: [{ app: "SampleReader.exe", ms: 2_160_000 }, { app: "SampleChat.exe", ms: 420_000 }], lines_new: 1204, frames: 41 },
    summary: { text: "示例任务 A（约 2 小时）、示例任务 B（约 1 小时）。", status: "ok", model: "sample-model" },
    items: [
      { kind: "piece", piece_id: 12, start_ms: at(9, 48), end_ms: at(9, 55), app: "SampleChat.exe", title: "示例小组群聊", task: "示例任务 B", summary_line: "讨论了分工。", pending_ocr: true },
      { kind: "gap", start_ms: at(9, 30), end_ms: at(9, 48), reason: "paused", note: "手动暂停" },
      { kind: "piece", piece_id: 11, start_ms: at(9, 12), end_ms: at(9, 30), app: "SampleReader.exe", title: "示例讲义第 3 章.pdf", task: "示例任务 A", summary_line: null, pending_ocr: false },
      { kind: "gap", start_ms: at(8, 0), end_ms: at(9, 12), reason: "locked", note: null },
    ],
    ...over,
  };
}

export const F1 = "f0000000000000000000000000000001";
export const F2 = "f0000000000000000000000000000002";

export function piece(over: Partial<PieceDetail> = {}): PieceDetail {
  return {
    piece: { piece_id: 11, start_ms: at(9, 12), end_ms: at(9, 48), app: "SampleReader.exe", title: "示例讲义第 3 章.pdf", task: "示例任务 A" },
    summary: { text: "示例概括：先看了定义，然后看了示例公式。", status: "ok", model: "sample-model" },
    summary_state: "ok",
    blocks: [
      { block_id: 1, items: [
        { kind: "line", line_id: 101, text: "示例标题：第 3 章", source: "uia", first_seen_ms: at(9, 12), frame_id: F1 },
        { kind: "line", line_id: 102, text: "示例定义：一行读到的文字", source: "uia", first_seen_ms: at(9, 13), frame_id: F1 },
        { kind: "skim", start_ms: at(9, 14), end_ms: at(9, 14) + 4000, distance_px: 2400 },
        { kind: "pending", region_id: 7, frame_id: F2 },
      ] },
      { block_id: 2, items: [
        { kind: "line", line_id: 201, text: "示例结尾：空闲时识别出来的一行", source: "ocr", first_seen_ms: at(9, 30), frame_id: F2 },
      ] },
    ],
    unjoined: true,
    frames: [
      { frame_id: F1, captured_ms: at(9, 14), width: 1600, height: 1000 },
      { frame_id: F2, captured_ms: at(9, 30), width: 1600, height: 1000 },
    ],
    ...over,
  };
}

export function frame(id = F1, over: Partial<FrameDetail> = {}): FrameDetail {
  return {
    frame_id: id, captured_ms: at(9, 14), width: 1600, height: 1000, app: "SampleReader.exe", title: "示例讲义第 3 章.pdf", piece_id: 11,
    lines: [
      { line_id: 101, text: "示例标题：第 3 章", x0: 100, y0: 80, x1: 700, y1: 120 },
      { line_id: 102, text: "示例定义：一行读到的文字", x0: 100, y0: 140, x1: 900, y1: 180 },
    ],
    ...over,
  };
}

export function search(over: Partial<SearchResult> = {}): SearchResult {
  return {
    query: "示例词", total_groups: 2, semantic: false,
    groups: [
      { segment_id: 5, app: "SampleReader.exe", title: "示例讲义第 3 章.pdf", first_seen_ms: at(9, 12), last_seen_ms: at(9, 41), hits_total: 4, piece_id: 11,
        hits: [
          { line_id: 102, text: "示例定义：这里提到了示例词，后面是占位文字。", spans: [[10, 13]], last_seen_ms: at(9, 14), frame_id: F1 },
          { line_id: 103, text: "示例词出现在开头", spans: [[0, 3]], last_seen_ms: at(9, 21), frame_id: null },
        ] },
      { segment_id: 6, app: "SampleChat.exe", title: "示例小组群聊", first_seen_ms: at(9, 48), last_seen_ms: at(9, 53), hits_total: 1, piece_id: null,
        hits: [{ line_id: 301, text: "示例消息：我来整理示例词那一部分。", spans: [[9, 12]], last_seen_ms: at(9, 50), frame_id: null }] },
    ],
    ...over,
  };
}

export function settings(over: Partial<Settings> = {}): Settings {
  return {
    pinned: [{ process: "SampleChat.exe", title: "示例小组群聊" }],
    excluded_processes: ["SamplePasswords.exe"], excluded_title_keywords: ["InPrivate"],
    hidden_from_agents: { apps: ["SampleBank.exe"], title_keywords: ["示例私事"] },
    agent_enabled: true, idle_minutes: 5, ocr_model: "tiny", autostart: false,
    llm: { configured: true, model: "sample-model", host: "llm.example.invalid" }, data_dir: "C:\\Users\\example\\AppData\\Local\\ScreenRecall\\data",
    image_days: 30, text_days: 90, storage_cap_gb: 0,
    ...over,
  };
}

export function storage(over: Partial<Storage> = {}): Storage {
  return { data_dir: "C:\\Users\\example\\AppData\\Local\\ScreenRecall\\data", db_bytes: 30_000_000, frames_bytes: 120_000_000, vectors_bytes: 8_000_000,
    frames: 41, lines: 1204, oldest_ms: at(8), ...over };
}

export function agentConfigs(): AgentConfigs {
  const cmd = { command: "C:\\example\\python.exe", args: ["-m", "screenrecall.app.mcp_shim"], env: { PYTHONPATH: "C:\\example" } };
  return {
    configs: {
      claude_desktop: { name: "Claude Desktop", file: "%APPDATA%\\Claude\\claude_desktop_config.json", json: { mcpServers: { screenrecall: cmd } } },
      claude_code: { name: "Claude Code", file: ".mcp.json", json: { mcpServers: { screenrecall: { type: "stdio", ...cmd } } }, cli: "claude mcp add screenrecall -- C:\\example\\python.exe" },
      cursor: { name: "Cursor", file: "%USERPROFILE%\\.cursor\\mcp.json", json: { mcpServers: { screenrecall: cmd } } },
    },
    mcp_url: "http://127.0.0.1:8611/mcp",
  };
}

export const windows = () => ({ windows: [{ hwnd: 1001, process: "SampleEditor.exe", title: "示例文档.txt" }, { hwnd: 1002, process: "SampleChat.exe", title: "示例小组群聊" }] });
export const apps = () => ({ apps: [{ app: "SampleReader.exe", n: 30 }, { app: "SampleChat.exe", n: 8 }] });

/** 各接口的默认回答（测试里没特别处理的请求） */
export function defaultReply(call: Call): Reply | null {
  const p = pathOf(call.path);
  if (p === "/api/v2/status") return { body: status() };
  if (p === "/api/v2/day") return { body: day({ date: queryOf(call.path).get("date") ?? "2026-10-27" }) };
  if (/^\/api\/v2\/pieces\/\d+$/.test(p)) return { body: piece() };
  if (/^\/api\/v2\/frames\/[^/]+$/.test(p)) return { body: frame(decodeURIComponent(p.split("/").pop()!)) };
  if (p === "/api/v2/search") return { body: search({ query: queryOf(call.path).get("q") ?? "" }) };
  if (p === "/api/v2/apps") return { body: apps() };
  if (p === "/api/v2/settings" && call.method === "GET") return { body: settings() };
  if (p === "/api/v2/windows") return { body: windows() };
  if (p === "/api/v2/storage") return { body: storage() };
  if (p === "/api/app/agent-configs") return { body: agentConfigs() };
  return null;
}

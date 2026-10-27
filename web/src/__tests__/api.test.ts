import { describe, expect, it, vi } from "vitest";
import { ApiError, api, frameImageUrl, frameThumbUrl, isUnauthorized, UNAUTHORIZED_EVENT, withQuery } from "../api";
import { highlight, fmtDuration, parseLocalDate, addDays } from "../lib";
import { mockFetch, status } from "../test/helpers";

describe("api client", () => {
  it("GET sends Accept and no CSRF header; POST sends X-ScreenRecall: 1 and a JSON body", async () => {
    const calls = mockFetch((c) => (c.path === "/api/v2/pause" ? { body: status({ recording: "paused" }) } : null));
    await api.status();
    const s = await api.pause({ minutes: 15 });
    expect(s.recording).toBe("paused");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].headers["X-ScreenRecall"]).toBeUndefined();
    expect(calls[1]).toMatchObject({ method: "POST", path: "/api/v2/pause", body: { minutes: 15 } });
    expect(calls[1].headers["X-ScreenRecall"]).toBe("1");
    expect(calls[1].headers["Content-Type"]).toBe("application/json");
  });

  it("builds query strings and drops empty values", async () => {
    expect(withQuery("/x", { a: 1, b: "", c: undefined, d: null, q: "a b" })).toBe("/x?a=1&q=a+b");
    const calls = mockFetch();
    await api.search({ q: "示例词", from: 10, app: undefined, limit: 30 });
    expect(calls[0].path).toBe(`/api/v2/search?q=${encodeURIComponent("示例词")}&from=10&limit=30`);
    await api.day(undefined);
    expect(calls[1].path).toBe("/api/v2/day");
    expect(frameImageUrl("ab/c")).toBe("/api/v2/frames/ab%2Fc/image");
    expect(frameThumbUrl("abc")).toBe("/api/v2/frames/abc/thumb?w=320");
  });

  it("throws ApiError with the server's error text", async () => {
    mockFetch(() => ({ status: 400, body: { error: "时间范围不对" } }));
    await expect(api.remove({ recent_minutes: 5 })).rejects.toMatchObject({ status: 400, message: "时间范围不对" });
    await expect(api.remove({ recent_minutes: 5 })).rejects.toBeInstanceOf(ApiError);
  });

  it("a 401 broadcasts the unauthorized event once and is remembered", async () => {
    mockFetch(() => ({ status: 401, body: { error: "需要登录" } }));
    const seen = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, seen);
    await expect(api.status()).rejects.toMatchObject({ status: 401 });
    await expect(api.settings()).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(UNAUTHORIZED_EVENT, seen);
    expect(seen).toHaveBeenCalledTimes(1);
    expect(isUnauthorized()).toBe(true);
  });

  it("passes the AbortSignal through", async () => {
    const calls = mockFetch();
    const c = new AbortController();
    await api.storage(c.signal);
    expect(calls[0].signal).toBe(c.signal);
  });
});

describe("helpers", () => {
  it("highlight uses code-point offsets and merges overlapping spans", () => {
    expect(highlight("abcdef", [[1, 3], [2, 4]])).toEqual([
      { text: "a", hit: false }, { text: "bcd", hit: true }, { text: "ef", hit: false },
    ]);
    // 😀 是一个码点、两个 UTF-16 单元：偏移按码点算
    expect(highlight("😀示例词", [[1, 4]])).toEqual([{ text: "😀", hit: false }, { text: "示例词", hit: true }]);
    expect(highlight("abc", [[5, 9]])).toEqual([{ text: "abc", hit: false }]);
  });

  it("formats durations and local dates", () => {
    expect(fmtDuration(36 * 60_000)).toBe("36 分钟");
    expect(fmtDuration(72 * 60_000)).toBe("1 小时 12 分");
    expect(fmtDuration(10_000)).toBe("不到 1 分钟");
    expect(parseLocalDate("2026-02-30")).toBeNull();
    expect(parseLocalDate("2026-10-27")?.getDate()).toBe(27);
    expect(addDays("2026-11-01", -1)).toBe("2026-10-31");
  });
});

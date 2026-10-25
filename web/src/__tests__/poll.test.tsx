import { describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen } from "@testing-library/react";
import App from "../App";
import { TIMEOUT_ERROR, usePoll } from "../lib";
import { hang, mockFetch, shellReply, status } from "../test/helpers";

const flush = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("usePoll", () => {
  it("flags the connection as lost after repeated failures and recovers on the next success", async () => {
    vi.useFakeTimers();
    let ok = true;
    const fn = vi.fn(() => (ok ? Promise.resolve({ n: 1 }) : Promise.reject(new Error("Failed to fetch"))));
    const { result } = renderHook(() => usePoll(fn, 2000));
    await flush();
    expect(result.current).toMatchObject({ data: { n: 1 }, lost: false, error: null });

    ok = false;
    await flush(2000); // 第 1 次失败：可能只是抖动，还不算中断
    expect(result.current.lost).toBe(false);
    expect(result.current.error).toBe("Failed to fetch");
    await flush(2000); // 第 2 次失败（距上次成功约 4 秒）：判为中断，旧数据保留但被标记
    expect(result.current.lost).toBe(true);
    expect(result.current.data).toEqual({ n: 1 });

    ok = true;
    await flush(2000);
    expect(result.current).toMatchObject({ lost: false, error: null });
  });

  it("treats a hung request as a failure via timeout", async () => {
    vi.useFakeTimers();
    let hung = false;
    const fn = vi.fn((signal: AbortSignal) => (hung ? hang(signal) : Promise.resolve("ok")));
    const { result } = renderHook(() => usePoll(fn, 2000, 4000));
    await flush();
    hung = true;
    await flush(2000); // 下一次请求发出，一直不回
    expect(result.current.lost).toBe(false);
    await flush(4000); // 4 秒超时，距上次成功 6 秒 > 5 秒
    expect(result.current.lost).toBe(true);
    expect(result.current.error).toBe(TIMEOUT_ERROR);
  });
});

describe("shell status", () => {
  it("shows connection lost in the sidebar and recorder, disables controls, then recovers", async () => {
    vi.useFakeTimers();
    window.location.hash = "#/recorder";
    let up = true;
    const calls = mockFetch((c) => {
      if (c.path === "/api/status" && !up) throw new TypeError("Failed to fetch");
      if (c.path === "/api/status") return { body: status(true) };
      return shellReply(c) ?? { status: 404 };
    });
    render(<App />);
    await flush();
    await flush();
    expect(screen.getAllByText("录制中").length).toBeGreaterThan(0);
    expect(screen.queryByText("连接中断")).toBeNull();
    const pause = screen.getByRole("button", { name: "暂停" }) as HTMLButtonElement;
    expect(pause.disabled).toBe(false);

    up = false;
    await flush(2000);
    await flush(2000);
    // 侧栏、顶栏、录制页状态卡都改为"连接中断"，不再显示"录制中"
    expect(screen.getAllByText("连接中断").length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText("录制中")).toBeNull();
    expect((screen.getByRole("button", { name: "暂停" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: /立即补拍/ }) as HTMLButtonElement).disabled).toBe(true);

    const metaBefore = calls.filter((c) => c.path === "/api/meta").length;
    up = true;
    await flush(2000);
    await flush();
    expect(screen.queryByText("连接中断")).toBeNull();
    expect(screen.getAllByText("录制中").length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "暂停" }) as HTMLButtonElement).disabled).toBe(false);
    // 连接恢复后重新拉一次筛选列表
    expect(calls.filter((c) => c.path === "/api/meta").length).toBeGreaterThan(metaBefore);
  });

  it("refreshes /api/meta when the window regains focus", async () => {
    vi.useFakeTimers();
    const calls = mockFetch((c) => shellReply(c) ?? { status: 404 });
    render(<App />);
    await flush();
    const n = calls.filter((c) => c.path === "/api/meta").length;
    await flush(6000);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await flush();
    expect(calls.filter((c) => c.path === "/api/meta").length).toBe(n + 1);
  });
});

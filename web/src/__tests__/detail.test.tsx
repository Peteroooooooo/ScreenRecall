import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import App from "../App";
import { FID, detail, mockFetch, shellReply } from "../test/helpers";

function openDetail(over: Parameters<typeof detail>[0] = {}) {
  window.location.hash = `#/frame/${FID}?occ=occ1`;
  return mockFetch((c) => {
    if (c.path === `/api/frames/${FID}/detail`) return { body: detail(over) };
    if (c.path === `/api/frames/${FID}/delete-plan`)
      return { body: { frame_id: FID, favorites: ["occ1"], image_status: "ok", occurrences: [detail().occurrence] } };
    if (c.path === `/api/frames/${FID}/delete`) return { body: { message: "已删除" } };
    return shellReply(c) ?? { status: 404 };
  });
}

describe("detail page OCR panel", () => {
  it("says recognition is in progress while OCR is pending", async () => {
    openDetail({ ocr_status: "pending" });
    render(<App />);
    expect(await screen.findByText(/文字识别中/)).toBeTruthy();
    expect(screen.queryByText("（识别完成，但没有文字）")).toBeNull();
  });

  it("says recognition failed when OCR failed", async () => {
    openDetail({ ocr_status: "failed" });
    render(<App />);
    expect(await screen.findByText(/文字识别失败/)).toBeTruthy();
  });

  it("says recognized-but-empty only when OCR is done", async () => {
    openDetail({ ocr_status: "done" });
    render(<App />);
    expect(await screen.findByText("（识别完成，但没有文字）")).toBeTruthy();
  });
});

describe("delete confirmation", () => {
  it("shows the plan before deleting and sends the confirmed favorites", async () => {
    const calls = openDetail();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /删除全部记录/ }));
    const dialog = await screen.findByRole("dialog");
    // 先看计划：列出影响范围与收藏，此时还没有发删除请求
    expect(within(dialog).getByText(/将删除这幅画面的全部 1 次出现记录，以及其中 1 个收藏/)).toBeTruthy();
    expect(within(dialog).getByText("★ 将删除")).toBeTruthy();
    expect(calls.some((c) => c.path === `/api/frames/${FID}/delete`)).toBe(false);

    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "确认删除全部记录" }));
    });
    const del = calls.filter((c) => c.path === `/api/frames/${FID}/delete`);
    expect(del).toHaveLength(1);
    expect(del[0].method).toBe("POST");
    expect(del[0].body).toEqual({ confirmed_favorites: ["occ1"] });
    const iPlan = calls.findIndex((c) => c.path.endsWith("/delete-plan"));
    expect(iPlan).toBeGreaterThanOrEqual(0);
    expect(iPlan).toBeLessThan(calls.indexOf(del[0]));
  });

  it("cancelling the dialog sends nothing", async () => {
    const calls = openDetail();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /删除全部记录/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(calls.some((c) => c.path === `/api/frames/${FID}/delete`)).toBe(false);
  });
});

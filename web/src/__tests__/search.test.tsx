import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import App from "../App";
import { hang, mockFetch, result, searchResponse, shellReply, type Reply } from "../test/helpers";

const models = (text: object, image: object = { state: "idle", elapsed_ms: null, reason: null }): Reply =>
  ({ body: { text: { elapsed_ms: null, reason: null, ...text }, image } });

describe("search page", () => {
  it("asks for input when searching with an empty query", async () => {
    window.location.hash = "#/";
    const calls = mockFetch((c) => shellReply(c) ?? { status: 404 });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "搜索" }));
    expect(screen.getByText("请输入要查找的文字")).toBeTruthy();
    expect(calls.some((c) => c.path === "/api/search")).toBe(false);
    // 输入内容后提示消失
    fireEvent.change(screen.getByRole("textbox", { name: "搜索内容" }), { target: { value: "pytest" } });
    expect(screen.queryByText("请输入要查找的文字")).toBeNull();
  });

  it("renders results with the matched fragments tag and the rank basis", async () => {
    window.location.hash = "#/?q=pytest%20conftest";
    mockFetch((c) => {
      if (c.path === "/api/search")
        return { body: searchResponse([result({ matched_terms: ["pytest", "conftest"] }), result({ frame_id: "f".repeat(32), matched_terms: [] })]) };
      return shellReply(c) ?? { status: 404 };
    });
    render(<App />);
    expect(await screen.findByText("命中片段：pytest、conftest")).toBeTruthy();
    expect(screen.getAllByText(/^命中片段：/)).toHaveLength(1); // 没有命中片段的卡片不显示
    expect(screen.getByText(/按词法与语义排名融合（RRF）排序/)).toBeTruthy();
  });

  it("falls back to the server's rank basis label for an unknown basis", async () => {
    window.location.hash = "#/?q=pytest";
    mockFetch((c) => {
      if (c.path === "/api/search") return { body: searchResponse([result()], { rank_basis: "newthing", rank_basis_label: "某种新依据" }) };
      return shellReply(c) ?? { status: 404 };
    });
    render(<App />);
    expect(await screen.findByText(/按某种新依据排序/)).toBeTruthy();
  });

  it("shows the model loading state with elapsed seconds", async () => {
    window.location.hash = "#/";
    mockFetch((c) => (c.path === "/api/models" ? models({ state: "loading", elapsed_ms: 3000 }) : shellReply(c) ?? { status: 404 }));
    render(<App />);
    expect(await screen.findByText(/文字语义模型加载中（3 秒）/)).toBeTruthy();
  });

  it("shows the reason when the model is unavailable", async () => {
    window.location.hash = "#/";
    mockFetch((c) => (c.path === "/api/models" ? models({ state: "unavailable", reason: "模型未下载（minilm）" }) : shellReply(c) ?? { status: 404 }));
    render(<App />);
    expect(await screen.findByText(/文字语义模型不可用。 模型未下载（minilm）/)).toBeTruthy();
  });

  it("keeps the old hint on an old backend and lets the user stop waiting", async () => {
    window.location.hash = "#/?q=pytest";
    let searchSignal: AbortSignal | null | undefined;
    mockFetch((c) => {
      if (c.path === "/api/search") {
        searchSignal = c.signal;
        return hang(c.signal);
      }
      return shellReply(c) ?? { status: 404 }; // /api/models → 404
    });
    render(<App />);
    expect(await screen.findByText("正在搜索…第一次查询要加载模型，约需十几秒")).toBeTruthy();
    expect(screen.getByText(/已等待 \d+ 秒/)).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /停止等待/ }));
    });
    expect(searchSignal?.aborted).toBe(true);
    expect(screen.getByText(/已停止等待这次搜索/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新搜索" })).toBeTruthy();
  });

  it("shows a plain searching label once the model is ready", async () => {
    window.location.hash = "#/?q=pytest";
    mockFetch((c) => {
      if (c.path === "/api/search") return hang(c.signal);
      if (c.path === "/api/models") return models({ state: "ready" });
      return shellReply(c) ?? { status: 404 };
    });
    render(<App />);
    expect(await screen.findByText("正在搜索…")).toBeTruthy();
    expect(await screen.findByText("文字语义模型已就绪")).toBeTruthy();
  });
});

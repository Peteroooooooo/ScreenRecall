import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { SettingsPatch } from "../api";
import { mockFetch, settings } from "../test/helpers";
import { renderAt, toasts } from "../test/render";

function server() {
  let cur = settings();
  return mockFetch((c) => {
    if (c.path === "/api/v2/settings" && c.method === "POST") {
      cur = { ...cur, ...(c.body as SettingsPatch) };
      return { body: cur };
    }
    if (c.path === "/api/v2/settings/llm") return { body: { ...cur, llm: { configured: true, model: (c.body as { model: string }).model, host: "new.example.invalid" } } };
    if (c.path === "/api/v2/settings/llm/test") return { body: { ok: true, model: "sample-model", latency_ms: 812 } };
    return null;
  });
}

describe("settings page", () => {
  it("shows the LLM status without any key, and saves the form keeping the key when left empty", async () => {
    const calls = server();
    renderAt("#/settings");
    expect(await screen.findByText("已配置")).toBeTruthy();
    expect(screen.getByText("模型 sample-model")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "修改" }));
    const key = screen.getByLabelText("密钥") as HTMLInputElement;
    expect(key.type).toBe("password");
    expect(key.value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "显示" }));
    expect(key.type).toBe("text");
    fireEvent.change(screen.getByLabelText("接口地址（OpenAI 兼容）"), { target: { value: "https://llm.example.invalid/v1" } });
    fireEvent.change(screen.getByLabelText("模型"), { target: { value: "other-model" } });
    fireEvent.submit(key.closest("form")!);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/settings/llm")?.body)
      .toEqual({ base_url: "https://llm.example.invalid/v1", model: "other-model" }));
    expect(await screen.findByText("模型 other-model")).toBeTruthy();

    // 填了密钥才发 api_key，保存后清空输入框
    fireEvent.change(key, { target: { value: "sk-test-synthetic" } });
    fireEvent.submit(key.closest("form")!);
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/v2/settings/llm")).toHaveLength(2));
    expect((calls.filter((c) => c.path === "/api/v2/settings/llm")[1].body as { api_key?: string }).api_key).toBe("sk-test-synthetic");
    await waitFor(() => expect(key.value).toBe(""));

    // 只有空白 = 留空（不会把已存的密钥换成空白）；带空白的密钥去掉首尾空白再发
    fireEvent.change(key, { target: { value: "   " } });
    fireEvent.submit(key.closest("form")!);
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/v2/settings/llm")).toHaveLength(3));
    expect(calls.filter((c) => c.path === "/api/v2/settings/llm")[2].body).not.toHaveProperty("api_key");
    fireEvent.change(key, { target: { value: " sk-test-synthetic2\n" } });
    fireEvent.submit(key.closest("form")!);
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/v2/settings/llm")).toHaveLength(4));
    expect((calls.filter((c) => c.path === "/api/v2/settings/llm")[3].body as { api_key?: string }).api_key).toBe("sk-test-synthetic2");
  });

  it("测试连接 reports latency and model", async () => {
    const calls = server();
    renderAt("#/settings");
    fireEvent.click(await screen.findByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/v2/settings/llm/test")?.method).toBe("POST"));
    await waitFor(() => expect(toasts().join()).toContain("连接正常 · 0.8 秒 · sample-model"));
  });

  it("saves idle minutes, OCR model and autostart", async () => {
    const calls = server();
    renderAt("#/settings");
    fireEvent.change(await screen.findByRole("combobox", { name: "键鼠多久没动开始整理" }), { target: { value: "10" } });
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ idle_minutes: 10 }));
    fireEvent.click(screen.getByRole("radio", { name: /大模型/ }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ ocr_model: "small" }));
    fireEvent.click(screen.getByRole("switch", { name: "开机自动启动" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ autostart: true }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "开机自动启动" }).getAttribute("aria-checked")).toBe("true"));
  });

  it("switching the language re-renders in English", async () => {
    server();
    renderAt("#/settings");
    fireEvent.click(await screen.findByRole("radio", { name: "English" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeTruthy();
    expect(localStorage.getItem("sr-lang")).toBe("en");
  });

  it("has no legacy import link (v2 privacy delete could not reach imported files)", async () => {
    server();
    renderAt("#/settings");
    await screen.findByText("数据文件夹");
    expect(screen.queryByRole("link", { name: "打开导入页" })).toBeNull();
  });
});

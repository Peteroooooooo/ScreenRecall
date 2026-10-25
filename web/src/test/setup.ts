import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom 的 <dialog> 可能没有 showModal / close：补一个只切换 open 属性的版本
const proto = window.HTMLDialogElement?.prototype;
if (proto && typeof proto.showModal !== "function") {
  proto.showModal = function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  proto.close = function (this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}

beforeEach(() => {
  localStorage.setItem("sr-lang", "zh");
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  localStorage.clear();
});

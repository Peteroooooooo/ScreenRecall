import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { resetUnauthorized } from "../api";

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
// jsdom 没有实现 scrollTo（只会往控制台报 Not implemented）
window.scrollTo = (() => {}) as typeof window.scrollTo;

beforeEach(() => {
  localStorage.setItem("sr-lang", "zh");
  window.location.hash = "";
  resetUnauthorized();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  localStorage.clear();
});

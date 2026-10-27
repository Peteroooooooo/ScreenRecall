// 把整个应用渲染到指定的哈希路由（外壳 + 页面一起测，和真实使用一致）。
import { render } from "@testing-library/react";
import App from "../App";

export function renderAt(hash: string) {
  window.location.hash = hash;
  return render(<App />);
}

/** 轻提示区域里的文字 */
export const toasts = () => Array.from(document.querySelectorAll("[aria-live='polite'] [role]")).map((x) => x.textContent ?? "");

import { describe, expect, it } from "vitest";
import { DICTS, format, hasKey } from "../i18n";

const vars = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe("i18n dictionaries", () => {
  it("have the same keys in zh and en", () => {
    expect(Object.keys(DICTS.en).sort()).toEqual(Object.keys(DICTS.zh).sort());
  });

  it("use the same placeholders in both languages and no empty strings", () => {
    for (const k of Object.keys(DICTS.zh) as (keyof typeof DICTS.zh)[]) {
      expect(vars(DICTS.en[k]), k).toEqual(vars(DICTS.zh[k]));
      expect(DICTS.zh[k].trim().length, k).toBeGreaterThan(0);
      expect(DICTS.en[k].trim().length, k).toBeGreaterThan(0);
    }
  });

  it("formats placeholders and checks runtime keys", () => {
    expect(format("{a} / {b}", { a: 1, b: "x" })).toBe("1 / x");
    expect(hasKey("num.still_ms")).toBe(true);
    expect(hasKey("num.nope")).toBe(false);
    expect(hasKey("toString")).toBe(false);
  });
});

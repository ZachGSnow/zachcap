import { describe, expect, it } from "vitest";

import { DEFAULT_KEYS, keysFor, keysFromEvent, setKeys, showKeys } from "./shortcuts";

/** A key press as the page gets it: ``keyCode`` is Windows' virtual-key code. */
function press(keyCode: number, key: string, mods: Partial<Record<"ctrl" | "alt" | "shift" | "meta", boolean>> = {}, code = "") {
  return {
    keyCode,
    key,
    code,
    ctrlKey: !!mods.ctrl,
    altKey: !!mods.alt,
    shiftKey: !!mods.shift,
    metaKey: !!mods.meta,
  } as KeyboardEvent;
}

describe("keysFromEvent", () => {
  it("reads the key Windows will match, whatever the layout", () => {
    // On a French keyboard the key marked A sits where a US keyboard has Q: it's still A.
    expect(keysFromEvent(press(65, "a", { ctrl: true, alt: true }, "KeyQ"))).toEqual({ keys: "Ctrl+Alt+A" });
    expect(keysFromEvent(press(57, "9", { ctrl: true, alt: true, shift: true }))).toEqual({ keys: "Ctrl+Alt+Shift+9" });
    expect(keysFromEvent(press(120, "F9", { ctrl: true, alt: true }))).toEqual({ keys: "Ctrl+Alt+F9" });
  });

  it("waits while only modifiers are down", () => {
    expect(keysFromEvent(press(17, "Control", { ctrl: true }, "ControlLeft"))).toBeNull();
    expect(keysFromEvent(press(18, "Alt", { ctrl: true, alt: true }, "AltRight"))).toBeNull();
  });

  it("refuses keys that would take a shortcut from every app", () => {
    expect(keysFromEvent(press(82, "r", { ctrl: true }))).toHaveProperty("problem"); // reload
    expect(keysFromEvent(press(116, "F5"))).toHaveProperty("problem");
    expect(keysFromEvent(press(80, "p"))).toHaveProperty("problem");
    expect(keysFromEvent(press(9, "Tab", { ctrl: true, alt: true }))).toHaveProperty("problem");
  });

  it("allows Win with one key, and the keys no app uses on their own", () => {
    expect(keysFromEvent(press(68, "d", { meta: true }))).toEqual({ keys: "Win+D" });
    expect(keysFromEvent(press(124, "F13"))).toEqual({ keys: "F13" });
  });

  it("refuses Ctrl+Alt keys that type a character on this keyboard (AltGr)", () => {
    const euro = keysFromEvent(press(69, "€", { ctrl: true, alt: true }));
    expect(euro).toHaveProperty("problem");
    expect((euro as { problem: string }).problem).toContain("€");
  });
});

describe("the keys in use", () => {
  it("are shown as key caps and follow Settings", () => {
    expect(showKeys("Ctrl+Alt+R")).toBe("Ctrl + Alt + R");
    expect(keysFor("record")).toBe(DEFAULT_KEYS.record);
    setKeys({ ...DEFAULT_KEYS, record: "Ctrl+Alt+F9" });
    expect(keysFor("record")).toBe("Ctrl+Alt+F9");
    setKeys(DEFAULT_KEYS);
    expect(keysFor("record")).toBe("Ctrl+Alt+R");
  });
});

import { describe, expect, it } from "vitest";

import { clock, hours, initials, levelPercent, minutes, toSeconds } from "./format";

describe("times", () => {
  it("are written the way the app shows them", () => {
    expect(clock(65)).toBe("01:05");
    expect(clock(3725)).toBe("1:02:05");
    expect(clock(-3)).toBe("00:00");
    expect(minutes(null)).toBe("");
    expect(minutes(42)).toBe("42 s");
    expect(minutes(37 * 60)).toBe("37 min");
    expect(minutes(95 * 60)).toBe("1 h 35 min");
    expect(hours(1800)).toBe("30 min");
    expect(hours(5400)).toBe("1.5 h");
  });

  it("read back from what Claude cites", () => {
    expect(toSeconds("01:05")).toBe(65);
    expect(toSeconds("[1:02:05]")).toBe(3725);
    expect(toSeconds("soon")).toBeNull();
    expect(toSeconds(undefined)).toBeNull();
  });
});

describe("small helpers", () => {
  it("make initials and meter levels", () => {
    expect(initials("dana  scully")).toBe("DS");
    expect(initials("Mark")).toBe("M");
    expect(levelPercent(-90)).toBe(0);
    expect(levelPercent(-30)).toBe(50);
    expect(levelPercent(3)).toBe(100);
  });
});

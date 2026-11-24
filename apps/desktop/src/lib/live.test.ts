import { describe, expect, it } from "vitest";

import type { LiveLine, LiveState } from "./engine";
import { EMPTY_VIEW, LINES_SHOWN, atEnd, dot, elapsedNow, livePath, mergeLines, notices, speaker } from "./live";

function line(id: number, track: LiveLine["track"] = "system"): LiveLine {
  return { id, track, start: id * 5, end: id * 5 + 2, text: `line ${id}` };
}

function answer(run: string | null, lines: LiveLine[], dropped: number[] = []) {
  return { run, lines, dropped };
}

function state(more: Partial<LiveState> = {}): LiveState {
  return {
    recording: true,
    run: "run-1",
    meeting_id: 7,
    on: true,
    elapsed: 65,
    paused: false,
    me: "Dana Lee",
    lines: [],
    dropped: [],
    waiting: false,
    error: null,
    skipped: 0,
    ...more,
  };
}

describe("the live panel's lines", () => {
  it("asks for everything first, then only what came after its cursor", () => {
    expect(livePath(EMPTY_VIEW)).toBe("/api/live");
    const view = mergeLines(EMPTY_VIEW, answer("run 1", [line(1), line(2)]));
    expect(view).toEqual({ run: "run 1", after: 2, lines: [line(1), line(2)] });
    expect(livePath(view)).toBe("/api/live?run=run%201&after=2");
  });

  it("adds new lines in order, each once", () => {
    const view = mergeLines(EMPTY_VIEW, answer("a", [line(1)]));
    const next = mergeLines(view, answer("a", [line(3), line(2), line(1)]));
    expect(next.lines.map((l) => l.id)).toEqual([1, 2, 3]);
    expect(next.after).toBe(3);
  });

  it("shows the lines in the order they were said, not the order they came in", () => {
    const view = mergeLines(EMPTY_VIEW, answer("a", [line(2)]));
    // A line comes when its speaker stops: yours started first, and ended last.
    const yours = { ...line(3, "mic"), start: 7 };
    const next = mergeLines(view, answer("a", [yours, line(4)]));
    expect(next.lines.map((l) => l.id)).toEqual([3, 2, 4]);
    expect(next.after).toBe(4);
  });

  it("keeps the same view when nothing changed, so nothing on screen moves", () => {
    const view = mergeLines(EMPTY_VIEW, answer("a", [line(1)]));
    expect(mergeLines(view, answer("a", []))).toBe(view);
    expect(mergeLines(view, answer("a", [], [9]))).toBe(view); // a line it never had
    expect(mergeLines(EMPTY_VIEW, answer(null, []))).toBe(EMPTY_VIEW);
  });

  it("takes away a line found to be echo after it was shown, and reads on past it", () => {
    const view = mergeLines(EMPTY_VIEW, answer("a", [line(1, "mic")]));
    const next = mergeLines(view, answer("a", [line(2)], [1]));
    expect(next.lines.map((l) => l.id)).toEqual([2]);
    expect(next.after).toBe(2);
    // The last line gone: the cursor stays past it.
    const later = mergeLines(next, answer("a", [], [2]));
    expect(later).toEqual({ run: "a", after: 2, lines: [] });
  });

  it("starts over for another recording's live transcript", () => {
    const view = mergeLines(EMPTY_VIEW, answer("a", [line(1), line(2), line(3)]));
    const next = mergeLines(view, answer("b", [line(1)]));
    expect(next).toEqual({ run: "b", after: 1, lines: [line(1)] });
    expect(mergeLines(next, answer(null, []))).toEqual(EMPTY_VIEW); // not kept: none left
  });

  it("keeps the latest lines only", () => {
    const many = Array.from({ length: LINES_SHOWN + 20 }, (_, i) => line(i + 1));
    const view = mergeLines(EMPTY_VIEW, answer("a", many));
    expect(view.lines).toHaveLength(LINES_SHOWN);
    expect(view.lines[0].id).toBe(21);
    expect(view.after).toBe(LINES_SHOWN + 20);
  });

  it("names you as you're known, and everyone else as the call", () => {
    expect(speaker(line(1, "mic"), "Dana Lee")).toBe("Dana Lee");
    expect(speaker(line(1, "mic"), "  ")).toBe("You");
    expect(speaker(line(1, "mic"), null)).toBe("You");
    expect(speaker(line(1, "system"), "Dana Lee")).toBe("Call");
  });
});

describe("the live panel's title bar and scrolling", () => {
  it("ticks on from what the engine last said", () => {
    expect(elapsedNow(65, 1000, 3500)).toBe(67.5);
    expect(elapsedNow(65, 1000, 900)).toBe(65); // never backwards
    expect(elapsedNow(null, 1000, 3500)).toBeNull();
  });

  it("doesn't look like it's recording while the engine can't be reached", () => {
    expect(dot(state(), null)).toBe("recording");
    expect(dot(state({ paused: true }), null)).toBe("paused");
    expect(dot(state({ recording: false }), null)).toBe("off");
    expect(dot(null, null)).toBe("off");
    expect(dot(state(), "Failed to fetch")).toBe("off");
    expect(dot(state({ paused: true }), "Failed to fetch")).toBe("off");
    expect(elapsedNow(65, 1000, 60_000, "Failed to fetch")).toBe(65); // the clock stands still
  });

  it("follows new lines only while it's at the end", () => {
    expect(atEnd({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })).toBe(true);
    expect(atEnd({ scrollTop: 590, scrollHeight: 1000, clientHeight: 400 })).toBe(true);
    expect(atEnd({ scrollTop: 300, scrollHeight: 1000, clientHeight: 400 })).toBe(false);
  });
});

describe("what the live panel says", () => {
  it("says nothing while all is well", () => {
    expect(notices(state(), null)).toEqual([]);
  });

  it("says plainly what went wrong, and why lines are late or missing", () => {
    const said = notices(state({ error: "CUDA out of memory", waiting: true, skipped: 95 }), null);
    expect(said.map((n) => n.key)).toEqual(["error", "waiting", "skipped"]);
    expect(said[0].text).toContain("CUDA out of memory");
    expect(said[2].text).toContain("2 min");
    expect(notices(state({ skipped: 0.4 }), null)).toEqual([]);
  });

  it("says when the engine can't be reached, the recording is paused, ended, or has none", () => {
    expect(notices(null, "Engine not connected")[0]).toMatchObject({ key: "engine", tone: "error" });
    expect(notices(state({ paused: true }), null).map((n) => n.key)).toEqual(["paused"]);
    expect(notices(state({ recording: false, on: false }), null).map((n) => n.key)).toEqual(["ended"]);
    expect(notices(state({ on: false, waiting: true }), null).map((n) => n.key)).toEqual(["off"]);
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type Writing } from "./engine";
import { announce, windowInFront } from "./feedback";
import { earlier, isHere, outcome, tell } from "./writing";

vi.mock("./engine", () => ({ api: vi.fn() }));
vi.mock("./feedback", () => ({ announce: vi.fn(), windowInFront: vi.fn() }));

const asked = vi.mocked(api);

function run(more: Partial<Writing> = {}): Writing {
  return {
    id: "a1",
    what: "answer",
    about: "Who?",
    by: "small:4b",
    part: 1,
    of: 1,
    state: "writing",
    result: null,
    error: null,
    ...more,
  };
}

describe("what the model on this PC writes", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    asked.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("is told from what Claude wrote", () => {
    expect(isHere({ writing: "a1" })).toBe(true);
    expect(isHere({ text: "Done." })).toBe(false);
  });

  it("is looked for until it's there", async () => {
    asked.mockResolvedValueOnce(run()).mockResolvedValueOnce(run({ state: "done", result: { text: "Hi" } }));
    const result = outcome<{ text: string }>("a1");
    await vi.advanceTimersByTimeAsync(1000);
    expect(asked).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await result).toEqual({ text: "Hi" });
    expect(asked).toHaveBeenCalledWith("/api/writing/a1");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(asked).toHaveBeenCalledTimes(2);
  });

  it("says why it wasn't written", async () => {
    asked.mockResolvedValue(run({ state: "stopped", error: "Stopped." }));
    const stopped = expect(outcome("a1")).rejects.toThrow("Stopped.");
    await vi.advanceTimersByTimeAsync(1000);
    await stopped;
  });

  it("is looked for again when a look fails, a few times", async () => {
    asked.mockRejectedValueOnce(new Error("Failed to fetch")).mockResolvedValueOnce(run({ state: "done", result: 1 }));
    const result = outcome("a1");
    await vi.advanceTimersByTimeAsync(2000);
    expect(await result).toBe(1);

    asked.mockReset();
    asked.mockRejectedValue(new Error("That isn't being written any more"));
    const gone = expect(outcome("a1")).rejects.toThrow("isn't being written any more");
    await vi.advanceTimersByTimeAsync(20_000);
    await gone;
    expect(asked).toHaveBeenCalledTimes(5);
  });

  it("is told of once, to someone who isn't looking at it", async () => {
    const said = vi.mocked(announce);
    vi.mocked(windowInFront).mockResolvedValue(true);
    await tell("b1", "Answer ready", "Weekly call", true);
    expect(said).not.toHaveBeenCalled();
    await tell("b1", "Answer ready", "Weekly call", false);
    vi.mocked(windowInFront).mockResolvedValue(false);
    await tell("b1", "Answer ready", "Weekly call", true);
    expect(said).toHaveBeenCalledTimes(1);
    expect(said).toHaveBeenCalledWith("Answer ready", "Weekly call", "success");
    await tell("b2", "Document ready", "Weekly call", true);
    expect(said).toHaveBeenCalledTimes(2);
  });

  it("is shown again when the page is opened again", () => {
    const done = run({ id: "a1", state: "done", result: { answer: "Dana." } });
    const failed = run({ id: "a2", state: "failed", error: "No memory" });
    const document = run({ id: "a3", what: "document", about: "ticket", state: "done", result: {} });
    const now = run({ id: "a4", about: "And when?" });
    expect(earlier("answer", [done, failed, document], now)).toEqual([done, now]);
    expect(earlier("document", [done, failed, document], now)).toEqual([document]);
    expect(earlier("answer", undefined, null)).toEqual([]);
  });
});

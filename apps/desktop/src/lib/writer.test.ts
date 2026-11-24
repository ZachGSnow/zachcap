import { describe, expect, it } from "vitest";

import { costLabel, gigabytes, modelLabel, writerFor, writingLabel } from "./writer";

describe("who writes for a meeting", () => {
  const both = { writer: "claude", local_model: "qwen3.5:4b" } as const;

  it("is the one picked on its page, else the usual one", () => {
    expect(writerFor(null, false, both)).toBe("claude");
    expect(writerFor("local", false, both)).toBe("local");
    expect(writerFor(null, false, { ...both, writer: "local" })).toBe("local");
    expect(writerFor("claude", false, { ...both, writer: "local" })).toBe("claude");
  });

  it("is Claude when there is no model on this PC", () => {
    expect(writerFor("local", false, { writer: "local", local_model: "" })).toBe("claude");
    expect(writerFor(null, false, undefined)).toBe("claude");
  });

  it("is never Claude for a private meeting", () => {
    expect(writerFor(null, true, both)).toBe("local");
    expect(writerFor("claude", true, both)).toBe("local");
    expect(writerFor("claude", true, { writer: "claude", local_model: "" })).toBeNull();
    expect(writerFor(null, true, undefined)).toBeNull();
  });
});

describe("what is said of who wrote", () => {
  it("names the model, and says when it was on this PC", () => {
    expect(modelLabel("claude-opus-5-5")).toBe("Claude Opus 5.5");
    expect(modelLabel("ollama/qwen3.5:4b")).toBe("qwen3.5:4b on this PC");
    expect(modelLabel("claude-next")).toBe("claude-next");
    expect(costLabel("claude-opus-5-5", 0.0312)).toBe("$0.031");
    expect(costLabel("ollama/qwen3.5:4b", 0)).toBe("nothing sent");
    expect(gigabytes(3_300_000_000)).toBe("3.3 GB");
  });

  it("says how far it is", () => {
    expect(writingLabel({ part: 0, of: 0 })).toContain("Waiting for its turn");
    expect(writingLabel({ part: 1, of: 1 })).toBe("Writing on this PC...");
    expect(writingLabel({ part: 2, of: 5 })).toBe("Writing on this PC: part 2 of 5...");
    expect(writingLabel({ part: 6, of: 5 })).toBe("Putting it together on this PC...");
  });
});

import { describe, expect, it } from "vitest";

import { backText, stoppedText } from "./engineStops";

const exit = { message: "The engine stopped (exit code: 1).", restarting: false, recording: false };

describe("what the window says when the engine stops", () => {
  it("says why, and that it's being started again", () => {
    expect(stoppedText(exit)).toBe("The engine stopped (exit code: 1).");
    expect(stoppedText({ ...exit, restarting: true })).toBe("Starting it again...");
  });

  it("says what became of a recording", () => {
    expect(stoppedText({ ...exit, recording: true })).toBe(
      "What it had recorded is kept. The engine stopped (exit code: 1).",
    );
    expect(stoppedText({ ...exit, restarting: true, recording: true })).toBe(
      "What it had recorded is kept. Starting it again...",
    );
    expect(backText(true)).toContain("press Record");
    expect(backText(false)).toBe("It wasn't recording.");
  });
});

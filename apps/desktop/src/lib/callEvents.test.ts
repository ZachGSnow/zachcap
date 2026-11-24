import { describe, expect, it } from "vitest";

import { newEvents, recordPath } from "./callEvents";
import type { CallEvent } from "./engine";

function event(id: number, kind: CallEvent["kind"] = "started"): CallEvent {
  return { id, kind, app: "Microsoft Teams", meeting_id: id };
}

describe("what the engine did on its own", () => {
  it("treats what happened before the page loaded as old news", () => {
    const first = newEvents(null, "run-1", [event(4), event(5)]);
    expect(first.fresh).toEqual([]);
    expect(first.cursor).toEqual({ boot: "run-1", id: 5 });
  });

  it("says each new one once, in order, even several between two looks", () => {
    const cursor = { boot: "run-1", id: 5 };
    const next = newEvents(cursor, "run-1", [event(7, "stopped"), event(6, "discarded")]);
    expect(next.fresh.map((e) => e.id)).toEqual([6, 7]);
    expect(next.cursor).toEqual({ boot: "run-1", id: 7 });
    expect(newEvents(next.cursor, "run-1", [event(7, "stopped")]).fresh).toEqual([]);
  });

  it("says everything a restarted engine did, though it numbers from 1 again", () => {
    const next = newEvents({ boot: "run-1", id: 9 }, "run-2", [event(1), event(2, "failed")]);
    expect(next.fresh.map((e) => e.kind)).toEqual(["started", "failed"]);
    expect(next.cursor).toEqual({ boot: "run-2", id: 2 });
  });

  it("asks only for what came after the cursor", () => {
    expect(recordPath(null)).toBe("/api/record");
    expect(recordPath({ boot: "a b", id: 3 })).toBe("/api/record?boot=a%20b&after=3");
  });
});

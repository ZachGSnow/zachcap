import { describe, expect, it } from "vitest";

import type { RelatedMeeting } from "./engine";
import { relatedDay, relatedKey, relatedRows, shownRows, SHOWN_AT_FIRST } from "./related";

const related = (id: number, started_at = "2026-11-20T09:00:00"): RelatedMeeting => ({
  id,
  title: `Call ${id}`,
  started_at,
  score: 32,
  reasons: ["Ticket 1234567", "Contoso", "Dana Lee, Mark Ruiz"],
});

describe("when a meeting's related ones are asked for", () => {
  const meeting = { id: 12 };

  it("is once it's loaded, and again for each time it is", () => {
    expect(relatedKey(undefined, false)).toBeNull();
    expect(relatedKey(meeting, false)).toBe(meeting);
    const reloaded = { ...meeting };
    expect(relatedKey(reloaded, false)).not.toBe(relatedKey(meeting, false));
  });

  it("isn't while it's being processed: its lines are still coming", () => {
    expect(relatedKey(meeting, true)).toBeNull();
    expect(relatedKey(undefined, true)).toBeNull();
  });
});

describe("a related meeting's date", () => {
  const now = new Date("2026-12-24T12:00:00");

  it("is the day, without the year, this year", () => {
    const shown = relatedDay("2026-11-20T09:00:00", now);
    expect(shown).toContain("20");
    expect(shown).not.toContain("2026");
  });

  it("has the year when it's another one: an old call isn't last month's", () => {
    expect(relatedDay("2025-11-20T09:00:00", now)).toContain("2025");
    expect(relatedDay("2027-01-05T09:00:00", now)).toContain("2027");
  });
});

describe("the list", () => {
  it("has a title, a day and the reasons on one line, and the whole of it on hover", () => {
    const now = new Date("2026-12-24T12:00:00");
    const [row] = relatedRows([related(12)], now);
    expect(row.id).toBe(12);
    expect(row.title).toBe("Call 12");
    expect(row.when).toBe(relatedDay("2026-11-20T09:00:00", now));
    expect(row.why).toBe("Ticket 1234567 \u00b7 Contoso \u00b7 Dana Lee, Mark Ruiz");
    expect(row.hint).toBe(`Call 12 (${row.when}): ${row.why}`);
    expect(relatedRows([])).toEqual([]);
  });

  it("shows the best few, then all of them", () => {
    const rows = relatedRows([1, 2, 3, 4, 5].map((id) => related(id)));
    const first = shownRows(rows, false);
    expect(first.rows.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(first.more).toBe(5 - SHOWN_AT_FIRST);
    expect(shownRows(rows, true)).toEqual({ rows, more: 0 });
    expect(shownRows(rows.slice(0, 2), false)).toEqual({ rows: rows.slice(0, 2), more: 0 });
    expect(shownRows([], false)).toEqual({ rows: [], more: 0 });
  });
});

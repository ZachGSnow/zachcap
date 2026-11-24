import { describe, expect, it } from "vitest";

import type { TicketFound } from "./engine";
import { closing, saveShowing } from "./inplace";
import { ticketShown } from "./tickets";

const found: TicketFound[] = ["1924086", "1924087", "7654321", "INC-0042"].map((ticket) => ({
  ticket,
  where: ["transcript"],
  count: 1,
}));

describe("the ticket box", () => {
  it("shows a picked ticket as the meeting's while it's saved, and offers no others", () => {
    expect(ticketShown(null, null, found).offered.map((suggestion) => suggestion.ticket)).toEqual([
      "1924086",
      "1924087",
      "7654321",
    ]);
    const saving = ticketShown(null, "1924086", found);
    expect([saving.ticket, saving.offered]).toEqual(["1924086", []]);
  });

  it("shows what the engine stored once the save is over, refused or not", async () => {
    const shown: (string | null)[] = [];
    await saveShowing("INC-0043", async () => false, (pending) => shown.push(pending));
    expect(shown).toEqual(["INC-0043", null]);
    expect(ticketShown("INC-0042", null, found)).toEqual({ ticket: "INC-0042", start: "INC-0042", offered: [] });
    expect(ticketShown("INC-0042", "", found).offered).toHaveLength(3); // being taken off
  });

  it("leaves focus where a click went, and gives it back after Enter or Escape", () => {
    const { start } = ticketShown("INC-0042", null, found);
    expect(closing("blur", start, "INC-0043")).toEqual({ save: "INC-0043", refocus: false });
    expect(closing("Enter", start, "INC-0043")).toEqual({ save: "INC-0043", refocus: true });
    expect(closing("Escape", start, "INC-0043")).toEqual({ save: null, refocus: true });
  });
});

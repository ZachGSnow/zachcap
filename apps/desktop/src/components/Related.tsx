import { useState } from "react";

import type { RelatedMeeting } from "@/lib/engine";
import { relatedRows, SHOWN_AT_FIRST, shownRows } from "@/lib/related";

/** Other meetings about the same thing as the one shown, the most related first, each with what
 * they share ("Ticket 1234567", "Contoso"): a click opens one. Nothing is shown when there are
 * none. */
export function Related({
  meetings,
  onOpen,
}: {
  meetings: RelatedMeeting[];
  onOpen: (meetingId: number) => void;
}) {
  const [all, setAll] = useState(false);
  const rows = relatedRows(meetings);
  const { rows: shown, more } = shownRows(rows, all);
  if (rows.length === 0) return null;

  return (
    <section aria-label="Related meetings" className="mt-4 flex select-none gap-3 rounded-xl border bg-card px-4 py-2">
      <h3 className="w-16 shrink-0 pt-1.5 text-xs text-muted-foreground">Related</h3>
      <div className="min-w-0 flex-1">
        {shown.map((row) => (
          <button
            key={row.id}
            onClick={() => onOpen(row.id)}
            title={row.hint}
            className="flex w-full items-baseline gap-3 rounded-md px-2 py-1 text-left text-sm transition-colors hover:bg-muted/60"
          >
            <span className="max-w-[45%] shrink-0 truncate font-medium">{row.title}</span>
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{row.why}</span>
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{row.when}</span>
          </button>
        ))}
        {rows.length > SHOWN_AT_FIRST && (
          <button
            onClick={() => setAll(!all)}
            aria-expanded={all}
            className="rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            {all ? "Fewer" : `${more} more`}
          </button>
        )}
      </div>
    </section>
  );
}

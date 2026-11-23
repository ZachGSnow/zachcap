import { EyeOff, Loader2, Minus, Plus } from "lucide-react";
import { useEffect, useState } from "react";

import { REDACTED } from "@/components/Transcript";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Segment } from "@/lib/engine";
import { clock } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A time with tenths of a second. */
function stamp(seconds: number): string {
  const tenths = Math.round(seconds * 10);
  return `${clock(Math.floor(tenths / 10))}.${tenths % 10}`;
}

function Nudge({
  label,
  value,
  onChange,
  min,
  max,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
}) {
  const step = (delta: number) => onChange(Math.round(Math.min(max, Math.max(min, value + delta)) * 100) / 100);
  return (
    <div className="grid gap-1.5">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="flex items-center gap-1">
        <Button size="icon" variant="outline" className="size-8" aria-label={`${label} a second earlier`} onClick={() => step(-1)}>
          <Minus className="size-3.5" />
        </Button>
        <span className="w-20 text-center font-mono text-sm tabular-nums">{stamp(value)}</span>
        <Button size="icon" variant="outline" className="size-8" aria-label={`${label} a second later`} onClick={() => step(1)}>
          <Plus className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** Silence a moment of the recording and remove what was said then: for a password or a
 * code read aloud. Starts at the line it was opened from; each end moves a second at a time. */
export function RedactDialog({
  line,
  segments,
  duration,
  hasSummary,
  audioRemoved,
  onCancel,
  onConfirm,
}: {
  line: Segment | null;
  segments: Segment[];
  duration: number;
  hasSummary: boolean;
  audioRemoved: boolean;
  onCancel: () => void;
  /** Resolves when the redaction finished (or failed: the dialog stays open then). */
  onConfirm: (start: number, end: number) => Promise<boolean>;
}) {
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!line) return;
    // Exactly the line: wider would take the words of the lines next to it too.
    setStart(line.start);
    setEnd(line.end);
    setBusy(false);
  }, [line]);

  const affected = segments.filter((s) => s.start < end && s.end > start);
  const run = async () => {
    setBusy(true);
    if (!(await onConfirm(start, end))) setBusy(false);
  };

  return (
    <Dialog open={line !== null} onOpenChange={(open) => !open && !busy && onCancel()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Redact this moment</DialogTitle>
          <DialogDescription>
            For a password, a code or anything else that shouldn't be kept.{" "}
            {audioRemoved
              ? "The recording was already deleted, so only the transcript changes: "
              : "The recording is silenced on both sides of the call, and "}
            what was said is removed from the transcript and search. This can't be undone.
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-end gap-6">
          <Nudge label="From" value={start} onChange={setStart} min={0} max={Math.max(0, end - 0.1)} />
          <Nudge label="To" value={end} onChange={setEnd} min={start + 0.1} max={duration || end + 60} />
          <span className="pb-2 text-xs text-muted-foreground">{(end - start).toFixed(1)} s</span>
        </div>

        <div className="max-h-48 overflow-auto rounded-lg border">
          {affected.length === 0 ? (
            <p className="px-3 py-2.5 text-xs text-muted-foreground">No lines in this stretch.</p>
          ) : (
            affected.map((s) => {
              const removed = s.start >= start && s.end <= end;
              return (
                <div key={s.id} className="flex items-start gap-2 border-b px-3 py-2 text-xs last:border-b-0">
                  <span className="shrink-0 font-mono tabular-nums text-muted-foreground">{clock(s.start)}</span>
                  <span className="w-24 shrink-0 truncate font-medium">{s.speaker}</span>
                  <span className={cn("min-w-0 flex-1 truncate", s.text === REDACTED && "italic text-muted-foreground")}>
                    {s.text === REDACTED ? "Redacted" : s.text}
                  </span>
                  <span className={cn("shrink-0 rounded px-1.5", removed ? "bg-destructive/10 text-destructive" : "bg-muted")}>
                    {removed ? "removed" : "words removed"}
                  </span>
                </div>
              );
            })
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          A line that reaches into the stretch loses all its words, since ZachCap can't tell which of them fall inside it.
          {hasSummary &&
            " The summary and action items were written before this and may mention it: regenerate them afterwards."}
        </p>

        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={busy || end <= start} onClick={() => void run()}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <EyeOff className="size-4" />}
            Redact {stamp(start)} to {stamp(end)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

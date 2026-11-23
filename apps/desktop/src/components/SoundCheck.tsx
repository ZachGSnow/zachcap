import { AudioWaveform, CircleCheck, CircleX, Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, type SoundCheckResult } from "@/lib/engine";
import { attempt } from "@/lib/feedback";

function Side({
  ok,
  label,
  device,
  peak,
  fix,
}: {
  ok: boolean;
  label: string;
  device: string;
  peak: number | null;
  fix: string;
}) {
  return (
    <li className="flex items-start gap-2">
      {ok ? (
        <CircleCheck className="mt-px size-3.5 shrink-0 text-emerald-600" />
      ) : (
        <CircleX className="mt-px size-3.5 shrink-0 text-destructive" />
      )}
      <span className="min-w-0">
        <span className="font-medium">{label}</span>{" "}
        <span className="text-muted-foreground">
          {ok ? "heard" : "not heard"}
          {peak != null && peak > -180 ? ` (peak ${Math.round(peak)} dB)` : ""}
          {device ? ` on ${device}` : ""}.
        </span>
        {!ok && <span className="block text-muted-foreground">{fix}</span>}
      </span>
    </li>
  );
}

/** Four seconds recorded exactly as a meeting would be, with a short sound halfway, played
 * where the computer's audio is recorded from (a quiet tone on a chosen output device): shows
 * both sides are heard before a call depends on it. Nothing is kept. */
export function SoundCheck({ onDone }: { onDone?: (result: SoundCheckResult) => void }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SoundCheckResult | null>(null);

  const run = async () => {
    setRunning(true);
    setResult(null);
    await attempt("The sound check didn't run", async () => {
      const checked = await api<SoundCheckResult>("/api/soundcheck", { method: "POST" });
      setResult(checked);
      onDone?.(checked);
    });
    setRunning(false);
  };

  return (
    <div className="grid w-full gap-2">
      <Button size="sm" variant="outline" className="justify-self-start" disabled={running} onClick={() => void run()}>
        {running ? <Loader2 className="size-3.5 animate-spin" /> : <AudioWaveform className="size-3.5" />}
        {running ? "Listening: say a few words" : result ? "Check again" : "Check"}
      </Button>
      {result && (
        <ul className="space-y-1 text-xs">
          <Side
            ok={result.mic.heard}
            label="Your microphone"
            device={result.mic.device}
            peak={result.mic.peak_dbfs}
            fix="Say a few words while it listens. Still nothing? The mic may be muted (the mic key) or not the one picked above."
          />
          <Side
            ok={result.system.heard}
            label="Computer audio"
            device={result.system.device}
            peak={result.system.peak_dbfs}
            fix={
              result.played
                ? "The test sound wasn't heard: is Windows muted, or the sound going to a device that's off?"
                : "The test sound didn't play; check the volume and the device picked above, and try again."
            }
          />
          {result.errors.map((error) => (
            <li key={error} className="text-destructive">
              {error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

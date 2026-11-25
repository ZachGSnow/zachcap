import { ChevronRight, Pencil } from "lucide-react";
import { useId, useState } from "react";

import { PlaySample } from "@/components/Transcript";
import { Button } from "@/components/ui/button";
import type { Speaker } from "@/lib/engine";
import { cn } from "@/lib/utils";
import { leftOutLabel, saidLabel, type BriefVoice } from "@/lib/voices";

/** The voices left out of a meeting's speakers, folded up under the ones that are listed: hear one,
 * and name it to list it after all. Nothing is shown when there are none. */
export function BriefVoices({
  meetingId,
  voices,
  playable,
  onName,
}: {
  meetingId: number;
  voices: BriefVoice[];
  /** The recording is still there to play samples from. */
  playable: boolean;
  onName: (voice: Speaker) => void;
}) {
  const [open, setOpen] = useState(false);
  const list = useId();
  const samples = playable && voices.some(({ voice }) => voice.sample_start != null);
  if (voices.length === 0) return null;
  return (
    <section aria-label="Brief voices" className="mt-2 text-xs text-muted-foreground">
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={open ? list : undefined}
        title="Heard only briefly, and ZachCap can't name them (Settings, Leave out brief voices)"
        className="flex select-none items-center gap-1 rounded-md py-1 transition-colors hover:text-foreground"
      >
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        {leftOutLabel(voices.length)}
      </button>
      {open && (
        <div id={list} className="mt-1 divide-y overflow-hidden rounded-xl border">
          {voices.map(({ voice, seconds, share }) => (
            <div key={voice.id} className="flex items-center gap-3 px-3.5 py-2">
              {samples &&
                (voice.sample_start != null ? (
                  <PlaySample meetingId={meetingId} voice={voice} className="size-7" />
                ) : (
                  <span className="size-7 shrink-0" />
                ))}
              <span className="min-w-0 flex-1 truncate">{voice.label}</span>
              <span className="tabular-nums">
                {voice.track === "mic" ? "Mic" : "Call"} {"\u00b7"} {saidLabel({ seconds, share })}
              </span>
              <Button size="icon" variant="ghost" className="size-7" title="Name this voice" onClick={() => onName(voice)}>
                <Pencil className="size-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

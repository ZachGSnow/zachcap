import { Headphones, Pause, Play, RotateCcw, RotateCw, X } from "lucide-react";
import type { RefObject } from "react";

import { Button } from "@/components/ui/button";
import { clock } from "@/lib/format";

const SPEEDS = [1, 1.25, 1.5, 2];

export type PlayerState = { playing: boolean; time: number; duration: number; rate: number };

export function PlayerBar({
  audio,
  state,
  solo,
  trackLabel,
  onClearSolo,
}: {
  audio: RefObject<HTMLAudioElement | null>;
  state: PlayerState;
  solo: string | null;
  trackLabel: string;
  onClearSolo: () => void;
}) {
  const element = () => audio.current;
  const toggle = () => {
    const a = element();
    if (!a) return;
    if (a.paused) void a.play();
    else a.pause();
  };
  const skip = (seconds: number) => {
    const a = element();
    if (a) a.currentTime = Math.max(0, Math.min(a.duration || Infinity, a.currentTime + seconds));
  };
  const cycleSpeed = () => {
    const a = element();
    // The default rate too: switching tracks for solo playback loads a new source,
    // which resets playbackRate to defaultPlaybackRate.
    if (a) a.defaultPlaybackRate = a.playbackRate = SPEEDS[(SPEEDS.indexOf(a.playbackRate) + 1) % SPEEDS.length];
  };

  return (
    <div className="flex select-none items-center gap-2 border-t bg-background/95 px-5 py-2.5 backdrop-blur">
      <Button size="icon" variant="ghost" className="size-8" onClick={() => skip(-10)} title="Back 10 s">
        <RotateCcw className="size-3.5" />
      </Button>
      <Button size="icon" className="size-9 rounded-full" onClick={toggle} title="Play or pause (Space)">
        {state.playing ? <Pause className="size-4" /> : <Play className="size-4" />}
      </Button>
      <Button size="icon" variant="ghost" className="size-8" onClick={() => skip(10)} title="Forward 10 s">
        <RotateCw className="size-3.5" />
      </Button>
      <span className="w-28 text-center font-mono text-xs tabular-nums text-muted-foreground">
        {clock(state.time)} / {clock(state.duration)}
      </span>
      <input
        type="range"
        min={0}
        max={state.duration || 0}
        step={0.1}
        value={state.time}
        onChange={(event) => {
          const a = element();
          if (a) a.currentTime = Number(event.target.value);
        }}
        className="h-1 flex-1 cursor-pointer accent-foreground"
        aria-label="Playback position"
      />
      {solo ? (
        <span className="flex items-center gap-1.5 rounded-full bg-foreground py-1 pr-1 pl-2.5 text-xs text-background">
          <Headphones className="size-3" />
          Only {solo}
          <button onClick={onClearSolo} className="rounded-full p-0.5 hover:bg-background/20" title="Play everyone">
            <X className="size-3" />
          </button>
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">{trackLabel}</span>
      )}
      <Button size="sm" variant="ghost" className="w-12 font-mono text-xs" onClick={cycleSpeed} title="Playback speed">
        {state.rate}x
      </Button>
    </div>
  );
}

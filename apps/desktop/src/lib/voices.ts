import type { Segment, Speaker } from "@/lib/engine";
import { minutes } from "@/lib/format";

/** A voice left out of a meeting's speakers, since it was heard only briefly and nobody is known
 * to be it, with how long it talked and its share of all the talk (0 to 1). */
export type BriefVoice = { voice: Speaker; seconds: number; share: number };

/** The voices listed as a meeting's speakers: not those left out, nor any that says nothing. */
export function listedVoices(speakers: Speaker[]): Speaker[] {
  return speakers.filter((s) => s.status !== "left_out" && (s.status !== "minor" || (s.speech_seconds ?? 0) > 0));
}

/** The voices left out, the most talkative first. */
export function leftOut(
  speakers: Speaker[],
  lines: Pick<Segment, "meeting_speaker_id" | "start" | "end">[],
): BriefVoice[] {
  const said = new Map<number | null, number>();
  let total = 0;
  for (const { meeting_speaker_id: voice, start, end } of lines) {
    said.set(voice, (said.get(voice) ?? 0) + end - start);
    total += end - start;
  }
  return speakers
    .filter((voice) => voice.status === "left_out")
    .map((voice) => {
      const seconds = said.get(voice.id) ?? 0;
      return { voice, seconds, share: total ? seconds / total : 0 };
    })
    .sort((a, b) => b.seconds - a.seconds);
}

/** How many voices are left out. */
export function leftOutLabel(count: number): string {
  return count === 1 ? "1 brief voice left out" : `${count} brief voices left out`;
}

/** How much a left-out voice said. Its lines may all have gone to others: as pieces of their
 * sentences, or moved by you. */
export function saidLabel({ seconds, share }: Pick<BriefVoice, "seconds" | "share">): string {
  if (!seconds) return "no lines left";
  const percent = Math.round(share * 100);
  return `${seconds < 1 ? "under 1 s" : minutes(seconds)} \u00b7 ${percent < 1 ? "under 1%" : `${percent}%`} of the talk`;
}

/** What to call a voice: one that's left out by its label, as all of those are named Unknown. */
export function voiceName(voice: Pick<Speaker, "status" | "label" | "display_name">): string {
  return voice.status === "left_out" ? voice.label : voice.display_name;
}

/** Whether a line is this listed speaker's. A left-out voice's has the name of the lines with no
 * voice, which are listed when there are many, but it isn't one of them. */
export function isBy(line: Pick<Segment, "speaker" | "speaker_status">, speaker: string): boolean {
  return line.speaker === speaker && line.speaker_status !== "left_out";
}

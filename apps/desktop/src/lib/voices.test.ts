import { describe, expect, it } from "vitest";

import type { Segment, Speaker, SpeakerStatus } from "./engine";
import { isBy, leftOut, leftOutLabel, listedVoices, saidLabel, voiceName } from "./voices";

const voice = (id: number, label: string, status: SpeakerStatus, speech_seconds = 5): Speaker => ({
  id,
  track: "system",
  label,
  person_id: null,
  person_name: null,
  display_name: status === "left_out" ? "Unknown" : label,
  status,
  match_score: null,
  speech_seconds,
  sample_start: 1,
  sample_end: 2,
});
const line = (meeting_speaker_id: number | null, start: number, end: number): Segment => ({
  id: start,
  meeting_speaker_id,
  track: "system",
  start,
  end,
  text: "Hello",
  speaker: "Unknown",
  speaker_status: null,
});

const dana = voice(1, "Dana", "auto");
const speakers = [
  dana,
  voice(2, "Speaker 1", "left_out"),
  voice(3, "Speaker 2", "unknown"),
  voice(4, "Speaker 3", "minor"),
  voice(5, "Speaker 4", "minor", 0),
  voice(6, "Speaker 5", "left_out"),
  voice(7, "Speaker 6", "left_out"),
];

describe("the voices of a meeting", () => {
  it("are listed as its speakers, but for those left out and those that say nothing", () => {
    expect(listedVoices(speakers).map((s) => s.label)).toEqual(["Dana", "Speaker 2", "Speaker 3"]);
  });

  it("that are left out have their length and share of the talk, the most talkative first", () => {
    // The lines with no voice are part of the talk: 99 seconds in all.
    const lines = [line(1, 0, 80), line(2, 80, 80.5), line(6, 81, 84), line(2, 85, 85.5), line(null, 86, 101)];
    const out = leftOut(speakers, lines);
    expect(out.map((v) => [v.voice.label, v.seconds])).toEqual([
      ["Speaker 5", 3],
      ["Speaker 1", 1],
      ["Speaker 6", 0],
    ]);
    expect(out[0].share).toBeCloseTo(3 / 99);
    expect(leftOut([dana], lines)).toEqual([]);
    expect(leftOut(speakers, []).map((v) => v.share)).toEqual([0, 0, 0]);
  });

  it("that are left out are counted, and say how much they said", () => {
    expect(leftOutLabel(1)).toBe("1 brief voice left out");
    expect(leftOutLabel(3)).toBe("3 brief voices left out");
    expect(saidLabel({ seconds: 4.2, share: 0.0312 })).toBe("4 s \u00b7 3% of the talk");
    expect(saidLabel({ seconds: 0.3, share: 0.0004 })).toBe("under 1 s \u00b7 under 1% of the talk");
    expect(saidLabel({ seconds: 95, share: 0.049 })).toBe("2 min \u00b7 5% of the talk");
    expect(saidLabel({ seconds: 0, share: 0 })).toBe("no lines left");
  });

  it("that are left out go by their labels: they're all named Unknown", () => {
    expect(voiceName(speakers[1])).toBe("Speaker 1");
    const suggested = { status: "suggested", label: "Speaker 2", display_name: "Speaker 2 (Dana?)" } as const;
    expect(voiceName(suggested)).toBe("Speaker 2 (Dana?)");
  });
});

describe("a listed speaker's lines", () => {
  it("are the ones with that name, but not a left-out voice's", () => {
    expect(isBy({ speaker: "Dana", speaker_status: "auto" }, "Dana")).toBe(true);
    expect(isBy({ speaker: "Dana", speaker_status: "auto" }, "Unknown")).toBe(false);
    expect(isBy({ speaker: "Unknown", speaker_status: null }, "Unknown")).toBe(true);
    expect(isBy({ speaker: "Unknown", speaker_status: "left_out" }, "Unknown")).toBe(false);
  });
});

"""Build a synthetic two-track meeting from a JSON script using Windows TTS voices.

Each line names a speaker, a voice and a track ("mic" or "system"). Lines are
spoken one after another with short random gaps, and each track only holds its
own lines, like a real call: you on the mic, everyone else on the speakers.

Writes mic.wav, system.wav and reference.json (who said what, and when).

    uv run python scripts/make_meeting.py SCRIPT.json OUT_DIR
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

TTS_SCRIPT = Path(__file__).with_name("tts.ps1")


def trim_silence(clip: np.ndarray, rate: int, threshold_db: float = -45.0) -> np.ndarray:
    """Drop the leading and trailing silence TTS adds around each line."""
    hop = rate // 100
    frames = len(clip) // hop
    if frames == 0:
        return clip
    rms = np.sqrt((clip[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    loud = np.flatnonzero(20 * np.log10(rms + 1e-9) > threshold_db)
    if loud.size == 0:
        return clip
    return clip[max(loud[0] - 2, 0) * hop : min(loud[-1] + 3, frames) * hop]


def speak(lines: list[dict], rate: int) -> list[np.ndarray]:
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "lines.json"
        spec.write_text(
            json.dumps([{"voice": line["voice"], "text": line["text"]} for line in lines]),
            encoding="utf-8",
        )
        subprocess.run(
            [
                # PowerShell 7: Windows PowerShell 5.1 only sees the two "Desktop" voices
                "pwsh",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(TTS_SCRIPT),
                "-ScriptJson",
                str(spec),
                "-OutDir",
                tmp,
                "-Rate",
                str(rate),
            ],
            check=True,
        )
        return [sf.read(Path(tmp) / f"{i:03d}.wav", dtype="float32")[0] for i in range(len(lines))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("script", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    meeting = json.loads(args.script.read_text(encoding="utf-8"))
    lines = meeting["lines"]
    clips = [trim_silence(c, args.rate) for c in speak(lines, args.rate)]

    rng = random.Random(args.seed)
    t = 1.0
    segments, placements = [], []
    for line, clip in zip(lines, clips, strict=True):
        start, end = t, t + len(clip) / args.rate
        placements.append((line["track"], round(start * args.rate), clip))
        segments.append({**line, "start": round(start, 3), "end": round(end, 3)})
        t = end + rng.uniform(0.25, 0.8)

    total = round((t + 1.0) * args.rate)
    tracks = {"mic": np.zeros(total, np.float32), "system": np.zeros(total, np.float32)}
    for track, offset, clip in placements:
        tracks[track][offset : offset + len(clip)] += 0.7 * clip

    args.out.mkdir(parents=True, exist_ok=True)
    for name, audio in tracks.items():
        sf.write(args.out / f"{name}.wav", audio, args.rate, subtype="PCM_16")
    reference = {
        "title": meeting.get("title"),
        "rate": args.rate,
        "duration": round(total / args.rate, 3),
        "segments": segments,
    }
    (args.out / "reference.json").write_text(json.dumps(reference, indent=2), encoding="utf-8")
    print(f"{len(segments)} lines, {total / args.rate:.1f} s -> {args.out}")


if __name__ == "__main__":
    main()

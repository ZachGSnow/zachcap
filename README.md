# ZachCap

**A local meeting recorder for Windows that knows who said what: it records any call, transcribes it on your own GPU, recognizes people by voice, and turns the meeting into decisions, action items and commitments.**

[![CI](https://github.com/ZachGSnow/zachcap/actions/workflows/ci.yml/badge.svg)](https://github.com/ZachGSnow/zachcap/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

![A meeting in ZachCap: speaker lanes, a Claude summary and action items](docs/images/meeting.jpg)

## Why

Most AI note takers join your call as a bot, read your calendar, or need an admin to approve an app in your Microsoft 365 tenant. Plenty of people can't do any of that, especially in meetings hosted by other companies. ZachCap needs none of it: it records your microphone and your computer's audio on your own machine, works with Teams, softphones or anything else that plays through your speakers, and keeps the audio on that machine.

Teams mixes every remote voice into one stream, so ZachCap separates speakers itself and learns their voices: name someone once and they are labeled automatically in later meetings.

## Features

- **Record any call** with one button or **Ctrl+Alt+R** from anywhere; the app lives in the tray and can start with Windows. It notices when Teams or another app starts using your microphone and offers to record. The other side is recorded from **every speaker and headset at once**, so a call on a headset that isn't the Windows default, or a headset plugged in mid-call, isn't lost. While it records it warns about a muted mic, a call with no call audio and a nearly full disk, keeps the PC from going to sleep, and when the call ends it **counts down and stops** (Keep recording cancels).
- **Notes and bookmarks while you record**: type a note in the recording bar, or press **Ctrl+Alt+B** to bookmark the moment. They appear on the meeting's timeline, and summaries make sure to cover them. **Pause** privately: nothing is recorded until you resume, and the tracks stay in sync.
- **Nothing lost to a crash**: the meeting is saved from its first second. If ZachCap is quit mid-call, crashes, or Windows restarts, the audio recorded so far is recovered and processed at the next start.
- **Import recordings** you already have (a Teams .mp4, .m4a, .mp3, .wav...), with an optional time range.
- **Transcription on your GPU** with Whisper (`large-v3-turbo`): about 2-3 minutes per hour of audio on a 4 GB laptop GPU. **Words to know** (names, products, jargon, and what Whisper mishears them as) are spelled your way, and so are the names of people you've identified.
- **Speaker separation and voice recognition**: a "Who's this?" card plays a short sample of each unknown voice; name them once and ZachCap recognizes them next time, including you on your own mic and people in the room with you.
- **Speaker lanes**: a timeline of who talked when, with talk-time stats, and **solo playback** that plays one person only, from their own side of the call. Listening back can **skip silences**, with video-player keys (Space or K, J and L, arrows). **Conversation stats** show each person's pace, longest stretch, questions asked, and how often they talked over the other side.
- **Summaries with names** from Claude (Opus 5.5 by default): overview, decisions, action items marked as commitments or assignments with due dates, who said what, open questions and topics. Every item links to the moment it was said. Pick the **kind of meeting** (1:1, standup, customer call, project status, incident or troubleshooting, interview) to set what the summary focuses on; **edit them or add your own** in Settings (a vendor call, a client's quarterly review).
- **Write from a meeting** in one click: a follow-up email, a status update, or notes for a service ticket, ready to copy. Edit these or add your own documents (a client's ticket format, a time entry).
- **Action item tracker** across all meetings, with Open/Done, "Mine", and commitments vs assigned tasks.
- **Ask across all your meetings**: "What did I commit to this week?", "What's still open with Dana?". Claude searches your transcripts, summaries and action items with read-only tools and links every fact to the moment it was said. **Brief me** on a person prepares you for your next meeting with them. Each meeting also has its own Ask tab.
- **History and search**: every meeting, searchable by anything anyone said.
- **Your meetings in Claude Code or the Claude app**: a read-only MCP server (`zachcap mcp`) lets Claude search your transcripts and read summaries, action items and notes. Settings has the command and config to copy.
- **Export** a meeting as Markdown (summary, action items, your notes, transcript), plain text or subtitles (.srt). An optional **export folder** keeps a Markdown copy of every meeting up to date, for Obsidian or any notes folder.
- **Fix anything**: rename people (renaming to an existing name merges them), move one line to another speaker, forget a voice, rename or delete a meeting.

| | |
|---|---|
| ![Home](docs/images/home.jpg) | ![Speakers](docs/images/speakers.jpg) |
| ![Action items](docs/images/action-items.jpg) | |

## Quick start

Requirements: Windows 10 or 11, an NVIDIA GPU with 4 GB or more (it falls back to the CPU, slowly), [uv](https://docs.astral.sh/uv/), Node.js LTS, and Rust with the Visual Studio C++ build tools.

```powershell
git clone https://github.com/ZachGSnow/zachcap.git
cd zachcap\engine
uv sync                      # Python 3.12 and the GPU speech stack (a few GB)

cd ..\apps\desktop
npm install
npm run tauri dev            # starts the local engine and opens the app
```

Then open **Settings** and add:

1. A free [Hugging Face](https://huggingface.co) token, after accepting the terms of [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1). Without it ZachCap still transcribes, labeling the two sides "You" and "Call audio".
2. An [Anthropic API key](https://platform.claude.com) for summaries and questions (optional; about $0.09 per hour-long meeting with Opus 5.5).

Keys are stored in Windows Credential Manager. The engine also has a command line (`uv run zachcap --help`): `record`, `import`, `show`, `name`, `confirm`, `search`, `keys`, `mcp`.

For everyday use, build the app once and start it from a shortcut:

```powershell
cd apps\desktop
npm run tauri build          # creates src-tauri\target\release\zachcap.exe
```

The exe runs the engine from this clone, so keep the folder where it is (there is no installer yet). Turn on **Start with Windows** in Settings to have ZachCap waiting in the tray, and its log is `engine.log` in the data folder.

## Configuration

Everything is set in the app under **Settings**:

| Setting | What it does | Default |
|---|---|---|
| Your name | How your own lines appear in transcripts and summaries | You |
| Microphone | Your voice; pick the one your meeting app uses | Windows default |
| Computer audio from | Every speaker and headset at once, the Windows default only, or one device (or a virtual cable) | Every speaker and headset |
| Language | Whisper's language, or automatic detection | English |
| Words to know | Terms to spell right, one per line; `Term: misheard, misheard` adds corrections | empty |
| Claude model | Opus 5.5, Sonnet 5.5 or Fable 5.1 | Opus 5.5 |
| Usual kind of meeting | What summaries focus on unless a meeting says otherwise | General |
| Kinds of meeting, one-click documents | Edit the built-in ones (and reset them), or add your own with your own instructions | As shipped |
| API keys | Hugging Face token and Anthropic key | not set |
| Start with Windows | Opens ZachCap in the tray when you sign in (release builds) | off |
| Stop when the call ends | Count down and stop when the call app lets go of the mic and it goes quiet, or after ten quiet minutes | on |
| Theme | Light, dark, or the same as Windows | Same as Windows |
| Export folder | Keeps a Markdown copy of every meeting in a folder you choose, one file per meeting, named with its id (M12) | off |

Environment variables for development: `ZACHCAP_HOME` (data folder, default `%LOCALAPPDATA%\ZachCap`), `ZACHCAP_ENGINE_DIR` (engine location for the desktop app).

## How it works

```
mic + loopback of every output device (WASAPI, mixed by packet time)
  -> two tracks, written as crash-safe FLAC chunks
  -> Whisper large-v3-turbo on the GPU: timed words per track
  -> pyannote community-1: who spoke when, one voice embedding per speaker
  -> words joined to speakers; mic lines that echo the call are dropped
  -> embeddings matched against saved voiceprints (cosine; 0.75 auto, 0.60 suggest)
  -> SQLite with full-text search
  -> Claude: summary, action items, commitments (transcript text only)
```

The desktop app is Tauri 2 (React, TypeScript, Tailwind, shadcn/ui). It starts the Python engine on 127.0.0.1 with a random per-launch token, and the engine exits if the app goes away. See [docs/PLAN.md](docs/PLAN.md) for the design, decisions and measurements.

## Your data and privacy

- **What is stored:** recordings (FLAC), transcripts, summaries, action items, people and voiceprints (voice embeddings plus the speaker's sample times), in `%LOCALAPPDATA%\ZachCap`. Files and the SQLite database are not encrypted beyond what Windows provides (use BitLocker).
- **What leaves the machine:** only when you ask for a summary or ask a question, the transcript text it needs goes to Anthropic's API with your key (for a question across meetings, the lines, summaries and action items Claude looks up). If you add the MCP server to a Claude app, what it looks up goes to that app. Audio and voiceprints never leave. Model files are downloaded once from Hugging Face. pyannote's usage telemetry is switched off.
- **Deleting:** deleting a meeting removes its audio, transcript, summary, action items and the voice samples learned from it. "Forget voice" removes a person's voiceprint. **Keep recordings** (Settings) deletes audio after 7, 30, 90 or 365 days and keeps the transcript, summary and notes. Deleting is a normal file delete, not a secure erase.
- **Masking:** an option replaces emails, phone numbers and card numbers with placeholders in everything sent to Claude, including through the MCP server.
- **Consent:** voiceprints count as biometric data in some places (for example Illinois), and recording laws differ; some require everyone's consent. Tell people you are recording (the recording bar copies a heads-up for the meeting chat) and follow your organization's policy.

## Testing

```powershell
cd engine
uv run pytest -q             # 130 tests: storage, matching, audio, capture and mixing, the recording watch, crash recovery, API, summaries and templates, Ask
cd ..\apps\desktop
npx tsc --noEmit; npx vite build
```

Verified on a Windows 11 laptop with an RTX A500 (4 GB), November 2026:

| What | Result |
|---|---|
| Mic + loopback capture | Works; continuous loopback, no gaps in silence |
| Whisper large-v3-turbo, batched | 29.7x real time (2 min per meeting hour), 2.3 GB GPU |
| Speaker separation (pyannote) | 37-minute real Teams recording in about 4 minutes after a fix for a cuDNN workspace spill on 4 GB GPUs |
| Voice recognition across meetings | On that recording split into halves: the returning speaker was recognized automatically (0.96); same person scored 0.88-0.96, different people 0.07-0.52 |
| Claude summaries, Ask | Opus 5.5, Sonnet 5.5 and Fable 5.1 against a synthetic meeting |

Not verified yet: a live two-track recording of a real Teams call (capture was tested with played audio), a second output device (a headset plugged in mid-call; the mixing is unit-tested), call detection with a real call (tested against a simulated registry), and echo handling on hardware without built-in echo cancellation. Test audio in the repo is synthetic (Windows text-to-speech, see `engine/scripts/make_meeting.py`); no real recordings are committed.

## Roadmap

Researched and planned in [docs/PLAN.md](docs/PLAN.md#13-next-from-competitor-research-2026-11-23):

- Record every speaker and headset, so a call on a non-default device or a mid-call switch isn't lost
- Capture health warnings, keeping the PC awake while recording, and a smart stop when the call ends
- Your own summary templates and one-click documents; redact a moment (a password read aloud); fix a line's words
- A daily and weekly digest with draft time entries; markers where your name was said; a watch folder for recordings
- Later: push-to-talk dictation for ticket notes, shared-screen snapshots with on-device text recognition, live transcript, signed installer

## License

[MIT](LICENSE) © Zach Snow

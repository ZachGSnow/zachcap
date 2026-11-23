# ZachCap: local meeting recorder that knows who's talking

Status: v0.1 built and tested (see section 11) | started 2026-11-22 | Repo: `ZachGSnow/zachcap` (MIT)

## 1. Goals and non-goals

**Goals**
- Record any call on this PC (Teams, a softphone, anything that plays through the speakers) with **no meeting bot, no calendar, no M365 app registration**.
- Transcribe **on the laptop's own GPU** with Whisper.
- Split the audio into speakers. Name each person once, and **voiceprints stored on the laptop** recognize them in later meetings.
- Summaries, decisions and action items with real names, from **Claude** (Opus 5.5 by default) with your own API key. Only text is sent, never audio.
- A searchable history of every meeting.
- A modern shadcn/Tailscale-style UI.

**Non-goals (v1):** cloud sync, accounts, multiple users, calendar or Graph integration (ever), dictation, live transcription, macOS or Linux.

## 2. Decisions (2026-11-22)

| Topic | Decision |
|---|---|
| Name / repo | ZachCap, `ZachGSnow/zachcap` |
| License | Public, MIT |
| Desktop shell | Tauri 2 (Rust + Visual Studio Build Tools installed) |
| Call setups to support | Headset, laptop or external speakers, and in-room meetings with other people |
| Apps | Teams, plus softphones and anything else that plays through the speakers |
| Transcription timing | After the meeting (no live transcript in v1) |

## 3. Facts that shape the design

- **Test laptop:** Intel Core i7-1360P, 16 GB RAM, NVIDIA RTX A500 Laptop GPU with 4 GB (CUDA compute 8.6), Windows 11.
- **Teams mixes all remote voices into one stream.** That leaves two real channels: the mic and the computer audio. Separate "channels" per person have to be rebuilt by diarization (working out who spoke when).
- **Claude only reads text,** so transcription has to happen locally.
- **Everyone is identified by voiceprint, including you.** That one approach covers all three call setups:
  - **Headset:** the mic track is only you.
  - **Speakers:** the mic can hear remote voices. This laptop's audio chip appears to cancel that echo (see section 9), and text de-duplication is the safety net.
  - **In a room:** the mic track has several people, so it gets diarized too. Your own voiceprint picks out "You".

## 4. Architecture

```mermaid
flowchart LR
  subgraph APP["Desktop app - Tauri 2"]
    UI["React + shadcn/ui<br/>WebView2"]
    SHELL["Rust core<br/>tray, hotkeys, pill window"]
  end
  subgraph ENG["Engine - Python 3.12 sidecar on 127.0.0.1"]
    API["FastAPI + WebSocket"]
    CAP["Capture<br/>WASAPI mic + loopback"]
    PIPE["Pipeline<br/>Whisper, pyannote, voiceprints"]
    LLM["Claude client"]
    DB[("SQLite + FTS5")]
    FILES[("Audio chunks<br/>LOCALAPPDATA")]
  end
  CLAUDE[("Anthropic API<br/>Claude Opus 5.5")]
  UI <--> API
  SHELL -- starts --> API
  API --> CAP --> FILES
  FILES --> PIPE --> DB
  PIPE --> LLM
  LLM -- "transcript text only" --> CLAUDE
  LLM --> DB
```

| Layer | Choice | Why | Alternative |
|---|---|---|---|
| Desktop shell | **Tauri 2** | Small and light on RAM next to Teams. Has plugins for tray, global hotkey, autostart, updater and single-instance, plus Mica on Windows 11. | Electron: same UI, heavier |
| UI | React + TypeScript + Vite + Tailwind v4 + **shadcn/ui**, lucide, cmdk, sonner, wavesurfer.js | The look you want | none |
| Engine | **Python 3.12** (uv), FastAPI | The best speech libraries are Python | Rust + ONNX (sherpa-onnx, whisper.cpp) for a small installer later |
| Capture | **PyAudioWPatch** (WASAPI mic + loopback). In v0.3, Rust process loopback to capture only the meeting app. | Proven in Phase 0 on this laptop | Virtual audio cable: rejected because of echo and drivers |
| Speech-to-text | **faster-whisper `large-v3-turbo`** (int8_float16, silence detection, word timestamps) | Fits in 4 GB and is accurate | NVIDIA Parakeet TDT 0.6B later |
| Diarization | **pyannote `speaker-diarization-community-1`** | Best open model; also returns per-speaker embeddings | sherpa-onnx |
| Voiceprints | Same embedding model, cosine matching, stores both the vector and a short clip | See section 4.3 | none |
| AI | **Claude Opus 5.5** for summaries by default; Sonnet 5.5 or Fable 5.1 selectable | Caught more implicit commitments in testing | Haiku 5.5 for titles and tags later |
| Storage | SQLite (WAL) + FTS5; FLAC while working, Opus to archive | One file, searchable | none |
| Secrets | Windows Credential Manager (`keyring`), with environment variables as a fallback | No plaintext keys | none |

`torch` is pinned to a CUDA 12.x wheel (cu128) because faster-whisper's CTranslate2 loads the cuBLAS 12 and cuDNN 9 DLLs that ship inside it.

### 4.1 Processing pipeline (runs when you press Stop)

1. Finish the recording chunks into `mic.flac` and `system.flac`, plus 16 kHz mono working copies.
2. Transcribe both tracks with Whisper (silence detection, word timestamps).
3. Diarize both tracks: the computer-audio track always, and the mic track when it holds more than one voice.
4. Assign each word to a speaker; flag overlapping speech.
5. Compute an embedding for each speaker, match it against the voiceprint library (you included), and auto-label, suggest a name, or mark it Unknown.
6. Merge both tracks into one timeline. Drop mic lines that duplicate the computer audio (echo).
7. Send the transcript to Claude and get back structured JSON: summary, decisions, action items (owner and due date), open questions, topics with timestamps.
8. Index it for search, then notify: "Ready: 2 speakers need names."

### 4.2 Data model (SQLite)

- `meetings`: id, title, source_app, started_at, ended_at, status, audio paths, notes
- `segments`: id, meeting_id, track, start, end, text, meeting_speaker_id, confidence (FTS5 index on text)
- `meeting_speakers`: id, meeting_id, track, label, person_id, match_score, centroid embedding, status
- `people`: id, name, org, color, notes, is_me
- `voice_samples`: id, person_id, meeting_id, start, end, clip_path, embedding, model_id, quality
- `summaries`: meeting_id, template, model, json, tokens, cost
- `action_items`: id, meeting_id, owner person_id, text, due, status
- `bookmarks`: meeting_id, t, note

Data folder: `%LOCALAPPDATA%\ZachCap\`, outside OneDrive.

### 4.3 Voiceprints: how it learns

- **Your voice first:** first-run onboarding records about 20 seconds of you reading a paragraph. That gives the "You" voiceprint.
- **Matching:** each meeting speaker's average embedding is compared with each person's saved samples (mean of the top 3 cosine similarities). Each person can match only one speaker per meeting. Two strong matches to the same person become a "merge?" suggestion.
- **Thresholds:** above a high threshold, auto-label and show the confidence. Between the high and low thresholds, suggest the name. Below the low threshold, mark it Unknown. Tune them from your corrections.
- **Learning:** each confirmation saves 2-5 clean segments (2 seconds or longer, no overlapping speech). Keep about 30 varied samples per person and drop the worst. "Not this person" is stored as a negative.
- **Backfill:** naming someone can label them in older meetings too, because per-meeting speaker embeddings are kept.
- **Model changes:** clips are kept, so voiceprints can be recomputed if the embedding model changes.
- **Deleting a person** removes their clips, vectors and samples.
- **Expectations:** regular colleagues should be recognized after 2-3 tagged meetings. Recognition is weaker for dial-ins, cross-talk and shared room mics.

### 4.4 Engineering gotchas

- **Silent loopback:** WASAPI loopback can stop delivering data when nothing is playing. On this laptop it didn't (see section 9), but a silent keepalive output stream costs nothing, so it stays.
- **Clock drift:** the mic and output devices drift apart. Timestamp every buffer and align when merging.
- **Echo on speakers:** handled by the audio chip on this laptop, with text de-duplication as a fallback. Software echo cancellation only if it's ever needed.
- **Device changes mid-meeting** (headset plugged in): reopen the streams without losing the recording.
- **Crash safety:** record in 1-5 minute chunks and recover on the next launch.
- **4 GB of GPU memory:** run the models one at a time, unload between stages, CPU fallback.
- **Local API security:** bind to 127.0.0.1 on a random port, require a per-launch token, and check Host/Origin headers.
- **The mic is a 4-channel array with identical channels** (already processed by the audio chip): downmix to mono.

## 5. Features

Legend: **MVP** = day 1 | **0.2** = next 1-2 weeks | **0.3** = after that | **Later**

### Recording
- **MVP:** Start/stop in the app; mic picker (defaults to the Windows default communications device); loopback of the default output; crash-safe two-track recording.
- **0.2:** Tray icon with a native menu like Tailscale's; global hotkey (`Ctrl+Alt+R`); floating **recording pill** that's always on top (timer, You/Meeting level meters, bookmark, stop); start with Windows.
- **0.2:** Notes pad and bookmarks during the meeting, which feed the summary.
- **0.2:** Consent helper: copy "I'm recording this for notes" for the chat, or a one-line script to say on a phone call.
- **0.2:** Meeting title from the Teams window title, with an AI title as fallback.
- **0.2:** Import a recording (mp3/mp4/wav).
- **0.3:** Call detection: watch Windows' microphone-in-use status (Teams, softphones), then show a "Call started, record?" toast. It never records silently by default.
- **0.3:** Capture one app only (process loopback): pick Teams or the softphone, and Spotify and notification sounds are left out.

### Transcript and speakers
- **MVP:** Transcript with names, colors and timestamps; click any line to play from there.
- **MVP:** **"Who's this?"** cards for unknown speakers: a 5-second sample, suggested names, or type a new name. Saving creates the voiceprint.
- **MVP:** Known voices (you included) are labeled automatically; in-room voices are diarized from the mic track.
- **0.2:** Merge speakers, reassign a single line, split a speaker, edit text inline.
- **0.2:** **Speaker lanes** (a timeline of who spoke when) with **solo** to play only one person.
- **0.2:** Talk-time stats; backfill names into older meetings; People page.

### AI (Claude)
- **MVP:** Summary: overview, decisions, action items (owner and due date), open questions.
- **0.2:** Templates (client call, 1:1, standup, internal) and custom instructions.
- **0.2:** **Ask this meeting** (prompt-cached).
- **0.3:** **Ask across meetings**; action-item tracker across meetings.
- **Cost display:** per-call estimate and a monthly total (about $0.05 per one-hour summary).

### History and search
- **MVP:** Meeting list grouped by Today / This week / Earlier, with status chips (Recording, Processing, Needs names, Ready).
- **0.2:** Full-text search with filters for person, date and tag; `Ctrl+K` palette; export to Markdown, DOCX, JSON or SRT/VTT.

### Privacy and settings
- **MVP:** API keys in Windows Credential Manager.
- **0.2:** Retention (delete audio after N days, keep text); deleting a meeting removes everything; a "local only" switch.
- **Later:** Encrypt the database at rest; signed installer and auto-updates.

## 6. Appearance

The shadcn/Tailscale look carries over. Tauri draws the UI with WebView2 (the Edge engine built into Windows 11), so shadcn components look exactly as they do on the web. Native touches (tray, Mica, custom title bar) make it feel like a Windows app. The app lives in the tray like Tailscale, and the window is for reviewing meetings.

- Neutral zinc palette, light/dark following the Windows setting, one accent color.
- Inter for the UI and Geist Mono for timestamps; 13-14 px text; tabular numbers.
- 1 px hairline borders, 8 px radius, shadows only on popovers and dialogs.
- lucide icons, sonner toasts, skeleton loaders. Keyboard-first: `Ctrl+K`, `J`/`K`, `Space`.
- Speaker colors: an accessible 8-color palette, always shown next to the name.
- Custom title bar, with Mica behind the sidebar on Windows 11.

**Screens:** tray menu | recording pill | Meetings | Meeting detail | People | Search | Settings (Audio, Transcription, Speakers, AI, Storage & privacy, Shortcuts, About)

```
+------------------------------------------------------------------------+
| ZachCap                                                       _  []  X |
+---------------+--------------------------------------------------------+
| Meetings      | Weekly sync - Contoso             Fri Nov 21 - 47 min  |
| People        | (DL) (MR) (ZS) +2      Summary  [Transcript]  Speakers |
| Search Ctrl+K | +----------------------------------------------------+ |
|               | | 2 speakers need names                              | |
|               | | > Speaker 3   "...so cutover is Friday night"      | |
|               | |   [Dana Lee 87%]  [Mark Ruiz]  [+ New person]      | |
|               | +----------------------------------------------------+ |
|               | 04:12  o Dana Lee   We moved the firewall rules...     |
|               | 04:31  o You        OK - and the VPN profile?          |
| Settings      | 04:40  o Speaker 3  So cutover is Friday night...      |
|               +--------------------------------------------------------+
|               | > 04:12 / 47:03   ..::||||::..::|||::..::||:.   1.0x   |
|               | Dana       ==    ===        =            [solo]        |
|               | You           ==      ==        ==       [solo]        |
|               | Speaker 3          ====           ==     [solo]        |
+---------------+--------------------------------------------------------+
```

## 7. Repo

- Changes go through short-lived branches and pull requests; `main` stays releasable and CI must pass.
- **Never commit** recordings, transcripts, voiceprints, `.env` or databases. Test audio is synthetic, generated with Windows TTS voices (`engine/scripts/make_meeting.py`).
- CI: GitHub Actions on `windows-latest`: engine tests on CPU, plus UI lint, typecheck and build.

```
zachcap/
  apps/desktop/          Tauri 2 + React + shadcn
  engine/                Python 3.12 package (uv)
    src/zachcap/         capture, asr, diarize, voiceprint, summarize, store, api
    scripts/             Phase 0 probes, TTS fixture generator
    tests/
  docs/
  .github/workflows/
```

## 8. Build plan

### Day 1

| Block | Goal | Done when |
|---|---|---|
| 0. Setup + capture test | Install the tools; record mic + computer audio; time Whisper on the A500 | Two aligned tracks and a minutes-per-hour number |
| 1. Engine pipeline | Whisper + diarization + merge + SQLite; `zachcap process` | Transcript shows You / Speaker 1..n |
| 2. Voiceprints | Enroll you; naming a speaker saves a voiceprint; the next meeting matches automatically | A second meeting auto-labels a person you tagged |
| 3. Desktop UI | Tauri + shadcn: meeting list, meeting view, transcript with playback, "Who's this?" cards, record button | The whole loop works in the app |
| 4. Claude + publish | Summary tab; repo pushed | A summary with real names; the repo on GitHub |

### After day 1

- **0.2:** tray, hotkey, pill, notes, speaker editing, lanes and solo, People page, search and palette, exports, templates, retention, backfill.
- **0.3:** call detection, one-app capture, ask across meetings, action-item tracker.
- **Later:** live transcript, Parakeet option, Rust/ONNX engine, database encryption, signed installer.

**Validation:** use it for 3-4 weeks and track the share of speakers auto-labeled correctly, processing minutes per meeting hour, and corrections per meeting.

## 9. Phase 0 findings (2026-11-22)

| Check | Result |
|---|---|
| Loopback capture on the test laptop | **Works.** Realtek speakers, 48 kHz stereo, 99.9% coverage |
| Loopback gaps during silence | **None on this laptop**, even without the keepalive stream; the keepalive stays as insurance |
| Mic | Intel SST 4-channel array, 48 kHz, identical channels (already processed). Speech at about -39 dBFS average and -19 dBFS peaks |
| Echo from laptop speakers into the mic | **Almost none** (envelope correlation 0.07) with the speakers at 36% volume. The audio chip appears to cancel echo; confirm in a real call |
| Whisper `large-v3-turbo` on the RTX A500, sequential | 18.5x real time on 2.4 min, 9.3x on 29 min (6.5 min per meeting hour); 1.3 GB GPU |
| Whisper `large-v3-turbo`, batched (batch 8) | **29.7x real time: 2.0 min per meeting hour**; 2.3 GB GPU. Now the default |
| Accuracy on synthetic speech | 0% word error rate (clean TTS; real calls will be higher) |
| pyannote 4.1 telemetry | On by default (file durations, speaker counts to `otel.pyannote.ai`). ZachCap sets `PYANNOTE_METRICS_ENABLED=false` |
| Speaker separation speed | pyannote's last embedding batch is smaller than the rest; that new shape made cuDNN pick a convolution needing ~10 GB of workspace, which Windows spilled into shared system memory (one batch took 95 s instead of 1 s). Padding every batch to the same size fixed it: about 7x faster, identical output. Half precision was faster still but broke clustering, so it is not used |
| Synthetic voices for voice recognition | Not usable: the embedding model rates two different Windows TTS voices 0.93 alike, above each voice's similarity to itself. Voice matching is tuned on real recordings only |
| Summary model | Same transcript through Sonnet 5.5, Opus 5.5 and Fable 5.1: all accurate; Opus 5.5 also caught an implicit commitment the others missed. Opus 5.5 is the default (about $0.08 per meeting hour), with Sonnet 5.5 and Fable 5.1 selectable in Settings |
| Real recording, end to end | A 37-minute real Teams recording: 96 s to transcribe, 257 s to separate 6 speakers, 353 s in total (about 9.5 min per meeting hour) |
| Voice recognition on real voices | The recording processed as two halves, first-half speakers named: the returning speaker was recognized automatically at 0.96. Same person 0.88-0.96, different people 0.07-0.52, so the thresholds are 0.75 (auto) and 0.60 (suggest) |
| SQLite across threads | FastAPI opened a request's connection on one worker thread and used it on another; audio playback failed until connections allowed it (regression test added) |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Recording consent and company policy | Consent helper; check policy before real use |
| Voiceprints count as biometric data | Stay on the laptop; delete controls; this is for personal use |
| Windows CUDA setup | Pinned versions; CPU fallback |
| Accuracy for dial-ins and cross-talk | Show confidence; make correcting one click |
| Echo on other hardware | Text de-duplication; software echo cancellation if needed |

## 11. What v0.1 includes (2026-11-22)

Built and tested in the app with synthetic meetings, plus the real recording above:

- Recording: one button, Ctrl+Alt+R from anywhere, tray menu, live level meters, crash-safe chunks,
  device pickers (Windows default, any mic or output, or a virtual cable), call detection from
  Windows' microphone-in-use status with a "Record?" prompt.
- Import: any audio or video file (Teams .mp4, .m4a, .mp3, .wav...) with an optional time range.
- Transcript: speaker names and colors, filter, click a time to play, move one line to another person.
- Speakers: "Who's this?" cards with a sample to play, automatic recognition, talk-time chart, rename.
- Speaker lanes with solo playback: plays only one person, from their own track.
- Summaries (Opus 5.5 by default): overview, decisions, action items as commitments or assignments
  with due dates and "said at" links, who said what, open questions, topics.
- Action item tracker across meetings (open/done, mine, commitments vs assigned).
- Ask a meeting: answers from the transcript with clickable timestamps.
- History with stats, full-text search, people with talk time and voice samples.
- Settings: your name, devices, language, Claude model, API keys, data folder.

Next: live transcript, one-app capture (process loopback), ask across meetings, signed installer.

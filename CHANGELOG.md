# Changelog

What's new in each version of ZachCap, newest first. It's in the app as well, under **About**. The releases themselves are on [GitHub](https://github.com/ZachGSnow/zachcap/releases).

## Unreleased

### Fixed

- **What leaves the PC** is said right in About and the README now that meetings can go to ZachCore: a summary written on this PC goes there too while sending is on.
- **An action item follows its voice.** Once you name the voice that promised it, or ZachCap recognizes it, the item shows that person, as ZachCore does, and one your own voice promised counts as yours. It kept the name the summary wrote, like Speaker 2, until the next summary.

## 0.6.0 - 2026-11-25

Your meetings in ZachCore.

### New

- **Send meetings to ZachCore.** Off until you turn it on in Settings, with your own ZachCore server's address and key. Each meeting recorded from then on becomes a note there: its summary and your notes, its tasks, who was there and how much they talked, and lasting facts about the people and the client, which wait there for your yes; the transcript too, if you want. A voice named later, a second summary or a ticked task corrects it there, and a meeting made private, deleted or redacted is taken back, also while sending is off. A meeting's menu has Send now and Take back, and Settings says how many wait and why. Audio, voice samples, snapshots, files and dictation never go.
- **Summaries say where.** Each decision and open question remembers the moment it was said, each action item the words it was agreed in, and a summary by Claude notes lasting facts about the people there and the client, each checked against the line it came from.

### Improved

- **A reworded action item stays ticked off.** When a new summary words an item you ticked off differently, it stays done instead of coming back open beside the old one.

## 0.5.0 - 2026-11-24

Tidier meetings, and a better way in.

### New

- **Brief voices are left out.** A voice ZachCap can't name that has less than 5% of a call's talk (background chatter, a stray word) isn't listed as a speaker, and you aren't asked who it is. What it said stays in the transcript, as Unknown, and the Speakers tab lists the voices left out, to name one after all. The share is in Settings (2%, 5%, 10% or off), and meetings you already have follow it too.
- **About.** At the bottom of the left menu: the version and build, what stays on this PC, what ZachCap is built on, and this list of changes.
- **A new installer** in ZachCap's own look: one page to install or update, the progress, and one at the end. An update replaces the program where it is, without asking whether to uninstall first, so the speech engine stays set up.

### Improved

- **Conversation stats** have room between their columns, and each heading explains itself when you point at it.
- **Pieces of a sentence stay together.** When a word in the middle of someone's sentence was heard as another, unknown voice, it goes back to them in meetings processed from now on.

### Fixed

- ZachCap's window no longer stays empty when WebView2, which shows it, crashes or is ended: it's put back, and a recording goes on meanwhile.

## 0.4.0 - 2026-11-24

Private meetings, and summaries that never leave the PC.

### New

- **Private meetings.** Keep a meeting from Claude with the lock in the recording bar, or later in the meeting's menu. Nothing in it is sent, and Ask and the MCP server don't have it. On this PC it stays like any other.
- **Summaries on this PC.** With Ollama installed and a model chosen in Settings, a model on your PC writes summaries and documents instead of Claude, and answers questions about private meetings. Nothing is sent and nothing is billed.
- **Related meetings.** A meeting's page lists the few others about the same thing, each with the reason: the same ticket, client, people, a topic, or the words that set it apart.
- **Words to know from a file.** An asset list, an exported email or a glossary: ZachCap lists the names and terms in it, and you tick the ones to keep. Nothing near a password is offered.
- **Faster speaker separation.** A setting for PCs without an NVIDIA GPU: a third to a half less time telling speakers apart.

### Improved

- **The live panel can't end a recording any more.** Its speech models run in a process of their own, so a GPU driver that crashes doesn't matter.
- **An engine that crashes starts again by itself**, and a Teams (or other chosen app's) call goes on in a new recording.
- **Setup repairs itself.** When the engine can't start because files of it are damaged or missing, Try again builds it again, mostly from what was downloaded before.
- **Imports** from slow drives are read to the end, and a full drive is said as such and tried again once there's room.

### Fixed

- What you say right after the other side is no longer left out as echo.
- A kind of meeting that couldn't be picked is picked the next time.
- With masking on, titles and questions are masked for Claude too.

## 0.3.0 - 2026-11-24

What's being said, while it's being said.

### New

- **A live panel during calls.** A small window over the call shows what's being said a second or two after each person stops. It's left out of screen shares and never takes the keyboard from the call.
- **The kind of meeting, picked for you.** With the kind on Automatic, each summary picks one (customer call, standup, incident...). Clear clues cost nothing; otherwise a small Claude call does it, under a tenth of a cent.

### Improved

- **A sturdier installer.** It installs only pinned, checked files, quitting during setup stops its download, and uninstalling leaves nothing behind but your data.

### Security

- Other people's words in exports and work logs can't run code through Obsidian plugins.
- Imported recordings are read in a separate, time-limited process, so a broken or crafted file can't hang or crash the engine.

## 0.2.0 - 2026-11-24

The first version to install from GitHub, with everything built since the first one.

### New

- **An installer that brings its own speech engine.** The first start sets it up, with its progress on screen.
- **Calls record themselves** when Teams or another call app takes your microphone, and stop when the call ends.
- **Every speaker and headset is recorded**, so a call on a headset that isn't the Windows default isn't lost.
- **Warnings while it records**: a muted mic, a call with no sound, a nearly full disk.
- **Notes, bookmarks and snapshots** during a call; the text in a snapshot is read on this PC.
- **Dictation in any app**: hold Ctrl+Alt+D, speak, let go. With snippets, and "scratch that".
- **Work log** for a day or a week, with a draft time entry for each meeting.
- **Tickets, clients and topics**: found in your meetings, and shown, filtered and exported with them.
- **Ask across all your meetings**, "Brief me" on a person, and an MCP server for Claude.
- **Kinds of meeting** for summaries, and follow-up emails and ticket notes in one click.
- **Words to know**, fixing a line's words, and redacting a moment.
- **A watch folder**, attached files, and exports to Markdown, text and subtitles.

### Improved

- **Faster without an NVIDIA GPU**: transcription uses every CPU core.

### Security

- Every request to the local engine is checked before it's read, and it gets a new port and token on each start.
- Other people's words go to Claude marked as data, and exports can't load images or HTML.

## 0.1.0 - 2026-11-22

The first version: records a call, transcribes it on your own machine, tells the speakers apart and recognizes them next time, and writes a summary with action items.

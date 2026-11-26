# Settings

Everything is set in the app under **Settings**.

| Setting | What it does | Default |
|---|---|---|
| Your name | How your own lines appear in transcripts and summaries | You |
| Also answer to | Other names people call you (a nickname, initials), for marking where your name was said | empty |
| Microphone | Your voice; pick the one your meeting app uses | Windows default |
| Computer audio from | Every speaker and headset at once, the Windows default only, or one device (or a virtual cable) | Every speaker and headset |
| Language | Whisper's language, or automatic detection | English |
| Words to know | Terms to spell right, one per line; `Term: misheard, misheard` adds corrections. Add from a file offers the names and terms found in a text file to tick | empty |
| Claude model | Opus 5.5, Sonnet 5.5 or Fable 5.1 | Opus 5.5 |
| Model on this PC | One of the models Ollama has on this PC, to write summaries and documents without sending anything | None |
| Written by | Who writes summaries and documents unless you pick the other on a meeting's page: Claude, or the model on this PC (private meetings are always left to that one) | Claude |
| Faster speaker separation | Looks for who's speaking half as often: a third to a half less time separating speakers on a PC without an NVIDIA GPU; turn edges a little less exact | Off |
| Leave out brief voices | A voice that can't be named and has less than this of a call's talk isn't listed as a speaker or asked about; what it said stays in the transcript, as Unknown. Off, or under 2%, 5% or 10% of the talk; changing it updates every meeting | Under 5% of the talk |
| Usual kind of meeting | What summaries focus on unless a meeting says otherwise; Automatic picks one for each meeting | Automatic (a usual kind chosen before is kept) |
| Kinds of meeting, one-click documents | Edit the built-in ones (and reset them), or add your own with your own instructions | As shipped |
| API keys | Hugging Face token and Anthropic key | not set |
| Start with Windows | Opens ZachCap in the tray when you sign in (release builds) | off |
| Dictation | Hold Ctrl+Alt+D in any app to type what you say | off |
| Snippets | For dictation: `cue: text to type`, one per line; saying just a cue types its text | empty |
| Shortcuts | New keys for start or stop recording, bookmark, snapshot and dictate: click the keys and press new ones (two of Ctrl, Alt and Win, or Win, with a letter, digit or F key; F13-F24 alone). Settings says when another app has registered the same keys (they never reach ZachCap then); apps that read keys with a hook of their own can't be seen | Ctrl+Alt+R, B, S and D |
| Sound check | Four seconds recorded as a meeting would be, with a short sound halfway, played where the computer's audio is recorded from (a quiet tone on a chosen output device): shows your mic and the computer's audio are both heard (nothing is kept) | - |
| Record calls on their own | Start recording when a chosen call app takes the mic: Microsoft Teams, 3CX, Zoom, Webex, RingCentral (Slack and Discord when picked; never a web browser). These recordings always stop when their call ends | on |
| Live panel during calls | A small panel over the call with what's being said, a moment after each person stops talking; left out of screen shares and screenshots. Uses the GPU, or the CPU when there's none. Off stops it at once; on starts it with the next recording | on with an NVIDIA GPU, else off |
| Stop when the call ends | For recordings you start: stop a few seconds after the call app lets go of the mic and nobody has spoken for ten seconds, or a minute after ten quiet minutes | on |
| Theme | Light, dark, or the same as Windows | Same as Windows |
| Export folder | Keeps a Markdown copy of every meeting in a folder you choose, one file per meeting, named with its id (M12) | off |
| Work logs there too | Also keeps a daily and a weekly work log in the export folder | off |
| Watch folder | Recordings saved in this folder import themselves within a minute; turning it on asks about the ones already there | off |
| Ticket numbers look like | Patterns for your ticket numbers: `#` a digit, `?` a letter, anything else itself, several separated by commas (`#######, INC-####`) | off |
| Topics | Words and phrases to track, one topic per line: `Name: word, phrase` | empty |
| ZachCore | Sends meetings to your own ZachCore server. Change... sets its address (https, or http to this PC), the key ZachCore made for ZachCap (kept in Credential Manager for that address), the space (Work or Home), what goes (summaries, tasks, facts, transcripts; the title, time, client, ticket, people and talk shares go with any of them) and clients none of whose meetings go; Test the connection checks the key. Meetings recorded from then on go; Send older meetings sends the ones before, and Take everything back takes them all back | Off; summaries, tasks and facts on, transcripts off |

For development, `ZACHCAP_HOME` sets the data folder (default `%LOCALAPPDATA%\ZachCap`) and `ZACHCAP_ENGINE_DIR` where the desktop app finds the engine.

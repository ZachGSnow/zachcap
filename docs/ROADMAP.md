# Roadmap

What is open. What has shipped is in the [changelog](../CHANGELOG.md).

## Next

| What | Where it stands |
|---|---|
| Help during the call | "Catch me up" on what was said so far, a question about the call while it goes on, and a first summary the moment it ends, from the live transcript. Granola, Fireflies, Notion and Teams have it. The live transcript is on by default only with an NVIDIA GPU |
| Calls that switch language | Whisper is told one language, or detects one for each track. faster-whisper can tell it for each stretch |
| A signed installer | Needs a code-signing certificate. Until then SmartScreen warns, and Smart App Control has to be off |
| Tests on more hardware | A live Teams call on a PC with an NVIDIA GPU, a headset plugged in mid-call, a laptop without built-in echo cancellation, Windows 10. PyTorch 2.13 comes after a run on an NVIDIA GPU |

## Maybe

| What | What has to be settled first |
|---|---|
| Automatic snapshots of the call window | Privacy: which windows, how often, how long pictures are kept. Google Meet's notes (August 2026) say so on the call and can be turned off per meeting |
| Voice edits in dictation | Saying "shorter" or "as a ticket note" over selected text (Wispr Flow, Superwhisper). It would be the "ticket note" dictation, with keys of its own so plain dictation stays as it is |
| Reading the call app's window | Its mute state and the names in its list of people (Fathom, Granola). Fragile: each call app shows them its own way |
| A meeting's title from its call window | Teams and Webex put the meeting's name in their window's title. Until it is tried on real calls, a summary's title is used |
| Faster without an NVIDIA GPU | Parakeet, or Whisper on the GPU through Vulkan: a second speech engine |
| Other model servers on this PC | LM Studio, or any server that answers like OpenAI's, beside Ollama |
| A webhook | Each meeting's notes and tasks sent to an address of your own when it's done, as ZachCore gets them |
| Calls through Phone Link | Knowing Phone Link as a call app, once it is clear what Windows says has the mic |
| What Claude cost this month | Only a meeting's latest summary keeps its cost: documents and answers would need a record of their own |
| Encrypting the library | Where the key lives without a sign-in |
| A palette (Ctrl+K) | Jumping to a meeting, a person or a setting by typing |
| Saved questions, Word and PDF export | Which questions and layouts are worth having |

## Not planned

Meeting bots, calendar access, accounts in a cloud of ZachCap's, syncing ZachCap between PCs, virtual audio drivers, mobile apps, macOS or Linux, recording the call's video. Sending meetings to a ZachCore server of your own is in: see [FEATURES.md](FEATURES.md).

## Upkeep

- PyTorch and torchaudio aren't in Dependabot's reach (a separate index) and are bumped by hand; setuptools moves with them (PyTorch 2.11 holds it below 82).
- Updating Tauri's CLI means bringing `src-tauri/windows/installer.nsi` up to its template.

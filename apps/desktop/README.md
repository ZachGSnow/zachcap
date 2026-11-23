# ZachCap desktop app

Tauri 2 shell with a React + TypeScript + Tailwind v4 + shadcn/ui interface.

The Rust side starts the Python engine (`../../engine`) with `uv run zachcap serve`, on a free
127.0.0.1 port with a random per-launch token, and hands both to the UI through the
`engine_info` command. It also puts ZachCap in the tray: closing the window keeps it running.

## Develop

```powershell
npm install
npm run tauri dev
```

Needs Node, Rust with the MSVC build tools, and `uv` on PATH. Set `ZACHCAP_HOME` to use a
scratch data folder instead of `%LOCALAPPDATA%\ZachCap`, and `ZACHCAP_ENGINE_DIR` to point at
a different engine checkout.

## Icon

```powershell
uv run --project ..\..\engine python scripts\make-icon.py app-icon.png
npx tauri icon app-icon.png
```

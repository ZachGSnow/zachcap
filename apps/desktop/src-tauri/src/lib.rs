//! ZachCap desktop shell: starts the Python engine, hands the UI its address and
//! token, and lives in the tray like Tailscale (closing the window keeps it running).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

// Start with Windows is release-only (see run()); this keeps debug builds from warning
// that the plugin is unused.
#[cfg(all(desktop, debug_assertions))]
use tauri_plugin_autostart as _;

#[cfg(windows)]
mod dictation_keys;

/// Added to the sign-in launch: ZachCap starts in the tray instead of opening its window.
const HIDDEN_ARG: &str = "--hidden";

#[derive(Clone, Serialize)]
struct EngineInfo {
    port: u16,
    token: String,
}

struct Engine {
    /// The running engine's port and token; None until it has said which port it got. Each
    /// start has a port and a token of its own.
    info: Mutex<Option<EngineInfo>>,
    launch: Launch,
    child: Mutex<Option<Child>>,
    /// Why the engine stopped, once it has; cleared by a restart.
    stopped: Mutex<Option<String>>,
    /// Set from the moment a start begins until the engine runs or fails, so a second
    /// start (Try again pressed twice) doesn't run another.
    starting: AtomicBool,
    /// While an installed copy sets its engine up: the last thing uv said, for the startup
    /// screen.
    setup: Mutex<Option<String>>,
}

/// Where the engine and uv are. An installed copy runs the engine that came with it, in a
/// venv of its own that it sets up with the bundled uv; a development build runs the
/// repo's engine with the uv on this PC.
#[derive(Clone)]
struct Launch {
    uv: PathBuf,
    engine: PathBuf,
    installed: bool,
}

/// Set while ZachCap exits, so the engine going away then isn't reported as a crash.
static EXITING: AtomicBool = AtomicBool::new(false);

/// The engine's log in the data folder (a debug build's engine prints to the terminal).
const LOG: &str = "engine.log";
/// The engine's log is emptied once it passes this; it's checked once a minute.
const LOG_LIMIT: u64 = 20 * 1024 * 1024;

/// Empties the engine's log once it passes `limit`, leaving a line that says so. The engine
/// appends to it (see start_engine), so it carries on from the top, not past the old end.
fn trim_log(log: &Path, limit: u64) {
    if !std::fs::metadata(log).is_ok_and(|meta| meta.len() > limit) {
        return;
    }
    // Emptied through one handle, the note added through another that appends, so a line the
    // engine wrote in between isn't written over.
    if let Ok(file) = std::fs::OpenOptions::new().write(true).open(log)
        && file.set_len(0).is_ok()
        && let Ok(mut file) = std::fs::OpenOptions::new().append(true).open(log)
    {
        let _ = writeln!(
            file,
            "[ZachCap] {LOG} passed {} MB and was emptied.",
            limit / (1024 * 1024)
        );
    }
}

/// The live panel's window: a small window over the call while a recording runs, with the
/// live transcript (the page shows it when its window has this label). Left out of screen
/// shares and screenshots, never in the taskbar, and it doesn't take the keyboard from the
/// call when it appears.
const LIVE: &str = "live";

/// What the shell follows of the engine, once a second, so the live panel and the
/// notifications about calls never wait on the page (a hidden window's timers run late).
#[derive(Default)]
struct Following {
    /// A recording with a live transcript ran at the last look.
    live: bool,
    /// The meeting being recorded at the last look.
    meeting: Option<i64>,
    /// The meeting whose live panel was closed: it isn't opened again for it.
    dismissed: Option<i64>,
    /// How far the shell has said what the engine did on its own: the engine's run (`boot`)
    /// and the last event said (see /api/record). None until the first answer.
    events: Option<(String, i64)>,
}

struct Follow(Mutex<Following>);

/// Where the page reaches the engine; None while it starts (the page asks again).
#[tauri::command]
fn engine_info(engine: tauri::State<Engine>) -> Option<EngineInfo> {
    engine.info.lock().unwrap().clone()
}

/// What the engine's setup is doing, while it runs (the first launch of an installed copy
/// downloads Python and the speech libraries); None otherwise.
#[tauri::command]
fn engine_setup(engine: tauri::State<Engine>) -> Option<String> {
    engine.setup.lock().unwrap().clone()
}

/// Why the engine stopped, if it has; the startup screen uses it to fail fast.
#[tauri::command]
fn engine_exited(engine: tauri::State<Engine>) -> Option<String> {
    check_engine(&engine);
    engine.stopped.lock().unwrap().clone()
}

/// Notices that the engine process has ended and records why. Returns the message the
/// first time only.
fn check_engine(engine: &Engine) -> Option<String> {
    let mut child = engine.child.lock().unwrap();
    let status = child.as_mut()?.try_wait().ok()??;
    *child = None;
    let log = if cfg!(debug_assertions) {
        "Its output is in the terminal that ran `npm run tauri dev`.".to_string()
    } else {
        format!("Its log is {}.", data_dir().join("engine.log").display())
    };
    let message = format!("The engine stopped ({status}). {log}");
    *engine.stopped.lock().unwrap() = Some(message.clone());
    Some(message)
}

/// Starts the engine again after it stopped, on a new port with a new token; the window
/// picks both up through engine_info without reloading. Does nothing while it is still
/// running or starting.
#[tauri::command]
fn restart_engine(app: AppHandle) {
    begin(app);
}

/// The tray icon shows a red dot and says so in its tooltip while a recording runs, so
/// it's visible even with the window closed.
#[tauri::command]
fn set_recording_indicator(app: AppHandle, recording: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        keys.0.lock().unwrap().recording = recording;
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
    let Some(tray) = app.tray_by_id("main") else {
        return;
    };
    let _ = tray.set_tooltip(Some(if recording {
        "ZachCap: recording"
    } else {
        "ZachCap"
    }));
    if let Some(icon) = app.default_window_icon() {
        let _ = tray.set_icon(Some(if recording {
            with_red_dot(icon)
        } else {
            icon.clone().to_owned()
        }));
    }
}

/// The global shortcuts, with their default keys, in the order Settings lists them.
/// Settings can change the keys; the page passes them on (set_shortcuts).
const ACTIONS: [(&str, &str); 4] = [
    ("record", "Ctrl+Alt+R"),
    ("bookmark", "Ctrl+Alt+B"),
    ("snapshot", "Ctrl+Alt+S"),
    ("dictate", "Ctrl+Alt+D"),
];

/// Which keys each shortcut has, and what decides when each is held: start or stop
/// recording always; bookmark and snapshot only while recording (the rest of the time Word,
/// an IDE, or a keyboard whose AltGr+S types a letter keep them); dictate only while
/// dictation is on.
#[derive(Default)]
struct KeyState {
    /// Keys picked in Settings, by shortcut ("Ctrl+Alt+P"); the default where absent.
    keys: std::collections::HashMap<String, String>,
    recording: bool,
    dictation: bool,
    /// While Settings waits for new keys, ZachCap lets go of its own so the page gets them.
    /// Counted: only pause_shortcuts changes it, so nothing else takes the keys back early.
    pauses: u32,
}

impl KeyState {
    fn keys_for(&self, action: &str) -> String {
        self.keys.get(action).cloned().unwrap_or_else(|| {
            let (_, default) = ACTIONS.iter().find(|(name, _)| *name == action).unwrap();
            default.to_string()
        })
    }

    fn wanted(&self, action: &str) -> bool {
        self.pauses == 0
            && match action {
                "record" => true,
                "bookmark" | "snapshot" => self.recording,
                "dictate" => self.dictation,
                _ => false,
            }
    }
}

struct Keys(Mutex<KeyState>);

/// What Settings shows for a shortcut.
#[derive(Clone, Serialize)]
struct KeyStatus {
    action: String,
    keys: String,
    /// "on" (held now), "shared" (dictate only: another app has registered the same keys,
    /// and dictation gets them first while it's on), "free" (not needed now, and no other app
    /// has registered them), "off" (not needed now, not checked), "taken" (another app has
    /// registered them), "failed" (Windows refused the keyboard hook) or "invalid".
    state: &'static str,
}

/// "Ctrl+Alt+R" as the plugin reads it (it calls the Windows key Super).
#[cfg(desktop)]
fn parse_keys(keys: &str) -> Option<tauri_plugin_global_shortcut::Shortcut> {
    keys.split('+')
        .map(|part| match part.trim() {
            win if win.eq_ignore_ascii_case("win") => "Super",
            other => other,
        })
        .collect::<Vec<_>>()
        .join("+")
        .parse()
        .ok()
}

/// Holds the shortcuts wanted now, lets go of the rest and of keys no longer used
/// (`retired`), and says how each one is. With `probe`, a shortcut not needed now is held
/// and let go at once, to learn whether another app has its keys. The plugin is called
/// without our lock held, since its handler takes the lock.
#[cfg(desktop)]
fn sync_keys(app: &AppHandle, retired: Vec<String>, probe: bool) -> Vec<KeyStatus> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;
    let Some(keys) = app.try_state::<Keys>() else {
        return Vec::new();
    };
    let plan: Vec<(String, String, bool)> = {
        let state = keys.0.lock().unwrap();
        ACTIONS
            .iter()
            .map(|(action, _)| {
                (
                    action.to_string(),
                    state.keys_for(action),
                    state.wanted(action),
                )
            })
            .collect()
    };
    let shortcuts = app.global_shortcut();
    for old in retired.iter().filter_map(|old_keys| parse_keys(old_keys)) {
        if shortcuts.is_registered(old) {
            let _ = shortcuts.unregister(old);
        }
    }
    plan.into_iter()
        .map(|(action, keys, wanted)| {
            let state = match parse_keys(&keys) {
                None => "invalid",
                #[cfg(windows)]
                Some(shortcut) if action == "dictate" => {
                    sync_dictation_keys(shortcuts, shortcut, &keys, wanted, probe)
                }
                Some(shortcut) => {
                    let held = shortcuts.is_registered(shortcut);
                    if wanted && !held {
                        match shortcuts.register(shortcut) {
                            Ok(()) => "on",
                            Err(error) => {
                                // Another app owns the combination; the tray menu still works.
                                eprintln!("{keys} is unavailable: {error}");
                                "taken"
                            }
                        }
                    } else if wanted {
                        "on"
                    } else {
                        if held {
                            let _ = shortcuts.unregister(shortcut);
                        }
                        if !probe {
                            "off"
                        } else if shortcuts.register(shortcut).is_ok() {
                            let _ = shortcuts.unregister(shortcut);
                            "free"
                        } else {
                            "taken"
                        }
                    }
                }
            };
            KeyStatus {
                action,
                keys,
                state,
            }
        })
        .collect()
}

/// Dictation's keys are read by a keyboard hook (dictation_keys), not registered: while
/// they're held the main key never reaches the app in front, and letting go of any of them
/// ends the dictation. Registering is only used to learn whether another app has them.
#[cfg(windows)]
fn sync_dictation_keys(
    shortcuts: &tauri_plugin_global_shortcut::GlobalShortcut<tauri::Wry>,
    shortcut: tauri_plugin_global_shortcut::Shortcut,
    keys: &str,
    wanted: bool,
    probe: bool,
) -> &'static str {
    let Some(chord) = dictation_keys::Chord::parse(keys) else {
        dictation_keys::listen(None);
        return "invalid";
    };
    if shortcuts.is_registered(shortcut) {
        let _ = shortcuts.unregister(shortcut);
    }
    // The hook gets the keys before another app's registered hotkey: "shared", not "taken".
    let shared = probe && {
        let taken = shortcuts.register(shortcut).is_err();
        if !taken {
            let _ = shortcuts.unregister(shortcut);
        }
        taken
    };
    if wanted && !dictation_keys::listen(Some(chord)) {
        eprintln!("The dictation keys couldn't be read: Windows refused the keyboard hook");
        return "failed";
    }
    if !wanted {
        dictation_keys::listen(None);
    }
    match (wanted, probe, shared) {
        (_, _, true) => "shared",
        (true, _, false) => "on",
        (false, false, _) => "off",
        (false, true, false) => "free",
    }
}

/// Which shortcut, if any, has these keys now.
#[cfg(desktop)]
fn action_for(
    app: &AppHandle,
    shortcut: &tauri_plugin_global_shortcut::Shortcut,
) -> Option<&'static str> {
    let keys = app.try_state::<Keys>()?;
    let state = keys.0.lock().unwrap();
    ACTIONS
        .iter()
        .map(|(action, _)| *action)
        .find(|action| parse_keys(&state.keys_for(action)).as_ref() == Some(shortcut))
}

/// The keys picked in Settings (the page passes them on at start and on every change).
/// Returns how each shortcut is, for Settings.
#[tauri::command]
fn set_shortcuts(
    app: AppHandle,
    keys: std::collections::HashMap<String, String>,
) -> Vec<KeyStatus> {
    let retired = {
        let Some(held) = app.try_state::<Keys>() else {
            return Vec::new();
        };
        let mut state = held.0.lock().unwrap();
        let before: Vec<String> = ACTIONS
            .iter()
            .map(|(action, _)| state.keys_for(action))
            .collect();
        state.keys = keys;
        save_keys(&state.keys);
        let after: Vec<String> = ACTIONS
            .iter()
            .map(|(action, _)| state.keys_for(action))
            .collect();
        before
            .into_iter()
            .filter(|keys| !after.contains(keys))
            .collect::<Vec<_>>()
    };
    #[cfg(desktop)]
    return sync_keys(&app, retired, true);
    #[cfg(not(desktop))]
    {
        let _ = retired;
        Vec::new()
    }
}

/// How each shortcut is now, for Settings (checking the ones not needed now).
#[tauri::command]
fn shortcut_status(app: AppHandle) -> Vec<KeyStatus> {
    #[cfg(desktop)]
    return sync_keys(&app, Vec::new(), true);
    #[cfg(not(desktop))]
    {
        let _ = app;
        Vec::new()
    }
}

/// Settings is waiting for new keys: let go of ZachCap's own until it's done, so pressing
/// one of them reaches the page instead of starting a recording.
#[tauri::command]
fn pause_shortcuts(app: AppHandle, paused: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        let mut state = keys.0.lock().unwrap();
        state.pauses = if paused {
            state.pauses + 1
        } else {
            state.pauses.saturating_sub(1)
        };
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
}

/// Dictation turned on or off in Settings (the page says so at start and on every change).
#[tauri::command]
fn set_dictation(app: AppHandle, enabled: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        keys.0.lock().unwrap().dictation = enabled;
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
}

/// The app icon with a red recording dot (and a white ring) in its bottom-right corner.
fn with_red_dot(icon: &Image<'_>) -> Image<'static> {
    let (width, height) = (icon.width(), icon.height());
    let mut rgba = icon.rgba().to_vec();
    let radius = width.min(height) as f32 * 0.24;
    let (cx, cy) = (width as f32 - radius - 1.0, height as f32 - radius - 1.0);
    for y in 0..height {
        for x in 0..width {
            let distance = ((x as f32 + 0.5 - cx).powi(2) + (y as f32 + 0.5 - cy).powi(2)).sqrt();
            if distance <= radius * 1.25 {
                let color = if distance <= radius {
                    [239, 68, 68, 255]
                } else {
                    [255, 255, 255, 255]
                };
                let at = ((y * width + x) * 4) as usize;
                rgba[at..at + 4].copy_from_slice(&color);
            }
        }
    }
    Image::new_owned(rgba, width, height)
}

/// Opens ZachCap's data folder in Explorer (from Settings). It takes no path, so the page
/// can't ask it to open anything else.
#[tauri::command]
fn open_data_folder() -> Result<(), String> {
    let folder = data_dir();
    if !folder.is_dir() {
        return Err(format!("{} doesn't exist yet", folder.display()));
    }
    Command::new("explorer")
        .arg(folder)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// The Python engine: ZACHCAP_ENGINE_DIR when set, else the one installed with the app (the
/// installer puts it and uv next to the exe), else the repo's `engine/` in development.
fn launch_for(app: &AppHandle) -> Launch {
    if let Some(dir) = std::env::var_os("ZACHCAP_ENGINE_DIR") {
        return Launch {
            uv: find_uv(),
            engine: PathBuf::from(dir),
            installed: false,
        };
    }
    if let Ok(resources) = app.path().resource_dir()
        && resources.join("engine").join("pyproject.toml").is_file()
    {
        let bundled = resources.join("uv").join("uv.exe");
        return Launch {
            uv: if bundled.is_file() {
                bundled
            } else {
                find_uv()
            },
            engine: resources.join("engine"),
            installed: true,
        };
    }
    Launch {
        uv: find_uv(),
        engine: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../engine"),
        installed: false,
    }
}

/// The keys last passed on from Settings, kept so the next start holds yours from the first
/// moment, not the defaults until the engine answers (an app starting meanwhile could lose
/// keys you gave it).
fn keys_file() -> PathBuf {
    data_dir().join("shortcuts.json")
}

fn save_keys(keys: &std::collections::HashMap<String, String>) {
    if let Ok(text) = serde_json::to_string(keys) {
        let _ = std::fs::write(keys_file(), text);
    }
}

fn saved_keys() -> std::collections::HashMap<String, String> {
    std::fs::read_to_string(keys_file())
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// Where the engine keeps its data; matches engine/src/zachcap/paths.py.
fn data_dir() -> PathBuf {
    std::env::var_os("ZACHCAP_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_default()
                .join("ZachCap")
        })
}

/// uv from PATH or its usual install folders: a shortcut started by Explorer can have an
/// older PATH than the terminal that installed uv.
fn find_uv() -> PathBuf {
    let exe = "uv.exe";
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(path) = std::env::var_os("PATH") {
        candidates.extend(std::env::split_paths(&path).map(|dir| dir.join(exe)));
    }
    if let Some(home) = std::env::var_os("USERPROFILE").map(PathBuf::from) {
        candidates.push(home.join(".local").join("bin").join(exe));
        candidates.push(home.join(".cargo").join("bin").join(exe));
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        let winget = local.join("Microsoft").join("WinGet");
        candidates.push(winget.join("Links").join(exe));
        if let Ok(packages) = std::fs::read_dir(winget.join("Packages")) {
            candidates.extend(
                packages
                    .flatten()
                    .filter(|entry| {
                        entry
                            .file_name()
                            .to_string_lossy()
                            .starts_with("astral-sh.uv_")
                    })
                    .map(|entry| entry.path().join(exe)),
            );
        }
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("uv"))
}

/// How long a started engine has to say which port it got before it counts as failed (the
/// first start after setup compiles the Python files, which takes a while on a slow PC).
const PORT_WAIT: Duration = Duration::from_secs(300);

/// Starts the engine on its own thread, setting an installed copy's engine up first when this
/// build hasn't been yet, with a new token, on a port it picks itself. Does nothing while the
/// engine runs or is already starting. A failure is kept as the reason it stopped, which the
/// startup screen and the restart banner show.
fn begin(app: AppHandle) {
    let Some(engine) = app.try_state::<Engine>() else {
        return;
    };
    if engine.child.lock().unwrap().is_some() || engine.starting.swap(true, Ordering::SeqCst) {
        return;
    }
    *engine.stopped.lock().unwrap() = None;
    *engine.info.lock().unwrap() = None;
    std::thread::spawn(move || {
        let engine = app.state::<Engine>();
        let token = uuid::Uuid::new_v4().simple().to_string();
        let port_file = data_dir().join(format!("engine-{}.port", uuid::Uuid::new_v4().simple()));
        let started = set_up(&engine, &data_dir()).and_then(|()| {
            start_engine(&engine.launch, &token, &port_file).map_err(|error| {
                // Its python.exe may be what's missing.
                unmark(&engine.launch);
                if engine.launch.installed {
                    format!("Couldn't start the engine ({error}).")
                } else {
                    format!(
                        "Couldn't run uv ({error}). Install it from https://docs.astral.sh/uv/ and start ZachCap again."
                    )
                }
            })
        });
        *engine.setup.lock().unwrap() = None;
        match started {
            Ok(child) => {
                *engine.child.lock().unwrap() = Some(child);
                match wait_for_port(&engine, &port_file) {
                    Some(port) => *engine.info.lock().unwrap() = Some(EngineInfo { port, token }),
                    None => start_failed(&engine),
                }
            }
            Err(message) => *engine.stopped.lock().unwrap() = Some(message),
        }
        let _ = std::fs::remove_file(&port_file);
        engine.starting.store(false, Ordering::SeqCst);
    });
}

/// Deals with an engine that never said which port it's on. One that stopped (check_engine
/// said why) may have damaged or missing files in its venv, so the venv loses its mark and the
/// next start (Try again) builds it from scratch instead of failing the same way. One still
/// starting after PORT_WAIT is ended, and its venv kept: a slow PC compiles the libraries'
/// Python files at the first start, and would have to start over in a new one.
fn start_failed(engine: &Engine) {
    if engine.stopped.lock().unwrap().is_some() {
        // Not while ZachCap exits, which ends the engine too.
        if !EXITING.load(Ordering::Relaxed) {
            unmark(&engine.launch);
        }
        return;
    }
    if let Some(mut child) = engine.child.lock().unwrap().take() {
        let _ = child.kill();
    }
    *engine.stopped.lock().unwrap() =
        Some("The engine started but never said which port it's on.".into());
}

/// Waits for the engine to write the port it got (see report_port in api.py). None when it
/// stops first or takes longer than PORT_WAIT.
fn wait_for_port(engine: &Engine, port_file: &Path) -> Option<u16> {
    let deadline = std::time::Instant::now() + PORT_WAIT;
    while std::time::Instant::now() < deadline {
        if let Some(port) = read_port(port_file) {
            return Some(port);
        }
        if engine.child.lock().unwrap().is_none() || EXITING.load(Ordering::Relaxed) {
            return None;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    None
}

/// The port in a port file, once it's there and whole.
fn read_port(port_file: &Path) -> Option<u16> {
    let text = std::fs::read_to_string(port_file).ok()?;
    text.trim().parse::<u16>().ok().filter(|&port| port != 0)
}

/// The file in an installed copy's venv that records which build it was set up for: written
/// when a setup has finished, and gone while one changes the venv and once the engine couldn't
/// start from it.
const SET_UP: &str = "zachcap-build.txt";

/// Where a venv that's replaced is moved, next to it, until it's deleted (the uninstaller
/// deletes it too).
const OLD_VENVS: &str = ".venv-old";

/// How long something that has a file of the venv open (an antivirus looking at it, say) gets
/// to let go when the venv is replaced.
const LET_GO: Duration = Duration::from_secs(10);

/// What an installed copy's venv needs before the engine can start from it.
#[derive(Debug, PartialEq)]
enum Setup {
    /// Nothing: it's set up for this build.
    Done,
    /// What changed since the build it was set up for (an update).
    Update,
    /// All of it: it isn't there, or may not be whole (a setup was cut short, the engine
    /// couldn't start from it, or an uninstall left half of it).
    Fresh,
}

/// What the venv needs for this build (`build` is the installer's BUILD stamp), by the mark of
/// the last setup that finished. Without one, packages may have files missing (an antivirus
/// took them, or uv was ended part-way through), and uv doesn't put them back: to it a package
/// is installed if its dist-info folder is there.
fn setup_for(venv: &Path, build: &str) -> Setup {
    use std::io::ErrorKind::{InvalidData, NotFound};
    let mark = match std::fs::read_to_string(venv.join(SET_UP)) {
        Ok(mark) => mark,
        Err(error) if matches!(error.kind(), NotFound | InvalidData) => String::new(),
        // It's there, but something has it open: no reason to throw the venv away.
        Err(_) => return Setup::Update,
    };
    // A stamp starts with a version (a power cut can leave an empty mark, or zeros). A venv
    // without its pyvenv.cfg was half removed (an uninstall while it ran): its python.exe would
    // quietly run uv's shared Python instead.
    let marked = mark.starts_with(|first: char| first.is_ascii_digit());
    if !marked || !venv.join("pyvenv.cfg").is_file() {
        Setup::Fresh
    } else if mark == build {
        Setup::Done
    } else {
        Setup::Update
    }
}

/// Has the next start build an installed copy's venv from scratch.
fn unmark(launch: &Launch) {
    if launch.installed {
        let _ = std::fs::remove_file(launch.engine.join(".venv").join(SET_UP));
    }
}

/// Whether anything is at a path: a link counts, wherever it points (exists() follows it).
fn is_there(path: &Path) -> bool {
    path.symlink_metadata().is_ok()
}

/// Moves a venv out of the way (into OLD_VENVS), to be deleted from there. It's renamed, which
/// Windows does whole or not at all, so there's never half a venv in its place. That works
/// while a program runs from it (ZachCap's MCP server for Claude), but not while one has a file
/// or a folder of it open: that one gets `wait` to let go. A link (in the venv, or in its place)
/// is moved itself, not what it points to.
fn set_aside(venv: &Path, wait: Duration) -> std::io::Result<()> {
    let old = venv.with_file_name(OLD_VENVS);
    let deadline = std::time::Instant::now() + wait;
    while is_there(venv) {
        // One that can't be deleted yet (a program runs from it) keeps its number.
        let mut number = 1;
        while is_there(&old.join(number.to_string())) {
            number += 1;
        }
        let moved = std::fs::create_dir_all(&old)
            .and_then(|()| std::fs::rename(venv, old.join(number.to_string())));
        if let Err(error) = moved {
            if std::time::Instant::now() >= deadline {
                return Err(error);
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    Ok(())
}

/// Brings an installed copy's engine venv up to date with the bundled uv: Python and the
/// speech libraries on first launch (a few GB), what changed after an update, all of it again
/// (from uv's cache) when the venv may not be whole. Skipped when this build is set up already
/// (the installer's BUILD stamp is in the venv). What uv says is shown on the startup screen
/// and kept in engine-setup.log, in `data`.
fn set_up(engine: &Engine, data: &Path) -> Result<(), String> {
    let launch = &engine.launch;
    if !launch.installed {
        return Ok(());
    }
    let venv = launch.engine.join(".venv");
    let old = launch.engine.join(OLD_VENVS);
    let marker = venv.join(SET_UP);
    let build = std::fs::read_to_string(launch.engine.join("BUILD")).unwrap_or_default();
    let setup = setup_for(&venv, &build);
    if setup == Setup::Done {
        // What was still in use when the venv was replaced; the engine doesn't wait for it.
        if is_there(&old) {
            std::thread::spawn(move || std::fs::remove_dir_all(old));
        }
        return Ok(());
    }
    let wheel = wheel_for(&launch.engine.join("wheel"), &build).ok_or(
        "The installer's copy of the engine is incomplete (no wheel). Install ZachCap again.",
    )?;
    let say = |step: &str| *engine.setup.lock().unwrap() = Some(step.to_string());
    say("Getting ready...");
    let mut log = std::fs::create_dir_all(data)
        .and_then(|_| std::fs::File::create(data.join("engine-setup.log")))
        .ok();
    let again = setup == Setup::Fresh && (is_there(&venv) || is_there(&old));
    let mut before = None;
    if again {
        say("Clearing the last setup...");
        set_aside(&venv, LET_GO).map_err(|error| {
            // Tauri gives the program's folder with \\?\ in front.
            let venv = venv.display().to_string();
            format!(
                "Couldn't replace the speech engine ({error}). Close anything that has {} open (a \
                 folder window, a terminal), or restart the PC, and press Try again.",
                venv.trim_start_matches(r"\\?\")
            )
        })?;
        // A link is deleted itself, never followed. What's still in use goes on a later start.
        let _ = std::fs::remove_dir_all(&old);
        say("Getting ready...");
    } else if setup == Setup::Update {
        // Not marked again until it's finished: cut short, it may not be whole.
        before = Some((std::fs::read(&marker), installed_packages(&venv)));
        std::fs::remove_file(&marker)
            .map_err(|error| format!("Couldn't update the speech engine ({error})."))?;
    }
    let installed = install(engine, &venv, &wheel, again, &mut log);
    if installed.is_ok() {
        let _ = std::fs::write(&marker, &build);
    } else if let Some((Ok(mark), packages)) = before
        && packages == installed_packages(&venv)
        && damaged_packages(&venv).is_empty()
    {
        // uv gave up before it changed anything (no internet, say): still the venv to update.
        let _ = std::fs::write(&marker, mark);
    }
    installed
}

/// Installs what `venv` is missing. In two steps, so nothing that isn't pinned is fetched or
/// run: the libraries from uv.lock (each checked against its hash), then ZachCap's own code
/// from the wheel the installer carries, built when the release was (building it here would
/// fetch its build tool). `again`: the venv is built from scratch after one that may not have
/// been whole.
fn install(
    engine: &Engine,
    venv: &Path,
    wheel: &Path,
    again: bool,
    log: &mut Option<std::fs::File>,
) -> Result<(), String> {
    // The lock as shipped, without the test tools or ZachCap itself. Only the project's own
    // settings: no uv.toml from the user's or the machine's config folders (another account
    // can write C:\ProgramData), and a Python uv manages, not whatever 3.12 is installed.
    let sync = |command: &mut Command| {
        command
            .arg("sync")
            .arg("--project")
            .arg(&engine.launch.engine)
            .args(["--locked", "--no-dev", "--no-install-project"])
            .args(["--no-config", "--managed-python"])
            .env("UV_PROJECT_ENVIRONMENT", venv);
    };
    run_uv(engine, log, sync)?;
    // A venv's files are mostly hard links to the ones in uv's cache: one damaged in the last
    // venv is damaged in the cache too, and an antivirus may have taken both. uv doesn't look,
    // so those packages are downloaded again.
    let damaged = if again {
        damaged_packages(venv)
    } else {
        Vec::new()
    };
    if !damaged.is_empty() {
        if let Some(log) = log.as_mut() {
            let _ = writeln!(
                log,
                "[ZachCap] Damaged in uv's cache: {}",
                damaged.join(" ")
            );
        }
        run_uv(engine, log, |command| {
            sync(command);
            command.arg("--no-cache");
            for package in &damaged {
                command.arg("--reinstall-package").arg(package);
            }
        })?;
    }
    // ZachCap's own code, copied into the venv (so the installed files are only read), from the
    // wheel itself and not what uv's cache has of it.
    run_uv(engine, log, |command| {
        command
            .args(["pip", "install", "--python"])
            .arg(venv)
            .args(["--no-deps", "--reinstall", "--offline", "--no-cache"])
            .arg("--no-config")
            .arg(wheel);
    })
}

/// The dist-info folders of a venv's packages, which are what uv goes by.
fn installed_packages(venv: &Path) -> Vec<PathBuf> {
    let mut folders: Vec<PathBuf> = std::fs::read_dir(venv.join("Lib").join("site-packages"))
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|kind| kind == "dist-info"))
        .collect();
    folders.sort();
    folders
}

/// The names of a venv's packages that have a file missing, or of another size than their wheel
/// had it (their RECORD says). Not by hash: that's a few GB to read.
fn damaged_packages(venv: &Path) -> Vec<String> {
    let packages = venv.join("Lib").join("site-packages");
    let whole = |folder: &PathBuf| {
        let record = std::fs::read_to_string(folder.join("RECORD"));
        record.is_ok_and(|record| {
            // Each line is "path,hash,size"; a path with a comma in it has quotes around it.
            record.lines().all(|line| {
                let mut fields = line.rsplitn(3, ',');
                let size = fields.next().and_then(|size| size.parse::<u64>().ok());
                let path = packages.join(fields.nth(1).unwrap_or_default().trim_matches('"'));
                size.is_none_or(|size| std::fs::metadata(path).is_ok_and(|file| file.len() == size))
            })
        })
    };
    installed_packages(venv)
        .iter()
        .filter(|folder| !whole(folder))
        .filter_map(|folder| folder.file_stem()?.to_str()?.split('-').next())
        .map(str::to_string)
        .collect()
}

/// The wheel of this build's version in the installer's wheel folder (`build` is the BUILD
/// stamp, which starts with the version). An update that didn't uninstall first leaves the
/// older version's wheel beside it, so the first one found isn't good enough.
fn wheel_for(folder: &Path, build: &str) -> Option<PathBuf> {
    let version = build.split_whitespace().next()?;
    let name = format!("zachcap-{version}-py3-none-any.whl");
    let wheel = folder.join(name);
    wheel.is_file().then_some(wheel)
}

/// Runs the bundled uv to set the engine up: what it says goes to the startup screen and
/// the log, and it ends with ZachCap (see end_with_zachcap).
fn run_uv(
    engine: &Engine,
    log: &mut Option<std::fs::File>,
    arguments: impl FnOnce(&mut Command),
) -> Result<(), String> {
    let mut command = Command::new(&engine.launch.uv);
    arguments(&mut command);
    command
        .env_remove("VIRTUAL_ENV")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    no_window(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Couldn't run the bundled uv ({error})."))?;
    let _job = end_with_zachcap(&child);
    let mut last = String::new();
    if let Some(stderr) = child.stderr.take() {
        use std::io::BufRead;
        for line in std::io::BufReader::new(stderr)
            .lines()
            .map_while(Result::ok)
        {
            if let Some(log) = log.as_mut() {
                let _ = writeln!(log, "{line}");
            }
            let line = line.trim();
            if !line.is_empty() {
                last = line.to_string();
                *engine.setup.lock().unwrap() = Some(last.clone());
            }
        }
    }
    let status = child.wait().map_err(|error| error.to_string())?;
    if !status.success() {
        return Err(format!(
            "Setting up the engine didn't finish ({last}). Check the internet connection and press Try again. \
             What happened is in {}.",
            data_dir().join("engine-setup.log").display()
        ));
    }
    Ok(())
}

/// Has a process, and those it starts, end when the job returned is dropped or ZachCap goes,
/// however it goes: Quit, a crash, or the installer closing it. A uv left running would go on
/// downloading, and keep the venv it's working in open. None if Windows won't do it.
#[cfg(windows)]
fn end_with_zachcap(child: &Child) -> Option<std::os::windows::io::OwnedHandle> {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
        SetInformationJobObject,
    };
    // SAFETY: the job's handle is owned from here on; the limits (plain numbers, all zero but
    // one) outlive the call that reads them.
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let job = OwnedHandle::from_raw_handle(job);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let limited = SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            (&raw const limits).cast(),
            size_of_val(&limits) as u32,
        );
        let added = AssignProcessToJobObject(job.as_raw_handle(), child.as_raw_handle());
        (limited != 0 && added != 0).then_some(job)
    }
}

#[cfg(not(windows))]
fn end_with_zachcap(_: &Child) {}

fn no_window(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = command;
}

fn start_engine(launch: &Launch, token: &str, port_file: &Path) -> std::io::Result<Child> {
    let mut command = if launch.installed {
        // Set up already (set_up), so its Python runs the engine directly.
        let mut command = Command::new(
            launch
                .engine
                .join(".venv")
                .join("Scripts")
                .join("python.exe"),
        );
        // Isolated (-I): no current folder, PYTHONPATH or user site-packages ahead of the
        // venv's own code; -u because -I also ignores PYTHONUNBUFFERED.
        command.args(["-I", "-u", "-m", "zachcap.cli"]);
        command.current_dir(&launch.engine);
        command
    } else {
        let mut command = Command::new(&launch.uv);
        command
            .arg("run")
            .arg("--project")
            .arg(&launch.engine)
            .arg("zachcap");
        command
    };
    command
        // The engine binds a port Windows picks and writes it to the port file (wait_for_port).
        .args(["serve", "--port", "0"])
        .env("ZACHCAP_PORT_FILE", port_file)
        // Passed through the environment so the token never shows in process listings.
        .env("ZACHCAP_TOKEN", token)
        // The engine exits by itself if this app goes away without stopping it.
        .env("ZACHCAP_PARENT_PID", std::process::id().to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null());
    // A release build has no console, so the engine's output goes to a log instead. The last
    // run's log is kept as engine.previous.log, so why an engine stopped survives a restart;
    // the new one is opened to append, so trim_log can empty it while the engine writes.
    #[cfg(not(debug_assertions))]
    {
        let folder = data_dir();
        let _ = std::fs::rename(folder.join(LOG), folder.join("engine.previous.log"));
        if let Ok(log) = std::fs::create_dir_all(&folder).and_then(|_| {
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(folder.join(LOG))
        }) && let Ok(copy) = log.try_clone()
        {
            command.stdout(copy).stderr(log);
        }
    }
    no_window(&mut command);
    command.spawn()
}

/// Asks the engine to stop and save a recording in progress, waiting a few seconds at
/// most. Runs as ZachCap exits, which includes Quit, sign-out and shutdown.
fn stop_recording(info: &EngineInfo) {
    let address = SocketAddr::from(([127, 0, 0, 1], info.port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_secs(1)) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let request = format!(
        "POST /api/record/stop HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Content-Length: 0\r\nConnection: close\r\n\r\n",
        info.port, info.token
    );
    if stream.write_all(request.as_bytes()).is_ok() {
        // The engine answers once the audio files are closed.
        let _ = stream.read_to_end(&mut Vec::new());
    }
}

/// GET from the engine, as JSON; None if it isn't answering (or not with a 200). Sent with
/// the host the engine's gate wants (gate.py) and the token; read up to a megabyte.
fn engine_get(info: &EngineInfo, path: &str) -> Option<serde_json::Value> {
    let address = SocketAddr::from(([127, 0, 0, 1], info.port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(500)).ok()?;
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Connection: close\r\n\r\n",
        info.port, info.token
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = Vec::new();
    stream.take(1 << 20).read_to_end(&mut response).ok()?;
    let text = String::from_utf8_lossy(&response);
    let (head, body) = text.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.1 200") {
        return None;
    }
    serde_json::from_str(body).ok()
}

/// GET from the engine running now: its port and token are read on every call, since each
/// start of it has its own (a restart included). None while it starts or isn't answering.
fn ask_engine(engine: &Engine, path: &str) -> Option<serde_json::Value> {
    let info = engine.info.lock().unwrap().clone()?;
    engine_get(&info, path)
}

/// The recording's state, with the call events after the shell's cursor (as the page asks).
fn record_path(cursor: &Option<(String, i64)>) -> String {
    match cursor {
        // The engine's run is hex; anything else is sent as no cursor at all.
        Some((boot, after)) if boot.bytes().all(|byte| byte.is_ascii_alphanumeric()) => {
            format!("/api/record?boot={boot}&after={after}")
        }
        _ => "/api/record".to_string(),
    }
}

/// The call events to tell about after an answer from /api/record, oldest first, and the
/// cursor after them. Unlike the page, the shell takes everything from a run of the engine it
/// hasn't seen as news: it started that run itself, so none of it has been told yet (a
/// restarted engine numbers its events from 1 again).
fn new_events(
    cursor: &Option<(String, i64)>,
    state: &serde_json::Value,
) -> (Option<(String, i64)>, Vec<serde_json::Value>) {
    let Some(boot) = state["boot"].as_str() else {
        return (cursor.clone(), Vec::new());
    };
    let from = match cursor {
        Some((seen, after)) if seen == boot => *after,
        _ => 0,
    };
    let mut fresh: Vec<serde_json::Value> = state["events"]
        .as_array()
        .map(|events| {
            events
                .iter()
                .filter(|event| event["id"].as_i64().is_some_and(|id| id > from))
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    fresh.sort_by_key(|event| event["id"].as_i64());
    let last = fresh
        .last()
        .and_then(|event| event["id"].as_i64())
        .unwrap_or(from);
    (Some((boot.to_string(), last)), fresh)
}

/// What to do with the live panel after a look: open it when a recording with a live
/// transcript starts (unless it was closed for that recording), close it when the recording
/// ends or its live transcript stops (turned off in Settings). None: leave it as it is.
fn panel_after(
    seen: &mut Following,
    recording: bool,
    meeting: Option<i64>,
    live: bool,
) -> Option<bool> {
    let running = recording && live;
    let started = running && !(seen.live && seen.meeting == meeting);
    let action = if started {
        (seen.dismissed != meeting).then_some(true)
    } else if seen.live && !running {
        Some(false)
    } else {
        None
    };
    seen.live = running;
    seen.meeting = if recording { meeting } else { None };
    action
}

/// Opens the live panel (or shows it again) at the top right of the main screen. It doesn't
/// take the keyboard from the call as it opens, and can't be maximized or minimized (it has
/// no taskbar button to come back from).
fn open_live_panel(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(LIVE) {
        let _ = window.unminimize();
        let _ = window.show();
        return;
    }
    let (width, height) = (360.0, 480.0);
    let mut builder =
        tauri::WebviewWindowBuilder::new(app, LIVE, tauri::WebviewUrl::App("index.html".into()))
            .title("ZachCap live")
            .inner_size(width, height)
            .min_inner_size(260.0, 120.0)
            .always_on_top(true)
            .skip_taskbar(true)
            .decorations(false)
            .shadow(true)
            .focused(false)
            .content_protected(true)
            .resizable(true)
            .maximizable(false)
            .minimizable(false);
    if let Ok(Some(monitor)) = app.primary_monitor() {
        let scale = monitor.scale_factor();
        let area = monitor.work_area();
        let right = (f64::from(area.position.x) + f64::from(area.size.width)) / scale;
        let top = f64::from(area.position.y) / scale;
        builder = builder.position(right - width - 16.0, top + 16.0);
    }
    let _ = builder.build();
}

fn close_live_panel(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(LIVE) {
        let _ = window.destroy();
    }
}

/// Shows the live panel again after it was closed (the recording bar's button). Async: on
/// Windows, a window made in a command that isn't can hang.
#[tauri::command]
async fn show_live_panel(app: AppHandle) {
    if let Some(follow) = app.try_state::<Follow>() {
        follow.0.lock().unwrap().dismissed = None;
    }
    open_live_panel(&app);
}

/// Whether the main window is where the user is looking: shown, not minimized, and focused
/// (as the page's windowInFront has it). Not while the live panel has the focus.
fn main_in_front(app: &AppHandle) -> bool {
    app.get_webview_window("main").is_some_and(|window| {
        window.is_visible().unwrap_or(false)
            && !window.is_minimized().unwrap_or(true)
            && window.is_focused().unwrap_or(false)
    })
}

/// A Windows notification for what the engine did on its own about a call: the events the
/// page says with a toast when its window is in front (the others it only shows there).
fn tell_about_call(app: &AppHandle, event: &serde_json::Value) {
    let name = event["app"].as_str().unwrap_or("The call app");
    let (title, body) = match event["kind"].as_str() {
        Some("started") => (
            format!("Recording your {name} call"),
            "It stops when the call ends. Not one to keep? Press Don't record in ZachCap."
                .to_string(),
        ),
        Some("stopped") => (
            "Recording saved".to_string(),
            if event["reason"].as_str() == Some("quiet") {
                "It stopped after ten quiet minutes. Transcribing and finding speakers..."
            } else {
                "It stopped when the call ended. Transcribing and finding speakers..."
            }
            .to_string(),
        ),
        Some("failed") => (
            format!("Couldn't record the {name} call"),
            event["detail"].as_str().unwrap_or_default().to_string(),
        ),
        _ => return,
    };
    use tauri_plugin_notification::NotificationExt;
    let _ = app.notification().builder().title(title).body(body).show();
}

/// Once a second: opens the live panel when a recording with a live transcript starts and
/// closes it when that ends, and tells about calls recorded or stopped on their own with a
/// Windows notification when the main window isn't in front (the page shows a toast when it
/// is). The events come after a cursor of the shell's own, so none is told twice, or lost to
/// a restart of the engine.
fn follow_engine(app: AppHandle) {
    std::thread::spawn(move || {
        while !EXITING.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_secs(1));
            let (Some(engine), Some(follow)) =
                (app.try_state::<Engine>(), app.try_state::<Follow>())
            else {
                continue;
            };
            let path = record_path(&follow.0.lock().unwrap().events);
            let Some(state) = ask_engine(&engine, &path) else {
                continue;
            };
            let recording = state["recording"].as_bool().unwrap_or(false);
            let meeting = state["meeting_id"].as_i64();
            let live = state["live"].as_bool().unwrap_or(false);
            let (panel, fresh) = {
                let mut seen = follow.0.lock().unwrap();
                let panel = panel_after(&mut seen, recording, meeting, live);
                let (cursor, fresh) = new_events(&seen.events, &state);
                seen.events = cursor;
                (panel, fresh)
            };
            match panel {
                Some(true) => open_live_panel(&app),
                Some(false) => close_live_panel(&app),
                None => {}
            }
            if !fresh.is_empty() && !main_in_front(&app) {
                for event in &fresh {
                    tell_about_call(&app, event);
                }
            }
        }
    });
}

/// How long after it stopped by itself the engine is started again, each time within
/// REVIVE_WITHIN; after the last it's left to the Restart button.
const REVIVE_AFTER: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(5),
    Duration::from_secs(30),
];
const REVIVE_WITHIN: Duration = Duration::from_secs(3600);

/// Starts the engine again when it stops by itself (a crash) after having run. The engine
/// keeps the recording that was cut off, and records the rest of a call app's call. One that
/// doesn't start isn't tried again, and neither is one that keeps stopping.
#[derive(Default)]
struct Revival {
    /// It ran at the last look.
    running: bool,
    /// It has said its port since it was started.
    ran: bool,
    /// It was recording when last seen running.
    recording: bool,
    /// When to start it again.
    due: Option<std::time::Instant>,
    /// Started again by itself, and hasn't said its port yet; whether it had been recording.
    reviving: Option<bool>,
    /// When it was started again by itself.
    tries: Vec<std::time::Instant>,
}

/// What a look at the engine finds to do (Revival::look).
#[derive(Debug, PartialEq)]
enum Found {
    Nothing,
    /// It stopped, and will be started again by itself or not.
    Stopped {
        restarting: bool,
    },
    /// It's time to start it again.
    Start,
    /// It was started again by itself, and runs.
    Back {
        recording: bool,
    },
}

impl Revival {
    /// Once a second: `running` (its process is there), `ready` (it has said its port),
    /// `starting` (a start is under way, by itself or with a button), `recording` (as last
    /// seen).
    fn look(
        &mut self,
        running: bool,
        ready: bool,
        starting: bool,
        recording: bool,
        now: std::time::Instant,
    ) -> Found {
        let stopped = self.running && !running;
        self.running = running;
        if running || starting {
            self.due = None; // whoever started it
            if !(running && ready) {
                return Found::Nothing;
            }
            self.ran = true;
            self.recording = recording;
            return match self.reviving.take() {
                Some(recording) => Found::Back { recording },
                None => Found::Nothing,
            };
        }
        if std::mem::take(&mut self.ran) {
            self.tries
                .retain(|&tried| now.duration_since(tried) < REVIVE_WITHIN);
            self.due = REVIVE_AFTER.get(self.tries.len()).map(|&wait| now + wait);
            return Found::Stopped {
                restarting: self.due.is_some(),
            };
        }
        if stopped || self.reviving.take().is_some() {
            self.reviving = None; // it never got as far as saying its port
            return Found::Stopped { restarting: false };
        }
        if self.due.is_some_and(|due| now >= due) {
            self.due = None;
            self.tries.push(now);
            self.reviving = Some(self.recording);
            return Found::Start;
        }
        Found::Nothing
    }
}

/// What the window is told when the engine stopped by itself.
#[derive(Clone, Serialize)]
struct EngineExit {
    /// Why, and where its log is.
    message: String,
    /// It's being started again by itself.
    restarting: bool,
    /// It was recording.
    recording: bool,
}

/// Once a second: notices that the engine stopped by itself (a crash), says so in the window
/// (or a Windows notification when that isn't in front), and starts it again.
fn watch_engine(app: &AppHandle, revival: &mut Revival) {
    let (Some(engine), Some(follow)) = (app.try_state::<Engine>(), app.try_state::<Follow>())
    else {
        return;
    };
    check_engine(&engine);
    let running = engine.child.lock().unwrap().is_some();
    let ready = engine.info.lock().unwrap().is_some();
    let starting = engine.starting.load(Ordering::SeqCst);
    let recording = follow.0.lock().unwrap().meeting.is_some();
    let found = revival.look(
        running,
        ready,
        starting,
        recording,
        std::time::Instant::now(),
    );
    if EXITING.load(Ordering::Relaxed) {
        return;
    }
    match found {
        Found::Nothing => {}
        Found::Stopped { restarting } => {
            let message = engine.stopped.lock().unwrap().clone().unwrap_or_default();
            let recording = revival.recording;
            let _ = app.emit(
                "engine-exited",
                EngineExit {
                    message,
                    restarting,
                    recording,
                },
            );
            if !restarting {
                tell_about_engine(
                    app,
                    "ZachCap's engine stopped",
                    "Nothing is recorded until it runs again: open ZachCap to restart it.",
                );
            }
        }
        Found::Start => begin(app.clone()),
        Found::Back { recording } => {
            let _ = app.emit("engine-restarted", recording);
            tell_about_engine(
                app,
                "ZachCap's engine stopped and was started again",
                if recording {
                    "What it had recorded is kept, and a call in Teams or another call app goes on \
                     in a new recording. Check that it's recording."
                } else {
                    "It wasn't recording."
                },
            );
        }
    }
}

/// A Windows notification about the engine, when the main window isn't in front to show it.
fn tell_about_engine(app: &AppHandle, title: &str, body: &str) {
    if main_in_front(app) {
        return;
    }
    use tauri_plugin_notification::NotificationExt;
    let _ = app.notification().builder().title(title).body(body).show();
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// The first time the window is closed, says where ZachCap went. A marker file in the
/// app's local data folder keeps it to once.
fn tell_about_tray(app: &AppHandle) {
    static TOLD: AtomicBool = AtomicBool::new(false);
    if TOLD.swap(true, Ordering::Relaxed) {
        return;
    }
    let Ok(folder) = app.path().app_local_data_dir() else {
        return;
    };
    let marker = folder.join("tray-hint-shown");
    if marker.exists()
        || std::fs::create_dir_all(&folder)
            .and_then(|_| std::fs::write(&marker, b""))
            .is_err()
    {
        return;
    }
    use tauri_plugin_notification::NotificationExt;
    let _ = app
        .notification()
        .builder()
        .title("ZachCap is still running")
        .body("It's in the tray, ready to record. Quit from the tray icon's menu.")
        .show();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();
    #[cfg(desktop)]
    {
        // A second launch (Start menu, shortcut) shows the running copy instead of starting
        // another engine and tray icon. The plugin has to be registered first.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app)
        }));
        // Release builds only: a debug build started at sign-in would load the dev server,
        // which isn't running then.
        #[cfg(not(debug_assertions))]
        {
            builder = builder.plugin(
                tauri_plugin_autostart::Builder::new()
                    .app_name("ZachCap")
                    .arg(HIDDEN_ARG)
                    .build(),
            );
        }
    }
    builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            // Started on its own thread (an installed copy's first launch sets the engine up
            // for minutes); the startup screen shows how that goes, or what went wrong.
            app.manage(Engine {
                info: Mutex::new(None),
                launch: launch_for(app.handle()),
                child: Mutex::new(None),
                stopped: Mutex::new(None),
                starting: AtomicBool::new(false),
                setup: Mutex::new(None),
            });
            begin(app.handle().clone());
            app.manage(Follow(Mutex::new(Following::default())));
            follow_engine(app.handle().clone());

            // Notice when the engine stops later (a crash), and start it again; and keep its
            // log from growing without end (ZachCap runs for weeks in the tray).
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut seconds: u64 = 0;
                let mut revival = Revival::default();
                while !EXITING.load(Ordering::Relaxed) {
                    std::thread::sleep(Duration::from_secs(1));
                    seconds += 1;
                    watch_engine(&handle, &mut revival);
                    if seconds % 60 == 0 {
                        trim_log(&data_dir().join(LOG), LOG_LIMIT);
                    }
                }
            });

            // The global shortcuts (see KeyState): start or stop recording from anywhere;
            // while recording, bookmark the moment and snapshot the call; hold to dictate.
            // Handed to the page like the tray menu, so the window's state stays in sync.
            app.manage(Keys(Mutex::new(KeyState {
                keys: saved_keys(),
                ..KeyState::default()
            })));
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::ShortcutState;
                app.handle().plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            let pressed = event.state() == ShortcutState::Pressed;
                            let event = match action_for(app, shortcut) {
                                // Dictation listens while the keys are held: down starts, up
                                // stops. On Windows its keys come from the hook instead.
                                #[cfg(not(windows))]
                                Some("dictate") if pressed => "dictate-start",
                                #[cfg(not(windows))]
                                Some("dictate") => "dictate-stop",
                                Some("record") if pressed => "toggle-recording",
                                Some("bookmark") if pressed => "bookmark",
                                Some("snapshot") if pressed => "snapshot",
                                _ => return,
                            };
                            let _ = app.emit(event, ());
                        })
                        .build(),
                )?;
                #[cfg(windows)]
                {
                    let handle = app.handle().clone();
                    dictation_keys::on_change(move |down| {
                        let _ = handle.emit(
                            if down {
                                "dictate-start"
                            } else {
                                "dictate-stop"
                            },
                            (),
                        );
                    });
                }
                sync_keys(app.handle(), Vec::new(), false);
            }

            let record =
                MenuItem::with_id(app, "record", "Start or stop recording", true, None::<&str>)?;
            let mark =
                MenuItem::with_id(app, "bookmark", "Bookmark this moment", true, None::<&str>)?;
            let shot = MenuItem::with_id(app, "snapshot", "Snapshot the call", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open ZachCap", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit ZachCap", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&record, &mark, &shot, &open, &separator, &quit])?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().expect("app icon"))
                .tooltip("ZachCap")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    // The page does the work, so the window's state stays in sync.
                    "record" => {
                        let _ = app.emit("toggle-recording", ());
                    }
                    "bookmark" => {
                        let _ = app.emit("bookmark", ());
                    }
                    "snapshot" => {
                        let _ = app.emit("snapshot", ());
                    }
                    "open" => show_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // The window starts hidden (tauri.conf.json); a sign-in launch leaves it in the tray.
            if !std::env::args().any(|arg| arg == HIDDEN_ARG) {
                show_main(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                if window.label() == LIVE {
                    // Closed for this recording; the recording bar's button brings it back.
                    if let Some(follow) = window.app_handle().try_state::<Follow>() {
                        let mut seen = follow.0.lock().unwrap();
                        seen.dismissed = seen.meeting;
                    }
                } else {
                    // Closing the window keeps ZachCap (and any recording) running in the tray.
                    tell_about_tray(window.app_handle());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            engine_info,
            engine_setup,
            engine_exited,
            restart_engine,
            open_data_folder,
            set_recording_indicator,
            set_dictation,
            set_shortcuts,
            shortcut_status,
            pause_shortcuts,
            show_live_panel
        ])
        .build(tauri::generate_context!())
        .expect("error while building ZachCap")
        .run(|app, event| {
            if let RunEvent::Exit = event
                && let Some(engine) = app.try_state::<Engine>()
            {
                EXITING.store(true, Ordering::Relaxed);
                if let Some(info) = engine.info.lock().unwrap().clone() {
                    stop_recording(&info);
                }
                if let Some(mut child) = engine.child.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    /// One request answered by a pretend engine: what it was asked, and the reply.
    fn answer_once(
        token: &str,
        reply: &'static str,
    ) -> (EngineInfo, std::thread::JoinHandle<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let asked = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let read = stream.read(&mut request).unwrap();
            stream.write_all(reply.as_bytes()).unwrap();
            String::from_utf8_lossy(&request[..read]).into_owned()
        });
        let info = EngineInfo {
            port,
            token: token.into(),
        };
        (info, asked)
    }

    /// A 200 with that JSON, as the engine sends it.
    fn json_reply(body: &str) -> &'static str {
        Box::leak(
            format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
                body.len()
            )
            .into_boxed_str(),
        )
    }

    fn engine(info: Option<EngineInfo>) -> Engine {
        Engine {
            info: Mutex::new(info),
            launch: Launch {
                uv: PathBuf::from("uv"),
                engine: PathBuf::from("engine"),
                installed: false,
            },
            child: Mutex::new(None),
            stopped: Mutex::new(None),
            starting: AtomicBool::new(false),
            setup: Mutex::new(None),
        }
    }

    #[test]
    fn the_engine_is_asked_with_its_host_and_token_and_its_json_read() {
        let reply = json_reply(r#"{"recording": true, "meeting_id": 7, "live": true}"#);
        let (info, asked) = answer_once("secret", reply);
        let state = engine_get(&info, "/api/record").expect("an answer");
        assert_eq!(state["meeting_id"].as_i64(), Some(7));
        let request = asked.join().unwrap();
        assert!(request.starts_with("GET /api/record HTTP/1.1\r\n"));
        // The engine's gate refuses any other host (a page rebinding a name of its own).
        assert!(request.contains(&format!("\r\nHost: 127.0.0.1:{}\r\n", info.port)));
        assert!(request.contains("\r\nAuthorization: Bearer secret\r\n"));

        let refused = "HTTP/1.1 401 Unauthorized\r\ncontent-length: 2\r\n\r\n{}";
        let (info, asked) = answer_once("secret", refused);
        assert!(engine_get(&info, "/api/record").is_none());
        asked.join().unwrap();
    }

    #[test]
    fn the_engine_running_now_is_asked_even_after_a_restart() {
        // While it starts there's no port yet, and nothing is asked.
        let engine = engine(None);
        assert!(ask_engine(&engine, "/api/record").is_none());

        let (first, asked) = answer_once("first", json_reply(r#"{"run": 1}"#));
        *engine.info.lock().unwrap() = Some(first);
        assert_eq!(ask_engine(&engine, "/api/record").unwrap()["run"], 1);
        assert!(asked.join().unwrap().contains("Bearer first"));

        // Started again: a new port and a new token, read at the next ask.
        let (second, asked) = answer_once("second", json_reply(r#"{"run": 2}"#));
        let port = second.port;
        *engine.info.lock().unwrap() = Some(second);
        assert_eq!(ask_engine(&engine, "/api/record").unwrap()["run"], 2);
        let request = asked.join().unwrap();
        assert!(request.contains("Bearer second"));
        assert!(request.contains(&format!("Host: 127.0.0.1:{port}")));
    }

    fn event(id: i64, kind: &str) -> serde_json::Value {
        serde_json::json!({"id": id, "kind": kind, "app": "Microsoft Teams", "meeting_id": id})
    }

    fn ids(events: &[serde_json::Value]) -> Vec<i64> {
        events
            .iter()
            .map(|event| event["id"].as_i64().unwrap())
            .collect()
    }

    #[test]
    fn call_events_are_told_once_each_in_order_and_all_after_a_restart() {
        // The first answer from a run the shell started: everything in it is news.
        let state =
            serde_json::json!({"boot": "a1", "events": [event(2, "stopped"), event(1, "started")]});
        let (cursor, fresh) = new_events(&None, &state);
        assert_eq!(ids(&fresh), [1, 2]);
        assert_eq!(cursor, Some(("a1".to_string(), 2)));
        assert_eq!(record_path(&cursor), "/api/record?boot=a1&after=2");

        // Asked from the cursor on: an event already told is never told again.
        let state =
            serde_json::json!({"boot": "a1", "events": [event(2, "stopped"), event(3, "failed")]});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert_eq!(ids(&fresh), [3]);
        let state = serde_json::json!({"boot": "a1", "events": []});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert!(fresh.is_empty());
        assert_eq!(cursor, Some(("a1".to_string(), 3)));

        // The engine started again numbers its events from 1: all of them are news.
        let state = serde_json::json!({"boot": "b2", "events": [event(1, "started")]});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert_eq!(ids(&fresh), [1]);
        assert_eq!(cursor, Some(("b2".to_string(), 1)));

        // An answer without a run changes nothing; a run that isn't hex isn't sent back.
        let (same, fresh) = new_events(&cursor, &serde_json::json!({"recording": false}));
        assert!(fresh.is_empty() && same == cursor);
        assert_eq!(record_path(&Some(("a&b".to_string(), 3))), "/api/record");
        assert_eq!(record_path(&None), "/api/record");
    }

    #[test]
    fn the_live_panel_opens_once_a_recording_and_closes_when_it_ends() {
        let mut seen = Following::default();
        assert_eq!(panel_after(&mut seen, false, None, false), None);
        assert_eq!(panel_after(&mut seen, true, Some(7), true), Some(true));
        assert_eq!(panel_after(&mut seen, true, Some(7), true), None);
        // Closed with its X: not opened again for this recording...
        seen.dismissed = seen.meeting;
        assert_eq!(panel_after(&mut seen, true, Some(7), true), None);
        assert_eq!(panel_after(&mut seen, false, None, false), Some(false));
        // ...but for the next one.
        assert_eq!(panel_after(&mut seen, true, Some(8), true), Some(true));
        // A Stop and a Start between two looks: the next recording's, at once.
        assert_eq!(panel_after(&mut seen, true, Some(9), true), Some(true));
        // Turned off in Settings mid-call: it closes; a recording without one never opens it.
        assert_eq!(panel_after(&mut seen, true, Some(9), false), Some(false));
        assert_eq!(panel_after(&mut seen, false, None, false), None);
        assert_eq!(panel_after(&mut seen, true, Some(10), false), None);
    }

    /// Looks at an engine once a second, as watch_engine does.
    struct Watched {
        revival: Revival,
        start: std::time::Instant,
        seconds: u64,
    }

    impl Watched {
        fn new() -> Self {
            let start = std::time::Instant::now();
            Watched {
                revival: Revival::default(),
                start,
                seconds: 0,
            }
        }

        fn look(&mut self, running: bool, ready: bool, starting: bool, recording: bool) -> Found {
            self.seconds += 1;
            let now = self.start + Duration::from_secs(self.seconds);
            self.revival.look(running, ready, starting, recording, now)
        }

        /// Runs and then crashes: whether it's started again by itself, and after how long.
        fn crash(&mut self, recording: bool) -> Option<u64> {
            assert_eq!(self.look(true, true, false, recording), Found::Nothing);
            let found = self.look(false, true, false, recording); // its port is still known
            if found == (Found::Stopped { restarting: false }) {
                assert_eq!(self.look(false, true, false, recording), Found::Nothing);
                return None;
            }
            assert_eq!(found, Found::Stopped { restarting: true });
            let stopped = self.seconds;
            loop {
                match self.look(false, true, false, recording) {
                    Found::Nothing => assert!(self.seconds < stopped + 100),
                    Found::Start => return Some(self.seconds - stopped),
                    other => panic!("{other:?}"),
                }
            }
        }

        fn comes_back(&mut self, recording: bool) {
            assert_eq!(self.look(false, false, true, recording), Found::Nothing);
            assert_eq!(self.look(true, false, true, recording), Found::Nothing);
            let back = Found::Back { recording };
            // What it was doing when it stopped, whatever it does now.
            assert_eq!(self.look(true, true, false, !recording), back);
            assert_eq!(self.look(true, true, false, recording), Found::Nothing);
        }
    }

    #[test]
    fn an_engine_that_crashes_is_started_again_a_few_times_with_longer_waits() {
        let mut watched = Watched::new();
        for (recording, wait) in [(true, 1), (false, 5), (true, 30)] {
            assert_eq!(watched.crash(recording), Some(wait));
            watched.comes_back(recording);
        }
        assert_eq!(watched.crash(true), None); // it keeps stopping: the button, then
        // Restarted with the button, it runs for hours: the next crash is a first one again.
        assert_eq!(watched.look(true, false, true, false), Found::Nothing);
        assert_eq!(watched.look(true, true, false, false), Found::Nothing);
        watched.seconds += 2 * 3600;
        assert_eq!(watched.crash(false), Some(1));
    }

    #[test]
    fn an_engine_that_cant_start_isnt_tried_again_and_again() {
        // Never said its port: the startup screen has it (Try again), and the setup its chance.
        let mut watched = Watched::new();
        assert_eq!(watched.look(false, false, true, false), Found::Nothing);
        assert_eq!(watched.look(true, false, true, false), Found::Nothing);
        let stopped = Found::Stopped { restarting: false };
        assert_eq!(watched.look(false, false, false, false), stopped);
        assert_eq!(watched.look(false, false, false, false), Found::Nothing);

        // Started again after a crash, and then it doesn't start.
        let mut watched = Watched::new();
        assert_eq!(watched.crash(true), Some(1));
        assert_eq!(watched.look(true, false, true, true), Found::Nothing);
        assert_eq!(watched.look(false, false, false, true), stopped);
        assert_eq!(watched.look(false, false, false, true), Found::Nothing);
        // Or there isn't even a process (the setup failed).
        let mut watched = Watched::new();
        assert_eq!(watched.crash(true), Some(1));
        assert_eq!(watched.look(false, false, false, true), stopped);
        assert_eq!(watched.look(false, false, false, true), Found::Nothing);
    }

    #[test]
    fn a_restart_with_the_button_comes_first() {
        let mut watched = Watched::new();
        assert_eq!(watched.look(true, true, false, true), Found::Nothing);
        let stopped = Found::Stopped { restarting: true };
        assert_eq!(watched.look(false, true, false, true), stopped);
        assert_eq!(watched.look(false, false, true, true), Found::Nothing); // pressed
        assert_eq!(watched.look(true, false, true, true), Found::Nothing);
        assert_eq!(watched.look(true, true, false, true), Found::Nothing);
        for _ in 0..40 {
            assert_eq!(watched.look(true, true, false, true), Found::Nothing);
        }
    }

    #[test]
    fn a_port_is_read_only_once_the_engine_has_written_it_whole() {
        let dir = std::env::temp_dir().join(format!("zachcap-port-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("engine.port");
        assert_eq!(read_port(&file), None, "not written yet");
        for (text, port) in [
            ("", None),
            ("0", None),
            ("70000", None),
            ("5432x", None),
            (
                "54321
",
                Some(54321),
            ),
        ] {
            std::fs::write(&file, text).unwrap();
            assert_eq!(read_port(&file), port, "{text:?}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn setup_takes_the_wheel_of_this_builds_version() {
        let dir = std::env::temp_dir().join(format!("zachcap-wheel-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // An update that didn't uninstall first: the older wheel is still there, listed first.
        for version in ["0.2.0", "0.3.0"] {
            std::fs::write(dir.join(format!("zachcap-{version}-py3-none-any.whl")), b"").unwrap();
        }
        let wheel = wheel_for(&dir, "0.3.0 2970921 2026-11-24T14:28:40Z").unwrap();
        assert!(
            wheel.ends_with("zachcap-0.3.0-py3-none-any.whl"),
            "{wheel:?}"
        );
        assert_eq!(wheel_for(&dir, "0.4.0 abc1234 2026-12-01T00:00:00Z"), None);
        assert_eq!(wheel_for(&dir, ""), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn every_manifest_has_the_same_version() {
        // Setup takes the wheel of tauri.conf.json's version (the BUILD stamp's), which is built
        // with pyproject.toml's, and needs uv.lock to go with that.
        let json = |text: &str| -> serde_json::Value { serde_json::from_str(text).unwrap() };
        let config = json(include_str!("../tauri.conf.json"));
        let version = config["version"].as_str().unwrap();
        assert_eq!(env!("CARGO_PKG_VERSION"), version, "Cargo.toml");
        let page = json(include_str!("../../package.json"));
        assert_eq!(page["version"], version, "package.json");
        let engine = format!("name = \"zachcap\"\nversion = \"{version}\"\n");
        for (name, text) in [
            (
                "pyproject.toml",
                include_str!("../../../../engine/pyproject.toml"),
            ),
            ("uv.lock", include_str!("../../../../engine/uv.lock")),
        ] {
            let text = text.replace("\r\n", "\n");
            assert!(text.contains(&engine), "{name} isn't at {version}");
        }
    }

    /// A folder for one test, gone after it.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let name = format!("zachcap-{name}-test-{}", std::process::id());
            let dir = std::env::temp_dir().join(name);
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            Scratch(dir)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    const BUILD: &str = "0.3.0 2970921 2026-11-24T14:28:40Z";
    const OLDER_BUILD: &str = "0.2.0 a297271 2026-11-24T12:28:06Z";
    /// The package in the venv of `installed`: its file, and what uv goes by.
    const FILE: &str = r"Lib\site-packages\library.py";
    const DIST_INFO: &str = r"Lib\site-packages\library-1.0.dist-info";

    /// An installed copy in `dir`, its venv marked like that, with a stand-in for uv.
    fn installed(dir: &Path, mark: Option<&str>) -> Engine {
        let folder = dir.join("engine");
        let venv = folder.join(".venv");
        std::fs::create_dir_all(folder.join("wheel")).unwrap();
        std::fs::write(folder.join(r"wheel\zachcap-0.3.0-py3-none-any.whl"), b"").unwrap();
        std::fs::write(folder.join("BUILD"), BUILD).unwrap();
        std::fs::create_dir_all(venv.join(DIST_INFO)).unwrap();
        std::fs::write(venv.join(FILE), b"").unwrap();
        std::fs::write(
            venv.join(DIST_INFO).join("RECORD"),
            "library.py,sha256=x,0\nlibrary-1.0.dist-info/RECORD,,\n",
        )
        .unwrap();
        std::fs::write(venv.join("pyvenv.cfg"), b"").unwrap();
        if let Some(mark) = mark {
            std::fs::write(venv.join(SET_UP), mark).unwrap();
        }
        uv_syncs(dir, &[]);
        let mut engine = engine(None);
        engine.launch = Launch {
            uv: dir.join("uv.cmd"),
            engine: folder,
            installed: true,
        };
        engine
    }

    /// Has the stand-in for uv note what it's asked and, for a sync, run these lines (the
    /// venv's packages are in %packages%), then make the venv if there's none.
    fn uv_syncs(dir: &Path, lines: &[&str]) {
        let mut batch = vec![
            "@echo off",
            r#">>"%~dp0asked.txt" echo %*"#,
            r#"if not "%~1"=="sync" exit /b 0"#,
            r#"set packages=%UV_PROJECT_ENVIRONMENT%\Lib\site-packages"#,
        ];
        batch.extend(lines);
        batch.push(r#"if not exist "%packages%" mkdir "%packages%""#);
        batch.push(r#">"%UV_PROJECT_ENVIRONMENT%\pyvenv.cfg" echo home"#);
        std::fs::write(dir.join("uv.cmd"), batch.join("\r\n") + "\r\n").unwrap();
    }

    /// What the stand-in for uv was asked so far, a line each time.
    fn asked(dir: &Path) -> Vec<String> {
        let text = std::fs::read_to_string(dir.join("asked.txt")).unwrap_or_default();
        text.lines().map(str::to_string).collect()
    }

    fn mark_of(engine: &Engine) -> Option<String> {
        std::fs::read_to_string(engine.launch.engine.join(".venv").join(SET_UP)).ok()
    }

    #[test]
    fn the_mark_says_what_a_venv_needs() {
        let dir = Scratch::new("marks");
        let venv = dir.0.join("venv");
        assert_eq!(setup_for(&venv, BUILD), Setup::Fresh, "there's none");
        for (mark, needs) in [
            (Some(BUILD), Setup::Done),
            (Some(OLDER_BUILD), Setup::Update),
            (None, Setup::Fresh),
            // Left by a power cut.
            (Some(""), Setup::Fresh),
            (Some("\0\0\0\0\0\0\0\0"), Setup::Fresh),
        ] {
            let _ = std::fs::remove_dir_all(&venv);
            std::fs::create_dir_all(&venv).unwrap();
            if let Some(mark) = mark {
                std::fs::write(venv.join(SET_UP), mark).unwrap();
            }
            assert_eq!(setup_for(&venv, BUILD), Setup::Fresh, "half removed");
            std::fs::write(venv.join("pyvenv.cfg"), b"").unwrap();
            assert_eq!(setup_for(&venv, BUILD), needs, "{mark:?}");
        }
        // One that can't be read just now is there, so the venv is kept.
        use std::os::windows::fs::OpenOptionsExt;
        let mut alone = std::fs::OpenOptions::new();
        let alone = alone.write(true).create(true).truncate(true).share_mode(0);
        let held = alone.open(venv.join(SET_UP)).unwrap();
        assert_eq!(setup_for(&venv, BUILD), Setup::Update);
        drop(held);
    }

    #[test]
    fn a_venv_without_its_mark_is_built_from_scratch() {
        let dir = Scratch::new("fresh");
        let engine = installed(&dir.0, None);
        let venv = engine.launch.engine.join(".venv");
        // An antivirus took it; to uv, the package is still installed.
        std::fs::remove_file(venv.join(FILE)).unwrap();
        set_up(&engine, &dir.0).unwrap();
        assert!(venv.join("pyvenv.cfg").is_file());
        assert!(!venv.join(DIST_INFO).exists());
        assert!(!is_there(&engine.launch.engine.join(OLD_VENVS)));
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));
        let calls = asked(&dir.0);
        assert_eq!(calls.len(), 2, "{calls:?}");
        assert!(calls[0].starts_with("sync "), "{calls:?}");
        assert!(calls[1].starts_with("pip install "), "{calls:?}");
        // ZachCap's own wheel as it is, not as uv's cache has it.
        assert!(calls[1].contains(" --no-cache "), "{calls:?}");

        // Set up: nothing more to ask uv.
        set_up(&engine, &dir.0).unwrap();
        assert_eq!(asked(&dir.0).len(), 2);
    }

    #[test]
    fn a_replacement_cut_short_is_finished_by_the_next_start() {
        let dir = Scratch::new("cut-short");
        let engine = installed(&dir.0, None);
        let venv = engine.launch.engine.join(".venv");
        // With the old venv moved away, and not yet deleted.
        set_aside(&venv, Duration::ZERO).unwrap();
        assert!(!is_there(&venv));
        set_up(&engine, &dir.0).unwrap();
        assert!(!is_there(&engine.launch.engine.join(OLD_VENVS)));
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));

        // With the new one half built: uv is ended (Quit, a power cut) before it's marked.
        unmark(&engine.launch);
        uv_syncs(&dir.0, &["exit /b 2"]);
        assert!(set_up(&engine, &dir.0).is_err());
        assert_eq!(mark_of(&engine), None);
        uv_syncs(&dir.0, &[]);
        set_up(&engine, &dir.0).unwrap();
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));
    }

    #[test]
    fn an_update_keeps_the_venv() {
        let dir = Scratch::new("update");
        let engine = installed(&dir.0, Some(OLDER_BUILD));
        // While uv changes it there's no mark: cut short, it's built from scratch next time.
        uv_syncs(
            &dir.0,
            &[r#"if exist "%UV_PROJECT_ENVIRONMENT%\zachcap-build.txt" exit /b 2"#],
        );
        set_up(&engine, &dir.0).unwrap();
        assert!(engine.launch.engine.join(".venv").join(FILE).is_file());
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));
    }

    #[test]
    fn an_update_that_failed_starts_over_unless_nothing_was_changed() {
        let dir = Scratch::new("failed-update");
        let engine = installed(&dir.0, Some(OLDER_BUILD));
        let venv = engine.launch.engine.join(".venv");
        // No internet.
        uv_syncs(&dir.0, &["exit /b 2"]);
        assert!(set_up(&engine, &dir.0).is_err());
        assert!(venv.join(FILE).is_file());
        assert_eq!(mark_of(&engine).as_deref(), Some(OLDER_BUILD));

        for changed in [
            r#"rmdir /s /q "%packages%\library-1.0.dist-info""#,
            r#"del "%packages%\library.py""#,
            r#"mkdir "%packages%\other-1.0.dist-info""#,
        ] {
            let engine = installed(&dir.0, Some(OLDER_BUILD));
            uv_syncs(&dir.0, &[changed, "exit /b 2"]);
            assert!(set_up(&engine, &dir.0).is_err());
            assert_eq!(mark_of(&engine), None, "{changed}");
            uv_syncs(&dir.0, &[]);
            set_up(&engine, &dir.0).unwrap();
            assert!(!venv.join(FILE).exists());
            assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));
        }
    }

    #[test]
    fn nothing_is_cleared_when_the_wheel_is_missing() {
        let dir = Scratch::new("no-wheel");
        let engine = installed(&dir.0, None);
        std::fs::remove_dir_all(engine.launch.engine.join("wheel")).unwrap();
        assert!(set_up(&engine, &dir.0).unwrap_err().contains("no wheel"));
        assert!(engine.launch.engine.join(".venv").join(FILE).is_file());
        assert!(asked(&dir.0).is_empty());
    }

    #[test]
    fn what_is_damaged_in_uvs_cache_is_downloaded_again() {
        let dir = Scratch::new("damaged");
        let engine = installed(&dir.0, None);
        let lost = [
            r#"if not exist "%packages%\lost-1.0.dist-info" mkdir "%packages%\lost-1.0.dist-info""#,
            r#">"%packages%\lost-1.0.dist-info\RECORD" echo lost/lost.py,sha256=x,5"#,
        ];
        uv_syncs(&dir.0, &lost);
        set_up(&engine, &dir.0).unwrap();
        let calls = asked(&dir.0);
        assert_eq!(calls.len(), 3, "{calls:?}");
        assert!(!calls[0].contains("--no-cache"), "{calls:?}");
        assert!(
            calls[1].ends_with(" --no-cache --reinstall-package lost"),
            "{calls:?}"
        );

        // Not at a first setup: the cache has just been filled.
        std::fs::remove_dir_all(engine.launch.engine.join(".venv")).unwrap();
        set_up(&engine, &dir.0).unwrap();
        assert_eq!(asked(&dir.0).len(), 5);
    }

    #[test]
    fn a_package_is_damaged_when_a_file_is_missing_or_of_another_size() {
        let dir = Scratch::new("record");
        let packages = dir.0.join(r"Lib\site-packages");
        for (package, record) in [
            (
                "whole_one-1.0",
                "whole_one/a.py,sha256=x,3\r\n\"b,c.py\",sha256=x,0\r\n\
                 ../../Scripts/whole.exe,sha256=x,2\r\nunsized.pyc,,\r\n\
                 whole_one-1.0.dist-info/RECORD,,\r\n",
            ),
            ("missing-2.0.1", "missing.py,sha256=x,1\n"),
            ("resized-3.0", "whole_one/a.py,sha256=x,4\n"),
        ] {
            let info = packages.join(format!("{package}.dist-info"));
            std::fs::create_dir_all(&info).unwrap();
            std::fs::write(info.join("RECORD"), record).unwrap();
        }
        // uv was ended before it wrote a RECORD.
        std::fs::create_dir_all(packages.join("half-4.0.dist-info")).unwrap();
        std::fs::create_dir_all(packages.join("whole_one")).unwrap();
        std::fs::create_dir_all(dir.0.join("Scripts")).unwrap();
        std::fs::write(packages.join(r"whole_one\a.py"), b"abc").unwrap();
        std::fs::write(packages.join("b,c.py"), b"").unwrap();
        std::fs::write(dir.0.join(r"Scripts\whole.exe"), b"MZ").unwrap();
        let damaged = ["half", "missing", "resized"];
        assert_eq!(damaged_packages(&dir.0), damaged);
        // As the app has it, with \\?\ in front (where a / in a path isn't a separator).
        assert_eq!(damaged_packages(&dir.0.canonicalize().unwrap()), damaged);
    }

    #[test]
    fn replacing_a_venv_never_follows_a_link_out_of_it() {
        let dir = Scratch::new("links");
        let elsewhere = dir.0.join("elsewhere");
        std::fs::create_dir_all(elsewhere.join("folder")).unwrap();
        std::fs::write(elsewhere.join(r"folder\kept.txt"), b"").unwrap();
        // A junction, which takes no rights to make.
        let link = |at: &Path| {
            let made = Command::new("cmd")
                .args(["/c", "mklink", "/J"])
                .args([at, &elsewhere])
                .stdout(Stdio::null())
                .status();
            assert!(made.unwrap().success());
        };
        let engine = installed(&dir.0, None);
        let venv = engine.launch.engine.join(".venv");
        let old = engine.launch.engine.join(OLD_VENVS);
        link(&venv.join(r"Lib\link"));
        set_up(&engine, &dir.0).unwrap();
        assert!(!is_there(&venv.join(r"Lib\link")) && !is_there(&old));
        assert!(elsewhere.join(r"folder\kept.txt").is_file());

        // One where the venv should be, and one where it's moved to.
        std::fs::remove_dir_all(&venv).unwrap();
        link(&venv);
        link(&old);
        set_up(&engine, &dir.0).unwrap();
        assert!(venv.symlink_metadata().unwrap().is_dir(), "a folder now");
        assert!(!is_there(&old));
        assert!(elsewhere.join(r"folder\kept.txt").is_file());
    }

    #[test]
    fn a_venv_with_a_file_open_stays_whole_until_its_let_go() {
        let dir = Scratch::new("open");
        let engine = installed(&dir.0, None);
        let venv = engine.launch.engine.join(".venv");
        let open = std::fs::File::open(venv.join(FILE)).unwrap();
        assert!(set_aside(&venv, Duration::from_millis(300)).is_err());
        assert!(venv.join(FILE).is_file() && venv.join("pyvenv.cfg").is_file());

        let letting_go = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            drop(open);
        });
        set_aside(&venv, Duration::from_secs(30)).unwrap();
        assert!(!is_there(&venv));
        letting_go.join().unwrap();
    }

    #[test]
    fn a_program_running_from_a_venv_doesnt_keep_it_from_being_replaced() {
        let dir = Scratch::new("running");
        let engine = installed(&dir.0, None);
        let venv = engine.launch.engine.join(".venv");
        let old = engine.launch.engine.join(OLD_VENVS);
        // Runs until its stdin is closed.
        let program = venv.join("sort.exe");
        let system = PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32");
        std::fs::copy(system.join("sort.exe"), &program).unwrap();
        let mut running = Command::new(&program)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        set_up(&engine, &dir.0).unwrap();
        assert!(!venv.join(FILE).exists());
        assert!(old.join(r"1\sort.exe").is_file(), "it can't be deleted yet");

        // Again, while it still runs.
        unmark(&engine.launch);
        std::fs::write(venv.join(FILE), b"").unwrap();
        set_up(&engine, &dir.0).unwrap();
        assert!(!venv.join(FILE).exists());
        assert!(running.try_wait().unwrap().is_none());

        // Once it has ended, a start deletes what's left of it.
        running.kill().unwrap();
        running.wait().unwrap();
        set_up(&engine, &dir.0).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while is_there(&old) {
            assert!(std::time::Instant::now() < deadline, "still there");
            std::thread::sleep(Duration::from_millis(50));
        }
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));
    }

    #[test]
    fn only_an_engine_that_stopped_has_its_venv_built_again() {
        let dir = Scratch::new("stopped");
        let engine = installed(&dir.0, Some(BUILD));
        // Still starting when its time was up: ended, and its venv kept.
        let mut slow = Command::new("cmd");
        slow.args(["/c", "pause"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null());
        *engine.child.lock().unwrap() = Some(slow.spawn().unwrap());
        start_failed(&engine);
        assert!(engine.child.lock().unwrap().is_none());
        assert!(engine.stopped.lock().unwrap().is_some());
        assert_eq!(mark_of(&engine).as_deref(), Some(BUILD));

        *engine.stopped.lock().unwrap() = Some("The engine stopped (exit code: 1).".into());
        start_failed(&engine);
        assert_eq!(mark_of(&engine), None);

        // The repo's engine in development is never touched.
        let mut repo = installed(&dir.0, Some(BUILD));
        repo.launch.installed = false;
        *repo.stopped.lock().unwrap() = Some("The engine stopped (exit code: 1).".into());
        start_failed(&repo);
        assert_eq!(mark_of(&repo).as_deref(), Some(BUILD));
    }

    #[test]
    fn uv_ends_with_its_job() {
        let mut waiting = Command::new("cmd")
            .args(["/c", "pause"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let job = end_with_zachcap(&waiting).expect("a job");
        assert!(waiting.try_wait().unwrap().is_none());
        // As Windows closes it when ZachCap goes.
        drop(job);
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while waiting.try_wait().unwrap().is_none() {
            assert!(std::time::Instant::now() < deadline, "still running");
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    #[test]
    fn the_engine_log_is_emptied_past_its_limit_and_carries_on_from_the_top() {
        let dir = std::env::temp_dir().join(format!("zachcap-log-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join(LOG);
        // The engine's end of it, opened to append the way start_engine opens it.
        let mut engine = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log)
            .unwrap();
        engine.write_all(&[b'x'; 2048]).unwrap();
        trim_log(&log, 4096);
        assert_eq!(
            std::fs::metadata(&log).unwrap().len(),
            2048,
            "under the limit"
        );
        engine.write_all(&[b'x'; 4096]).unwrap();
        trim_log(&log, 4096);
        engine.write_all(b"after\n").unwrap();
        let text = std::fs::read_to_string(&log).unwrap();
        assert!(text.starts_with("[ZachCap] engine.log passed"), "{text}");
        // Appended after the note, not past the old end with a hole of zeros before it.
        assert!(text.ends_with("was emptied.\nafter\n"), "{text:?}");
        drop(engine);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn keys_the_engine_saves_parse_in_both_shell_parsers() {
        // The forms engine/src/zachcap/shortcuts.py writes (tests/test_shortcuts.py keeps the
        // same list): modifiers in the order Ctrl, Alt, Shift, Win, then the key.
        for keys in [
            "Ctrl+Alt+R",
            "Ctrl+Alt+F9",
            "Ctrl+Alt+Shift+9",
            "Shift+Win+D",
            "Ctrl+Win+F24",
            "Alt+Win+0",
            "F13",
        ] {
            assert!(parse_keys(keys).is_some(), "the plugin can't read {keys}");
            assert!(
                dictation_keys::Chord::parse(keys).is_some(),
                "the hook can't read {keys}"
            );
        }
    }
}

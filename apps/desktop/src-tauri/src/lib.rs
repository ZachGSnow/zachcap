//! ZachCap desktop shell: starts the Python engine, hands the UI its address and
//! token, and lives in the tray like Tailscale (closing the window keeps it running).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
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

/// Added to the sign-in launch: ZachCap starts in the tray instead of opening its window.
const HIDDEN_ARG: &str = "--hidden";

#[derive(Clone, Serialize)]
struct EngineInfo {
    port: u16,
    token: String,
}

struct Engine {
    info: Result<EngineInfo, String>,
    child: Mutex<Option<Child>>,
    /// Why the engine stopped, once it has; cleared by a restart.
    stopped: Mutex<Option<String>>,
}

/// Set while ZachCap exits, so the engine going away then isn't reported as a crash.
static EXITING: AtomicBool = AtomicBool::new(false);

#[tauri::command]
fn engine_info(engine: tauri::State<Engine>) -> Result<EngineInfo, String> {
    engine.info.clone()
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

/// Starts the engine again after it stopped, on the same port and token, so the window
/// reconnects without reloading. Does nothing while it is still running.
#[tauri::command]
fn restart_engine(engine: tauri::State<Engine>) -> Result<(), String> {
    let info = engine.info.clone()?;
    let mut child = engine.child.lock().unwrap();
    if child.is_some() {
        return Ok(());
    }
    let started = start_engine(info.port, &info.token)
        .map_err(|error| format!("Couldn't run uv ({error})."))?;
    *child = Some(started);
    *engine.stopped.lock().unwrap() = None;
    Ok(())
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
    paused: bool,
}

impl KeyState {
    fn keys_for(&self, action: &str) -> String {
        self.keys.get(action).cloned().unwrap_or_else(|| {
            let (_, default) = ACTIONS.iter().find(|(name, _)| *name == action).unwrap();
            default.to_string()
        })
    }

    fn wanted(&self, action: &str) -> bool {
        !self.paused
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
    /// "on" (held now), "free" (not needed now, and no other app has the keys), "off" (not
    /// needed now, not checked), "taken" (another app has the keys) or "invalid".
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
        state.paused = false;
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
        keys.0.lock().unwrap().paused = paused;
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

/// The Python engine folder: ZACHCAP_ENGINE_DIR, or the repo's `engine/` in development.
fn engine_dir() -> PathBuf {
    std::env::var("ZACHCAP_ENGINE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../engine"))
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

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|listener| listener.local_addr())
        .map(|addr| addr.port())
        .unwrap_or(8765)
}

fn start_engine(port: u16, token: &str) -> std::io::Result<Child> {
    let mut command = Command::new(find_uv());
    command
        .arg("run")
        .arg("--project")
        .arg(engine_dir())
        .args(["zachcap", "serve", "--port", &port.to_string()])
        // Passed through the environment so the token never shows in process listings.
        .env("ZACHCAP_TOKEN", token)
        // The engine exits by itself if this app goes away without stopping it.
        .env("ZACHCAP_PARENT_PID", std::process::id().to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null());
    // A release build has no console, so the engine's output goes to a log instead.
    #[cfg(not(debug_assertions))]
    {
        let folder = data_dir();
        if let Ok(log) = std::fs::create_dir_all(&folder)
            .and_then(|_| std::fs::File::create(folder.join("engine.log")))
            && let Ok(copy) = log.try_clone()
        {
            command.stdout(copy).stderr(log);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
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
    let port = free_port();
    let token = uuid::Uuid::new_v4().simple().to_string();

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
            // On failure the window still opens and the startup screen says what went wrong.
            let (info, child) = match start_engine(port, &token) {
                Ok(child) => (Ok(EngineInfo { port, token: token.clone() }), Some(child)),
                Err(error) => (
                    Err(format!(
                        "Couldn't run uv ({error}). Install it from https://docs.astral.sh/uv/ and start ZachCap again."
                    )),
                    None,
                ),
            };
            app.manage(Engine {
                info,
                child: Mutex::new(child),
                stopped: Mutex::new(None),
            });

            // Notice when the engine stops later (a crash), so the window can offer a restart.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                while !EXITING.load(Ordering::Relaxed) {
                    std::thread::sleep(Duration::from_secs(1));
                    if let Some(engine) = handle.try_state::<Engine>()
                        && let Some(message) = check_engine(&engine)
                        && !EXITING.load(Ordering::Relaxed)
                    {
                        let _ = handle.emit("engine-exited", message);
                    }
                }
            });

            // The global shortcuts (see KeyState): start or stop recording from anywhere;
            // while recording, bookmark the moment and snapshot the call; hold to dictate.
            // Handed to the page like the tray menu, so the window's state stays in sync.
            app.manage(Keys(Mutex::new(KeyState::default())));
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::ShortcutState;
                app.handle().plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            let pressed = event.state() == ShortcutState::Pressed;
                            let event = match action_for(app, shortcut) {
                                // Dictation listens while the keys are held: down starts, up stops.
                                Some("dictate") if pressed => "dictate-start",
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
                sync_keys(app.handle(), Vec::new(), false);
            }

            let record = MenuItem::with_id(app, "record", "Start or stop recording", true, None::<&str>)?;
            let mark = MenuItem::with_id(app, "bookmark", "Bookmark this moment", true, None::<&str>)?;
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
            // Closing the window keeps ZachCap (and any recording) running in the tray.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                tell_about_tray(window.app_handle());
            }
        })
        .invoke_handler(tauri::generate_handler![
            engine_info,
            engine_exited,
            restart_engine,
            open_data_folder,
            set_recording_indicator,
            set_dictation,
            set_shortcuts,
            shortcut_status,
            pause_shortcuts
        ])
        .build(tauri::generate_context!())
        .expect("error while building ZachCap")
        .run(|app, event| {
            if let RunEvent::Exit = event
                && let Some(engine) = app.try_state::<Engine>()
            {
                EXITING.store(true, Ordering::Relaxed);
                if let Ok(info) = &engine.info {
                    stop_recording(info);
                }
                if let Some(mut child) = engine.child.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}

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
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

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
}

#[tauri::command]
fn engine_info(engine: tauri::State<Engine>) -> Result<EngineInfo, String> {
    engine.info.clone()
}

/// Why the engine stopped, if it has; the startup screen uses it to fail fast.
#[tauri::command]
fn engine_exited(engine: tauri::State<Engine>) -> Option<String> {
    let mut child = engine.child.lock().unwrap();
    let status = child.as_mut()?.try_wait().ok()??;
    let log = if cfg!(debug_assertions) {
        "Its output is in the terminal that ran `npm run tauri dev`.".to_string()
    } else {
        format!("Its log is {}.", data_dir().join("engine.log").display())
    };
    Some(format!("The engine stopped ({status}). {log}"))
}

/// Opens a folder in Explorer (the data folder, from Settings).
#[tauri::command]
fn open_folder(path: String) -> Result<(), String> {
    let folder = PathBuf::from(&path);
    if !folder.is_dir() {
        return Err(format!("{path} is not a folder"));
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
        .plugin(tauri_plugin_opener::init())
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
            app.manage(Engine { info, child: Mutex::new(child) });

            // Ctrl+Alt+R starts or stops recording from anywhere. Registered here, once,
            // and handed to the page like the tray menu, so the window's state stays in sync.
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::{
                    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
                };
                let toggle = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyR);
                app.handle().plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            if shortcut == &toggle && event.state() == ShortcutState::Pressed {
                                let _ = app.emit("toggle-recording", ());
                            }
                        })
                        .build(),
                )?;
                if let Err(error) = app.global_shortcut().register(toggle) {
                    // Another app may own the combination; the tray menu still works.
                    eprintln!("Ctrl+Alt+R is unavailable: {error}");
                }
            }

            let record = MenuItem::with_id(app, "record", "Start or stop recording", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open ZachCap", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit ZachCap", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&record, &open, &separator, &quit])?;
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
        .invoke_handler(tauri::generate_handler![engine_info, engine_exited, open_folder])
        .build(tauri::generate_context!())
        .expect("error while building ZachCap")
        .run(|app, event| {
            if let RunEvent::Exit = event
                && let Some(engine) = app.try_state::<Engine>()
            {
                if let Ok(info) = &engine.info {
                    stop_recording(info);
                }
                if let Some(mut child) = engine.child.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}

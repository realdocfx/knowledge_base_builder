#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::net::TcpListener;
use std::process::Command;
use std::thread;
use std::time::Duration;
use tauri::RunEvent;
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

// Dynamically allocate a free loopback port to prevent host network collisions
fn get_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn main() {
    // 1. Resolve isolated paths relative to the USB executable
    let exe_path = std::env::current_exe().expect("Failed to resolve executable path");
    let usb_root = exe_path.parent().unwrap();

    // Point WebView2 at the runtime bundled on the stick, so the launcher renders
    // on ANY Windows host — even one with no WebView2 installed and no internet.
    // Must be set before the WebView2 environment is created (before the window).
    // Falls back to the host's WebView2 if the bundle is absent.
    #[cfg(target_os = "windows")]
    {
        let webview2_dir = usb_root.join(".kb_env").join("webview2");
        if webview2_dir.join("msedgewebview2.exe").exists() {
            std::env::set_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", &webview2_dir);
        }
    }

    // Target the portable Python runtime and KBB package inside .kb_env
    let python_exe = if cfg!(target_os = "windows") {
        usb_root.join(".kb_env").join("python").join("python.exe")
    } else {
        usb_root.join(".kb_env").join("python").join("bin").join("python3")
    };
    let kbb_app = usb_root.join(".kb_env").join("app");

    // 2. Allocate the loopback port
    let port = get_free_port();

    // 3. Spawn KBB FastAPI Portal Backend in an isolated environment
    let mut cmd = Command::new(&python_exe);
    cmd.arg("-m")
       .arg("knowledge_base_builder.cli")
       .arg("portal")
       .arg(usb_root.to_str().unwrap())
       .arg("--host")
       .arg("127.0.0.1")
       .arg("--port")
       .arg(port.to_string())
       .arg("--no-browser");

    // Enforce airgapped environment variables
    cmd.env("PYTHONPATH", &kbb_app);
    cmd.env("KBB_AIRGAPPED", "1");
    
    // Prevent the Python console window from flashing on Windows hosts
    #[cfg(target_os = "windows")]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW

    let mut backend_child = cmd.spawn().expect("CRITICAL: Failed to start KBB FastAPI backend.");

    // 4. Poll the backend's Healthcheck API
    let mut is_healthy = false;
    for _ in 0..60 { // Wait up to 30 seconds
        if reqwest::blocking::get(format!("http://127.0.0.1:{}/api/stats", port)).is_ok() {
            is_healthy = true;
            break;
        }
        thread::sleep(Duration::from_millis(500));
    }

    if !is_healthy {
        let _ = backend_child.kill();
        panic!("CRITICAL FAULT: KBB Backend failed healthcheck API verification. Aborting.");
    }

    // 5. Initialize the Hardened Tauri App Shell
    let target_url = format!("http://127.0.0.1:{}", port);

    tauri::Builder::default()
        .setup(move |app| {
            tauri::WindowBuilder::new(
                app,
                "main",
                tauri::WindowUrl::External(target_url.parse().unwrap()),
            )
            .title("Knowledge Base Command Console")
            .inner_size(1280.0, 800.0)
            .disable_file_drop_handler() // Prevents drag-and-drop vector exploits
            .build()
            .unwrap();
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Tauri app shell")
        .run(move |_app_handle, event| {
            // 6. Graceful Mission Abort & Cleanup
            if let RunEvent::Exit = event {
                // KBB guarantees data preservation via atomic writes, so a hard kill is safe
                let _ = backend_child.kill();
                let _ = backend_child.wait();
            }
        });
}

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

/// The boot screen is compiled INTO the binary and displayed from a `data:` URL.
///
/// This deliberately bypasses Tauri's distDir/devPath asset resolution. That
/// pipeline silently fell back to `devPath` (`http://127.0.0.1`, i.e. port 80),
/// which produced the ERR_CONNECTION_REFUSED page instead of the loading screen.
/// Embedding the HTML makes the first frame independent of any asset lookup or
/// filesystem layout on the target host.
const LOADING_HTML: &str = include_str!("../public/index.html");

/// True when the host already has a WebView2 runtime installed locally.
///
/// Checked by looking for the Evergreen runtime's versioned install directory
/// rather than the registry, to stay dependency-free. Being wrong is safe in
/// both directions: a false negative merely uses the (working) bundled runtime,
/// and a false positive is impossible because we require the executable itself.
#[cfg(target_os = "windows")]
fn host_webview2_present() -> bool {
    const ROOTS: [&str; 2] = [
        r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        r"C:\Program Files\Microsoft\EdgeWebView\Application",
    ];
    ROOTS.iter().any(|root| {
        std::fs::read_dir(root)
            .map(|entries| {
                entries
                    .flatten()
                    .any(|e| e.path().join("msedgewebview2.exe").is_file())
            })
            .unwrap_or(false)
    })
}

/// Minimal, dependency-free HTTP probe: true ONLY on a real HTTP 200.
///
/// A bare TCP connect is not sufficient (the socket can accept before the app
/// serves), and probing from the webview is impossible because that request is
/// cross-origin and blocked by the portal's CORS policy. So we speak just enough
/// HTTP/1.1 here to read the status line.
fn probe_http_ok(port: u16, path: &str) -> bool {
    use std::io::{Read, Write};
    let addr: std::net::SocketAddr = match format!("127.0.0.1:{}", port).parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let mut stream = match std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(1200)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(2000)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(2000)));
    let req = format!(
        "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        path, port
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 128];
    match stream.read(&mut buf) {
        Ok(n) if n > 12 => {
            let head = String::from_utf8_lossy(&buf[..n]);
            let status_line = head.lines().next().unwrap_or("");
            status_line.starts_with("HTTP/1.") && status_line.contains(" 200")
        }
        _ => false,
    }
}

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

    // Choose the WebView2 runtime. The bundle on the stick guarantees the console
    // renders on a host with no WebView2 and no internet, but it is ~500 MB across
    // 84 files: loading it off USB *while* the Python backend is also starting
    // saturates the drive and stretched a 6s backend boot to 25-50s.
    //
    // So prefer the host's installed runtime when present (it lives on fast local
    // disk and costs the stick nothing) and fall back to the bundled copy only
    // when the host genuinely lacks one. The airgap guarantee is preserved; the
    // common case simply stops paying for it.
    #[cfg(target_os = "windows")]
    {
        let bundled = usb_root.join(".kb_env").join("webview2");
        let have_bundle = bundled.join("msedgewebview2.exe").is_file();
        if have_bundle && !host_webview2_present() {
            std::env::set_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", &bundled);
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
    // /api/* now requires a control-plane token. Rust std has no CSPRNG, so the
    // backend mints it with Python's `secrets` and publishes it here; we read it
    // back and navigate pre-authorised. Stale files are removed first so we can
    // never present a previous run's token.
    let token_path = std::env::temp_dir().join(format!("kbb_token_{}.txt", port));
    let _ = std::fs::remove_file(&token_path);
    cmd.env("KBB_TOKEN_FILE", &token_path);
    
    // Prevent the Python console window from flashing on Windows hosts
    #[cfg(target_os = "windows")]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW

    let mut backend_child = cmd.spawn().expect("CRITICAL: Failed to start KBB FastAPI backend.");

    // 4. Initialize the hardened Tauri shell. The window opens IMMEDIATELY on the
    // bundled loading screen (index.html), which is told the portal URL via an
    // injected global and polls it itself (fetch) — redirecting only once the
    // *webview* can actually reach the backend. Doing the reachability check from
    // the webview (not a Rust-side request) avoids navigating to a URL the webview
    // cannot open, which otherwise shows ERR_CONNECTION_REFUSED.
    let target_url = format!("http://127.0.0.1:{}", port);

    tauri::Builder::default()
        .setup(move |app| {
            // Publish the portal URL and a queueing stub BEFORE the page's scripts
            // run, so telemetry pushed during early boot is never lost.
            let init = format!(
                "window.__KBB_PORTAL__ = {:?};\
                 window.__kbbBootQueue = [];\
                 window.__kbbBoot = function(p,n,a,m,e){{ window.__kbbBootQueue.push([p,n,a,m,e]); }};",
                target_url
            );
            // Materialise the embedded boot screen to a temp file and load it over
            // file://. Two dead ends ruled this: Chromium (hence WebView2) BLOCKS
            // top-level navigation to data: URLs, and Tauri's distDir/devPath
            // resolution silently fell back to http://127.0.0.1:80 (the bare
            // "127.0.0.1 refused to connect" page). file:// has neither problem.
            // Never panics: falls back to the bundled asset path if anything fails.
            let boot_path = std::env::temp_dir().join("kbb_boot.html");
            let boot_written = std::fs::write(&boot_path, LOADING_HTML).is_ok();
            let boot_target = if boot_written {
                let boot_url = format!(
                    "file:///{}",
                    boot_path.to_string_lossy().replace('\\', "/")
                );
                match boot_url.parse() {
                    Ok(u) => tauri::WindowUrl::External(u),
                    Err(_) => tauri::WindowUrl::App("index.html".into()),
                }
            } else {
                tauri::WindowUrl::App("index.html".into())
            };
            let window = tauri::WindowBuilder::new(app, "main", boot_target)
            .title("Knowledge Base Command Console")
            .inner_size(1280.0, 800.0)
            .center()
            .initialization_script(&init)
            .disable_file_drop_handler() // Prevents drag-and-drop vector exploits
            .build()?;

            // Probe the backend NATIVELY. A webview fetch to the portal is
            // cross-origin (page runs on tauri://localhost) and would be blocked by
            // CORS, so the reachability check must happen here. Each probe pushes
            // determinate progress (phase, attempt N of M, elapsed) to the loading
            // screen per MIL-STD-1472H 5.17; we navigate only on a real HTTP 200,
            // never on a bare socket, so ERR_CONNECTION_REFUSED cannot surface.
            let win = window.clone();
            let url = target_url.clone();
            thread::spawn(move || {
                const MAX_PROBES: u32 = 240; // 240 * 500ms = 120s hard budget
                let started = std::time::Instant::now();
                for attempt in 1..=MAX_PROBES {
                    let elapsed = started.elapsed().as_secs();
                    let ready = probe_http_ok(port, "/");
                    if ready {
                        let _ = win.eval(&format!(
                            "window.__kbbBoot(3,'Console ready',{},{},{});",
                            attempt, MAX_PROBES, elapsed
                        ));
                        // Append the token so the console can exchange it for an
                        // HttpOnly session cookie; without it every /api/* call 401s.
                        let target = match std::fs::read_to_string(&token_path) {
                            Ok(tok) if !tok.trim().is_empty() => {
                                format!("{}/?t={}", url, tok.trim())
                            }
                            _ => url.clone(),
                        };
                        let _ = win.eval(&format!("window.location.replace({:?})", target));
                        return;
                    }
                    let _ = win.eval(&format!(
                        "window.__kbbBoot(2,'Starting ZIM engine and portal backend',{},{},{});",
                        attempt, MAX_PROBES, elapsed
                    ));
                    thread::sleep(Duration::from_millis(500));
                }
                let _ = win.eval("window.kbbBootFailed && window.kbbBootFailed();");
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Tauri app shell")
        .run(move |_app_handle, event| {
            // 5. Graceful mission abort & cleanup.
            if let RunEvent::Exit = event {
                // KBB guarantees data preservation via atomic writes, so a hard kill is safe
                let _ = backend_child.kill();
                let _ = backend_child.wait();
            }
        });
}

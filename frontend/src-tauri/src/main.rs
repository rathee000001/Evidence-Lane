#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
#[cfg(feature = "embedded-worker")]
use sha2::{Digest, Sha256};
#[cfg(windows)]
use std::os::windows::{io::AsRawHandle, process::CommandExt};
use std::{
    collections::VecDeque,
    env, fs,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Command, Stdio},
    sync::{Arc, Mutex, OnceLock},
    thread,
    time::Duration,
};
#[cfg(feature = "embedded-worker")]
use std::{fs::File, io::Read};
use tauri::{AppHandle, Emitter, LogicalSize, Manager, Size, State, WebviewWindow};

#[cfg(windows)]
fn apply_balanced_active_priority_to_current_process() {
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcess, SetPriorityClass, ABOVE_NORMAL_PRIORITY_CLASS,
    };
    unsafe {
        let _ = SetPriorityClass(GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS);
    }
}

#[cfg(not(windows))]
fn apply_balanced_active_priority_to_current_process() {}

#[cfg(windows)]
fn apply_balanced_active_priority_to_child(child: &std::process::Child) {
    use windows_sys::Win32::System::Threading::{SetPriorityClass, ABOVE_NORMAL_PRIORITY_CLASS};
    unsafe {
        let _ = SetPriorityClass(
            child.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE,
            ABOVE_NORMAL_PRIORITY_CLASS,
        );
    }
}

#[cfg(not(windows))]
fn apply_balanced_active_priority_to_child(_child: &std::process::Child) {}

#[cfg(windows)]
mod external_foreground {
    use super::*;
    use windows_sys::Win32::{
        Foundation::{BOOL, HWND, LPARAM},
        UI::WindowsAndMessaging::{
            BringWindowToTop, EnumWindows, GetClassNameW, GetForegroundWindow, GetWindowTextW,
            IsWindowVisible, SetForegroundWindow, SetWindowPos, ShowWindow, HWND_NOTOPMOST,
            HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SW_MAXIMIZE, SW_RESTORE,
        },
    };

    struct SearchContext {
        target: String,
        title_hint: String,
        exact: HWND,
        fallback: HWND,
    }

    unsafe fn window_text(hwnd: HWND) -> String {
        let mut buffer = [0_u16; 512];
        let count = GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32);
        String::from_utf16_lossy(&buffer[..count.max(0) as usize]).to_ascii_lowercase()
    }

    unsafe fn class_name(hwnd: HWND) -> String {
        let mut buffer = [0_u16; 256];
        let count = GetClassNameW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32);
        String::from_utf16_lossy(&buffer[..count.max(0) as usize]).to_ascii_lowercase()
    }

    unsafe extern "system" fn enumerate(hwnd: HWND, lparam: LPARAM) -> BOOL {
        if IsWindowVisible(hwnd) == 0 {
            return 1;
        }
        let context = &mut *(lparam as *mut SearchContext);
        let title = window_text(hwnd);
        let class = class_name(hwnd);
        let exact = match context.target.as_str() {
            "chatgpt" => class.contains("chrome_widgetwin") && title.contains("chatgpt"),
            "gemini" => class.contains("chrome_widgetwin") && title.contains("gemini"),
            "codex" => title.contains("codex"),
            "ollama" => title.contains("ollama"),
            "vscode" => title.contains("visual studio code") || title.ends_with(" - code"),
            "explorer" => {
                let explorer = class == "cabinetwclass" || class == "explorewclass";
                explorer && (context.title_hint.is_empty() || title.contains(&context.title_hint))
            }
            "owner" => {
                !context.title_hint.is_empty()
                    && title.contains(&context.title_hint)
                    && !title.contains("evidence os")
            }
            _ => false,
        };
        let fallback = match context.target.as_str() {
            "chatgpt" | "gemini" => class.contains("chrome_widgetwin"),
            "codex" => class == "applicationframewindow" && title.contains("openai"),
            "ollama" => title.contains("ollama"),
            "vscode" => class.contains("chrome_widgetwin") && title.contains("code"),
            "explorer" => class == "cabinetwclass" || class == "explorewclass",
            "owner" => !context.title_hint.is_empty()
                && title.contains(&context.title_hint)
                && !title.contains("evidence os"),
            _ => false,
        };
        if exact {
            context.exact = hwnd;
            return 0;
        }
        if fallback {
            context.fallback = hwnd;
        }
        1
    }

    unsafe fn find(target: &str, title_hint: &str) -> (HWND, HWND) {
        let mut context = SearchContext {
            target: target.to_string(),
            title_hint: title_hint.to_ascii_lowercase(),
            exact: std::ptr::null_mut(),
            fallback: std::ptr::null_mut(),
        };
        let _ = EnumWindows(
            Some(enumerate),
            &mut context as *mut SearchContext as LPARAM,
        );
        (context.exact, context.fallback)
    }

    unsafe fn activate(hwnd: HWND, maximize: bool) -> bool {
        if hwnd.is_null() {
            return false;
        }
        let _ = ShowWindow(hwnd, SW_RESTORE);
        if maximize {
            let _ = ShowWindow(hwnd, SW_MAXIMIZE);
        }
        let _ = BringWindowToTop(hwnd);
        let _ = SetForegroundWindow(hwnd);
        if GetForegroundWindow() != hwnd {
            let _ = SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            );
            let _ = SetWindowPos(
                hwnd,
                HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            );
            let _ = SetForegroundWindow(hwnd);
        }
        GetForegroundWindow() == hwnd
    }

    pub fn activate_target(target: &str, title_hint: Option<&str>) -> bool {
        // Every native handoff owns the desktop after Evidence OS minimizes.
        // Explorer is intentionally maximized too: folder handoffs follow the
        // same foreground/full-screen law as Codex, Ollama, VS Code and the
        // registered Windows owner of a non-code file.
        let maximize = true;
        let title_hint = title_hint.unwrap_or_default();
        let mut fallback = std::ptr::null_mut();
        for _ in 0..24 {
            let (exact, candidate) = unsafe { find(target, title_hint) };
            if !candidate.is_null() {
                fallback = candidate;
            }
            if !exact.is_null() && unsafe { activate(exact, maximize) } {
                return true;
            }
            thread::sleep(Duration::from_millis(250));
        }
        !fallback.is_null() && unsafe { activate(fallback, maximize) }
    }
}

#[cfg(not(windows))]
mod external_foreground {
    pub fn activate_target(_target: &str, _title_hint: Option<&str>) -> bool {
        false
    }
}

#[cfg(feature = "embedded-worker")]
const EMBEDDED_WORKER_BYTES: &[u8] = include_bytes!(env!("EVIDENCE_OS_EMBEDDED_WORKER_EXE"));
#[cfg(feature = "embedded-worker")]
const EMBEDDED_WORKER_SHA256: &str = env!("EVIDENCE_OS_EMBEDDED_WORKER_SHA256");
#[cfg(feature = "embedded-worker")]
static EMBEDDED_WORKER_PATH_CACHE: OnceLock<Result<PathBuf, String>> = OnceLock::new();
const CANONICAL_PRODUCTION_ROOT: &str = r"D:\EvidenceLane";

fn canonical_production_root() -> Result<PathBuf, String> {
    let root = env::var("EVIDENCE_OS_CANONICAL_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(CANONICAL_PRODUCTION_ROOT));
    if !root.is_absolute() {
        return Err("CANONICAL_PRODUCTION_ROOT_MUST_BE_ABSOLUTE".to_string());
    }
    Ok(root)
}

fn canonical_production_root_ready() -> Result<PathBuf, String> {
    let root = canonical_production_root()?;
    let registry = root.join("config").join("workspace-roots.json");
    let workspace = root.join("workspace.sqlite");
    if !root.is_dir() || !registry.is_file() || !workspace.is_file() {
        return Err(format!("CANONICAL_PRODUCTION_ROOT_NOT_READY:{}", root.display()));
    }
    fs::canonicalize(&root)
        .map_err(|err| format!("CANONICAL_PRODUCTION_ROOT_RESOLVE_FAILED:{err}"))
}

fn chrome_executable() -> PathBuf {
    let mut candidates = Vec::new();
    if let Ok(local) = env::var("LOCALAPPDATA") {
        candidates.push(
            PathBuf::from(local)
                .join("Google")
                .join("Chrome")
                .join("Application")
                .join("chrome.exe"),
        );
    }
    if let Ok(program_files) = env::var("PROGRAMFILES") {
        candidates.push(
            PathBuf::from(program_files)
                .join("Google")
                .join("Chrome")
                .join("Application")
                .join("chrome.exe"),
        );
    }
    if let Ok(program_files_x86) = env::var("PROGRAMFILES(X86)") {
        candidates.push(
            PathBuf::from(program_files_x86)
                .join("Google")
                .join("Chrome")
                .join("Application")
                .join("chrome.exe"),
        );
    }
    candidates
        .into_iter()
        .find(|path| path.exists())
        .unwrap_or_else(|| PathBuf::from("chrome.exe"))
}

#[tauri::command]
async fn open_chrome_profile(window: WebviewWindow, provider: String) -> Result<Value, String> {
    let provider = provider.trim().to_ascii_lowercase();
    let url = match provider.as_str() {
        "chatgpt" => "https://chatgpt.com/",
        "gemini" => "https://gemini.google.com/app",
        _ => return Err("UNSUPPORTED_CHROME_DESTINATION".to_string()),
    };
    let mut command = Command::new(chrome_executable());
    command.args(["--profile-directory=Default", "--new-window", url]);
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        const CREATE_DEFAULT_ERROR_MODE: u32 = 0x04000000;
        command.creation_flags(CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE);
    }
    command
        .spawn()
        .map_err(|err| format!("CHROME_OPEN_FAILED: {err}"))?;
    window
        .minimize()
        .map_err(|err| format!("EVIDENCE_OS_MINIMIZE_FAILED: {err}"))?;
    let activation_target = provider.clone();
    let activated = tauri::async_runtime::spawn_blocking(move || {
        external_foreground::activate_target(&activation_target, None)
    })
    .await
    .map_err(|err| format!("CHROME_FOREGROUND_JOIN_FAILED: {err}"))?;
    if !activated {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        return Err(format!("CHROME_FOREGROUND_HANDOFF_FAILED:{provider}"));
    }
    Ok(serde_json::json!({
        "status": "LAUNCHED_MAXIMIZED_AND_FOREGROUNDED",
        "provider": provider,
        "url": url,
        "chrome_profile_directory": "Default",
        "evidence_os_minimized": true,
        "external_window_maximized": true
    }))
}

#[tauri::command(rename_all = "camelCase")]
async fn handoff_external_window(
    window: WebviewWindow,
    target: String,
    minimize_evidence_os: bool,
    title_hint: Option<String>,
) -> Result<Value, String> {
    let target = target.trim().to_ascii_lowercase();
    if !matches!(target.as_str(), "codex" | "ollama" | "explorer" | "vscode" | "owner") {
        return Err(format!("EXTERNAL_HANDOFF_TARGET_UNSUPPORTED:{target}"));
    }
    if minimize_evidence_os {
        window
            .minimize()
            .map_err(|err| format!("EVIDENCE_OS_MINIMIZE_FAILED: {err}"))?;
    }
    let activation_target = target.clone();
    let activation_hint = title_hint.clone();
    let activated = tauri::async_runtime::spawn_blocking(move || {
        external_foreground::activate_target(&activation_target, activation_hint.as_deref())
    })
    .await
    .map_err(|err| format!("EXTERNAL_FOREGROUND_JOIN_FAILED: {err}"))?;
    if !activated {
        if minimize_evidence_os {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
        return Err(format!("EXTERNAL_FOREGROUND_HANDOFF_FAILED:{target}"));
    }
    Ok(serde_json::json!({
        "status": "FOREGROUND_HANDOFF_COMPLETE",
        "target": target,
        "title_hint": title_hint,
        "evidence_os_minimized": minimize_evidence_os,
        "external_window_maximized": true
    }))
}

#[tauri::command]
async fn open_native_folder(window: WebviewWindow, path: String) -> Result<Value, String> {
    let requested = PathBuf::from(path.trim());
    if !requested.is_absolute() {
        return Err("NATIVE_FOLDER_PATH_MUST_BE_ABSOLUTE".to_string());
    }
    let destination = fs::canonicalize(&requested)
        .map_err(|err| format!("NATIVE_FOLDER_PATH_UNAVAILABLE: {err}"))?;
    if !destination.is_dir() {
        return Err("NATIVE_FOLDER_TARGET_NOT_DIRECTORY".to_string());
    }
    #[cfg(windows)]
    Command::new("explorer.exe")
        .arg(&destination)
        .spawn()
        .map_err(|err| format!("NATIVE_FOLDER_OPEN_FAILED: {err}"))?;
    #[cfg(not(windows))]
    return Err("NATIVE_FOLDER_OPEN_WINDOWS_ONLY".to_string());

    window
        .minimize()
        .map_err(|err| format!("EVIDENCE_OS_MINIMIZE_FAILED: {err}"))?;
    let title_hint = destination
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_string();
    let activation_hint = title_hint.clone();
    let activated = tauri::async_runtime::spawn_blocking(move || {
        external_foreground::activate_target("explorer", Some(&activation_hint))
    })
    .await
    .map_err(|err| format!("NATIVE_FOLDER_FOREGROUND_JOIN_FAILED: {err}"))?;
    if !activated {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        return Err("NATIVE_FOLDER_FOREGROUND_HANDOFF_FAILED".to_string());
    }
    Ok(serde_json::json!({
        "status": "OPENED_MAXIMIZED_AND_FOREGROUNDED",
        "path": destination,
        "title_hint": title_hint,
        "evidence_os_minimized": true,
        "external_window_maximized": true,
        "handoff_owner": "TAURI_NATIVE_INDEPENDENT_CHANNEL"
    }))
}

#[cfg(not(feature = "embedded-worker"))]
fn repo_root() -> PathBuf {
    if let Ok(root) = env::var("EVIDENCE_OS_REPO_ROOT") {
        let path = PathBuf::from(root);
        if path
            .join("src")
            .join("sqlite_brain_builder")
            .join("ipc_worker.py")
            .exists()
        {
            return path;
        }
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            for candidate in [
                dir.to_path_buf(),
                dir.parent()
                    .map(PathBuf::from)
                    .unwrap_or_else(|| dir.to_path_buf()),
                dir.parent()
                    .and_then(|p| p.parent())
                    .map(PathBuf::from)
                    .unwrap_or_else(|| dir.to_path_buf()),
            ] {
                if candidate
                    .join("src")
                    .join("sqlite_brain_builder")
                    .join("ipc_worker.py")
                    .exists()
                {
                    return candidate;
                }
            }
        }
    }
    if let Ok(current) = env::current_dir() {
        if current
            .join("src")
            .join("sqlite_brain_builder")
            .join("ipc_worker.py")
            .exists()
        {
            return current;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(PathBuf::from)
        .unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

#[cfg(not(feature = "embedded-worker"))]
fn worker_script() -> PathBuf {
    env::var("EVIDENCE_OS_WORKER")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            repo_root()
                .join("src")
                .join("sqlite_brain_builder")
                .join("ipc_worker.py")
        })
}

#[cfg(not(feature = "embedded-worker"))]
fn python_bin() -> String {
    if let Ok(explicit) = env::var("EVIDENCE_OS_PYTHON") {
        if !explicit.trim().is_empty() {
            return explicit;
        }
    }
    let root = repo_root();
    for candidate in [
        root.join(".venv").join("Scripts").join("python.exe"),
        PathBuf::from(r"C:\Python314\python.exe"),
        PathBuf::from(r"C:\Python313\python.exe"),
        PathBuf::from(r"C:\Python312\python.exe"),
        PathBuf::from(r"C:\Python311\python.exe"),
    ] {
        if candidate.exists() {
            return candidate.display().to_string();
        }
    }
    "python".to_string()
}

#[cfg(feature = "embedded-worker")]
fn file_sha256(path: &PathBuf) -> Result<String, String> {
    let mut file =
        File::open(path).map_err(|err| format!("EMBEDDED_WORKER_HASH_OPEN_FAILED: {err}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|err| format!("EMBEDDED_WORKER_HASH_READ_FAILED: {err}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:X}", hasher.finalize()))
}

#[cfg(feature = "embedded-worker")]
fn extract_and_verify_embedded_worker_path() -> Result<PathBuf, String> {
    let expected = EMBEDDED_WORKER_SHA256.trim().to_ascii_uppercase();
    let embedded_digest = format!("{:X}", Sha256::digest(EMBEDDED_WORKER_BYTES));
    if embedded_digest != expected {
        return Err(format!(
            "EMBEDDED_WORKER_COMPILE_HASH_MISMATCH: expected={expected} actual={embedded_digest}"
        ));
    }
    let base = canonical_production_root_ready()?
        .join("runtime").join("embedded-worker")
        .join(&expected);
    fs::create_dir_all(&base)
        .map_err(|err| format!("EMBEDDED_WORKER_DIRECTORY_CREATE_FAILED: {err}"))?;
    let worker = base.join(format!(
        "EvidenceOS-Embedded-Backend-{}.exe",
        &expected[..12.min(expected.len())]
    ));
    if worker.is_file() && file_sha256(&worker)? == expected {
        return Ok(worker);
    }
    let temporary = base.join(format!("worker-{}.tmp", std::process::id()));
    fs::write(&temporary, EMBEDDED_WORKER_BYTES)
        .map_err(|err| format!("EMBEDDED_WORKER_EXTRACT_FAILED: {err}"))?;
    let extracted_digest = file_sha256(&temporary)?;
    if extracted_digest != expected {
        let _ = fs::remove_file(&temporary);
        return Err(format!(
            "EMBEDDED_WORKER_EXTRACT_HASH_MISMATCH: expected={expected} actual={extracted_digest}"
        ));
    }
    if worker.exists() {
        fs::remove_file(&worker).map_err(|err| format!("EMBEDDED_WORKER_REPLACE_FAILED: {err}"))?;
    }
    fs::rename(&temporary, &worker)
        .map_err(|err| format!("EMBEDDED_WORKER_COMMIT_FAILED: {err}"))?;
    Ok(worker)
}

#[cfg(feature = "embedded-worker")]
fn embedded_worker_path() -> Result<PathBuf, String> {
    EMBEDDED_WORKER_PATH_CACHE
        .get_or_init(extract_and_verify_embedded_worker_path)
        .clone()
}

#[cfg(feature = "embedded-worker")]
fn write_self_test_receipt(path: &PathBuf, receipt: &Value) -> Result<(), String> {
    if !path.is_absolute() {
        return Err("SELF_TEST_RECEIPT_PATH_MUST_BE_ABSOLUTE".to_string());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("SELF_TEST_RECEIPT_DIRECTORY_CREATE_FAILED: {err}"))?;
    }
    let body = serde_json::to_vec_pretty(receipt)
        .map_err(|err| format!("SELF_TEST_RECEIPT_ENCODE_FAILED: {err}"))?;
    let temporary = path.with_extension(format!("tmp.{}", std::process::id()));
    fs::write(&temporary, body).map_err(|err| format!("SELF_TEST_RECEIPT_WRITE_FAILED: {err}"))?;
    if path.exists() {
        fs::remove_file(path).map_err(|err| format!("SELF_TEST_RECEIPT_REPLACE_FAILED: {err}"))?;
    }
    fs::rename(&temporary, path)
        .map_err(|err| format!("SELF_TEST_RECEIPT_COMMIT_FAILED: {err}"))?;
    Ok(())
}

#[cfg(feature = "embedded-worker")]
fn installed_app_self_test() -> Result<Value, String> {
    let app_exe =
        env::current_exe().map_err(|err| format!("SELF_TEST_CURRENT_EXE_FAILED: {err}"))?;
    let app_sha256 = file_sha256(&app_exe)?;
    let worker_exe = embedded_worker_path()?;
    let worker_sha256 = file_sha256(&worker_exe)?;
    if worker_sha256 != EMBEDDED_WORKER_SHA256.trim().to_ascii_uppercase() {
        return Err("SELF_TEST_EMBEDDED_WORKER_HASH_MISMATCH".to_string());
    }

    let mut command = worker_process_command()?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        const CREATE_DEFAULT_ERROR_MODE: u32 = 0x04000000;
        command.creation_flags(CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE);
    }
    let mut child = command
        .spawn()
        .map_err(|err| format!("SELF_TEST_WORKER_START_FAILED: {err}"))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "SELF_TEST_WORKER_STDIN_NOT_CAPTURED".to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "SELF_TEST_WORKER_STDOUT_NOT_CAPTURED".to_string())?;
    let request = serde_json::json!({
        "id": "installed-app-self-test-ping",
        "command": "system.ping",
        "payload": {"tauri_pid": std::process::id()}
    });
    let request_body = serde_json::to_vec(&request)
        .map_err(|err| format!("SELF_TEST_REQUEST_ENCODE_FAILED: {err}"))?;
    stdin
        .write_all(&request_body)
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|err| format!("SELF_TEST_WORKER_STDIN_FAILED: {err}"))?;
    drop(stdin);

    let mut response: Option<Value> = None;
    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|err| format!("SELF_TEST_WORKER_STDOUT_FAILED: {err}"))?;
        let parsed = match serde_json::from_str::<Value>(line.trim()) {
            Ok(value) => value,
            Err(_) => continue,
        };
        if parsed.get("type").and_then(Value::as_str) == Some("response")
            && parsed.get("id").and_then(Value::as_str) == Some("installed-app-self-test-ping")
        {
            response = Some(parsed);
            break;
        }
    }
    let _ = child.kill();
    let _ = child.wait();

    let response = response.ok_or_else(|| "SELF_TEST_WORKER_RESPONSE_MISSING".to_string())?;
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(format!("SELF_TEST_WORKER_RESPONSE_FAILED:{response}"));
    }
    let result = response
        .get("result")
        .ok_or_else(|| "SELF_TEST_WORKER_RESULT_MISSING".to_string())?;
    if result.get("contract").and_then(Value::as_str) != Some("T023_FULL_APP_BACKEND_V2")
        || result.get("frozen").and_then(Value::as_bool) != Some(true)
        || result.get("packaging_mode").and_then(Value::as_str)
            != Some("PYINSTALLER_EMBEDDED_WORKER")
    {
        return Err(format!("SELF_TEST_BACKEND_CONTRACT_INVALID:{result}"));
    }

    Ok(serde_json::json!({
        "schema": "T023_INSTALLER_V1_INSTALLED_APP_RUNTIME_SELF_TEST_V1",
        "status": "PASS",
        "app_exe": app_exe,
        "app_sha256": app_sha256,
        "embedded_worker_exe": worker_exe,
        "embedded_worker_sha256": worker_sha256,
        "backend_contract": result.get("contract"),
        "backend_frozen": true,
        "packaging_mode": result.get("packaging_mode"),
        "tested_by_pid": std::process::id()
    }))
}

#[cfg(feature = "embedded-worker")]
fn run_cli_self_test_if_requested() -> Option<i32> {
    let receipt_path = env::args().skip(1).find_map(|argument| {
        argument
            .strip_prefix("--evidenceos-self-test=")
            .map(PathBuf::from)
    })?;
    let outcome = installed_app_self_test();
    let receipt = match &outcome {
        Ok(value) => value.clone(),
        Err(error) => serde_json::json!({
            "schema": "T023_INSTALLER_V1_INSTALLED_APP_RUNTIME_SELF_TEST_V1",
            "status": "FAIL",
            "error": error,
            "tested_by_pid": std::process::id()
        }),
    };
    if let Err(error) = write_self_test_receipt(&receipt_path, &receipt) {
        eprintln!("{error}");
        return Some(1);
    }
    Some(if outcome.is_ok() { 0 } else { 1 })
}

#[cfg(feature = "embedded-worker")]
fn worker_process_command() -> Result<Command, String> {
    let worker = embedded_worker_path()?;
    let mut command = Command::new(worker);
    command
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("EVIDENCE_OS_EMBEDDED_WORKER", "1")
        .env("EVIDENCE_OS_NATIVE_PRODUCTION", "1")
        .env("EVIDENCE_OS_CANONICAL_ROOT", canonical_production_root()?);
    Ok(command)
}

#[cfg(not(feature = "embedded-worker"))]
fn worker_process_command() -> Result<Command, String> {
    let root = repo_root();
    let script = worker_script();
    if !script.exists() {
        return Err(format!("WORKER_SCRIPT_NOT_FOUND: {}", script.display()));
    }
    let mut python_path = root.join("src").display().to_string();
    if let Ok(existing) = env::var("PYTHONPATH") {
        if !existing.trim().is_empty() {
            python_path.push(';');
            python_path.push_str(&existing);
        }
    }
    let mut command = Command::new(python_bin());
    command
        .arg(script)
        .env("PYTHONPATH", python_path)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("EVIDENCE_OS_NATIVE_PRODUCTION", "1")
        .env("EVIDENCE_OS_CANONICAL_ROOT", canonical_production_root()?);
    Ok(command)
}

struct WorkerProcess {
    child: std::process::Child,
    stdin: std::process::ChildStdin,
    stdout: BufReader<std::process::ChildStdout>,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
}

#[derive(Clone, Default)]
struct PersistentWorker {
    inner: Arc<Mutex<Option<WorkerProcess>>>,
}

#[derive(Clone, Default)]
struct MetricsWorker(PersistentWorker);

impl PersistentWorker {
    fn spawn() -> Result<WorkerProcess, String> {
        let mut command = worker_process_command()?;
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            const CREATE_DEFAULT_ERROR_MODE: u32 = 0x04000000;
            command.creation_flags(CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE);
        }
        let mut child = command
            .spawn()
            .map_err(|err| format!("WORKER_START_FAILED: {err}"))?;
        apply_balanced_active_priority_to_child(&child);
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "WORKER_STDIN_NOT_CAPTURED".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "WORKER_STDOUT_NOT_CAPTURED".to_string())?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "WORKER_STDERR_NOT_CAPTURED".to_string())?;
        let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(32)));
        let stderr_tail_writer = Arc::clone(&stderr_tail);
        thread::spawn(move || {
            let mut reader = BufReader::new(stderr);
            loop {
                let mut line = String::new();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }
                        if let Ok(mut tail) = stderr_tail_writer.lock() {
                            if tail.len() == 32 {
                                tail.pop_front();
                            }
                            tail.push_back(trimmed.to_string());
                        }
                    }
                }
            }
        });
        Ok(WorkerProcess {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            stderr_tail,
        })
    }

    fn ensure(&self) -> Result<(), String> {
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| "WORKER_LOCK_POISONED".to_string())?;
        if guard.is_none() {
            *guard = Some(Self::spawn()?);
        }
        Ok(())
    }

    fn request(&self, app: &AppHandle, mut request: Value) -> Result<Value, String> {
        if let Some(request_object) = request.as_object_mut() {
            let payload = request_object
                .entry("payload")
                .or_insert_with(|| serde_json::json!({}));
            if let Some(payload_object) = payload.as_object_mut() {
                payload_object.insert("tauri_pid".into(), serde_json::json!(std::process::id()));
            }
        }
        let request_id = request
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let body =
            serde_json::to_vec(&request).map_err(|err| format!("REQUEST_ENCODE_FAILED: {err}"))?;
        for attempt in 0..2 {
            let mut guard = self
                .inner
                .lock()
                .map_err(|_| "WORKER_LOCK_POISONED".to_string())?;
            if guard.is_none() {
                *guard = Some(Self::spawn()?);
            }
            let worker = guard.as_mut().expect("worker initialized");
            let exchange = (|| {
                worker
                    .stdin
                    .write_all(&body)
                    .and_then(|_| worker.stdin.write_all(b"\n"))
                    .and_then(|_| worker.stdin.flush())
                    .map_err(|err| format!("WORKER_STDIN_FAILED: {err}"))?;
                loop {
                    let mut line = String::new();
                    if worker
                        .stdout
                        .read_line(&mut line)
                        .map_err(|err| format!("WORKER_STDOUT_READ_FAILED: {err}"))?
                        == 0
                    {
                        let stderr = worker
                            .stderr_tail
                            .lock()
                            .map(|tail| tail.iter().cloned().collect::<Vec<_>>().join(" | "))
                            .unwrap_or_default();
                        return Err(if stderr.is_empty() {
                            "WORKER_STREAM_CLOSED".to_string()
                        } else {
                            format!("WORKER_STREAM_CLOSED: {stderr}")
                        });
                    }
                    let trimmed = line.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Value>(trimmed) {
                        Ok(value) if value.get("type").and_then(Value::as_str) == Some("event") => {
                            let _ = app.emit("worker-event", value);
                        }
                        Ok(value)
                            if value.get("type").and_then(Value::as_str) == Some("response")
                                && value.get("id").and_then(Value::as_str)
                                    == Some(request_id.as_str()) =>
                        {
                            return Ok(value);
                        }
                        Ok(_) => {}
                        Err(_) => {
                            let _ = app.emit("worker-event", serde_json::json!({"type":"event","event":"task.log","payload":{"line":trimmed}}));
                        }
                    }
                }
            })();
            if exchange.is_ok() || attempt == 1 {
                return exchange;
            }
            if let Some(mut failed) = guard.take() {
                let _ = failed.child.kill();
                let _ = failed.child.wait();
            }
        }
        Err("WORKER_NO_RESPONSE".to_string())
    }
}

#[tauri::command]
async fn worker_command(
    app: AppHandle,
    worker: State<'_, PersistentWorker>,
    metrics_worker: State<'_, MetricsWorker>,
    request: Value,
) -> Result<Value, String> {
    let command = request
        .get("command")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let worker = if matches!(
        command,
        "metrics.snapshot" | "process.metrics.snapshot" | "processes.metrics.snapshot"
            | "runtime.snapshot"
    ) {
        metrics_worker.inner().0.clone()
    } else {
        worker.inner().clone()
    };
    tauri::async_runtime::spawn_blocking(move || worker.request(&app, request))
        .await
        .map_err(|err| format!("WORKER_JOIN_FAILED: {err}"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn choose_paths(
    window: WebviewWindow,
    kind: String,
    multiple: bool,
    filters: Vec<String>,
) -> Result<Vec<String>, String> {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
    let dialog_window = window.clone();
    let selected = tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = rfd::FileDialog::new();
        dialog = dialog.set_parent(&dialog_window);
        let clean: Vec<String> = filters
            .into_iter()
            .map(|item| item.trim().trim_start_matches('.').to_ascii_lowercase())
            .filter(|item| !item.is_empty() && item != "*")
            .collect();
        if kind != "folder" && !clean.is_empty() {
            let refs: Vec<&str> = clean.iter().map(String::as_str).collect();
            dialog = dialog.add_filter("Classified source files", &refs);
        }
        let picked = if kind == "folder" {
            dialog
                .pick_folder()
                .map(|path| vec![path])
                .unwrap_or_default()
        } else if multiple {
            dialog.pick_files().unwrap_or_default()
        } else {
            dialog
                .pick_file()
                .map(|path| vec![path])
                .unwrap_or_default()
        };
        let governed = picked.into_iter().filter(|path| {
            if kind == "folder" || clean.is_empty() {
                return true;
            }
            path.extension()
                .and_then(|extension| extension.to_str())
                .map(|extension| {
                    clean
                        .iter()
                        .any(|allowed| allowed.eq_ignore_ascii_case(extension))
                })
                .unwrap_or(false)
        });
        Ok::<Vec<String>, String>(governed.map(|path| path.display().to_string()).collect())
    })
    .await
    .map_err(|err| format!("DIALOG_JOIN_FAILED: {err}"))??;
    let _ = window.show();
    let _ = window.set_focus();
    Ok(selected)
}

fn workspace_root_preference_path(_app: &AppHandle) -> Result<PathBuf, String> {
    Ok(canonical_production_root_ready()?.join("config").join("workspace-root.txt"))
}

#[tauri::command]
fn load_workspace_root(app: AppHandle) -> Result<Option<String>, String> {
    let canonical = canonical_production_root_ready()?;
    let config_path = workspace_root_preference_path(&app)?;
    if !config_path.is_file() {
        return Ok(Some(canonical.display().to_string()));
    }
    let value = fs::read_to_string(&config_path)
        .map_err(|err| format!("WORKSPACE_ROOT_READ_FAILED: {err}"))?;
    let root = PathBuf::from(value.trim());
    if !root.is_absolute() || !root.is_dir() {
        return Err("CANONICAL_PRODUCTION_ROOT_NOT_READY".to_string());
    }
    let resolved = fs::canonicalize(&root)
        .map_err(|err| format!("WORKSPACE_ROOT_RESOLVE_FAILED: {err}"))?;
    if resolved != canonical {
        return Err("NON_CANONICAL_PRODUCTION_ROOT_FORBIDDEN".to_string());
    }
    Ok(Some(canonical.display().to_string()))
}

#[tauri::command]
fn save_workspace_root(app: AppHandle, path: String) -> Result<String, String> {
    let canonical = canonical_production_root_ready()?;
    let root = PathBuf::from(path.trim());
    if !root.is_absolute() || !root.is_dir() {
        return Err("WORKSPACE_ROOT_MUST_BE_AN_EXISTING_ABSOLUTE_DIRECTORY".to_string());
    }
    let resolved = fs::canonicalize(&root)
        .map_err(|err| format!("WORKSPACE_ROOT_RESOLVE_FAILED: {err}"))?;
    if resolved != canonical {
        return Err("NON_CANONICAL_PRODUCTION_ROOT_FORBIDDEN".to_string());
    }
    let config_path = workspace_root_preference_path(&app)?;
    if let Some(parent) = config_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("WORKSPACE_ROOT_CONFIG_CREATE_FAILED: {err}"))?;
    }
    fs::write(&config_path, canonical.display().to_string())
        .map_err(|err| format!("WORKSPACE_ROOT_SAVE_FAILED: {err}"))?;
    Ok(canonical.display().to_string())
}

#[tauri::command]
fn record_full_app_boot_probe(mut probe: Value) -> Result<String, String> {
    if probe.get("frontend_contract").and_then(Value::as_str) != Some("T023_FULL_APP_FRONTEND_V2") {
        return Err("BOOT_PROBE_FRONTEND_CONTRACT_INVALID".to_string());
    }
    if probe.get("backend_contract").and_then(Value::as_str) != Some("T023_FULL_APP_BACKEND_V2") {
        return Err("BOOT_PROBE_BACKEND_CONTRACT_INVALID".to_string());
    }
    let destination = match env::var("EVIDENCE_OS_BOOT_PROBE_PATH") {
        Ok(value) if !value.trim().is_empty() => PathBuf::from(value),
        _ => return Ok("BOOT_PROBE_DISABLED".to_string()),
    };
    if !destination.is_absolute() {
        return Err("BOOT_PROBE_PATH_MUST_BE_ABSOLUTE".to_string());
    }
    if let Some(object) = probe.as_object_mut() {
        object.insert("tauri_pid".into(), serde_json::json!(std::process::id()));
        object.insert("recorded_by".into(), serde_json::json!("TAURI_RUST_HOST"));
    }
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("BOOT_PROBE_DIRECTORY_CREATE_FAILED: {err}"))?;
    }
    let body = serde_json::to_vec_pretty(&probe)
        .map_err(|err| format!("BOOT_PROBE_ENCODE_FAILED: {err}"))?;
    let temporary = destination.with_extension(format!("tmp.{}", std::process::id()));
    fs::write(&temporary, body).map_err(|err| format!("BOOT_PROBE_WRITE_FAILED: {err}"))?;
    if destination.exists() {
        fs::remove_file(&destination).map_err(|err| format!("BOOT_PROBE_REPLACE_FAILED: {err}"))?;
    }
    fs::rename(&temporary, &destination)
        .map_err(|err| format!("BOOT_PROBE_COMMIT_FAILED: {err}"))?;
    Ok(format!("BOOT_PROBE_RECORDED:{}", destination.display()))
}

fn main() {
    #[cfg(feature = "embedded-worker")]
    if let Some(exit_code) = run_cli_self_test_if_requested() {
        std::process::exit(exit_code);
    }
    apply_balanced_active_priority_to_current_process();
    tauri::Builder::default()
        .manage(PersistentWorker::default())
        .manage(MetricsWorker::default())
        .invoke_handler(tauri::generate_handler![
            worker_command,
            choose_paths,
            load_workspace_root,
            save_workspace_root,
            open_chrome_profile,
            open_native_folder,
            handoff_external_window,
            record_full_app_boot_probe
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_size(Size::Logical(LogicalSize {
                    width: 1620.0,
                    height: 940.0,
                }));
                let _ = window.center();
                let _ = window.show();
                let _ = window.set_focus();
            }

            // Keep the accepted independent metrics worker and registry
            // prewarm, but never hold the embedded UI behind two large
            // one-file worker startups. The first real IPC request shares the
            // same locks and either reuses the prewarmed channel or waits for
            // its truthful result.
            let persistent_worker = app.state::<PersistentWorker>().inner().clone();
            let metrics_worker = app.state::<MetricsWorker>().inner().0.clone();
            let app_handle = app.handle().clone();
            thread::spawn(move || {
                let registry_bootstrap = persistent_worker.request(
                    &app_handle,
                    serde_json::json!({
                        "type": "request",
                        "id": "native_process_registry_bootstrap",
                        "command": "processes.snapshot",
                        "payload": {}
                    }),
                );
                let metrics_bootstrap = metrics_worker.request(
                    &app_handle,
                    serde_json::json!({
                        "type": "request",
                        "id": "native_metrics_bootstrap",
                        "command": "process.metrics.snapshot",
                        "payload": {}
                    }),
                );
                let _ = app_handle.emit(
                    "native-bootstrap",
                    serde_json::json!({
                        "schema": "T023_NATIVE_BACKGROUND_PREWARM_V1",
                        "registry_ok": registry_bootstrap.as_ref().ok().and_then(|value| value.get("ok")).and_then(Value::as_bool) == Some(true),
                        "metrics_ok": metrics_bootstrap.as_ref().ok().and_then(|value| value.get("ok")).and_then(Value::as_bool) == Some(true),
                        "window_blocked": false
                    }),
                );
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Evidence OS");
}

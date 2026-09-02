// Установщик Vera — отдельная нативная оболочка (Rust, Tauri: окно + WebView2).
//
// Три P0 из видения владельца (installer/ui/OWNER_VISION.md) живут здесь:
//   1. один экземпляр: повторный запуск поднимает и фокусирует живое окно
//      (плагин single-instance регистрируется ПЕРВЫМ — до любого окна);
//   2. закрыть окно = завершить процесс: трея у установщика нет по построению,
//      поведение основной программы «закрыть в трей» сюда не наследуется;
//   3. открывается развёрнутым окном; кадр сцены — Full HD, масштаб считает UI.
//
// Окно рождается невидимым и показывается из UI после первого кадра: иначе
// на тёмной системной теме мигнул бы белый прямоугольник WebView2.
//
// `vera-setup.exe --uninstall [--purge]` — удаление без окна: программа,
// ярлыки, запись в «Приложениях», служба; данные остаются, если не --purge.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod install;

use std::process::Command;
use std::time::Duration;

use tauri::{Emitter, Manager};

fn focus_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[tauri::command]
fn defaults() -> install::Defaults {
    install::defaults()
}

#[tauri::command]
async fn probe_model(base_url: String, key: String) -> serde_json::Value {
    let (ok, note, models) = tauri::async_runtime::spawn_blocking(move || install::probe_model(&base_url, &key))
        .await
        .unwrap_or((false, "проверка не выполнилась".into(), Vec::new()));
    serde_json::json!({ "ok": ok, "note": note, "models": models })
}

#[tauri::command]
fn relay_login() -> Result<String, String> {
    install::relay_login()
}

#[tauri::command]
fn relay_status() -> String {
    install::relay_status()
}

/// Установка целиком; ход — событиями `install-progress`, итог — распиской.
#[tauri::command]
async fn install(app: tauri::AppHandle, setup: install::Setup) -> Result<install::Receipt, String> {
    tauri::async_runtime::spawn_blocking(move || {
        install::install(&setup, |p| {
            let _ = app.emit("install-progress", p);
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Открыть установленный Vera и закрыть установщик.
#[tauri::command]
fn open_frame(app: tauri::AppHandle, exe: String) -> Result<(), String> {
    let path = std::path::PathBuf::from(&exe);
    let mut cmd = Command::new(&path);
    if let Some(dir) = path.parent() {
        cmd.current_dir(dir);
    }
    cmd.spawn().map_err(|e| format!("Vera не запустилась: {e}"))?;
    // Окно установщика прячем сразу, а процесс держим ещё несколько секунд:
    // Windows отдаёт передний план новому окну, только пока запустивший его
    // процесс жив. Иначе Vera открывалась позади других окон, и казалось,
    // что не открылся вовсе.
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        install::relay_abort();
        app.exit(0);
    });
    Ok(())
}

fn message_box(text: &str) {
    let script = format!(
        "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('{}', '{}') | Out-Null",
        text.replace('\'', "''"),
        install::PRODUCT
    );
    let _ = Command::new("powershell.exe")
        .args(["-NoProfile", "-Command", &script])
        .status();
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // Безоконная установка по готовому JSON решений: для проверок и тихой
    // установки. Ход и итог — в install.log рядом с установщиком.
    if let Some(i) = args.iter().position(|a| a == "--install") {
        let Some(path) = args.get(i + 1) else {
            message_box("--install требует путь к JSON с решениями");
            return;
        };
        let log_path = install::exe_dir().join("install.log");
        let mut log = String::new();
        let result = std::fs::read_to_string(path)
            .map_err(|e| format!("{path}: {e}"))
            .and_then(|raw| serde_json::from_str::<install::Setup>(&raw).map_err(|e| e.to_string()))
            .and_then(|setup| {
                install::install(&setup, |p| {
                    log.push_str(&format!("[{}/{}] {}
", p.step, p.total, p.label));
                })
            });
        match &result {
            Ok(r) => log.push_str(&format!("OK {}
", serde_json::to_string(r).unwrap_or_default())),
            Err(e) => log.push_str(&format!("FAIL {e}
")),
        }
        let _ = std::fs::write(&log_path, &log);
        let failed = result.is_err();
        if !args.iter().any(|a| a == "--quiet") {
            message_box(&match result {
                Ok(r) => format!("Vera установлена в {}", r.dir),
                Err(e) => format!("Установка не удалась: {e}"),
            });
        }
        if failed {
            std::process::exit(1);
        }
        return;
    }
    if args.iter().any(|a| a == "--uninstall") {
        let purge = args.iter().any(|a| a == "--purge");
        let quiet = args.iter().any(|a| a == "--quiet");
        let text = match install::uninstall(purge) {
            Ok(text) => text,
            Err(err) => format!("Удаление не удалось: {err}"),
        };
        let _ = std::fs::write(std::env::temp_dir().join("vera-uninstall.log"), &text);
        if !quiet {
            message_box(&text);
        }
        install::uninstall_finish();
        return;
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main(app);
        }))
        .invoke_handler(tauri::generate_handler![defaults, install, open_frame, probe_model, relay_login, relay_status])
        .setup(|app| {
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .title("Установка Vera")
            .inner_size(1600.0, 900.0)
            .min_inner_size(960.0, 600.0)
            .center()
            .maximized(true)
            .decorations(false)
            .shadow(true)
            .visible(false)
            .build()?;
            Ok(())
        })
        // Закрыли установщик посреди входа в ChatGPT — помощник входа не
        // должен остаться держать порт.
        .on_window_event(|_window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                install::relay_abort();
            }
        })
        .build(tauri::generate_context!())
        .expect("окно установщика не поднялось")
        // Любой выход установщика — и отменённый вход в ChatGPT не остаётся
        // висеть с занятым портом.
        .run(|_app, event| {
            if let tauri::RunEvent::Exit = event {
                install::relay_abort();
            }
        });
}

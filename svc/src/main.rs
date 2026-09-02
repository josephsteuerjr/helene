//! vera-svc — ОПЦИОНАЛЬНАЯ Windows-служба харнесса (решение владельца 31.08).
//!
//! По умолчанию продукт живёт без службы: харнесс — дочерний процесс окна/трея.
//! Служба добавляет ровно одно: агент жив до логина и без окна. Паттерн — тот же,
//! что у виндоус-тела (praxis-system-router супервизит исполнителя): служба
//! супервизит deskapp+runner теми же правилами, что оболочка (shell/main.rs).
//!
//! Ограничение по построению: из Session 0 интерактивный стол не виден — руки
//! computer-use всегда живут в трее сессии; служба их не заменяет.
//!
//! CLI:
//!   frame-svc install --config C:\...\vera.json   (требует админа, один UAC)
//!   frame-svc uninstall
//!   frame-svc run --config <path>                 (вход SCM; руками не звать)
//!   frame-svc foreground --config <path>          (та же логика в консоли — отладка)

use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use windows_service::service::{
    ServiceAccess, ServiceControl, ServiceControlAccept, ServiceErrorControl,
    ServiceExitCode, ServiceInfo, ServiceStartType, ServiceState, ServiceStatus,
    ServiceType,
};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
use windows_service::{define_windows_service, service_dispatcher};

const SERVICE_NAME: &str = "Vera";
const SERVICE_DISPLAY: &str = "Vera · агент";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn now_stamp() -> String {
    // Без chrono: миллисекунды с эпохи достаточно для журнала службы.
    let ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("[{ms}]")
}

struct Log(Option<std::fs::File>);

impl Log {
    fn open(dir: &Path) -> Self {
        let _ = std::fs::create_dir_all(dir);
        Log(std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("service.log"))
            .ok())
    }
    fn line(&mut self, text: &str) {
        eprintln!("{text}");
        if let Some(f) = &mut self.0 {
            let _ = writeln!(f, "{} {}", now_stamp(), text);
        }
    }
}

fn arg_after(flag: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn resolve(base: &Path, raw: &str) -> PathBuf {
    let p = PathBuf::from(raw);
    if p.is_absolute() { p } else { base.join(p) }
}

/// Те же правила запуска ребёнка, что у оболочки (shell/main.rs::spawn_child):
/// одна семантика запуска, два супервизора — окно и служба.
fn spawn_child(python: &Path, script: &Path, args: &[String], tree: &Path, host: &str) -> Option<Child> {
    #[cfg(windows)]
    use std::os::windows::process::CommandExt;
    let mut cmd = Command::new(python);
    cmd.arg("-u")
        .arg(script)
        .args(args)
        .env("VERA_TREE", tree)
        .env("VERA_HOST", host)
        .env("PYTHONUTF8", "1");
    if let Some(dir) = script.parent() {
        cmd.current_dir(dir);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.spawn().ok()
}

struct Plan {
    python: PathBuf,
    app: Option<PathBuf>,
    runner: Option<PathBuf>,
    config: PathBuf,
    tree: PathBuf,
    port: u16,
    relay_enabled: bool,
    relay_port: u16,
    relay_key: String,
    phone: bool,
}

fn load_plan(config_path: &Path) -> Result<Plan, String> {
    let raw = std::fs::read_to_string(config_path)
        .map_err(|e| format!("vera.json не читается: {e}"))?;
    let cfg: serde_json::Value =
        serde_json::from_str(&raw).map_err(|e| format!("vera.json не разобрался: {e}"))?;
    let base = config_path.parent().unwrap_or(Path::new(".")).to_path_buf();
    let python = match cfg.get("python").and_then(|v| v.as_str()) {
        Some(raw) if raw != "python" => resolve(&base, raw),
        _ => {
            let embedded = base.join("runtime").join("python.exe");
            if embedded.exists() { embedded } else { PathBuf::from("python") }
        }
    };
    let tree = resolve(
        &base,
        cfg.get("tree").and_then(|v| v.as_str()).unwrap_or("data"),
    );
    let _ = std::fs::create_dir_all(&tree);
    Ok(Plan {
        python,
        app: cfg
            .get("app")
            .and_then(|v| v.as_str())
            .map(|r| resolve(&base, r)),
        runner: cfg
            .get("runner")
            .and_then(|v| v.as_str())
            .map(|r| resolve(&base, r)),
        config: config_path.to_path_buf(),
        tree,
        port: cfg.get("port").and_then(|v| v.as_u64()).unwrap_or(8094) as u16,
        relay_enabled: cfg
            .get("relay")
            .and_then(|r| r.get("enabled"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        relay_port: cfg
            .get("relay")
            .and_then(|r| r.get("port"))
            .and_then(|v| v.as_u64())
            .unwrap_or(5011) as u16,
        relay_key: cfg
            .get("model")
            .and_then(|m| m.get("key"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        phone: cfg
            .get("phone")
            .and_then(|p| p.get("enabled"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
    })
}

/// Супервизор: держать обоих детей живыми, пока не попросили остановиться.
/// Упавший ребёнок перезапускается с нарастающей паузой (5→60 с) — быстрая
/// смерть подряд не должна крутить машину в горячем цикле.
fn spawn_relay(plan: &Plan) -> Option<Child> {
    // Та же семантика, что у оболочки (shell/main.rs::spawn_relay): реле живёт
    // в data/relay, конфигурируется окружением, консоли не имеет.
    let base = plan.config.parent()?.to_path_buf();
    let exe = base.join("vera-relay.exe");
    if !exe.exists() {
        return None;
    }
    let home = plan.tree.join("relay");
    let _ = std::fs::create_dir_all(&home);
    let mut cmd = Command::new(&exe);
    cmd.arg("serve")
        .current_dir(&home)
        .env("RELAY_PORT", plan.relay_port.to_string())
        .env("RELAY_LOCAL", "1")
        .env("RELAY_LOG_DIR", home.join("logs"));
    if !plan.relay_key.trim().is_empty() {
        // Тот же контракт, что у оболочки: ключ мозга обязателен Bearer-ом.
        cmd.env("RELAY_API_KEY", &plan.relay_key);
    }
    let python = base.join("runtime").join("python.exe");
    if python.exists() {
        cmd.env("RELAY_PYTHON", &python);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn().ok()
}

fn supervise(plan: &Plan, stop: Arc<AtomicBool>, log: &mut Log) {
    let mut children: Vec<(String, Option<Child>, Vec<String>, PathBuf, Instant, u64)> = vec![];
    if let Some(app) = &plan.app {
        children.push((
            "труба".into(),
            None,
            vec![plan.port.to_string()],
            app.clone(),
            Instant::now(),
            0,
        ));
    }
    if let Some(runner) = &plan.runner {
        children.push((
            "руннер".into(),
            None,
            vec!["--config".into(), plan.config.to_string_lossy().into_owned()],
            runner.clone(),
            Instant::now(),
            0,
        ));
    }
    log.line(&format!(
        "служба: python={} · детей={} · порт={}",
        plan.python.display(),
        children.len(),
        plan.port
    ));
    let mut relay: Option<Child> = None;
    let mut relay_not_before = Instant::now();
    let mut relay_backoff: u64 = 0;
    while !stop.load(Ordering::Relaxed) {
        for (name, slot, args, script, not_before, backoff) in children.iter_mut() {
            let dead = match slot {
                Some(child) => child.try_wait().map(|s| s.is_some()).unwrap_or(true),
                None => true,
            };
            if dead && Instant::now() >= *not_before {
                if slot.is_some() {
                    log.line(&format!("{name}: умер — перезапускаю (пауза {backoff} c)"));
                }
                // Труба слушает Wi-Fi только если разрешён телефон; руннер — всегда петля.
                let host = if name == "труба" && plan.phone { "0.0.0.0" } else { "127.0.0.1" };
                *slot = spawn_child(&plan.python, script, args, &plan.tree, host);
                if slot.is_none() {
                    log.line(&format!("{name}: не поднялся ({})", script.display()));
                }
                *backoff = (*backoff * 2).clamp(5, 60);
                *not_before = Instant::now() + Duration::from_secs(*backoff);
            }
        }
        if plan.relay_enabled {
            let dead = match &mut relay {
                Some(child) => child.try_wait().map(|s| s.is_some()).unwrap_or(true),
                None => true,
            };
            if dead && Instant::now() >= relay_not_before {
                if relay.is_some() {
                    log.line(&format!("реле: умерло — перезапускаю (пауза {relay_backoff} c)"));
                }
                relay = spawn_relay(plan);
                if relay.is_none() {
                    log.line("реле: не поднялось (нет vera-relay.exe?)");
                }
                relay_backoff = (relay_backoff * 2).clamp(5, 60);
                relay_not_before = Instant::now() + Duration::from_secs(relay_backoff);
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    log.line("служба: остановка — гашу детей");
    for (_, slot, ..) in children.iter_mut() {
        if let Some(child) = slot {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    if let Some(child) = &mut relay {
        let _ = child.kill();
        let _ = child.wait();
    }
}

define_windows_service!(ffi_service_main, service_main);

fn service_main(_arguments: Vec<OsString>) {
    let config = arg_after("--config")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_exe().unwrap().with_file_name("vera.json"));
    let plan = match load_plan(&config) {
        Ok(p) => p,
        Err(_) => return,
    };
    let mut log = Log::open(&plan.tree);
    let stop = Arc::new(AtomicBool::new(false));
    let stop_handler = stop.clone();
    let handler = move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown => {
            stop_handler.store(true, Ordering::Relaxed);
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = match service_control_handler::register(SERVICE_NAME, handler) {
        Ok(s) => s,
        Err(_) => return,
    };
    let running = ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: ServiceState::Running,
        controls_accepted: ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
        exit_code: ServiceExitCode::Win32(0),
        checkpoint: 0,
        wait_hint: Duration::default(),
        process_id: None,
    };
    let _ = status.set_service_status(running.clone());
    supervise(&plan, stop, &mut log);
    let _ = status.set_service_status(ServiceStatus {
        current_state: ServiceState::Stopped,
        controls_accepted: ServiceControlAccept::empty(),
        ..running
    });
}

fn install(config: &Path) -> Result<(), String> {
    let manager = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
    )
    .map_err(|e| format!("SCM: {e} (запусти от администратора)"))?;
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from(SERVICE_DISPLAY),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: exe,
        launch_arguments: vec![
            OsString::from("run"),
            OsString::from("--config"),
            config.as_os_str().to_os_string(),
        ],
        dependencies: vec![],
        account_name: None, // LocalSystem: детям нужен доступ к дереву в любой учётке
        account_password: None,
    };
    let service = manager
        .create_service(&info, ServiceAccess::START | ServiceAccess::CHANGE_CONFIG)
        .map_err(|e| format!("создание службы: {e}"))?;
    let _ = service.start::<&str>(&[]);
    println!("служба {SERVICE_NAME} установлена (autostart) и запущена");
    println!("конфиг: {}", config.display());
    Ok(())
}

fn uninstall() -> Result<(), String> {
    let manager =
        ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
            .map_err(|e| format!("SCM: {e} (запусти от администратора)"))?;
    let service = manager
        .open_service(
            SERVICE_NAME,
            ServiceAccess::STOP | ServiceAccess::DELETE | ServiceAccess::QUERY_STATUS,
        )
        .map_err(|e| format!("службы нет? {e}"))?;
    let _ = service.stop();
    // Дать SCM секунду погасить процесс прежде, чем удалять запись.
    std::thread::sleep(Duration::from_millis(1200));
    service.delete().map_err(|e| format!("удаление: {e}"))?;
    println!("служба {SERVICE_NAME} удалена");
    Ok(())
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    match mode.as_str() {
        "install" => {
            let config = arg_after("--config")
                .map(PathBuf::from)
                .unwrap_or_else(|| std::env::current_exe().unwrap().with_file_name("vera.json"))
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from("vera.json"));
            if let Err(e) = install(&config) {
                eprintln!("не установилась: {e}");
                std::process::exit(1);
            }
        }
        "uninstall" => {
            if let Err(e) = uninstall() {
                eprintln!("{e}");
                std::process::exit(1);
            }
        }
        "run" => {
            // Вход из SCM. Прямой запуск руками даст честную ошибку диспетчера.
            if service_dispatcher::start(SERVICE_NAME, ffi_service_main).is_err() {
                eprintln!("`run` зовёт SCM (это вход службы). Для отладки: foreground");
                std::process::exit(1);
            }
        }
        "foreground" => {
            let config = arg_after("--config")
                .map(PathBuf::from)
                .expect("foreground --config <path>");
            let plan = load_plan(&config).unwrap_or_else(|e| {
                eprintln!("{e}");
                std::process::exit(1);
            });
            let mut log = Log::open(&plan.tree);
            let stop = Arc::new(AtomicBool::new(false));
            let stop_ctrlc = stop.clone();
            let _ = ctrlc_handler(stop_ctrlc);
            supervise(&plan, stop, &mut log);
        }
        _ => {
            eprintln!("frame-svc install|uninstall|run|foreground [--config vera.json]");
            std::process::exit(2);
        }
    }
}

/// Ctrl+C в foreground-режиме — без крейта ctrlc: SetConsoleCtrlHandler руками.
fn ctrlc_handler(stop: Arc<AtomicBool>) -> Result<(), ()> {
    use std::sync::OnceLock;
    static STOP: OnceLock<Arc<AtomicBool>> = OnceLock::new();
    STOP.set(stop).map_err(|_| ())?;
    #[cfg(windows)]
    unsafe {
        unsafe extern "system" fn handler(_ctrl: u32) -> i32 {
            if let Some(s) = STOP.get() {
                s.store(true, Ordering::Relaxed);
            }
            1
        }
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn SetConsoleCtrlHandler(
                handler: Option<unsafe extern "system" fn(u32) -> i32>,
                add: i32,
            ) -> i32;
        }
        SetConsoleCtrlHandler(Some(handler), 1);
    }
    Ok(())
}

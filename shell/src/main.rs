// Vera — нативная оболочка основной программы (Rust, Tauri: окно + WebView2,
// ни одного консольного окна по построению — windows_subsystem ниже).
//
// Архитектура продукта: харнесс — самопереписываемый Python-организм агента,
// его язык не меняется. Оболочка — то, что не самопереписывается: окно, рендер,
// связь, трей, уведомления. Где живёт харнесс, решает `vera.json` рядом с exe:
//
//   {"mode": "local", "python": "python", "app": "..\\deskapp.py",
//    "tree": "data", "port": 8094, "agent": {"name": "…"}}
//   {"mode": "remote", "base": "https://…", "key": "…"}
//
// local: оболочка поднимает харнесс дочерним тихим процессом (CREATE_NO_WINDOW)
// и втыкает связь в 127.0.0.1 — тот же протокол frame.desk.v1, что и к VPS.
// Файла нет — поведение прежнее: вшитый сборкой config.js (удалённый харнесс).
//
// Обязательное по решениям владельца 02.09:
//   * один экземпляр: повторный запуск поднимает и фокусирует живое окно;
//   * закрыть окно = в трей, и в первый раз об этом говорит уведомление;
//   * слово агента, когда окно не перед глазами, приходит уведомлением Windows;
//   * имя агента — из конфига; продукт зовётся Vera.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const PRODUCT: &str = "Vera";
const CONFIG_NAME: &str = "vera.json";
/// Комната окна в памяти агента: memory/groups/<WINDOW_ROOM>.jsonl.
const WINDOW_ROOM: &str = "window";
/// AppUserModelID уведомлений = identifier из tauri.conf.json (см. register_toast_identity).
const TOAST_ID: &str = "app.vera.desk";

/// Журнал оболочки рядом с exe: то, что иначе терялось бы без консоли.
fn log_line(text: &str) {
    use std::io::Write;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(exe_dir().join("vera.log"))
    {
        let _ = writeln!(f, "[{stamp}] {text}");
    }
}

/// Уведомление Windows. Ошибка не глотается: она уходит в vera.log.
#[cfg(windows)]
fn toast(title: &str, body: &str) {
    use tauri_winrt_notification::{Duration as ToastDuration, Toast};
    let result = Toast::new(TOAST_ID)
        .title(title)
        .text1(body)
        .duration(ToastDuration::Short)
        .show();
    if let Err(err) = result {
        log_line(&format!("уведомление не показалось: {err}"));
    }
}

#[cfg(not(windows))]
fn toast(_title: &str, _body: &str) {}

/// Как поднять ребёнка заново, если он упал: оболочка — надзиратель, а не
/// просто запускатель. Всё нужное для повторного spawn лежит здесь.
#[derive(Clone)]
enum ChildSpec {
    Script { python: PathBuf, script: PathBuf, args: Vec<String>, tree: PathBuf, host: String },
    Relay { base: PathBuf, cfg: serde_json::Value, tree: PathBuf },
}

impl ChildSpec {
    fn label(&self) -> String {
        match self {
            ChildSpec::Script { script, .. } => script
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default(),
            ChildSpec::Relay { .. } => "vera-relay.exe".into(),
        }
    }

    fn spawn(&self) -> Option<Child> {
        match self {
            ChildSpec::Script { python, script, args, tree, host } => spawn_child(python, script, args, tree, host),
            ChildSpec::Relay { base, cfg, tree } => spawn_relay(base, cfg, tree),
        }
    }
}

struct Managed {
    spec: ChildSpec,
    child: Child,
    falls: Vec<std::time::Instant>,
    retry_at: Option<std::time::Instant>,
}

struct LocalHarness {
    children: Mutex<Vec<Managed>>,
    stopping: std::sync::atomic::AtomicBool,
}

/// Кто здесь живёт: имя агента и дерево данных (None — удалённый режим).
struct Identity {
    agent: String,
    tree: Option<PathBuf>,
}

fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn resolve(base: &Path, raw: &str) -> PathBuf {
    let p = PathBuf::from(raw);
    if p.is_absolute() {
        p
    } else {
        base.join(p)
    }
}

/// Имя агента даёт владелец при установке (`agent.name`); старые конфиги
/// держали его в `telegram.agent_name`. Пустое — честное «Агент».
fn agent_name(cfg: Option<&serde_json::Value>) -> String {
    let pick = |v: Option<&serde_json::Value>| {
        v.and_then(|s| s.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(String::from)
    };
    cfg.and_then(|c| {
        pick(c.get("agent").and_then(|a| a.get("name")))
            .or_else(|| pick(c.get("telegram").and_then(|t| t.get("agent_name"))))
    })
    .unwrap_or_else(|| "Агент".to_string())
}

/// Питон для харнесса: явный из конфига → runtime/python.exe рядом с exe
/// (embedded CPython поставки) → системный из PATH.
fn python_path(base: &Path, cfg: &serde_json::Value) -> PathBuf {
    if let Some(raw) = cfg.get("python").and_then(|v| v.as_str()) {
        if raw != "python" {
            return resolve(base, raw);
        }
    }
    let embedded = base.join("runtime").join("python.exe");
    if embedded.exists() {
        return embedded;
    }
    PathBuf::from("python")
}

/// Телефон подключается по Wi-Fi: труба слушает все адреса, а не только петлю.
/// Доступ не с петли deskapp даёт только по ключу устройства.
fn phone_enabled(cfg: &serde_json::Value) -> bool {
    cfg.get("phone")
        .and_then(|p| p.get("enabled"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// Дети живут ровно столько, сколько окно: job-объект с KILL_ON_JOB_CLOSE.
/// Убили окно из диспетчера — дети не остаются сиротами держать порт и файлы.
#[cfg(windows)]
mod job {
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct Job(HANDLE);
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    impl Job {
        pub fn new() -> Option<Job> {
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return None;
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const std::ffi::c_void,
                    std::mem::size_of_val(&info) as u32,
                );
                if ok == 0 {
                    return None;
                }
                Some(Job(handle))
            }
        }

        pub fn adopt(&self, child: &std::process::Child) {
            use std::os::windows::io::AsRawHandle;
            unsafe {
                AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE);
            }
        }
    }
}

#[cfg(windows)]
static JOB: std::sync::OnceLock<Option<job::Job>> = std::sync::OnceLock::new();

/// Приписать ребёнка к job-объекту окна (ничего не делает вне Windows).
fn adopt(child: &Child) {
    #[cfg(windows)]
    if let Some(job) = JOB.get_or_init(job::Job::new).as_ref() {
        job.adopt(child);
    }
    #[cfg(not(windows))]
    let _ = child;
}

fn spawn_child(python: &Path, script: &Path, args: &[String], tree: &Path, host: &str) -> Option<Child> {
    let mut cmd = Command::new(python);
    cmd.arg(script)
        .args(args)
        .env("VERA_TREE", tree)
        .env("VERA_HOST", host)
        .env("PYTHONUTF8", "1");
    if let Some(dir) = script.parent() {
        cmd.current_dir(dir);
    }
    // Вывод ребёнка — в файл рядом с данными (deskapp.log, runner.log): без
    // этого падение харнесса не оставляло следов.
    if let Some(log) = child_log(tree, script) {
        if let Ok(err) = log.try_clone() {
            cmd.stdout(Stdio::from(log)).stderr(Stdio::from(err));
        }
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    match cmd.spawn() {
        Ok(child) => {
            adopt(&child);
            Some(child)
        }
        Err(err) => {
            eprintln!("дочерний процесс {script:?} не поднялся: {err}");
            None
        }
    }
}

/// Файл вывода ребёнка; больше 5 МБ — уходит в .1, чтобы диск не пух.
fn child_log(tree: &Path, script: &Path) -> Option<std::fs::File> {
    let stem = script.file_stem()?.to_string_lossy().into_owned();
    let path = tree.join(format!("{stem}.log"));
    if let Ok(meta) = std::fs::metadata(&path) {
        if meta.len() > 5 * 1024 * 1024 {
            let _ = std::fs::rename(&path, tree.join(format!("{stem}.log.1")));
        }
    }
    std::fs::OpenOptions::new().create(true).append(true).open(path).ok()
}

/// Харнесс уже жив на этом порту? (служба или другое окно.)
/// Проба — только коннект: держатель порта и есть харнесс по построению,
/// а поднимать второго на занятый порт бессмысленно в любом случае.
fn harness_alive(port: u16) -> bool {
    use std::net::{SocketAddr, TcpStream};
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok()
}

/// Встроенное реле подписки ChatGPT (отдельный exe в поставке). Поднимается
/// ребёнком, когда vera.json просит: relay.enabled. Дом реле — data/relay:
/// учётные данные живут в папке продукта и переезжают вместе с ней.
fn spawn_relay(base: &Path, cfg: &serde_json::Value, tree: &Path) -> Option<Child> {
    let relay = cfg.get("relay")?;
    if !relay
        .get("enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        return None;
    }
    let exe = base.join("vera-relay.exe");
    if !exe.exists() {
        eprintln!("relay.enabled, но vera-relay.exe рядом нет — реле не поднимаю");
        return None;
    }
    let port = relay.get("port").and_then(|v| v.as_u64()).unwrap_or(5011);
    let home = tree.join("relay");
    let _ = std::fs::create_dir_all(&home);
    let python = base.join("runtime").join("python.exe");
    let mut cmd = Command::new(&exe);
    cmd.arg("serve")
        .current_dir(&home)
        .env("RELAY_PORT", port.to_string())
        .env("RELAY_LOCAL", "1")
        .env("RELAY_LOG_DIR", home.join("logs"));
    // Ключ мозга = ключ реле: сгенерированный при установке ключ обязателен
    // Bearer-ом на /chat/completions — открытый локальный порт позволял бы
    // любому процессу на машине жечь подписку владельца.
    if let Some(key) = cfg
        .get("model")
        .and_then(|m| m.get("key"))
        .and_then(|v| v.as_str())
        .filter(|k| !k.trim().is_empty())
    {
        cmd.env("RELAY_API_KEY", key);
    }
    if python.exists() {
        cmd.env("RELAY_PYTHON", &python);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    match cmd.spawn() {
        Ok(child) => {
            adopt(&child);
            Some(child)
        }
        Err(err) => {
            eprintln!("реле не поднялось: {err}");
            None
        }
    }
}

/// Локальный режим: труба (deskapp) + руннер (localharness) — оба дети окна.
/// Если харнесс уже держит порт (служба или второе окно) — детей не плодим:
/// окно становится чистым клиентом.
fn spawn_local(base: &Path, cfg: &serde_json::Value) -> (Vec<Managed>, u16, PathBuf) {
    let port = cfg.get("port").and_then(|v| v.as_u64()).unwrap_or(8094) as u16;
    let tree = resolve(
        base,
        cfg.get("tree").and_then(|v| v.as_str()).unwrap_or("data"),
    );
    if harness_alive(port) {
        eprintln!("харнесс уже жив на 127.0.0.1:{port} — подключаюсь без своих детей");
        return (Vec::new(), port, tree);
    }
    let python = python_path(base, cfg);
    let app = resolve(
        base,
        cfg.get("app")
            .and_then(|v| v.as_str())
            .unwrap_or("deskapp.py"),
    );
    let _ = std::fs::create_dir_all(&tree);
    let host = if phone_enabled(cfg) { "0.0.0.0" } else { "127.0.0.1" };
    let mut specs = vec![
        ChildSpec::Relay { base: base.to_path_buf(), cfg: cfg.clone(), tree: tree.clone() },
        ChildSpec::Script {
            python: python.clone(),
            script: app,
            args: vec![port.to_string()],
            tree: tree.clone(),
            host: host.into(),
        },
    ];
    if let Some(runner_raw) = cfg.get("runner").and_then(|v| v.as_str()) {
        let config = base.join(CONFIG_NAME).to_string_lossy().into_owned();
        specs.push(ChildSpec::Script {
            python,
            script: resolve(base, runner_raw),
            args: vec!["--config".into(), config],
            tree: tree.clone(),
            host: "127.0.0.1".into(),
        });
    }
    let children = specs
        .into_iter()
        .filter_map(|spec| {
            spec.spawn()
                .map(|child| Managed { spec, child, falls: Vec::new(), retry_at: None })
        })
        .collect();
    (children, port, tree)
}

/// Не настроено: явного receipt ещё нет и ключ модели тоже пуст.
/// `setup_complete` нужен не для красоты: у локальных Ollama/LM Studio ключа по
/// построению нет, поэтому проверка только `model.key` считала бы их ненастроенными.
fn unconfigured(cfg: &Option<serde_json::Value>) -> bool {
    match cfg {
        None => true,
        Some(cfg) => {
            if cfg
                .get("setup_complete")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            {
                return false;
            }
            cfg.get("model")
                .and_then(|m| m.get("key"))
                .and_then(|k| k.as_str())
                .map(|k| k.trim().is_empty())
                .unwrap_or(true)
        }
    }
}

/// Записать конфиг целиком (экран настроек). Атомарно: tmp + rename.
#[tauri::command]
fn config_save(config: String) -> Result<(), String> {
    let parsed: serde_json::Value =
        serde_json::from_str(&config).map_err(|e| format!("это не JSON: {e}"))?;
    let pretty = serde_json::to_string_pretty(&parsed).map_err(|e| e.to_string())?;
    let target = exe_dir().join(CONFIG_NAME);
    let tmp = exe_dir().join(format!(".tmp-{CONFIG_NAME}"));
    std::fs::write(&tmp, pretty).map_err(|e| format!("не записалось: {e}"))?;
    std::fs::rename(&tmp, &target).map_err(|e| format!("не подменилось: {e}"))?;
    Ok(())
}

/// Перезапуск начисто: новый процесс прочтёт конфиг и поднимет харнесс обычным путём.
#[tauri::command]
fn restart_self(app: tauri::AppHandle) {
    let exe = std::env::current_exe().ok();
    if let Some(exe) = exe {
        let _ = Command::new(exe).spawn();
    }
    app.exit(0);
}

/// Незавершённый вход в ChatGPT: один за раз. Повторное нажатие отменяет
/// прежнюю попытку — иначе помощник реле держит порт 1455, и новые попытки
/// падают с 400 в браузере.
static LOGIN: Mutex<Option<Child>> = Mutex::new(None);

fn relay_abort() {
    let Ok(mut guard) = LOGIN.lock() else { return };
    if let Some(mut child) = guard.take() {
        if child.try_wait().ok().flatten().is_none() {
            let mut kill = Command::new("taskkill");
            kill.args(["/PID", &child.id().to_string(), "/T", "/F"]);
            let _ = run_hidden(&mut kill);
            let _ = child.wait();
        }
    }
}

#[tauri::command]
fn relay_login() -> Result<String, String> {
    // Логин реле в подписку ChatGPT: колбэк-сервер поднимает встроенный питон,
    // браузер открывается сам. Консоль не нужна — и не появляется.
    relay_abort();
    let base = exe_dir();
    let exe = base.join("vera-relay.exe");
    if !exe.exists() {
        return Err("в этой поставке нет vera-relay.exe".into());
    }
    let home = base.join("data").join("relay");
    let _ = std::fs::create_dir_all(&home);
    let mut cmd = Command::new(&exe);
    cmd.arg("login")
        .current_dir(&home)
        .env("RELAY_LOCAL", "1")
        .env("RELAY_LOG_DIR", home.join("logs"));
    let python = base.join("runtime").join("python.exe");
    if python.exists() {
        cmd.env("RELAY_PYTHON", &python);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let child = cmd.spawn().map_err(|e| format!("логин не запустился: {e}"))?;
    if let Ok(mut guard) = LOGIN.lock() {
        *guard = Some(child);
    }
    Ok("сейчас откроется браузер — войди в свой аккаунт ChatGPT".into())
}

#[tauri::command]
fn relay_status() -> String {
    let auth = exe_dir()
        .join("data")
        .join("relay")
        .join("local_auth")
        .join("auth.json");
    if auth.exists() {
        return "authorized".into();
    }
    if let Ok(mut guard) = LOGIN.lock() {
        if let Some(child) = guard.as_mut() {
            if child.try_wait().ok().flatten().is_none() {
                return "pending".into();
            }
        }
    }
    "no-auth".into()
}

#[tauri::command]
fn install_service() -> Result<String, String> {
    // Опциональная служба: один UAC. Запускаем скрипт установки с подъёмом
    // прав; само окно прав не требует и не получает.
    let script = exe_dir().join("install-service.ps1");
    if !script.exists() {
        return Err("в этой поставке нет install-service.ps1".into());
    }
    Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File \"{}\"'",
                script.display()
            ),
        ])
        .spawn()
        .map_err(|e| format!("установка не запустилась: {e}"))?;
    Ok("запрошен запуск установки (появится окно UAC)".into())
}

/// Уведомление Windows из веб-части (заголовок, текст).
#[tauri::command]
fn notify(title: String, body: String) {
    toast(&title, &body);
}

/// Конфиг целиком для экрана настроек плюс где он лежит и где данные.
#[tauri::command]
fn config_load() -> Result<serde_json::Value, String> {
    let base = exe_dir();
    let path = base.join(CONFIG_NAME);
    let cfg: serde_json::Value = match std::fs::read_to_string(&path) {
        Ok(raw) => serde_json::from_str(&raw).map_err(|e| format!("{CONFIG_NAME} не разобрался: {e}"))?,
        Err(_) => serde_json::json!({}),
    };
    let tree = resolve(&base, cfg.get("tree").and_then(|v| v.as_str()).unwrap_or("data"));
    Ok(serde_json::json!({
        "config": cfg,
        "path": path.display().to_string(),
        "tree": tree.display().to_string(),
        "exe_dir": base.display().to_string(),
    }))
}

fn run_hidden(cmd: &mut Command) -> Result<std::process::Output, String> {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.output().map_err(|e| e.to_string())
}

/// Состояние службы по SCM: running / stopped / absent. Прав не требует.
#[tauri::command]
fn service_state() -> String {
    let mut cmd = Command::new("sc.exe");
    cmd.args(["query", PRODUCT]);
    match run_hidden(&mut cmd) {
        Ok(out) if out.status.success() => {
            let text = String::from_utf8_lossy(&out.stdout).to_uppercase();
            if text.contains("RUNNING") || text.contains("START_PENDING") {
                "running".into()
            } else {
                "stopped".into()
            }
        }
        _ => "absent".into(),
    }
}

#[tauri::command]
fn remove_service() -> Result<String, String> {
    let script = exe_dir().join("uninstall-service.ps1");
    if !script.exists() {
        return Err("в этой поставке нет uninstall-service.ps1".into());
    }
    Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File \"{}\"'",
                script.display()
            ),
        ])
        .spawn()
        .map_err(|e| format!("снятие не запустилось: {e}"))?;
    Ok("запрошено снятие службы (появится окно прав администратора)".into())
}

/// Открыть папку в Проводнике.
#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    Command::new("explorer.exe")
        .arg(&path)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Показать файл в Проводнике с выделением — для собранных логов.
#[tauri::command]
fn reveal_path(path: String) -> Result<(), String> {
    Command::new("explorer.exe")
        .arg(format!("/select,{path}"))
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

fn startup_lnk() -> Option<PathBuf> {
    std::env::var_os("APPDATA").map(|a| {
        PathBuf::from(a)
            .join("Microsoft\\Windows\\Start Menu\\Programs\\Startup")
            .join(format!("{PRODUCT}.lnk"))
    })
}

/// Автозапуск — ярлык в папке автозагрузки пользователя, без реестра и прав.
#[tauri::command]
fn autostart_get() -> bool {
    startup_lnk().map(|p| p.exists()).unwrap_or(false)
}

#[tauri::command]
fn autostart_set(on: bool) -> Result<(), String> {
    let lnk = startup_lnk().ok_or("нет APPDATA")?;
    if !on {
        let _ = std::fs::remove_file(&lnk);
        return Ok(());
    }
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let script = format!(
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{}'); $s.TargetPath='{}'; $s.WorkingDirectory='{}'; $s.Save()",
        lnk.display(),
        exe.display(),
        exe.parent().map(|p| p.display().to_string()).unwrap_or_default()
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-Command", &script]);
    let out = run_hidden(&mut cmd)?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

/// Адрес этой машины в локальной сети — для QR телефону. Сокет не отправляет
/// ничего: connect на внешний адрес лишь выбирает интерфейс.
#[tauri::command]
fn lan_ip() -> Option<String> {
    let sock = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    sock.local_addr().ok().map(|a| a.ip().to_string())
}

/// Адрес этой машины в сети Tailscale (100.64.0.0/10), если он установлен и
/// включён: телефон с Tailscale в том же аккаунте достучится из любой сети,
/// не только из этой Wi-Fi. Спрашиваем у их же CLI, ничего не угадываем.
#[tauri::command]
fn tailscale_ip() -> Option<String> {
    let mut candidates = vec![PathBuf::from("tailscale.exe")];
    if let Ok(pf) = std::env::var("ProgramFiles") {
        candidates.insert(0, PathBuf::from(pf).join("Tailscale").join("tailscale.exe"));
    }
    for exe in candidates {
        let mut cmd = Command::new(&exe);
        cmd.args(["ip", "-4"]);
        let Ok(out) = run_hidden(&mut cmd) else { continue };
        if !out.status.success() {
            continue;
        }
        let text = String::from_utf8_lossy(&out.stdout);
        if let Some(ip) = text.lines().map(str::trim).find(|l| l.starts_with("100.")) {
            return Some(ip.to_string());
        }
    }
    None
}

/// Разрешить входящие к трубе в брандмауэре Windows. Нужны права
/// администратора; без них — честная ошибка, а не тишина.
#[tauri::command]
fn firewall_allow(port: u16) -> Result<String, String> {
    let base = exe_dir();
    let python = base.join("runtime").join("python.exe");
    let program = if python.exists() { python.display().to_string() } else { "python.exe".to_string() };
    let mut cmd = Command::new("netsh");
    cmd.args([
        "advfirewall", "firewall", "add", "rule",
        &format!("name={PRODUCT} ({port})"), "dir=in", "action=allow", "protocol=TCP",
        &format!("localport={port}"), &format!("program={program}"),
    ]);
    let out = run_hidden(&mut cmd)?;
    if out.status.success() {
        Ok(format!("правило брандмауэра для порта {port} добавлено"))
    } else {
        Err(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }
}

/// Идентификаторы моделей из ответа /models: по ним человек выбирает модель
/// одним нажатием, а не переписывает имя вслепую.
fn model_ids(body: &str) -> Vec<String> {
    let mut ids: Vec<String> = serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("data").and_then(|d| d.as_array()).cloned())
        .unwrap_or_default()
        .iter()
        .filter_map(|m| m.get("id").and_then(|i| i.as_str()).map(|s| s.to_string()))
        .collect();
    ids.sort();
    ids.dedup();
    ids.truncate(80);
    ids
}

/// Живая проверка адреса и ключа: GET {base}/models с Bearer. Фраза и список.
#[tauri::command]
async fn probe_model(base_url: String, key: String) -> serde_json::Value {
    let url = format!("{}/models", base_url.trim().trim_end_matches('/'));
    let result = tauri::async_runtime::spawn_blocking(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout(std::time::Duration::from_secs(12))
            .build();
        let mut req = agent.get(&url);
        if !key.trim().is_empty() {
            req = req.set("Authorization", &format!("Bearer {}", key.trim()));
        }
        match req.call() {
            Ok(resp) => {
                let body = resp.into_string().unwrap_or_default();
                let models = model_ids(&body);
                if models.is_empty() {
                    (true, "Отвечает, список моделей не в привычном виде".to_string(), models)
                } else {
                    (true, format!("Отвечает: моделей доступно {}", models.len()), models)
                }
            }
            Err(ureq::Error::Status(401, _)) | Err(ureq::Error::Status(403, _)) => {
                (false, "Ключ не подошёл".to_string(), Vec::new())
            }
            Err(ureq::Error::Status(404, _)) => (false, "По этому адресу нет /models".to_string(), Vec::new()),
            Err(ureq::Error::Status(code, _)) => (false, format!("Ответ {code}"), Vec::new()),
            Err(err) => (false, format!("Нет связи: {}", err.to_string().chars().take(120).collect::<String>()), Vec::new()),
        }
    })
    .await
    .unwrap_or((false, "проверка не выполнилась".to_string(), Vec::new()));
    serde_json::json!({ "ok": result.0, "note": result.1, "models": result.2 })
}

/// Windows показывает уведомления только от известного ей приложения (AUMID).
/// Установленному через ярлык Start-меню это даёт установщик; переносной сборке
/// достаточно записи в HKCU — она и делается здесь, без прав администратора.
#[cfg(windows)]
fn register_toast_identity(identifier: &str, name: &str, icon: Option<&Path>) {
    use winreg::enums::HKEY_CURRENT_USER;
    use winreg::RegKey;
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let path = format!("Software\\Classes\\AppUserModelId\\{identifier}");
    if let Ok((key, _)) = hkcu.create_subkey(path) {
        let _ = key.set_value("DisplayName", &name);
        let icon = icon.map(Path::to_path_buf).unwrap_or_else(|| exe_dir().join("vera.ico"));
        if icon.exists() {
            let _ = key.set_value("IconUri", &icon.to_string_lossy().into_owned());
        }
    }
}

#[cfg(not(windows))]
fn register_toast_identity(_identifier: &str, _name: &str, _icon: Option<&Path>) {}

/// Ярлык в меню «Пуск» с AppUserModel.ID — то, без чего Windows молча
/// выбрасывает уведомления непакованного exe (проверено 02.09: запись в HKCU
/// одна не помогает, ярлык — помогает). Установщик создаёт ярлык сам; здесь
/// он чинится, если его нет: скрипт вшит в exe и выполняется без окна.
#[cfg(windows)]
fn ensure_start_menu_shortcut(identifier: &str, name: &str, icon: Option<&Path>) {
    let Some(appdata) = std::env::var_os("APPDATA") else { return };
    let programs = PathBuf::from(appdata)
        .join("Microsoft")
        .join("Windows")
        .join("Start Menu")
        .join("Programs");
    if programs.join(format!("{name}.lnk")).exists() || programs.join(format!("{PRODUCT}.lnk")).exists() {
        return;
    }
    let Ok(exe) = std::env::current_exe() else { return };
    let script = std::env::temp_dir().join("vera-start-menu-shortcut.ps1");
    if std::fs::write(&script, include_str!("../resources/start-menu-shortcut.ps1")).is_err() {
        log_line("ярлык меню «Пуск»: не записался скрипт");
        return;
    }
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(&script)
        .arg("-Exe")
        .arg(&exe)
        .arg("-Aumid")
        .arg(identifier)
        .arg("-Name")
        .arg(name);
    if let Some(icon) = icon {
        cmd.arg("-Icon").arg(icon);
    }
    cmd.creation_flags(CREATE_NO_WINDOW);
    match cmd.output() {
        Ok(out) if out.status.success() => log_line("ярлык меню «Пуск» создан (уведомления)"),
        Ok(out) => log_line(&format!(
            "ярлык меню «Пуск» не создался: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )),
        Err(err) => log_line(&format!("ярлык меню «Пуск»: powershell не запустился: {err}")),
    }
}

#[cfg(not(windows))]
fn ensure_start_menu_shortcut(_identifier: &str, _name: &str, _icon: Option<&Path>) {}

/// Слово агента, когда окно не перед глазами, — уведомлением.
///
/// Читаем хвост архива комнаты окна (его пишет транспорт руннера) и показываем
/// только исходящие строки, только пока окно скрыто или не в фокусе. Старт — с
/// текущего конца файла: прошлые реплики уведомлениями не становятся.
fn watch_outbound(app: tauri::AppHandle, tree: PathBuf, agent: String) {
    std::thread::spawn(move || {
        let archive = tree
            .join("memory")
            .join("groups")
            .join(format!("{WINDOW_ROOM}.jsonl"));
        let mut offset: u64 = std::fs::metadata(&archive).map(|m| m.len()).unwrap_or(0);
        let mut last_body = String::new();
        let mut last_toast = std::time::Instant::now() - Duration::from_secs(60);
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let len = match std::fs::metadata(&archive) {
                Ok(m) => m.len(),
                Err(_) => continue,
            };
            if len < offset {
                offset = 0; // файл пересоздали
            }
            if len == offset {
                continue;
            }
            let mut file = match std::fs::File::open(&archive) {
                Ok(f) => f,
                Err(_) => continue,
            };
            if file.seek(SeekFrom::Start(offset)).is_err() {
                continue;
            }
            let mut buf = String::new();
            if file.read_to_string(&mut buf).is_err() {
                continue; // строка ещё дописывается — придём через два секунды
            }
            let complete = match buf.rfind('\n') {
                Some(i) => &buf[..=i],
                None => continue,
            };
            offset += complete.len() as u64;
            let in_view = app
                .get_webview_window("main")
                .map(|w| w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false))
                .unwrap_or(false);
            if in_view {
                continue;
            }
            // Уведомление — только слово агента, и не чаще одного за пятнадцать
            // секунд: служебные пометки («ход не состоялся», ⚠) и очереди из
            // нескольких строк подряд превращали трей в дребезг (слово владельца).
            let mut fresh: Option<String> = None;
            for line in complete.lines() {
                let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else {
                    continue;
                };
                if !row.get("outgoing").and_then(|v| v.as_bool()).unwrap_or(false) {
                    continue;
                }
                let text = row.get("text").and_then(|v| v.as_str()).unwrap_or("").trim();
                if text.is_empty() || text.starts_with('⚠') || text.contains("ход не состоялся") {
                    continue;
                }
                fresh = Some(text.chars().take(240).collect());
            }
            let Some(body) = fresh else { continue };
            if body == last_body || last_toast.elapsed() < Duration::from_secs(15) {
                continue;
            }
            last_body = body.clone();
            last_toast = std::time::Instant::now();
            toast(&agent, &body);
        }
    });
}

fn main() {
    let base = exe_dir();
    log_line(&format!("старт {PRODUCT} {}", env!("CARGO_PKG_VERSION")));
    let cfg: Option<serde_json::Value> = std::fs::read_to_string(base.join(CONFIG_NAME))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok());
    let agent = agent_name(cfg.as_ref());
    if unconfigured(&cfg) {
        // Ненастроенный продукт встречает установщик, если он лежит рядом.
        // Без него окно открывается как есть: состояние в шапке скажет
        // «Модель не настроена» и куда идти.
        let setup = base.join("vera-setup.exe");
        if setup.exists() {
            if Command::new(&setup).current_dir(&base).spawn().is_ok() {
                return;
            }
            log_line("vera-setup.exe рядом есть, но не запустился — открываю окно");
        }
    }
    let configured = !unconfigured(&cfg);

    let mut children: Vec<Managed> = Vec::new();
    let mut init_script = String::new();
    let mut tree: Option<PathBuf> = None;
    // Без настройки харнесс не поднимаем: без ключа модели дети бесполезны.
    if let Some(cfg) = cfg.as_ref().filter(|_| configured) {
        match cfg.get("mode").and_then(|v| v.as_str()).unwrap_or("") {
            "local" => {
                let (spawned, port, data) = spawn_local(&base, cfg);
                // Пустой список детей — не провал: харнесс уже живёт (служба или
                // другое окно), и это окно — просто ещё один клиент того же порта.
                children = spawned;
                tree = Some(data);
                init_script = format!(
                    "window.PULT_CONFIG_OVERRIDE = {{base: \"http://127.0.0.1:{port}\", key: \"\", agent: {}}};",
                    serde_json::Value::String(agent.clone())
                );
            }
            "remote" => {
                let base_url = cfg.get("base").and_then(|v| v.as_str()).unwrap_or("");
                let key = cfg.get("key").and_then(|v| v.as_str()).unwrap_or("");
                if !base_url.is_empty() {
                    init_script = format!(
                        "window.PULT_CONFIG_OVERRIDE = {};",
                        serde_json::json!({"base": base_url, "key": key, "agent": agent})
                    );
                }
            }
            _ => {}
        }
    }

    tauri::Builder::default()
        // Один экземпляр — ПЕРВЫМ плагином: повторный запуск не доходит до
        // окна, а поднимает и фокусирует уже живое.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app);
        }))
        // Память окна: размер, положение и «развёрнуто/нет» переживают перезапуск.
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .manage(LocalHarness {
            children: Mutex::new(children),
            stopping: std::sync::atomic::AtomicBool::new(false),
        })
        .manage(Identity {
            agent: agent.clone(),
            tree: tree.clone(),
        })
        .invoke_handler(tauri::generate_handler![
            config_save,
            restart_self,
            install_service,
            remove_service,
            service_state,
            config_load,
            open_path,
            autostart_get,
            autostart_set,
            probe_model,
            lan_ip,
            tailscale_ip,
            firewall_allow,
            relay_login,
            relay_status,
            notify,
            app_info,
            update_check,
            logs_bundle,
            reveal_path,
            telegram_account
        ])
        .setup(move |app| {
            // Иконка агента (инициал на бумаге) — её рисует установщик в data/.
            let icon_png = tree.as_ref().map(|t| t.join("icon.png")).filter(|p| p.exists());
            let icon_ico = tree.as_ref().map(|t| t.join("icon.ico")).filter(|p| p.exists());
            let icon_32 = tree.as_ref().map(|t| t.join("icon-32.png")).filter(|p| p.exists());
            register_toast_identity(TOAST_ID, &agent, icon_png.as_deref());
            ensure_start_menu_shortcut(TOAST_ID, &agent, icon_ico.as_deref());
            let window_icon = icon_png
                .as_deref()
                .and_then(|p| tauri::image::Image::from_path(p).ok())
                .unwrap_or(tauri::image::Image::from_bytes(include_bytes!("../icons/icon.png"))?);
            let tray_icon = icon_32
                .as_deref()
                .and_then(|p| tauri::image::Image::from_path(p).ok())
                .unwrap_or(tauri::image::Image::from_bytes(include_bytes!("../icons/32x32.png"))?);
            debug_assert_eq!(app.config().identifier, TOAST_ID);
            let mut builder =
                tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                    .title(&agent)
                    .icon(window_icon)?
                    .inner_size(1360.0, 860.0)
                    .min_inner_size(900.0, 600.0)
                    .center()
                    .maximized(true)
                    .decorations(false)
                    .shadow(true);
            if !init_script.is_empty() {
                builder = builder.initialization_script(&init_script);
            }
            builder.build()?;
            // Передний план: Windows отдаёт его неохотно, когда запустивший нас
            // процесс (установщик) уже вышел, — окно появлялось позади других,
            // и казалось, что не открылось. Короткий «поверх всех» лечит.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(500));
                show_main(&handle);
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || watch_children(handle));

            // Трей: закрытие окна прячет его, харнесс-дети живут дальше;
            // настоящий выход — только из меню трея.
            use tauri::menu::{Menu, MenuItem};
            use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
            let open = MenuItem::with_id(app, "open", "Открыть Vera", true, None::<&str>)?;
            let quit = MenuItem::with_id(
                app,
                "quit",
                "Выход (остановить агента)",
                true,
                None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            TrayIconBuilder::with_id("frame")
                .icon(tray_icon)
                .tooltip(agent.clone())
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "quit" => {
                        let state = app.state::<LocalHarness>();
                        kill_children(&state);
                        app.exit(0);
                    }
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

            // Слово агента, когда окно не перед глазами, — уведомлением.
            if let Some(data) = tree.clone() {
                watch_outbound(app.handle().clone(), data, agent.clone());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                // Закрыть окно ≠ убить организм: окно в трей, дети живут.
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                    close_hint(window.app_handle());
                }
                // Настоящая смерть окна (выход) — дети не остаются сиротами.
                tauri::WindowEvent::Destroyed => {
                    let state = window.app_handle().state::<LocalHarness>();
                    kill_children(&state);
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("окно Vera не поднялось");
}

/// Первое закрытие окна: сказать, что агент жив и где его найти. Один раз —
/// отметка рядом с exe, чтобы не повторять очевидное.
fn close_hint(app: &tauri::AppHandle) {
    let flag = exe_dir().join(".close-hint-shown");
    if flag.exists() {
        return;
    }
    let _ = std::fs::write(&flag, "1");
    let identity = app.state::<Identity>();
    let body = format!(
        "Окно закрыто, {} продолжает работать. Открыть снова — значок у часов.",
        identity.agent
    );
    toast(PRODUCT, &body);
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        let _ = window.set_always_on_top(true);
        let _ = window.set_always_on_top(false);
    }
}

fn kill_children(state: &tauri::State<LocalHarness>) {
    state.stopping.store(true, std::sync::atomic::Ordering::Relaxed);
    if let Ok(mut guard) = state.children.lock() {
        for m in guard.iter_mut() {
            let _ = m.child.kill();
            let _ = m.child.wait();
        }
        guard.clear();
    };
}

/// Надзор: упавший ребёнок поднимается снова с растущей паузой; шестое падение
/// за десять минут — пауза на десять минут и честное уведомление, чтобы не
/// жечь машину петлёй. Под замком не спим: выход из трея не должен ждать.
fn watch_children(app: tauri::AppHandle) {
    use std::sync::atomic::Ordering;
    use std::time::Instant;
    loop {
        std::thread::sleep(Duration::from_secs(5));
        let state = app.state::<LocalHarness>();
        if state.stopping.load(Ordering::Relaxed) {
            return;
        }
        let Ok(mut guard) = state.children.lock() else { continue };
        let now = Instant::now();
        for m in guard.iter_mut() {
            let label = m.spec.label();
            if let Some(at) = m.retry_at {
                if now < at {
                    continue;
                }
                m.retry_at = None;
                match m.spec.spawn() {
                    Some(child) => {
                        m.child = child;
                        log_line(&format!("{label} поднят снова"));
                    }
                    None => {
                        log_line(&format!("{label} не поднялся, ещё попытка через 30 с"));
                        m.retry_at = Some(now + Duration::from_secs(30));
                    }
                }
                continue;
            }
            let Ok(Some(status)) = m.child.try_wait() else { continue };
            m.falls.retain(|t| now.duration_since(*t) < Duration::from_secs(600));
            m.falls.push(now);
            if m.falls.len() > 5 {
                log_line(&format!("{label} завершился ({status}) шестой раз за десять минут — пауза десять минут"));
                toast(PRODUCT, &format!("{label} падает раз за разом. Подробности в vera.log и в логах рядом с данными."));
                m.falls.clear();
                m.retry_at = Some(now + Duration::from_secs(600));
                continue;
            }
            let pause = Duration::from_secs(1u64 << (m.falls.len() - 1).min(5));
            log_line(&format!("{label} завершился ({status}), поднимаю снова через {} с", pause.as_secs()));
            m.retry_at = Some(now + pause);
        }
    }
}

/// Версия, папка, журнал — для экрана «О программе».
#[tauri::command]
fn app_info() -> serde_json::Value {
    let base = exe_dir();
    serde_json::json!({
        "version": env!("CARGO_PKG_VERSION"),
        "exe_dir": base.display().to_string(),
        "log": base.join("vera.log").display().to_string(),
    })
}

/// Новее ли версия-кандидат: сравнение по числам через точку.
fn version_newer(candidate: &str, current: &str) -> bool {
    let parse = |s: &str| -> Vec<u64> {
        s.trim()
            .trim_start_matches('v')
            .split('.')
            .map(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().unwrap_or(0))
            .collect()
    };
    let a = parse(candidate);
    !a.is_empty() && a > parse(current)
}

/// Проверка обновлений: JSON по адресу владельца — {"version","url","notes"}.
/// Ничего не скачивает и не подменяет, только говорит, есть ли новее, и даёт
/// ссылку. Замена файлов — решение человека, по его же слову.
#[tauri::command]
async fn update_check(url: String) -> Result<serde_json::Value, String> {
    let url = url.trim().to_string();
    if url.is_empty() {
        return Err("адрес обновлений не задан".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(12)).build();
        let body = agent
            .get(&url)
            .call()
            .map_err(|e| format!("не ответил: {e}"))?
            .into_string()
            .map_err(|e| e.to_string())?;
        let v: serde_json::Value = serde_json::from_str(&body).map_err(|e| format!("это не JSON: {e}"))?;
        // Два формата: свой {"version","url","notes"} и GitHub Releases
        // (.../releases/latest → tag_name, html_url, body, assets[]).
        let text = |key: &str| v.get(key).and_then(|x| x.as_str()).unwrap_or("").to_string();
        let latest = if !text("version").is_empty() { text("version") } else { text("tag_name") };
        let asset_zip = v
            .get("assets")
            .and_then(|a| a.as_array())
            .and_then(|a| {
                a.iter()
                    .filter_map(|x| x.get("browser_download_url").and_then(|u| u.as_str()))
                    .find(|u| u.to_lowercase().ends_with(".zip"))
                    .map(str::to_string)
            });
        let url = if !text("url").is_empty() { text("url") } else { asset_zip.unwrap_or_else(|| text("html_url")) };
        let notes = if !text("notes").is_empty() { text("notes") } else { text("body") };
        let notes: String = notes.lines().find(|l| !l.trim().is_empty()).unwrap_or("").chars().take(200).collect();
        let current = env!("CARGO_PKG_VERSION");
        Ok(serde_json::json!({
            "current": current,
            "latest": latest,
            "newer": version_newer(&latest, current),
            "url": url,
            "notes": notes,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Собрать логи для поддержки в один zip во временной папке: vera.log,
/// вывод харнесса, службы и реле. Файлы сперва копируются: дети держат свои
/// логи открытыми, и архиватор напрямую их не читает.
/// Вход агента в Telegram своим аккаунтом: шаги status / send / code / logout
/// выполняет помощник на встроенном Python; ответ — его JSON как есть.
#[tauri::command]
async fn telegram_account(
    step: String,
    api_id: String,
    api_hash: String,
    phone: String,
    code: String,
    password: String,
) -> Result<serde_json::Value, String> {
    let base = exe_dir();
    let info = config_load()?;
    let tree = PathBuf::from(info.get("tree").and_then(|v| v.as_str()).unwrap_or("data"));
    tauri::async_runtime::spawn_blocking(move || {
        let python = base.join("runtime").join("python.exe");
        let script = base.join("app").join("localharness").join("mtproto_login.py");
        if !python.exists() || !script.exists() {
            return Err("в этой поставке нет помощника входа".to_string());
        }
        let session = tree.join("telegram").join("account");
        let mut cmd = Command::new(python);
        cmd.arg(script)
            .arg("--session").arg(&session)
            .arg("--api-id").arg(api_id.trim())
            .arg("--api-hash").arg(api_hash.trim())
            .arg("--phone").arg(phone.trim())
            .arg("--code").arg(code.trim())
            .arg("--password").arg(password)
            .arg(step.trim())
            .env("PYTHONUTF8", "1");
        let out = run_hidden(&mut cmd)?;
        let text = String::from_utf8_lossy(&out.stdout);
        let line = text.lines().rev().find(|l| l.trim_start().starts_with('{')).unwrap_or("");
        serde_json::from_str::<serde_json::Value>(line)
            .map_err(|_| format!("помощник входа ответил не JSON: {}", text.trim().chars().take(200).collect::<String>()))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn logs_bundle() -> Result<String, String> {
    let base = exe_dir();
    let info = config_load()?;
    let tree = PathBuf::from(info.get("tree").and_then(|v| v.as_str()).unwrap_or("data"));
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let stage = std::env::temp_dir().join(format!("vera-logs-{stamp}"));
    std::fs::create_dir_all(&stage).map_err(|e| e.to_string())?;
    let mut copied = 0usize;
    let mut sources = vec![base.join("vera.log")];
    for name in ["deskapp.log", "runner.log", "service.log", "desk.log"] {
        sources.push(tree.join(name));
    }
    if let Ok(rd) = std::fs::read_dir(tree.join("relay").join("logs")) {
        sources.extend(rd.flatten().map(|e| e.path()).filter(|p| p.is_file()));
    }
    for src in sources {
        if !src.is_file() {
            continue;
        }
        let Some(name) = src.file_name() else { continue };
        if std::fs::copy(&src, stage.join(name)).is_ok() {
            copied += 1;
        }
    }
    if copied == 0 {
        let _ = std::fs::remove_dir_all(&stage);
        return Err("логов пока нет".into());
    }
    let out = std::env::temp_dir().join(format!("vera-logs-{stamp}.zip"));
    let script = format!(
        "Compress-Archive -Force -Path '{}\\*' -DestinationPath '{}'",
        stage.display(),
        out.display()
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-Command", &script]);
    let o = run_hidden(&mut cmd)?;
    let _ = std::fs::remove_dir_all(&stage);
    if !o.status.success() || !out.is_file() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { "архив не собрался".into() } else { err });
    }
    Ok(out.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::{agent_name, unconfigured};
    use serde_json::json;

    #[test]
    fn first_run_is_unconfigured() {
        assert!(unconfigured(&None));
        assert!(unconfigured(&Some(json!({
            "model": {"base_url": "https://example.invalid/v1", "model": "demo", "key": ""}
        }))));
    }

    #[test]
    fn explicit_setup_receipt_allows_keyless_local_model() {
        assert!(!unconfigured(&Some(json!({
            "setup_complete": true,
            "model": {"base_url": "http://127.0.0.1:11434/v1", "model": "local", "key": ""}
        }))));
    }

    #[test]
    fn legacy_config_with_key_stays_configured() {
        assert!(!unconfigured(&Some(json!({
            "model": {"key": "legacy-key"}
        }))));
    }

    #[test]
    fn agent_name_comes_from_owner_not_from_product() {
        assert_eq!(agent_name(None), "Агент");
        assert_eq!(agent_name(Some(&json!({"agent": {"name": "  Вера "}}))), "Вера");
        assert_eq!(
            agent_name(Some(&json!({"telegram": {"agent_name": "Старое"}}))),
            "Старое"
        );
        assert_eq!(
            agent_name(Some(&json!({"agent": {"name": ""}, "telegram": {"agent_name": ""}}))),
            "Агент"
        );
    }
}

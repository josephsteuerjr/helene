//! Установка Vera: поставка → папка пользователя, конфиг, конституция,
//! ярлыки, запись об удалении, служба по желанию. Каждый шаг — расписка.
//!
//! Раскладка: установщик лежит ВНУТРИ поставки (папка из build_dist.py:
//! vera.exe, app/, runtime/, tree/, vera-svc.exe, vera-relay.exe…).
//! Установка = копия этой папки в %LocalAppData%\Programs\Vera плюс то,
//! чего в поставке нет по построению: vera.json и data/soul/SOUL.md.
//! Машинная часть (служба в Program Files) — отдельный поднятый вызов; сама
//! установка прав не требует и не получает.
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub const PRODUCT: &str = "Vera";
pub const AUMID: &str = "app.vera.desk";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Файлы поставки, по которым мы узнаём её папку.
const PAYLOAD_MARKERS: [&str; 3] = ["vera.exe", "app", "runtime"];

#[derive(Deserialize, Clone)]
pub struct Setup {
    pub agent: String,
    pub owner: String,
    pub constitution: String,
    pub accepted: bool,
    pub provider: String,
    #[serde(default = "default_chatgpt_model")]
    pub chatgpt_model: String,
    pub api: Endpoint,
    pub local: LocalEndpoint,
    pub telegram: Telegram,
    pub service: bool,
    pub dir: String,
}

fn default_chatgpt_model() -> String {
    "gpt-5.2".into()
}

#[derive(Deserialize, Clone)]
pub struct Endpoint {
    pub base_url: String,
    pub model: String,
    pub key: String,
}

#[derive(Deserialize, Clone)]
pub struct LocalEndpoint {
    pub base_url: String,
    pub model: String,
}

#[derive(Deserialize, Clone)]
pub struct Telegram {
    pub bot_token: String,
    pub owner_id: String,
}

#[derive(Serialize, Clone)]
pub struct Step {
    pub label: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Serialize, Clone)]
pub struct Receipt {
    pub dir: String,
    pub exe: String,
    pub service: String,
    pub steps: Vec<Step>,
}

#[derive(Serialize, Clone)]
pub struct Progress {
    pub step: usize,
    pub total: usize,
    pub label: String,
}

#[derive(Serialize)]
pub struct Defaults {
    pub dir: String,
    pub payload: Option<String>,
    pub version: String,
}

pub fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn default_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("Programs").join(PRODUCT)
}

/// Папка поставки: рядом с установщиком лежат vera.exe, app/ и runtime/.
pub fn payload_dir() -> Option<PathBuf> {
    let here = exe_dir();
    if PAYLOAD_MARKERS.iter().all(|m| here.join(m).exists()) {
        Some(here)
    } else {
        None
    }
}

pub fn defaults() -> Defaults {
    Defaults {
        dir: default_dir().to_string_lossy().into_owned(),
        payload: payload_dir().map(|p| p.to_string_lossy().into_owned()),
        version: VERSION.to_string(),
    }
}

fn copy_dir(src: &Path, dst: &Path, skip_root: &[&str]) -> Result<usize, String> {
    std::fs::create_dir_all(dst).map_err(|e| format!("{}: {e}", dst.display()))?;
    let mut count = 0;
    for entry in std::fs::read_dir(src).map_err(|e| format!("{}: {e}", src.display()))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name();
        if skip_root.iter().any(|s| name == *s) {
            continue;
        }
        let from = entry.path();
        let to = dst.join(&name);
        if from.is_dir() {
            count += copy_dir(&from, &to, &[])?;
        } else {
            std::fs::copy(&from, &to).map_err(|e| format!("{}: {e}", from.display()))?;
            count += 1;
        }
    }
    Ok(count)
}

/// vera.json из решений установки. Относительные пути: папка переносима целиком.
fn config_json(s: &Setup) -> serde_json::Value {
    let mut model = serde_json::json!({ "framework": "openai", "max_tokens": 8192 });
    let mut relay = serde_json::Value::Null;
    match s.provider.as_str() {
        "chatgpt" => {
            // Ключ реле = ключ мозга: случайный, обязателен Bearer-ом на локальном порту.
            let key = format!("sk-frame-{}", random_hex(24));
            model["base_url"] = "http://127.0.0.1:5011/v1".into();
            let chosen = s.chatgpt_model.trim();
            model["model"] = (if chosen.is_empty() { "gpt-5.2" } else { chosen }).into();
            model["key"] = key.into();
            relay = serde_json::json!({ "enabled": true, "port": 5011 });
        }
        "local" => {
            model["base_url"] = s.local.base_url.trim().into();
            model["model"] = s.local.model.trim().into();
            model["key"] = "".into();
        }
        _ => {
            model["base_url"] = s.api.base_url.trim().into();
            model["model"] = s.api.model.trim().into();
            model["key"] = s.api.key.trim().into();
        }
    }
    let mut cfg = serde_json::json!({
        "mode": "local",
        "python": "runtime/python.exe",
        "app": "app/deskapp.py",
        "runner": "app/localharness/runner.py",
        "tree": "data",
        "code": "tree",
        "port": 8094,
        "agent": { "name": s.agent.trim() },
        "owner": { "name": s.owner.trim(), "room": PRODUCT },
        "model": model,
        "telegram": {
            "bot_token": s.telegram.bot_token.trim(),
            "owner_id": s.telegram.owner_id.trim().parse::<i64>().unwrap_or(0),
        },
        "read_dotenv": false,
        "setup_complete": true,
        "installed": { "version": VERSION, "service": s.service },
    });
    if !relay.is_null() {
        cfg["relay"] = relay;
    }
    cfg
}

fn random_hex(bytes: usize) -> String {
    // Без внешних крейтов: время + адреса стека через хэш — достаточно для
    // локального ключа, который ходит только по 127.0.0.1.
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut out = String::new();
    while out.len() < bytes * 2 {
        let mut h = RandomState::new().build_hasher();
        h.write_u128(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        );
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out.truncate(bytes * 2);
    out
}

fn write_atomic(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(())
}

fn run_hidden(cmd: &mut Command) -> Result<std::process::Output, String> {
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.output().map_err(|e| e.to_string())
}

/// Ярлыки: меню «Пуск» (с AUMID — без него нет уведомлений) и рабочий стол.
/// Остановить всё, что запущено из папки установки: окно, детей харнесса,
/// реле. По пути exe, а не по имени, чтобы не задеть чужой python.
pub fn stop_running(dir: &Path) {
    let script = format!(
        "Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{}\\*' -and $_.ProcessId -ne {} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}",
        dir.display(),
        std::process::id()
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-Command", &script]);
    let _ = run_hidden(&mut cmd);
    std::thread::sleep(std::time::Duration::from_millis(600));
}

/// Имя агента из установленного vera.json — для снятия ярлыков с его именем.
fn installed_agent_name(dir: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(dir.join("vera.json")).ok()?;
    let cfg: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let name = cfg.get("agent")?.get("name")?.as_str()?.trim().to_string();
    if name.is_empty() { None } else { Some(name) }
}

/// Иконка агента: бежевый квадрат с его инициалом, рисует встроенный Python
/// (app/resources/make_icon.py). Ложится в data/icon.{ico,png}; без неё
/// ярлыки берут значок exe.
fn agent_icon(dir: &Path, name: &str) -> Option<PathBuf> {
    let python = dir.join("runtime").join("python.exe");
    let script = dir.join("app").join("resources").join("make_icon.py");
    if !python.exists() || !script.exists() {
        return None;
    }
    let out = dir.join("data").join("icon");
    let mut cmd = Command::new(python);
    cmd.arg(script).arg("--out").arg(&out).arg("--letter").arg(name).env("PYTHONUTF8", "1");
    let _ = run_hidden(&mut cmd);
    let ico = dir.join("data").join("icon.ico");
    if ico.exists() { Some(ico) } else { None }
}

fn shortcuts(exe: &Path, name: &str, icon: Option<&Path>) -> Result<String, String> {
    let script = std::env::temp_dir().join("vera-start-menu-shortcut.ps1");
    std::fs::write(&script, include_str!("../../shell/resources/start-menu-shortcut.ps1"))
        .map_err(|e| e.to_string())?;
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(&script)
        .arg("-Exe")
        .arg(exe)
        .arg("-Aumid")
        .arg(AUMID)
        .arg("-Name")
        .arg(name);
    if let Some(icon) = icon {
        cmd.arg("-Icon").arg(icon);
    }
    let out = run_hidden(&mut cmd)?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
    }
    // Рабочий стол — обычный ярлык, без свойств.
    let desktop = Command::new("powershell.exe");
    let mut desktop = desktop;
    desktop.args([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        &format!(
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\\{}.lnk'); $s.TargetPath='{}'; $s.WorkingDirectory='{}'; {} $s.Save()",
            name,
            exe.display(),
            exe.parent().map(|p| p.display().to_string()).unwrap_or_default(),
            icon.map(|i| format!("$s.IconLocation='{},0';", i.display())).unwrap_or_default()
        ),
    ]);
    let _ = run_hidden(&mut desktop);
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Запись в «Приложениях» Windows: удаление через тот же установщик.
#[cfg(windows)]
fn register_uninstall(dir: &Path, name: &str, icon: Option<&Path>) -> Result<(), String> {
    use winreg::enums::HKEY_CURRENT_USER;
    use winreg::RegKey;
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let (key, _) = hkcu
        .create_subkey(format!(
            "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{PRODUCT}"
        ))
        .map_err(|e| e.to_string())?;
    let setup = dir.join("vera-setup.exe");
    let exe = dir.join("vera.exe");
    let set = |name: &str, value: String| key.set_value(name, &value).map_err(|e| e.to_string());
    set("DisplayName", PRODUCT.to_string())?;
    set("DisplayVersion", VERSION.to_string())?;
    set("Publisher", PRODUCT.to_string())?;
    set("InstallLocation", dir.display().to_string())?;
    set("DisplayIcon", icon.map(|i| i.display().to_string()).unwrap_or_else(|| exe.display().to_string()))?;
    set("UninstallString", format!("\"{}\" --uninstall", setup.display()))?;
    key.set_value("NoModify", &1u32).map_err(|e| e.to_string())?;
    key.set_value("NoRepair", &1u32).map_err(|e| e.to_string())?;
    // Та же запись, что делает оболочка: имя и значок для центра уведомлений.
    if let Ok((toast, _)) =
        hkcu.create_subkey(format!("Software\\Classes\\AppUserModelId\\{AUMID}"))
    {
        let _ = toast.set_value("DisplayName", &name);
        let png = dir.join("data").join("icon.png");
        let icon_uri = if png.exists() { png } else { dir.join("vera.ico") };
        let _ = toast.set_value("IconUri", &icon_uri.display().to_string());
    }
    Ok(())
}

#[cfg(not(windows))]
fn register_uninstall(_dir: &Path) -> Result<(), String> {
    Ok(())
}

/// Дом реле на время установки: вход в ChatGPT делается ДО копирования, а
/// учётные данные переезжают в data/relay установленной программы.
pub fn relay_home() -> PathBuf {
    std::env::temp_dir().join("vera-setup-relay")
}

pub fn relay_status() -> String {
    if relay_home().join("local_auth").join("auth.json").exists() {
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

/// Незавершённый вход в ChatGPT: один за раз. Повторное нажатие отменяет
/// прежнюю попытку — иначе помощник реле держит порт 1455, а новые попытки
/// падают и роняют браузерный обратный вызов с 400.
static LOGIN: Mutex<Option<Child>> = Mutex::new(None);

/// Отменить незавершённый вход: дерево процессов целиком (реле + помощник).
pub fn relay_abort() {
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

/// Логин реле в подписку ChatGPT: реле лежит в поставке, браузер откроется сам.
pub fn relay_login() -> Result<String, String> {
    relay_abort();
    let payload = payload_dir().ok_or("рядом с установщиком нет поставки")?;
    let exe = payload.join("vera-relay.exe");
    if !exe.exists() {
        return Err("в этой поставке нет vera-relay.exe".into());
    }
    let home = relay_home();
    std::fs::create_dir_all(&home).map_err(|e| e.to_string())?;
    let mut cmd = Command::new(&exe);
    cmd.arg("login")
        .current_dir(&home)
        .env("RELAY_LOCAL", "1")
        .env("RELAY_LOG_DIR", home.join("logs"));
    let python = payload.join("runtime").join("python.exe");
    if python.exists() {
        cmd.env("RELAY_PYTHON", &python);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let child = cmd.spawn().map_err(|e| format!("логин не запустился: {e}"))?;
    if let Ok(mut guard) = LOGIN.lock() {
        *guard = Some(child);
    }
    Ok("Сейчас откроется браузер: войди в свой аккаунт ChatGPT".into())
}

fn move_relay_auth(dir: &Path) -> Option<String> {
    let from = relay_home().join("local_auth");
    if !from.is_dir() {
        return None;
    }
    let to = dir.join("data").join("relay").join("local_auth");
    match copy_dir(&from, &to, &[]) {
        Ok(n) => Some(format!("вход в ChatGPT перенесён ({n} файлов)")),
        Err(e) => Some(format!("вход в ChatGPT не перенёсся: {e}")),
    }
}

/// Живая проверка адреса и ключа: GET {base}/models с Bearer.
/// Идентификаторы моделей из ответа /models: по ним человек выбирает модель
/// одним нажатием, а не переписывает имя вслепую.
pub fn model_ids(body: &str) -> Vec<String> {
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

pub fn probe_model(base_url: &str, key: &str) -> (bool, String, Vec<String>) {
    let url = format!("{}/models", base_url.trim().trim_end_matches('/'));
    let agent = ureq::AgentBuilder::new().timeout(std::time::Duration::from_secs(12)).build();
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
        Err(ureq::Error::Status(401, _)) | Err(ureq::Error::Status(403, _)) => (false, "Ключ не подошёл".to_string(), Vec::new()),
        Err(ureq::Error::Status(404, _)) => (false, "По этому адресу нет /models".to_string(), Vec::new()),
        Err(ureq::Error::Status(code, _)) => (false, format!("Ответ {code}"), Vec::new()),
        Err(err) => (false, format!("Нет связи: {}", err.to_string().chars().take(120).collect::<String>()), Vec::new()),
    }
}

/// Служба: один UAC на машинную часть. Ждём завершения скрипта, потом
/// спрашиваем SCM сами — квитанция о фактическом состоянии, не «запустил».
fn install_service(dir: &Path) -> String {
    let script = dir.join("install-service.ps1");
    if !script.exists() || !dir.join("vera-svc.exe").exists() {
        return "absent".into();
    }
    let mut cmd = Command::new("powershell.exe");
    cmd.args([
        "-NoProfile",
        "-Command",
        &format!(
            "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{}\"'",
            script.display()
        ),
    ]);
    let _ = run_hidden(&mut cmd);
    service_state()
}

pub fn service_state() -> String {
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

/// Сама установка. `progress` зовётся перед каждым шагом.
pub fn install(s: &Setup, mut progress: impl FnMut(Progress)) -> Result<Receipt, String> {
    if !s.accepted {
        return Err("конституция не принята".into());
    }
    let payload = payload_dir().ok_or_else(|| {
        "рядом с установщиком нет поставки (vera.exe, app/, runtime/) — запусти его из папки Vera".to_string()
    })?;
    let dir = if s.dir.trim().is_empty() {
        default_dir()
    } else {
        PathBuf::from(s.dir.trim())
    };
    // Повторная установка поверх живой: сначала гасим всё, что запущено из
    // этой папки (окно, дети харнесса, реле), иначе копирование споткнётся о
    // занятый файл рантайма.
    if dir.exists() {
        stop_running(&dir);
    }
    let total = if s.service { 6 } else { 5 };
    let mut steps = Vec::new();
    let mut n = 0;
    let mut tick = |label: &str, progress: &mut dyn FnMut(Progress)| {
        n += 1;
        progress(Progress { step: n, total, label: label.to_string() });
    };

    tick("Копирую файлы программы", &mut progress);
    // vera.json и data/ поставки не копируем: конфиг пишем свой, данные рождаются здесь.
    let copied = copy_dir(&payload, &dir, &["vera.json", "data", ".close-hint-shown", "vera.log"])?;
    steps.push(Step { label: "Файлы программы".into(), ok: true, note: Some(format!("{copied} файлов")) });

    tick("Записываю настройки и конституцию", &mut progress);
    let cfg = serde_json::to_string_pretty(&config_json(s)).map_err(|e| e.to_string())?;
    write_atomic(&dir.join("vera.json"), &(cfg + "\n"))?;
    let soul = s.constitution.replace("\r\n", "\n");
    write_atomic(&dir.join("data").join("soul").join("SOUL.md"), &soul)?;
    steps.push(Step { label: "Настройки и конституция".into(), ok: true, note: None });
    if s.provider == "chatgpt" {
        if let Some(note) = move_relay_auth(&dir) {
            steps.push(Step { label: "Подписка ChatGPT".into(), ok: !note.contains("не "), note: Some(note) });
        }
    }

    let exe = dir.join("vera.exe");
    tick("Создаю ярлыки", &mut progress);
    let name = if s.agent.trim().is_empty() { PRODUCT.to_string() } else { s.agent.trim().to_string() };
    let icon = agent_icon(&dir, &name);
    match shortcuts(&exe, &name, icon.as_deref()) {
        Ok(note) => steps.push(Step { label: "Ярлыки".into(), ok: true, note: Some(note) }),
        Err(err) => steps.push(Step { label: "Ярлыки".into(), ok: false, note: Some(err) }),
    }

    tick("Регистрирую удаление в «Приложениях»", &mut progress);
    match register_uninstall(&dir, &name, icon.as_deref()) {
        Ok(()) => steps.push(Step { label: "Запись об удалении".into(), ok: true, note: None }),
        Err(err) => steps.push(Step { label: "Запись об удалении".into(), ok: false, note: Some(err) }),
    }

    let service = if s.service {
        tick("Ставлю службу Windows (появится окно прав администратора)", &mut progress);
        let state = install_service(&dir);
        steps.push(Step { label: "Служба".into(), ok: state == "running", note: Some(state.clone()) });
        state
    } else {
        "skipped".to_string()
    };

    tick("Готово", &mut progress);
    Ok(Receipt {
        dir: dir.display().to_string(),
        exe: exe.display().to_string(),
        service,
        steps,
    })
}

/// Хвост снятия: сам установщик занят, пока работает, поэтому его и папку
/// доудаляет отложенная команда. Зовётся ПОСЛЕ окна с сообщением: пока окно
/// открыто, exe занят, и `del` из-под него не срабатывал.
/// Командную строку cmd отдаём как есть (raw_arg): std::process::Command
/// иначе экранирует кавычки как \", чего cmd не понимает.
pub fn uninstall_finish() {
    let dir = exe_dir();
    let mut cmd = Command::new("cmd");
    cmd.arg("/C");
    cmd.raw_arg(format!(
        "ping 127.0.0.1 -n 3 >nul & del /q \"{}\" & rmdir \"{}\"",
        dir.join("vera-setup.exe").display(),
        dir.display()
    ));
    cmd.creation_flags(CREATE_NO_WINDOW);
    let _ = cmd.spawn();
}

/// Удаление: программа, ярлыки, запись — данные остаются, если не попросили иначе.
pub fn uninstall(purge: bool) -> Result<String, String> {
    let dir = exe_dir();
    stop_running(&dir);
    if dir.join("vera-svc.exe").exists() && service_state() != "absent" {
        let script = dir.join("uninstall-service.ps1");
        let mut cmd = Command::new("powershell.exe");
        cmd.args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{}\"'",
                script.display()
            ),
        ]);
        let _ = run_hidden(&mut cmd);
    }
    #[cfg(windows)]
    {
        use winreg::enums::HKEY_CURRENT_USER;
        use winreg::RegKey;
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let _ = hkcu.delete_subkey_all(format!(
            "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{PRODUCT}"
        ));
        let _ = hkcu.delete_subkey_all(format!("Software\\Classes\\AppUserModelId\\{AUMID}"));
    }
    // Ярлыки: с именем продукта и с именем агента (после установки они его).
    let mut names = vec![PRODUCT.to_string()];
    if let Some(agent) = installed_agent_name(&dir) {
        names.push(agent);
    }
    for name in &names {
        if let Some(appdata) = std::env::var_os("APPDATA") {
            let lnk = PathBuf::from(appdata)
                .join("Microsoft\\Windows\\Start Menu\\Programs")
                .join(format!("{name}.lnk"));
            let _ = std::fs::remove_file(lnk);
        }
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            let _ = std::fs::remove_file(PathBuf::from(profile).join("Desktop").join(format!("{name}.lnk")));
        }
    }
    // Файлы программы: всё, кроме data/ и самого установщика (он занят) — их
    // доудалит отложенная команда после выхода.
    for entry in std::fs::read_dir(&dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name();
        if name == "data" && !purge {
            continue;
        }
        if name == "vera-setup.exe" {
            continue;
        }
        let path = entry.path();
        let _ = if path.is_dir() { std::fs::remove_dir_all(&path) } else { std::fs::remove_file(&path) };
    }
    Ok(if purge {
        format!("{PRODUCT} удалена вместе с данными")
    } else {
        format!("{PRODUCT} удалена; данные агента оставлены в {}", dir.join("data").display())
    })
}

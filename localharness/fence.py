# -*- coding: utf-8 -*-
"""Песочница v1 — ограда для рук, которые трогают машину.

Что даёт честно:
  * рука `shell` выполняется процессом в AppContainer Windows: у него нет доступа
    к файлам пользователя, кроме выданных явно — папка Vera (чтение) и рабочая
    папка агента `data/workspace` (чтение и запись). Сеть — по ручке
    `sandbox.network` (по умолчанию есть);
  * файловые руки fs_read / fs_write / fs_edit / fs_ls / fs_search получают отказ,
    если путь ведёт за пределы папки Vera.
Чего не даёт: остальные руки (computer, host_ctl, run, coding_*) не огорожены —
это записано в анатомии, чтобы владелец не думал иначе. Это ограда, не тюрьма
для самого агента: его память и код внутри папки Vera ему доступны.

Включается `sandbox.enabled` в vera.json (по умолчанию включена). Если контейнер
поднять не удалось (старая Windows, ошибка прав), shell работает без ограды,
и причина видна в анатомии и в логе — молча притворяться защитой нельзя.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import logging
import msvcrt
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("vera.fence")

STATE: dict = {"enabled": False, "container": False, "reason": "выключена", "roots": []}

_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_WAIT_TIMEOUT = 0x102
_INTERNET_CLIENT_SID = "S-1-15-3-1"


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def _dlls():
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.CreateAppContainerProfile.argtypes = [
        wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wt.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wt.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.InitializeProcThreadAttributeList.restype = wt.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p]
    kernel32.UpdateProcThreadAttribute.restype = wt.BOOL
    kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
        ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION)]
    kernel32.CreateProcessW.restype = wt.BOOL
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    return userenv, kernel32, advapi32


class Container:
    """AppContainer для одной установки: имя от пути папки, права выданы один раз."""

    def __init__(self, install_root: Path, workspace: Path, network: bool):
        self.root = install_root
        self.workspace = workspace
        self.network = network
        self.userenv, self.kernel32, self.advapi32 = _dlls()
        digest = hashlib.sha1(str(install_root).lower().encode("utf-8")).hexdigest()[:12]
        self.name = f"vera.shell.{digest}"
        self.sid = ctypes.c_void_p()
        self.sid_text = ""
        self.caps: list[ctypes.c_void_p] = []

    def prepare(self) -> None:
        hr = self.userenv.CreateAppContainerProfile(
            self.name, "Vera shell", "Ограда shell-руки агента Vera", None, 0,
            ctypes.byref(self.sid))
        if hr != 0:
            # 0x800700B7 = уже есть: тогда просто выводим SID из имени.
            hr2 = self.userenv.DeriveAppContainerSidFromAppContainerName(
                self.name, ctypes.byref(self.sid))
            if hr2 != 0:
                raise OSError(f"AppContainer не создался (hr=0x{hr & 0xFFFFFFFF:08X})")
        text = wt.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(self.sid, ctypes.byref(text)):
            raise OSError("SID контейнера не читается")
        self.sid_text = str(text.value)
        if self.network:
            cap = ctypes.c_void_p()
            if self.advapi32.ConvertStringSidToSidW(_INTERNET_CLIENT_SID, ctypes.byref(cap)):
                self.caps.append(cap)
        self._grant()

    def _grant(self) -> None:
        """Права контейнеру: папка Vera — читать, workspace — читать и писать.
        Один раз на установку: отметка рядом с workspace."""
        marker = self.workspace / ".fence" / f"acl-{self.sid_text}.ok"
        if marker.exists():
            return
        (self.workspace / ".fence").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".tmp").mkdir(parents=True, exist_ok=True)
        grants = [(self.root, "(OI)(CI)RX"), (self.workspace, "(OI)(CI)F")]
        for path, right in grants:
            proc = subprocess.run(
                ["icacls", str(path), "/grant", f"*{self.sid_text}:{right}", "/Q"],
                capture_output=True, text=True, encoding="cp866", errors="replace",
                creationflags=_CREATE_NO_WINDOW, timeout=600)
            if proc.returncode != 0:
                raise OSError(f"icacls {path}: {proc.stdout.strip() or proc.stderr.strip()}")
        marker.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")

    def run(self, argv: list[str], cwd: Path, timeout: float) -> tuple[str, int, bool]:
        """Запустить argv в контейнере; -> (вывод, код, прервано по таймауту)."""
        caps_arr = (_SID_AND_ATTRIBUTES * max(1, len(self.caps)))()
        for i, cap in enumerate(self.caps):
            caps_arr[i].Sid = cap
            caps_arr[i].Attributes = 4  # SE_GROUP_ENABLED
        sc = _SECURITY_CAPABILITIES()
        sc.AppContainerSid = self.sid
        sc.Capabilities = ctypes.cast(caps_arr, ctypes.POINTER(_SID_AND_ATTRIBUTES)) if self.caps else None
        sc.CapabilityCount = len(self.caps)
        size = ctypes.c_size_t(0)
        self.kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not self.kernel32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
            raise OSError("InitializeProcThreadAttributeList")
        if not self.kernel32.UpdateProcThreadAttribute(
                attrs, 0, _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sc), ctypes.sizeof(sc), None, None):
            raise OSError(f"UpdateProcThreadAttribute: {ctypes.get_last_error()}")

        tmp = self.workspace / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out_path = tmp / f"shell-{os.getpid()}-{int(time.time() * 1000)}.out"
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
        handle = msvcrt.get_osfhandle(fd)
        os.set_handle_inheritable(handle, True)

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        si.StartupInfo.hStdOutput = handle
        si.StartupInfo.hStdError = handle
        si.StartupInfo.hStdInput = None
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        pi = _PROCESS_INFORMATION()

        runtime = self.root / "runtime"
        # Среда: системные переменные наследуем (без них CreateProcess в
        # AppContainer отвечает 203), своё — поверх: дом и временные файлы в
        # workspace, PATH только из рантайма и системных папок.
        keep = ("SystemRoot", "windir", "SystemDrive", "ComSpec", "LOCALAPPDATA", "APPDATA",
                "USERPROFILE", "ALLUSERSPROFILE", "ProgramData", "PUBLIC", "USERNAME",
                "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update({
            "PATH": os.pathsep.join([str(runtime), str(runtime / "shims"), str(runtime / "Scripts"),
                                     r"C:\Windows\System32", r"C:\Windows"]),
            "TEMP": str(tmp), "TMP": str(tmp), "HOME": str(self.workspace),
            "PYTHONUTF8": "1", "LANG": "ru_RU.UTF-8", "VERA_SANDBOX": "1",
        })
        env_block = ctypes.create_unicode_buffer(
            "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0")
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        ok = self.kernel32.CreateProcessW(
            None, cmdline, None, None, True,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT,
            env_block, str(cwd), ctypes.byref(si), ctypes.byref(pi))
        err = ctypes.get_last_error()
        os.close(fd)
        if not ok:
            out_path.unlink(missing_ok=True)
            raise OSError(f"CreateProcess в контейнере не удался (код {err})")
        timed_out = False
        wait = self.kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
        if wait == _WAIT_TIMEOUT:
            timed_out = True
            self.kernel32.TerminateProcess(pi.hProcess, 124)
            self.kernel32.WaitForSingleObject(pi.hProcess, 5000)
        code = wt.DWORD(0)
        self.kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        self.kernel32.CloseHandle(pi.hThread)
        self.kernel32.CloseHandle(pi.hProcess)
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        # Внук (python, запущенный из bash) может ещё держать унаследованный
        # вывод: удаляем с короткими повторами, остаток подметает следующий запуск.
        for _ in range(8):
            try:
                out_path.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.25)
        self._sweep(tmp)
        return text, int(code.value), timed_out

    @staticmethod
    def _sweep(tmp: Path) -> None:
        cutoff = time.time() - 3600
        for stale in tmp.glob("shell-*.out"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                continue


class _SubprocessShim:
    """Подмена `subprocess` в модуле агента: `bash -lc …` уходит в контейнер, всё
    остальное — в настоящий subprocess. Так её tool_shell (журнал, точки отката)
    остаётся её, меняется только исполнение."""

    def __init__(self, real, container: Container, workspace: Path):
        self._real = real
        self._container = container
        self._workspace = workspace

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, args, *pargs, **kwargs):
        is_shell = (isinstance(args, (list, tuple)) and len(args) == 3
                    and str(args[0]).lower().rstrip(".exe") in ("bash", "sh")
                    and args[1] == "-lc")
        if not is_shell:
            return self._real.run(args, *pargs, **kwargs)
        bash = self._container.root / "runtime" / "bash.exe"
        argv = [str(bash if bash.exists() else args[0]), "-lc", str(args[2])]
        timeout = float(kwargs.get("timeout") or 30)
        try:
            out, code, timed_out = self._container.run(argv, self._workspace, timeout)
        except OSError as exc:
            log.warning("песочница не запустила команду (%s) — выполняю без ограды", exc)
            return self._real.run(args, *pargs, **kwargs)
        if timed_out:
            raise self._real.TimeoutExpired(args, timeout, output=out)
        return self._real.CompletedProcess(args, code, stdout=out, stderr=None)


def _inside(path: str, roots: list[Path], base: Path) -> bool:
    raw = str(path or "").strip()
    if not raw:
        return True
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    try:
        resolved = p.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def install(agent_mod, tree: Path, cfg: dict) -> None:
    """Поднять ограду по vera.json. Ничего не роняет: не вышло — записано почему."""
    sandbox = dict(cfg.get("sandbox") or {})
    enabled = bool(sandbox.get("enabled", True))
    network = bool(sandbox.get("network", True))
    tree = Path(tree).resolve()
    install_root = tree.parent
    workspace = tree / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    STATE.update({"enabled": enabled, "container": False, "roots": [str(install_root)],
                  "reason": "выключена в настройках" if not enabled else ""})
    if not enabled:
        log.info("песочница: выключена в настройках")
        return

    # 1. shell — в контейнер.
    try:
        container = Container(install_root, workspace, network)
        container.prepare()
        agent_mod.subprocess = _SubprocessShim(agent_mod.subprocess, container, workspace)
        STATE["container"] = True
        STATE["reason"] = f"shell в AppContainer {container.sid_text}, сеть {'есть' if network else 'нет'}"
        log.info("песочница: %s", STATE["reason"])
    except Exception as exc:
        STATE["reason"] = f"контейнер не поднялся: {exc}; shell без ограды"
        log.warning("песочница: %s", STATE["reason"])

    # 2. файловые руки — только внутри папки Vera.
    impl = getattr(agent_mod, "TOOL_IMPL", None)
    if not isinstance(impl, dict):
        return
    roots = [install_root]
    base = Path(getattr(agent_mod, "BASE", install_root))

    def fenced(name, fn):
        def wrapper(*args, **kwargs):
            for key in ("path", "root", "directory", "glob_root"):
                value = kwargs.get(key)
                if isinstance(value, str) and not _inside(value, roots, base):
                    return (f"песочница: путь вне папки Vera — {value}. Руки работают "
                            f"только внутри {install_root}; ограду снимает владелец в настройках.")
            if args and isinstance(args[0], str) and not _inside(args[0], roots, base):
                return (f"песочница: путь вне папки Vera — {args[0]}. Руки работают "
                        f"только внутри {install_root}; ограду снимает владелец в настройках.")
            return fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", name)
        wrapper.__doc__ = getattr(fn, "__doc__", "")
        return wrapper

    fenced_names = [n for n in ("fs_read", "fs_write", "fs_edit", "fs_ls", "fs_search") if n in impl]
    for name in fenced_names:
        impl[name] = fenced(name, impl[name])
    log.info("песочница: файловые руки в ограде — %s", ", ".join(fenced_names) or "нет")


def state() -> dict:
    return dict(STATE)

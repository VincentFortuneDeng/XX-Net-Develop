"""只保存可公开的本机启动与桌面状态。"""
import threading


_LOCK = threading.Lock()
_STATE = {
    "startup_mode": "not_started",
    "allow_remote": None,
    "no_mess_system": None,
    "no_popup": None,
    "no_systray": None,
    "restart_from_except": None,
    "page_open_request": "not_requested",
    "tray_backend": "unknown",
    "tray_state": "not_started",
}


def record_argv(argv):
    flags = set(argv[1:] if argv and not str(argv[0]).startswith("-") else argv)
    no_popup = "-no_popup" in flags
    no_systray = "-no_systray" in flags
    if no_popup and no_systray:
        mode = "headless_service"
    elif not no_popup and not no_systray:
        mode = "default"
    else:
        mode = "custom"
    with _LOCK:
        _STATE.update({
            "startup_mode": mode,
            "allow_remote": "-allow_remote" in flags,
            "no_mess_system": "-no_mess_system" in flags,
            "no_popup": no_popup,
            "no_systray": no_systray,
        })


def record_restart_from_except(value):
    with _LOCK:
        _STATE["restart_from_except"] = bool(value)


def record_page_open(result):
    with _LOCK:
        _STATE["page_open_request"] = "succeeded" if result else "failed"


def record_tray_backend(backend):
    if backend not in ("native", "fallback", "disabled", "unknown"):
        backend = "unknown"
    with _LOCK:
        _STATE["tray_backend"] = backend


def record_tray_started():
    with _LOCK:
        _STATE["tray_state"] = "running"


def record_tray_returned():
    with _LOCK:
        _STATE["tray_state"] = "returned"


def record_tray_disabled():
    with _LOCK:
        _STATE["tray_backend"] = "disabled"
        _STATE["tray_state"] = "disabled"


def snapshot():
    with _LOCK:
        return dict(_STATE)

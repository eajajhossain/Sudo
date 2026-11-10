"""Start SUDO: `python -m sudo_agent` opens the desktop window."""
from __future__ import annotations

import argparse
import ctypes
import secrets
import socket
import sys
import threading
import time
import traceback
import webbrowser

import uvicorn

from . import config
from .agent import TaskManager, start_stop_hotkey
from .events import EventBus
from .memory import Memory
from .server import create_app
from .tools.browser import Browser


def _free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def _prepare_streams() -> None:
    """A double-clicked app has no console, and libraries that write to one crash without this."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    folder = config.data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log = open(folder / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def _show_startup_error(error: BaseException) -> None:
    """Never fail silently: the owner may have double-clicked and be looking at nothing."""
    traceback.print_exception(error)
    ctypes.windll.user32.MessageBoxW(None, f"SUDO could not start.\n\n{error}\n\nDetails: {config.data_dir() / 'logs' / 'app.log'}", "SUDO", 0x10)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sudo_agent")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser", action="store_true", help="Open the UI in the default browser instead of a window")
    args = parser.parse_args()

    config.load_dotenv(config.ROOT / ".env")
    directory = config.data_dir()
    settings = config.Settings.load(directory)
    memory = Memory(directory / "sudo.db")
    resumable = memory.resumable_tasks()
    recovered = memory.recover_interrupted()
    events = EventBus(directory / "logs")
    browser = Browser(settings)
    manager = TaskManager(settings, memory, events, browser)
    start_stop_hotkey(manager, events)
    if recovered:
        events.emit("recovered", tasks=recovered)

    token = secrets.token_urlsafe(32)
    port = _free_port(args.port)
    app = create_app(manager, memory, settings, token, port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="sudo-server")
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    manager.schedule_auto_resume(resumable)

    url = f"http://127.0.0.1:{port}/?token={token}"
    opened_window = False
    if not args.browser:
        try:
            import webview

            webview.create_window("SUDO", url, width=1320, height=860, min_size=(420, 560), background_color="#0B0C0E")
            opened_window = True
            webview.start(gui="edgechromium")
        except Exception as exc:  # noqa: BLE001 - fall back to the browser
            print(f"Native window unavailable ({exc}); opening in the browser.")
            opened_window = False
    if not opened_window:
        webbrowser.open(url)
        print(f"SUDO is running on port {port}. Press Ctrl+C here to quit.")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    manager.close()
    browser.shutdown()
    server.should_exit = True


if __name__ == "__main__":
    _prepare_streams()
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - shown to the owner, then the app exits
        _show_startup_error(exc)
        sys.exit(1)

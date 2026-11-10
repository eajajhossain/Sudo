"""Local API and live event socket. This server can operate the computer, so it binds
to loopback only and refuses anything without the per-run token, a local Host header
and a same-origin Origin header. That blocks other websites from reaching it."""
from __future__ import annotations

import asyncio
import hmac
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import TaskManager
from .memory import KINDS, Memory, SecretRejected

UI_DIR = Path(__file__).parent / "ui"
VOICE_SCRIPT = Path(__file__).parent / "voice_listen.ps1"
CSP = ("default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; img-src 'self' data:; "
       "style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class LocalGuard:
    """Checks every HTTP and WebSocket request before the app sees it."""

    def __init__(self, app, token: str, port: int):
        self.app, self.token, self.port = app, token, port
        self.hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self.origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        allowed = headers.get(b"host") in self.hosts
        origin = headers.get(b"origin")
        path = scope["path"]
        if path == "/ext":  # Only a browser extension can send this origin; a website cannot.
            allowed = allowed and scope["type"] == "websocket" and (origin or b"").startswith(b"chrome-extension://")
        elif origin is not None and origin not in self.origins:
            allowed = False
        if allowed and (path.startswith("/api/") or path == "/ws"):
            query = parse_qs(scope.get("query_string", b"").decode())
            supplied = headers.get(b"x-sudo-token", b"").decode() or (query.get("token") or [""])[0]
            allowed = hmac.compare_digest(supplied.encode(), self.token.encode())
        if allowed:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Forbidden"})


class Command(BaseModel):
    text: str
    mode: str = "auto"


class PromptAnswer(BaseModel):
    value: bool | str | None = None


class MemoryIn(BaseModel):
    kind: str
    title: str
    content: str


class MemoryPatch(BaseModel):
    kind: str | None = None
    title: str | None = None
    content: str | None = None


class KeyIn(BaseModel):
    value: str


def create_app(manager: TaskManager, memory: Memory, settings: config.Settings, token: str, port: int) -> LocalGuard:
    app = FastAPI(title="SUDO", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # -- commands --------------------------------------------------------------
    @app.get("/api/state")
    def state():
        return {**manager.state(), "history": list(manager.events.history)[-200:]}

    @app.post("/api/command")
    def command(body: Command):
        if not body.text.strip():
            raise HTTPException(400, "Empty command.")
        if body.mode not in ("auto", "queue", "urgent"):
            raise HTTPException(400, "Unknown mode.")
        return manager.command(body.text.strip(), body.mode)

    @app.post("/api/stop")
    def stop():
        manager.stop()
        return {"ok": True}

    @app.post("/api/pause")
    def pause():
        manager.pause()
        return {"ok": True}

    @app.post("/api/resume")
    def resume():
        manager.resume()
        return {"ok": True}

    @app.post("/api/prompts/{prompt_id}")
    def answer(prompt_id: str, body: PromptAnswer):
        if not manager.answer_prompt(prompt_id, body.value):
            raise HTTPException(404, "That request is no longer waiting.")
        return {"ok": True}

    # -- screen ----------------------------------------------------------------
    @app.get("/api/screen")
    def screen():
        from .tools.desktop import capture

        data, width, height = capture(55)
        return {"image": f"data:image/jpeg;base64,{data}", "width": width, "height": height}

    # -- tasks -----------------------------------------------------------------
    @app.get("/api/tasks")
    def tasks():
        return memory.list_tasks()

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        row = memory.get_task(task_id)
        if row is None:
            raise HTTPException(404)
        return {**row, "steps": memory.steps(task_id)}

    @app.get("/api/conversation")
    def conversation():
        return memory.conversation()

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str):
        try:
            return {"task_id": manager.resume_task(task_id)}
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        memory.delete_task(task_id)
        return {"ok": True}

    # -- memory ----------------------------------------------------------------
    @app.get("/api/memories")
    def memories():
        return {"items": memory.list_memories(), "kinds": sorted(KINDS)}

    @app.post("/api/memories")
    def add_memory(body: MemoryIn):
        try:
            return memory.remember(body.kind, body.title, body.content)
        except (SecretRejected, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.patch("/api/memories/{mem_id}")
    def patch_memory(mem_id: str, body: MemoryPatch):
        try:
            row = memory.update_memory(mem_id, body.title, body.content, body.kind)
        except (SecretRejected, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        if row is None:
            raise HTTPException(404)
        return row

    @app.delete("/api/memories/{mem_id}")
    def delete_memory(mem_id: str):
        if not memory.delete_memory(mem_id):
            raise HTTPException(404)
        return {"ok": True}

    # -- settings --------------------------------------------------------------
    def settings_view() -> dict:
        from dataclasses import asdict

        from .tools.browser import BROWSER_PATHS, find_browser

        return {**asdict(settings),
                "keys": {p: bool(config.get_key(p)) for p in config.KEY_ENV},
                "installed_browsers": {b: find_browser(b) is not None for b in BROWSER_PATHS},
                "extension": {"connected": manager.bridge.connected, "code": config.pairing_code(),
                              "port": port, "folder": str(config.ROOT / "browser_extension")}}

    @app.get("/api/models")
    def models():
        from .llm import LLMError, list_models

        try:
            return {"models": list_models(settings)}
        except LLMError:
            return {"models": []}
        except Exception:  # noqa: BLE001 - the dropdown is a convenience; the box still accepts typing
            return {"models": []}

    @app.get("/api/settings")
    def get_settings():
        return settings_view()

    @app.put("/api/settings")
    def put_settings(values: dict):
        values.pop("keys", None)
        values.pop("installed_browsers", None)
        values.pop("extension", None)
        try:
            settings.update(values)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        settings.save()
        if manager.browser is not None:
            manager.browser.switch(settings.browser)
        return settings_view()

    @app.put("/api/keys/{provider}")
    def put_key(provider: str, body: KeyIn):
        if provider not in config.KEY_ENV or not body.value.strip():
            raise HTTPException(422, "Unknown provider or empty key.")
        config.set_key(provider, body.value.strip())
        return settings_view()

    @app.delete("/api/keys/{provider}")
    def delete_key(provider: str):
        if provider not in config.KEY_ENV:
            raise HTTPException(422, "Unknown provider.")
        config.delete_key(provider)
        return settings_view()

    # -- voice -----------------------------------------------------------------
    @app.post("/api/voice")
    def voice():
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-File", str(VOICE_SCRIPT)],
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if done.returncode == 3:
            raise HTTPException(503, "Windows has no speech recognizer installed.")
        if done.returncode != 0:
            raise HTTPException(503, "No microphone is available, or Windows could not use it.")
        return {"text": done.stdout.decode("utf-8", "replace").strip()}

    # -- the owner's own browser, through the extension -------------------------
    @app.websocket("/ext")
    async def extension(socket: WebSocket):
        await socket.accept()  # Accept first: a browser can only read the reason from an open socket.
        if not hmac.compare_digest(socket.query_params.get("code", "").encode(), config.pairing_code().encode()):
            await socket.close(code=4403)
            return
        await socket.send_json({"hello": True})
        bridge = manager.bridge
        bridge.attach(socket, asyncio.get_running_loop())
        manager.events.emit("extension_link", connected=True)
        try:
            while True:
                bridge.deliver(await socket.receive_json())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bridge.detach(socket)
            manager.events.emit("extension_link", connected=False)

    # -- live events -----------------------------------------------------------
    @app.websocket("/ws")
    async def events(socket: WebSocket):
        await socket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)

        def enqueue(event: dict) -> None:
            if queue.full() and event["type"] in ("text_delta", "screenshot", "terminal_output"):
                return
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        unsubscribe = manager.events.subscribe(lambda e: loop.call_soon_threadsafe(enqueue, e))
        try:
            while True:
                await socket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsubscribe()

    # -- UI --------------------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    return LocalGuard(app, token, port)

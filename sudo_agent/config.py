"""Settings and credential lookup. No secret is ever written to settings.json."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
CRED_PREFIX = "SUDO/"


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into the environment without overriding real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def data_dir() -> Path:
    path = Path(os.environ.get("SUDO_DATA_DIR") or ROOT / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-6.1-terra"}
PERMISSION_GROUPS = ("computer", "browser", "terminal", "files", "research", "system")
PERMISSION_LEVELS = ("auto", "ask", "off")
BROWSERS = ("chrome", "edge", "brave")


def _default_permissions() -> dict:
    return {group: "auto" for group in PERMISSION_GROUPS}


@dataclass
class Settings:
    provider: str = "anthropic"  # "anthropic" | "openai" (OpenAI, or any compatible server)
    model: str = "claude-opus-5-5"
    effort: str = "high"
    openai_base_url: str = ""
    autonomy: str = "balanced"  # "balanced" | "cautious"
    vision: bool = True
    max_steps: int = 80
    cdp_port: int = 9333
    browser: str = "chrome"
    auto_resume: bool = True
    permissions: dict = field(default_factory=_default_permissions)

    @classmethod
    def load(cls, directory: Path | None = None) -> "Settings":
        directory = directory or data_dir()
        settings = cls()
        file = directory / "settings.json"
        if file.is_file():
            try:
                saved = json.loads(file.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}
            for f in fields(cls):
                if f.name in saved:
                    setattr(settings, f.name, saved[f.name])
        for f in fields(cls):
            env = os.environ.get("SUDO_" + f.name.upper())
            if env is not None and f.name != "permissions":
                current = getattr(settings, f.name)
                if isinstance(current, bool):
                    setattr(settings, f.name, env.lower() in ("1", "true", "yes"))
                elif isinstance(current, int):
                    setattr(settings, f.name, int(env))
                else:
                    setattr(settings, f.name, env)
        settings.permissions = {**_default_permissions(), **settings.permissions}
        return settings

    def save(self, directory: Path | None = None) -> None:
        directory = directory or data_dir()
        (directory / "settings.json").write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )

    def update(self, values: dict) -> None:
        allowed = {f.name: type(getattr(self, f.name)) for f in fields(self)}
        previous = self.provider
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Unknown setting: {key}")
            setattr(self, key, allowed[key](value))
        if self.autonomy not in ("balanced", "cautious"):
            raise ValueError("autonomy must be 'balanced' or 'cautious'")
        if self.provider not in KEY_ENV:
            raise ValueError("provider must be 'anthropic' or 'openai'")
        if self.browser not in BROWSERS:
            raise ValueError(f"browser must be one of {BROWSERS}")
        if set(self.permissions) != set(PERMISSION_GROUPS) or any(v not in PERMISSION_LEVELS for v in self.permissions.values()):
            raise ValueError("permissions must give auto, ask or off for every group")
        # A model name from the other provider can never work, so switch it too.
        if self.provider != previous and self.model == DEFAULT_MODELS[previous]:
            self.model = DEFAULT_MODELS[self.provider]


# ---- API keys: environment first, then Windows Credential Manager ------------

def get_key(provider: str) -> str:
    value = os.environ.get(KEY_ENV.get(provider, ""), "")
    if value:
        return value
    try:
        import win32cred

        cred = win32cred.CredRead(CRED_PREFIX + provider, win32cred.CRED_TYPE_GENERIC)
        return cred["CredentialBlob"].decode("utf-16-le")
    except Exception:
        return ""


def set_key(provider: str, value: str) -> None:
    import win32cred

    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": CRED_PREFIX + provider,
            "UserName": "sudo",
            "CredentialBlob": value,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


def pairing_code() -> str:
    """The code the browser extension must present. Made once, kept in Credential Manager."""
    override = os.environ.get("SUDO_PAIRING_CODE")
    if override:
        return override
    code = get_key("extension-pairing")
    if not code:
        code = secrets.token_urlsafe(18)
        set_key("extension-pairing", code)
    return code


def delete_key(provider: str) -> None:
    import win32cred

    try:
        win32cred.CredDelete(CRED_PREFIX + provider, win32cred.CRED_TYPE_GENERIC)
    except Exception:
        pass

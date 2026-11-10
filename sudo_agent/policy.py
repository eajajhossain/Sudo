"""Safety policy: what runs freely, what needs the owner's approval, what never runs.

These are pattern checks, not a sandbox. They catch ordinary cases and mistakes;
they are a guard rail on a process that runs with the owner's own permissions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ALLOW, CONFIRM, BLOCK = "allow", "confirm", "block"


@dataclass(frozen=True)
class Decision:
    level: str = ALLOW
    reason: str = ""


OK = Decision()


def confirm(reason: str) -> Decision:
    return Decision(CONFIRM, reason)


def block(reason: str) -> Decision:
    return Decision(BLOCK, reason)


# ---- Secrets ----------------------------------------------------------------

_SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_\-]{10,}",
    r"\bsk-[A-Za-z0-9_\-]{20,}",
    r"\bgsk_[A-Za-z0-9]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
]
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in _SECRET_PATTERNS))
_ASSIGNED_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token)"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;]{4,})"
)
_CARD_RE = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def contains_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text) or _ASSIGNED_RE.search(text) or _has_card(text))


def redact(text: str, extra: set[str] | None = None) -> str:
    if not text:
        return text
    for value in extra or ():
        if value:
            text = text.replace(value, "[REDACTED]")
    text = _SECRET_RE.sub("[REDACTED]", text)
    return _ASSIGNED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def redact_args(value, extra: set[str] | None = None):
    if isinstance(value, str):
        return redact(value, extra)
    if isinstance(value, dict):
        return {k: redact_args(v, extra) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_args(v, extra) for v in value]
    return value


# ---- Paths ------------------------------------------------------------------

_SENSITIVE_PATH = re.compile(
    r"(?i)(\\|/)\.ssh(\\|/)|(\\|/)\.aws(\\|/)credentials|(\\|/)\.env($|\.(?!example)[^\\/]*$)"
    r"|(\\|/)\.git-credentials$|login data|(\\|/)cookies(-journal)?$|(\\|/)local state$"
    r"|\.(pem|pfx|p12|kdbx)$|(\\|/)id_(rsa|ed25519|ecdsa)|credentials\.json$"
    r"|microsoft(\\|/)(credentials|protect|vault)|browser-profiles"
)
_SYSTEM_PATH = re.compile(
    r"(?i)^[a-z]:(\\|/)(windows|program files( \(x86\))?|programdata|\$recycle\.bin|system volume information)(\\|/|$)"
)
_SENSITIVE_IN_COMMAND = re.compile(
    r"(?i)\.ssh[\\/]id_|login data|\.git-credentials|[\\/]\.aws[\\/]credentials"
    r"|microsoft[\\/](credentials|protect|vault)|browser-profiles|[\\/]\.env\b(?!\.example)"
)


def check_read(path: str) -> Decision:
    if _SENSITIVE_PATH.search(_normal(path)):
        return block("This file holds credentials or private keys, so SUDO will not read it.")
    return OK


def check_write(path: str) -> Decision:
    normal = _normal(path)
    if _SENSITIVE_PATH.search(normal):
        return block("This file holds credentials or private keys, so SUDO will not change it.")
    if _SYSTEM_PATH.match(normal):
        return confirm("This changes a protected Windows system folder.")
    return OK


def _normal(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return path


# ---- Terminal commands ------------------------------------------------------

_BLOCKED_COMMANDS = [
    (r"\b(set|add)-mppreference\b", "changes Windows Defender"),
    (r"\bnetsh\b.*\b(advfirewall|firewall)\b", "changes the firewall"),
    (r"\b(set|new|remove|disable)-netfirewall", "changes the firewall"),
    (r"\bset-executionpolicy\b", "changes PowerShell's security policy"),
    (r"\bbcdedit\b", "changes the boot configuration"),
    (r"\b(enablelua|consentpromptbehavior)", "changes User Account Control"),
    (r"\b(sc|sc\.exe|stop-service|set-service)\b.*\b(windefend|wscsvc|mpssvc|wuauserv)\b", "stops a security service"),
    (r"\b(wevtutil\s+cl|clear-eventlog)\b", "erases the system logs"),
    (r"\bvssadmin\b.*\bdelete\b|\bwbadmin\b.*\bdelete\b", "deletes system backups"),
    (r"\bmimikatz\b|procdump.*lsass|comsvcs.*minidump", "dumps credentials"),
    (r"\s-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{16,}", "hides its contents with encoding"),
]
_CONFIRM_COMMANDS = [
    (r"\b(remove-item|rm|rmdir|rd|del|erase|ri)\b", "deletes files"),
    (r"\bformat(-volume|\.com)?\s+([a-z]:|-)|\bdiskpart\b|\bclear-disk\b|\binitialize-disk\b", "formats or repartitions a disk"),
    (r"\b(shutdown|restart-computer|stop-computer)\b", "shuts down or restarts the computer"),
    (r"\b(taskkill|stop-process|kill)\b", "ends a running program"),
    (r"\b(stop|restart|set|remove|new)-service\b|\bsc(\.exe)?\s+(stop|delete|config|create)\b", "changes a Windows service"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--|branch\s+-d|rebase)\b", "changes git history or a remote"),
    (r"\b(winget|choco|scoop)\s+(install|uninstall|upgrade)\b|\bmsiexec\b", "installs or removes software"),
    (r"\b(pip3?|python\s+-m\s+pip)\s+(install|uninstall)\b|\bnpm\s+(i|install|uninstall)\s+(-g|--global)\b", "installs or removes packages"),
    (r"\b(iex|invoke-expression)\b|\|\s*(sh|bash|pwsh|powershell)\b", "runs downloaded or generated code"),
    (r"-verb\s+runas\b|\brunas\b|\bsudo\b", "runs with administrator rights"),
    (r"\breg(\.exe)?\s+(add|delete|import)\b|\b(set|new|remove)-itemproperty\b.*\bhk(cu|lm)\b", "changes the registry"),
    (r"\bschtasks\s+/create\b|\bregister-scheduledtask\b|\bcurrentversion\\run\b", "adds something that runs automatically"),
    (r"\bsend-mailmessage\b|\b(curl|invoke-webrequest|invoke-restmethod|iwr|irm)\b.*(-x\s*post|-method\s+(post|put|delete)|--data|-d\s|-infile|-body)", "sends data to another system"),
    (r"\bsetx\b", "changes environment variables permanently"),
]
_READ_ONLY_COMMAND = re.compile(
    r"(?i)^\s*(get-|test-|select-string|measure-|dir\b|ls\b|cat\b|type\b|echo\b|where(\.exe)?\b|whoami\b|hostname\b"
    r"|ipconfig\b|ping\b|tracert\b|nslookup\b|systeminfo\b|tasklist\b|netstat\b|git\s+(status|log|diff|show|branch\s*$|remote)\b"
    r"|(node|npm|pnpm|python|pip|git|docker|java|go|rustc|cargo)\s+(-v|--version|version)\b)"
)


def check_command(command: str, autonomy: str = "balanced") -> Decision:
    for pattern, why in _BLOCKED_COMMANDS:
        if re.search(pattern, command, re.I):
            return block(f"This command {why}. The owner has to do that personally.")
    if _SENSITIVE_IN_COMMAND.search(command):
        return block("This command touches stored credentials or private keys.")
    for pattern, why in _CONFIRM_COMMANDS:
        if re.search(pattern, command, re.I):
            return confirm(f"This command {why}.")
    if autonomy == "cautious":
        chained = re.search(r"[;&|>]", command)
        if chained or not _READ_ONLY_COMMAND.match(command):
            return confirm("Cautious mode: this command is not known to be read-only.")
    return OK


# ---- Finalizing actions -----------------------------------------------------

_FINALIZE_RE = re.compile(
    r"(?i)\b(send|submit|post|publish|pay|purchase|buy|check\s?out|place\s+order|order\s+now|confirm|delete|remove"
    r"|sign\s?up|register|create\s+account|transfer|subscribe|agree|accept|authori[sz]e|allow|deactivate"
    r"|close\s+account|reply|tweet|share|donate|book\s+now|uninstall|erase|reset|upload)\b"
)
_CHAT_APPS = re.compile(r"(?i)whatsapp|telegram|discord|slack|teams|outlook|thunderbird|signal|messenger|skype|zoom")
_BROWSERS = re.compile(r"(?i)^(chrome|msedge|brave|firefox|opera|vivaldi)\.exe$")


def is_finalize_label(label: str) -> bool:
    return bool(label and _FINALIZE_RE.search(label))


def is_message_app(process_name: str) -> bool:
    return bool(_CHAT_APPS.search(process_name or "") or _BROWSERS.match(process_name or ""))


# ---- Processes --------------------------------------------------------------

PROTECTED_PROCESSES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "msmpeng.exe", "securityhealthservice.exe", "memory compression",
}


def check_kill(process_name: str) -> Decision:
    if process_name.lower() in PROTECTED_PROCESSES:
        return block(f"{process_name} is a core Windows or security process.")
    return confirm(f"This ends the program {process_name}; any unsaved work in it is lost.")

"""System monitoring and troubleshooting. Everything here is read-only except process_end."""
from __future__ import annotations

import json
import re
import socket
import time

import httpx
import psutil

from .. import policy
from .base import ToolContext, ToolResult, tool
from .terminal import run_shell


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.1f} GB"


def _top_processes(limit: int) -> list[dict]:
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        info = proc.info
        if info["memory_info"]:
            rows.append({"pid": info["pid"], "name": info["name"], "mb": info["memory_info"].rss / 1048576})
    rows.sort(key=lambda r: r["mb"], reverse=True)
    return rows[:limit]


def _group_by_name(limit: int) -> list[tuple[str, float, int]]:
    grouped: dict[str, list[float]] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        if proc.info["memory_info"]:
            grouped.setdefault(proc.info["name"] or "?", []).append(proc.info["memory_info"].rss / 1048576)
    rows = [(n, sum(v), len(v)) for n, v in grouped.items()]
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


@tool("system_overview", "Complete CPU, memory, disk, uptime and heaviest-program figures. Start here for 'my computer is slow' or any question about memory or disk space; no extra commands are needed for those figures.",
      group="system", phase="OBSERVE", retries=1, timeout=20)
def system_overview(args, ctx):
    procs = list(psutil.process_iter())
    for proc in procs:
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            pass
    total_cpu = psutil.cpu_percent(interval=1.0)
    hot = []
    for proc in procs:
        try:
            hot.append((proc.cpu_percent(None), proc.name(), proc.pid))
        except psutil.Error:
            pass
    hot.sort(reverse=True)
    vm, swap = psutil.virtual_memory(), psutil.swap_memory()
    lines = [
        f"CPU: {total_cpu:.0f}% across {psutil.cpu_count()} logical cores",
        f"Memory: {_gb(vm.used)} used of {_gb(vm.total)} ({vm.percent:.0f}%), {_gb(vm.available)} available",
        f"Page file: {_gb(swap.used)} of {_gb(swap.total)}",
        f"Uptime: {(time.time() - psutil.boot_time()) / 3600:.1f} hours",
        "Disks:",
    ]
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            lines.append(f"  {part.device} {_gb(usage.free)} free of {_gb(usage.total)} ({usage.percent:.0f}% used)")
        except OSError:
            pass
    battery = psutil.sensors_battery()
    if battery:
        lines.append(f"Battery: {battery.percent:.0f}%, {'plugged in' if battery.power_plugged else 'on battery'}")
    lines.append("Most CPU right now:")
    lines += [f"  {cpu:5.1f}%  {name} (pid {pid})" for cpu, name, pid in hot if name != "System Idle Process"][:8]
    lines.append("Most memory, grouped by program:")
    lines += [f"  {mb:7.0f} MB  {name} x{count}" for name, mb, count in _group_by_name(10)]
    return ToolResult("\n".join(lines))


@tool("process_list", "List running programs, optionally filtered by name.", {"filter": {"type": "string"}, "limit": {"type": "integer"}},
      group="system", phase="OBSERVE", retries=1)
def process_list(args, ctx):
    needle = (args.get("filter") or "").lower()
    rows = [r for r in _top_processes(100000) if needle in (r["name"] or "").lower()]
    return ToolResult("\n".join(f"{r['pid']:>7} {r['mb']:8.0f} MB  {r['name']}" for r in rows[:args.get("limit", 60)]) or "(none)")


def _risk_end(args: dict, ctx: ToolContext) -> policy.Decision:
    try:
        return policy.check_kill(psutil.Process(args["pid"]).name())
    except psutil.Error:
        return policy.OK


@tool("process_end", "End a program by process id. Needs the owner's approval; core Windows processes are refused.",
      {"pid": {"type": "integer"}}, ["pid"], group="system", risk=_risk_end)
def process_end(args, ctx):
    try:
        proc = psutil.Process(args["pid"])
        name = proc.name()
        proc.terminate()
        proc.wait(5)
    except psutil.NoSuchProcess:
        return ToolResult("That process is not running.", ok=False)
    except psutil.TimeoutExpired:
        return ToolResult(f"{name} did not exit within 5s.", ok=False)
    except psutil.AccessDenied:
        return ToolResult("Windows denied access; that process needs administrator rights.", ok=False)
    return ToolResult(f"Ended {name}.")


@tool("network_diagnose", "Check adapters, DNS, ping and HTTPS reachability.", {"host": {"type": "string"}},
      group="system", phase="OBSERVE", timeout=40)
def network_diagnose(args, ctx):
    host = args.get("host", "www.google.com")
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        return ToolResult("That is not a valid host name.", ok=False)
    lines = ["Adapters that are up:"]
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if stats.get(name) and stats[name].isup:
            ips = [a.address for a in addrs if a.family == socket.AF_INET]
            if ips:
                lines.append(f"  {name}: {', '.join(ips)}")
    try:
        lines.append(f"DNS: {host} -> {socket.gethostbyname(host)}")
    except OSError as exc:
        lines.append(f"DNS: FAILED ({exc})")
    code, output, _ = run_shell(f"ping -n 3 {host}", "cmd", None, 15, ctx)
    lines.append(f"Ping (exit {code}): {output.strip().splitlines()[-1] if output.strip() else 'no output'}")
    try:
        started = time.monotonic()
        response = httpx.get(f"https://{host}", timeout=10, follow_redirects=True)
        lines.append(f"HTTPS: {response.status_code} in {(time.monotonic() - started) * 1000:.0f} ms")
    except httpx.HTTPError as exc:
        lines.append(f"HTTPS: FAILED ({type(exc).__name__})")
    return ToolResult("\n".join(lines))


@tool("services_list", "List Windows services, by name filter and/or status.",
      {"filter": {"type": "string"}, "status": {"type": "string", "enum": ["running", "stopped"]}},
      group="system", phase="OBSERVE", retries=1, timeout=30)
def services_list(args, ctx):
    needle, want = (args.get("filter") or "").lower(), args.get("status")
    rows = []
    for service in psutil.win_service_iter():
        try:
            info = service.as_dict()
        except (psutil.Error, OSError):  # Some services have broken config entries.
            continue
        if needle not in f"{info['name']} {info['display_name']}".lower():
            continue
        if want and info["status"] != want:
            continue
        rows.append(f"{info['status']:<9} {info['start_type'] or '':<10} {info['name']}: {info['display_name']}")
    return ToolResult("\n".join(rows[:150]) or "(none)")


@tool("system_errors", "Recent application and system errors from the Windows event log.", {"hours": {"type": "integer"}},
      group="system", phase="OBSERVE", timeout=45, untrusted=True)
def system_errors(args, ctx):
    hours = min(args.get("hours", 24), 720)
    script = (
        f"Get-WinEvent -FilterHashtable @{{LogName='Application','System'; Level=1,2; StartTime=(Get-Date).AddHours(-{hours})}} "
        "-MaxEvents 40 -ErrorAction SilentlyContinue | ForEach-Object { '{0:u} [{1}] {2}: {3}' -f $_.TimeCreated, $_.LogName, $_.ProviderName, "
        "($_.Message -replace '\\s+',' ').Substring(0, [Math]::Min(220, ($_.Message -replace '\\s+',' ').Length)) }"
    )
    _, output, timed_out = run_shell(script, "powershell", None, 35, ctx)
    if timed_out:
        return ToolResult("Reading the event log timed out.", ok=False)
    return ToolResult(output.strip() or f"No errors in the last {hours} hours.")


@tool("startup_apps", "List programs that start with Windows.", group="system", phase="OBSERVE", timeout=30)
def startup_apps(args, ctx):
    _, output, _ = run_shell(
        "Get-CimInstance Win32_StartupCommand | Select-Object Name,Location,Command | ConvertTo-Json -Compress",
        "powershell", None, 25, ctx,
    )
    try:
        rows = json.loads(output)
    except ValueError:
        return ToolResult(output.strip() or "Could not read startup apps.", ok=False)
    rows = rows if isinstance(rows, list) else [rows]
    return ToolResult("\n".join(f"{r['Name']}  [{r['Location']}]  {r['Command']}" for r in rows))


@tool("dev_environment", "Versions of common developer tools on this machine.", group="system", phase="OBSERVE", timeout=45)
def dev_environment(args, ctx):
    lines = []
    for command in ("python --version", "node --version", "npm --version", "pnpm --version", "git --version", "docker --version", "java -version"):
        code, output, _ = run_shell(command, "cmd", None, 8, ctx)
        first = output.strip().splitlines()[0] if output.strip() else ""
        lines.append(f"{command.split()[0]}: {first if code == 0 else 'not available'}")
    return ToolResult("\n".join(lines))

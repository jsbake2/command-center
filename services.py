"""Service abstraction layer for the services-panel GUI.

Each Service knows how to:
  - status()        -> ServiceStatus
  - metrics()       -> ServiceMetrics (cpu_pct, mem_mb, started_at_unix, n_procs)
  - autostart()     -> "enabled" | "disabled" | "unsupported" | "unknown"
  - start() / stop() / enable() / disable() -> ActionResult
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import psutil


class State(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class ServiceStatus:
    state: State
    detail: str = ""


@dataclass
class ServiceMetrics:
    cpu_pct: float = 0.0       # 0..100*ncpu (Linux convention) — we'll show as 0..100
    mem_mb: float = 0.0
    started_at: float = 0.0    # unix timestamp; 0 = unknown / not running
    n_procs: int = 0


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


def run(cmd: list[str] | str, *, timeout: int = 30, env: dict | None = None) -> tuple[int, str, str]:
    if isinstance(cmd, str):
        argv = shlex.split(cmd)
    else:
        argv = cmd
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(argv)}"
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e}"


def _proc_cpu_mem(pids: list[int]) -> tuple[float, float, float, int]:
    """Aggregate CPU%, mem MB, oldest-start-time over a set of pids using psutil.

    Returns (cpu_pct, mem_mb, started_at_unix, n_alive).
    """
    cpu = 0.0
    mem = 0.0
    started = 0.0
    n = 0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                # cpu_percent without an interval is non-blocking and uses last call
                cpu += p.cpu_percent(interval=None)
                mem += p.memory_info().rss
                ct = p.create_time()
                if started == 0 or ct < started:
                    started = ct
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return cpu, mem / (1024 * 1024), started, n


# Process-cache for CPU% deltas. psutil tracks CPU% per-Process instance, so we
# keep instances alive across polls.
_PROC_CACHE: dict[int, psutil.Process] = {}


def _cached_proc(pid: int) -> psutil.Process | None:
    p = _PROC_CACHE.get(pid)
    if p is not None and p.is_running() and p.pid == pid:
        return p
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        _PROC_CACHE.pop(pid, None)
        return None
    _PROC_CACHE[pid] = p
    # Prime the CPU% counter so the next call yields a valid delta.
    try:
        p.cpu_percent(interval=None)
    except psutil.NoSuchProcess:
        return None
    return p


def _gc_proc_cache() -> None:
    dead = [pid for pid, p in _PROC_CACHE.items() if not p.is_running()]
    for pid in dead:
        _PROC_CACHE.pop(pid, None)


def _aggregate_metrics(pids: list[int]) -> ServiceMetrics:
    cpu = 0.0
    mem_bytes = 0
    started = 0.0
    n = 0
    for pid in pids:
        p = _cached_proc(pid)
        if p is None:
            continue
        try:
            with p.oneshot():
                cpu += p.cpu_percent(interval=None)
                mem_bytes += p.memory_info().rss
                ct = p.create_time()
                if started == 0 or ct < started:
                    started = ct
                # children too — for compose containers the visible PID is shim,
                # but we'll handle containers via cgroups separately.
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _gc_proc_cache()
    return ServiceMetrics(cpu_pct=cpu, mem_mb=mem_bytes / (1024 * 1024), started_at=started, n_procs=n)


# ---------------------------------------------------------------------------
# Base service
# ---------------------------------------------------------------------------


@dataclass
class Service:
    key: str
    label: str
    category: str = "service"
    description: str = ""
    kill_style: str = "graceful"  # "graceful" or "hard"
    supports_disable: bool = True

    def status(self) -> ServiceStatus:
        raise NotImplementedError

    def metrics(self) -> ServiceMetrics:
        return ServiceMetrics()

    def autostart(self) -> str:
        return "unsupported"

    def start(self) -> ActionResult:
        raise NotImplementedError

    def stop(self) -> ActionResult:
        raise NotImplementedError

    def enable(self) -> ActionResult:
        return ActionResult(False, f"{self.label}: enable not supported")

    def disable(self) -> ActionResult:
        return ActionResult(False, f"{self.label}: disable not supported")


# ---------------------------------------------------------------------------
# Docker Compose project
# ---------------------------------------------------------------------------


@dataclass
class ComposeService(Service):
    compose_file: str = ""
    project_name: str = ""
    expected_services: int = 0  # if > 0, compare running count to detect partial

    def _compose(self, *args: str, timeout: int = 120) -> tuple[int, str, str]:
        cmd = ["docker", "compose", "-f", self.compose_file]
        if self.project_name:
            cmd += ["-p", self.project_name]
        cmd += list(args)
        return run(cmd, timeout=timeout)

    def _container_ids(self) -> list[str]:
        rc, out, _ = self._compose("ps", "-q", timeout=10)
        if rc != 0 or not out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def status(self) -> ServiceStatus:
        rc, out, err = run(["docker", "compose", "ls", "-a", "--format", "json"], timeout=10)
        if rc != 0:
            return ServiceStatus(State.UNKNOWN, err or "docker compose ls failed")
        try:
            entries = json.loads(out) if out else []
        except json.JSONDecodeError:
            return ServiceStatus(State.UNKNOWN, "could not parse docker compose ls output")
        for entry in entries:
            name = entry.get("Name") or ""
            cfg = entry.get("ConfigFiles") or ""
            if name == self.project_name or os.path.realpath(self.compose_file) in cfg:
                status = (entry.get("Status") or "").lower()
                running = 0
                for token in status.replace(",", " ").split():
                    if token.startswith("running("):
                        try:
                            running = int(token[len("running("):-1])
                        except ValueError:
                            pass
                if running == 0:
                    return ServiceStatus(State.STOPPED, status or "exited")
                if self.expected_services and running < self.expected_services:
                    return ServiceStatus(State.PARTIAL, status)
                return ServiceStatus(State.RUNNING, status)
        return ServiceStatus(State.STOPPED, "project not present")

    def metrics(self) -> ServiceMetrics:
        ids = self._container_ids()
        if not ids:
            return ServiceMetrics()
        rc, out, _ = run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.CPUPerc}}\t{{.MemUsage}}\t{{.Name}}", *ids],
            timeout=10,
        )
        if rc != 0 or not out:
            return ServiceMetrics()
        total_cpu = 0.0
        total_mem_mb = 0.0
        n = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            cpu_s = parts[0].strip().rstrip("%")
            mem_s = parts[1].strip().split("/")[0].strip()
            try:
                total_cpu += float(cpu_s)
            except ValueError:
                pass
            total_mem_mb += _parse_size_mb(mem_s)
            n += 1
        # Earliest StartedAt across containers
        started = 0.0
        rc2, out2, _ = run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", *ids],
            timeout=10,
        )
        if rc2 == 0 and out2:
            for line in out2.splitlines():
                t = _parse_docker_time(line.strip())
                if t and (started == 0 or t < started):
                    started = t
        return ServiceMetrics(cpu_pct=total_cpu, mem_mb=total_mem_mb, started_at=started, n_procs=n)

    def autostart(self) -> str:
        ids = self._container_ids()
        if not ids:
            return "unknown"
        rc, out, _ = run(
            ["docker", "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", *ids],
            timeout=10,
        )
        if rc != 0 or not out:
            return "unknown"
        policies = {line.strip() for line in out.splitlines() if line.strip()}
        if not policies:
            return "unknown"
        if policies <= {"no", ""}:
            return "disabled"
        return "enabled"

    def start(self) -> ActionResult:
        rc, out, err = self._compose("up", "-d", timeout=600)
        if rc == 0:
            return ActionResult(True, f"{self.label}: started")
        return ActionResult(False, err or out or f"compose up failed (rc={rc})")

    def stop(self) -> ActionResult:
        rc, out, err = self._compose("stop", timeout=300)
        if rc == 0:
            return ActionResult(True, f"{self.label}: stopped gracefully")
        return ActionResult(False, err or out or f"compose stop failed (rc={rc})")

    def disable(self) -> ActionResult:
        # Stop first, then flip restart policy on each container so the daemon
        # won't bring them back at boot.
        rc, out, err = self._compose("stop", timeout=300)
        if rc != 0:
            return ActionResult(False, err or out or "compose stop failed")
        ids = self._container_ids()
        if not ids:
            return ActionResult(True, f"{self.label}: stopped (no containers to update)")
        rc2, out2, err2 = run(
            ["docker", "update", "--restart=no", *ids],
            timeout=30,
        )
        if rc2 == 0:
            return ActionResult(True, f"{self.label}: stopped + autostart disabled")
        return ActionResult(False, err2 or out2 or "docker update failed")

    def enable(self) -> ActionResult:
        ids = self._container_ids()
        if ids:
            run(["docker", "update", "--restart=unless-stopped", *ids], timeout=30)
        rc, out, err = self._compose("up", "-d", timeout=600)
        if rc == 0:
            return ActionResult(True, f"{self.label}: started + autostart enabled")
        return ActionResult(False, err or out or "compose up failed")


def _parse_size_mb(s: str) -> float:
    s = s.strip()
    if not s:
        return 0.0
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGT]?i?B)$", s, re.IGNORECASE)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).lower()
    mult = {
        "b": 1 / (1024 * 1024),
        "kb": 1 / 1024, "kib": 1 / 1024,
        "mb": 1.0,      "mib": 1.0,
        "gb": 1024,     "gib": 1024,
        "tb": 1024 * 1024, "tib": 1024 * 1024,
    }.get(unit, 1.0)
    return val * mult


def _parse_docker_time(s: str) -> float:
    # Docker emits RFC3339 with nanos like 2026-04-29T07:36:00.123456789Z
    if not s:
        return 0.0
    s = s.replace("Z", "+00:00")
    # Trim sub-second to 6 digits for fromisoformat
    m = re.match(r"^(.*?\.\d{1,6})\d*([+-]\d{2}:\d{2})$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# systemd (system-level) service
# ---------------------------------------------------------------------------


@dataclass
class SystemdService(Service):
    unit: str = ""
    use_sudo: bool = True

    def _systemctl(self, verb: str, *, timeout: int = 30) -> tuple[int, str, str]:
        cmd = []
        if self.use_sudo and verb in ("start", "stop", "restart", "enable", "disable"):
            cmd += ["sudo", "-n"]
        cmd += ["systemctl", verb, self.unit]
        return run(cmd, timeout=timeout)

    def status(self) -> ServiceStatus:
        rc, out, err = run(["systemctl", "is-active", self.unit], timeout=5)
        text = (out or err or "").strip()
        if text == "active":
            return ServiceStatus(State.RUNNING, "active")
        if text == "activating":
            return ServiceStatus(State.PARTIAL, "activating")
        return ServiceStatus(State.STOPPED, text or "inactive")

    def _main_pid(self) -> int:
        rc, out, _ = run(
            ["systemctl", "show", self.unit, "-p", "MainPID", "--value"], timeout=5
        )
        if rc != 0:
            return 0
        try:
            return int(out.strip())
        except ValueError:
            return 0

    def _started_at(self) -> float:
        rc, out, _ = run(
            ["systemctl", "show", self.unit, "-p", "ActiveEnterTimestamp", "--value"],
            timeout=5,
        )
        if rc != 0 or not out:
            return 0.0
        # Format: "Mon 2026-04-29 07:36:00 CDT"
        from datetime import datetime
        parts = out.strip().split(None, 1)
        if len(parts) < 2:
            return 0.0
        date_part = parts[1]
        try:
            # Keep only "2026-04-29 07:36:00"
            ts_str = " ".join(date_part.split(" ")[:2])
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return 0.0

    def metrics(self) -> ServiceMetrics:
        pid = self._main_pid()
        if pid <= 0:
            return ServiceMetrics()
        # Include all descendants of the main pid for a fuller picture.
        try:
            root = _cached_proc(pid)
            if root is None:
                return ServiceMetrics()
            descendants = root.children(recursive=True)
            pids = [pid] + [c.pid for c in descendants]
        except psutil.NoSuchProcess:
            return ServiceMetrics()
        m = _aggregate_metrics(pids)
        m.started_at = self._started_at() or m.started_at
        return m

    def autostart(self) -> str:
        rc, out, _ = run(["systemctl", "is-enabled", self.unit], timeout=5)
        text = out.strip()
        if text in ("enabled", "static", "alias", "enabled-runtime"):
            return "enabled"
        if text in ("disabled", "masked", "linked"):
            return "disabled"
        return "unknown"

    def start(self) -> ActionResult:
        rc, out, err = self._systemctl("start")
        if rc == 0:
            return ActionResult(True, f"{self.label}: started")
        return ActionResult(False, err or out or f"systemctl start failed (rc={rc})")

    def stop(self) -> ActionResult:
        rc, out, err = self._systemctl("stop")
        if rc == 0:
            return ActionResult(True, f"{self.label}: stopped gracefully")
        return ActionResult(False, err or out or f"systemctl stop failed (rc={rc})")

    def enable(self) -> ActionResult:
        rc, out, err = self._systemctl("enable")
        if rc == 0:
            self._systemctl("start")
            return ActionResult(True, f"{self.label}: enabled + started")
        return ActionResult(False, err or out or "systemctl enable failed")

    def disable(self) -> ActionResult:
        # stop + disable so it won't come back at boot
        self._systemctl("stop")
        rc, out, err = self._systemctl("disable")
        if rc == 0:
            return ActionResult(True, f"{self.label}: stopped + disabled")
        return ActionResult(False, err or out or "systemctl disable failed")


# ---------------------------------------------------------------------------
# Hybrid: systemd unit that brings up a docker compose stack
# ---------------------------------------------------------------------------


@dataclass
class SystemdComposeService(SystemdService):
    """A systemd unit (Type=oneshot or similar) whose work is `docker compose
    up -d` / `docker compose down`. Status comes from the unit's is-active
    plus container health; metrics come from `docker stats` on the project's
    containers; start/stop/enable/disable go through systemd so we respect the
    unit's wrapper logic (patches, healthchecks, etc.)."""

    compose_file: str = ""
    project_name: str = ""
    expected_services: int = 0

    def _container_ids(self) -> list[str]:
        if not self.compose_file:
            return []
        cmd = ["docker", "compose", "-f", self.compose_file]
        if self.project_name:
            cmd += ["-p", self.project_name]
        cmd += ["ps", "-q"]
        rc, out, _ = run(cmd, timeout=10)
        if rc != 0 or not out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def status(self) -> ServiceStatus:
        rc, out, _ = run(["systemctl", "is-active", self.unit], timeout=5)
        unit_state = (out or "").strip()
        ids = self._container_ids()
        running_ct = 0
        if ids:
            rc2, out2, _ = run(
                ["docker", "inspect", "-f", "{{.State.Status}}", *ids],
                timeout=10,
            )
            if rc2 == 0:
                running_ct = sum(1 for line in out2.splitlines() if line.strip() == "running")
        if unit_state == "active":
            if self.expected_services and running_ct < self.expected_services:
                return ServiceStatus(State.PARTIAL, f"unit active, {running_ct}/{self.expected_services} containers running")
            return ServiceStatus(State.RUNNING, f"unit active, {running_ct} container(s)")
        if unit_state == "activating":
            return ServiceStatus(State.PARTIAL, "starting")
        if unit_state == "deactivating":
            return ServiceStatus(State.PARTIAL, "stopping")
        return ServiceStatus(State.STOPPED, unit_state or "inactive")

    def metrics(self) -> ServiceMetrics:
        ids = self._container_ids()
        if not ids:
            return ServiceMetrics()
        rc, out, _ = run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.CPUPerc}}\t{{.MemUsage}}", *ids],
            timeout=10,
        )
        if rc != 0 or not out:
            return ServiceMetrics()
        total_cpu = 0.0
        total_mem_mb = 0.0
        n = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                total_cpu += float(parts[0].strip().rstrip("%"))
            except ValueError:
                pass
            total_mem_mb += _parse_size_mb(parts[1].strip().split("/")[0].strip())
            n += 1
        started = 0.0
        rc2, out2, _ = run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", *ids],
            timeout=10,
        )
        if rc2 == 0 and out2:
            for line in out2.splitlines():
                t = _parse_docker_time(line.strip())
                if t and (started == 0 or t < started):
                    started = t
        # Fall back to the unit's ActiveEnterTimestamp if no containers reported.
        if started == 0:
            started = self._started_at()
        return ServiceMetrics(cpu_pct=total_cpu, mem_mb=total_mem_mb, started_at=started, n_procs=n)


# ---------------------------------------------------------------------------
# Satisfactory VM (libvirt) + in-VM game server service
# ---------------------------------------------------------------------------


@dataclass
class SatisfactoryVMService(Service):
    domain: str = "satisfactory"
    ssh_alias: str = "satisfactory"
    in_vm_units: tuple = ("satisfactory-sat2", "satisfactory-sat3-modded")
    _last_cpu_sample: tuple = field(default_factory=tuple)  # (ns, walltime)

    def _virsh(self, *args: str, timeout: int = 15, sudo: bool = True) -> tuple[int, str, str]:
        cmd = []
        if sudo:
            cmd += ["sudo", "-n"]
        cmd += ["virsh", "-c", "qemu:///system", *args]
        return run(cmd, timeout=timeout)

    def status(self) -> ServiceStatus:
        rc, out, err = self._virsh("domstate", self.domain, timeout=8)
        text = (out or err or "").strip()
        if rc != 0:
            return ServiceStatus(State.UNKNOWN, text or "virsh domstate failed")
        if text == "running":
            probe = " ; ".join(f"systemctl is-active {u}" for u in self.in_vm_units)
            rc2, out2, _ = run(
                ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes",
                 self.ssh_alias, probe],
                timeout=10,
            )
            if rc2 == 0 and "active" in out2 and "inactive" not in out2.replace("active", "", 1):
                return ServiceStatus(State.RUNNING, "VM up, game running")
            if "active" in out2:
                return ServiceStatus(State.RUNNING, f"VM up, game: {out2.replace(chr(10), '/')}")
            return ServiceStatus(State.PARTIAL, "VM up, game stopped")
        if text in ("shut off", "shutdown"):
            return ServiceStatus(State.STOPPED, text)
        return ServiceStatus(State.PARTIAL, text)

    def metrics(self) -> ServiceMetrics:
        # Skip metrics when the VM isn't running — domstats still returns the
        # configured balloon size which is misleading.
        rc0, out0, _ = self._virsh("domstate", self.domain, timeout=5)
        if rc0 != 0 or out0.strip() in ("shut off", "shutdown", ""):
            self._last_cpu_sample = ()
            return ServiceMetrics()
        rc, out, _ = self._virsh("domstats", self.domain, "--state", "--cpu-total", "--balloon", timeout=8)
        if rc != 0 or not out:
            return ServiceMetrics()
        kv: dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        cpu_pct = 0.0
        # cpu.time is in nanoseconds since VM start.
        cpu_ns = int(kv.get("cpu.time", "0") or 0)
        now = time.monotonic()
        if self._last_cpu_sample:
            prev_ns, prev_t = self._last_cpu_sample
            dt = now - prev_t
            if dt > 0 and cpu_ns >= prev_ns:
                # nanoseconds of CPU consumed / wall ns -> ratio; multiply by 100 for %.
                # vCPU count is in balloon.maximum? actually use vcpu.maximum.
                cpu_pct = ((cpu_ns - prev_ns) / (dt * 1e9)) * 100
        self._last_cpu_sample = (cpu_ns, now)

        mem_mb = 0.0
        # balloon.current is in KiB
        cur = kv.get("balloon.current")
        if cur:
            try:
                mem_mb = int(cur) / 1024
            except ValueError:
                pass

        # started_at: parse virsh domstats state.reason or use an explicit query
        rc2, out2, _ = self._virsh("dominfo", self.domain, timeout=8)
        started_at = 0.0
        if rc2 == 0:
            # virsh dominfo doesn't include start time. Query via systemd of qemu? Use qemu pid create_time.
            rc3, out3, _ = self._virsh("domid", self.domain, timeout=5)
            try:
                domid = int(out3.strip())
            except (ValueError, AttributeError):
                domid = 0
            if domid > 0:
                # Find the qemu process whose --uuid matches this VM.
                rc4, out4, _ = run(["pgrep", "-f", f"guest={self.domain},"], timeout=4)
                if rc4 == 0 and out4:
                    pid = int(out4.split()[0])
                    p = _cached_proc(pid)
                    if p is not None:
                        try:
                            started_at = p.create_time()
                        except psutil.NoSuchProcess:
                            pass
        return ServiceMetrics(cpu_pct=cpu_pct, mem_mb=mem_mb, started_at=started_at, n_procs=1)

    def autostart(self) -> str:
        rc, out, _ = self._virsh("dominfo", self.domain, timeout=8)
        if rc != 0:
            return "unknown"
        for line in out.splitlines():
            if line.lower().startswith("autostart"):
                val = line.split(":", 1)[1].strip().lower()
                if val in ("enable", "enabled", "yes"):
                    return "enabled"
                if val in ("disable", "disabled", "no"):
                    return "disabled"
        return "unknown"

    def start(self) -> ActionResult:
        rc, out, err = self._virsh("start", self.domain, timeout=30)
        text = (err or out or "").strip()
        if rc == 0 or "already active" in text.lower():
            return ActionResult(True, f"{self.label}: VM starting")
        return ActionResult(False, text or f"virsh start failed (rc={rc})")

    def stop(self) -> ActionResult:
        try:
            stop_units = " ".join(self.in_vm_units)
            run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 self.ssh_alias, f"sudo systemctl stop {stop_units} 2>/dev/null; true"],
                timeout=120,
            )
        except Exception:
            pass
        rc, out, err = self._virsh("shutdown", self.domain, timeout=30)
        text = (err or out or "").strip()
        if rc == 0 or "is not running" in text.lower():
            return ActionResult(True, f"{self.label}: graceful shutdown sent")
        return ActionResult(False, text or f"virsh shutdown failed (rc={rc})")

    def enable(self) -> ActionResult:
        rc, out, err = self._virsh("autostart", self.domain, timeout=8)
        if rc == 0:
            self.start()
            return ActionResult(True, f"{self.label}: autostart enabled")
        return ActionResult(False, err or out or "virsh autostart failed")

    def disable(self) -> ActionResult:
        self.stop()
        rc, out, err = self._virsh("autostart", "--disable", self.domain, timeout=8)
        if rc == 0:
            return ActionResult(True, f"{self.label}: autostart disabled + shutdown sent")
        return ActionResult(False, err or out or "virsh autostart --disable failed")


# ---------------------------------------------------------------------------
# Process-based desktop apps (Steam, Battle.net) — start by exec, stop by hard kill
# ---------------------------------------------------------------------------


@dataclass
class ProcessApp(Service):
    """A user-launched desktop app identified by process patterns."""

    detect_patterns: tuple = ()  # pgrep -f patterns; any match -> RUNNING
    detect_exact: tuple = ()     # pgrep -x names
    start_cmd: tuple = ()        # argv to exec (detached)
    start_env: dict = field(default_factory=dict)
    kill_patterns: tuple = ()    # pkill -KILL -f patterns
    kill_exact: tuple = ()       # pkill -KILL -x names
    extra_killers: tuple = ()    # additional argvs to invoke at kill time (e.g. `steam -shutdown`)
    autostart_desktop_basename: str = ""  # if set, manage ~/.config/autostart/<name>.desktop

    _NEVER_MATCH_COMM = frozenset(
        {
            "zsh", "bash", "sh", "dash", "fish", "ksh", "tcsh",
            "pgrep", "pkill", "ps", "grep", "rg", "ripgrep", "awk", "sed",
            "python", "python3", "python3.13", "python3.14",
            "services-pa", "main.py",
            "claude", "claude-code", "claude-cli",
            "node", "deno",
        }
    )

    def _proc_comm(self, pid: int) -> str:
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _running_pids(self) -> list[int]:
        pids: set[int] = set()
        for pat in self.detect_patterns:
            rc, out, _ = run(["pgrep", "-f", pat], timeout=4)
            if rc == 0 and out:
                pids.update(int(x) for x in out.split() if x.strip().isdigit())
        for name in self.detect_exact:
            rc, out, _ = run(["pgrep", "-x", name], timeout=4)
            if rc == 0 and out:
                pids.update(int(x) for x in out.split() if x.strip().isdigit())
        my_pid = os.getpid()
        clean: list[int] = []
        for p in pids:
            if p == my_pid or p in (os.getppid(),):
                continue
            comm = self._proc_comm(p)
            if not comm or comm in self._NEVER_MATCH_COMM:
                continue
            clean.append(p)
        return clean

    def status(self) -> ServiceStatus:
        pids = self._running_pids()
        if pids:
            return ServiceStatus(State.RUNNING, f"{len(pids)} process(es)")
        return ServiceStatus(State.STOPPED, "")

    def metrics(self) -> ServiceMetrics:
        pids = self._running_pids()
        if not pids:
            return ServiceMetrics()
        return _aggregate_metrics(pids)

    def autostart(self) -> str:
        if not self.autostart_desktop_basename:
            return "unsupported"
        path = Path.home() / ".config" / "autostart" / f"{self.autostart_desktop_basename}.desktop"
        if path.exists():
            try:
                txt = path.read_text()
            except OSError:
                return "unknown"
            if "Hidden=true" in txt or "X-GNOME-Autostart-enabled=false" in txt:
                return "disabled"
            return "enabled"
        return "disabled"

    def _kill_candidates(self) -> list[int]:
        pids: set[int] = set()
        for pat in self.kill_patterns:
            rc, out, _ = run(["pgrep", "-f", pat], timeout=4)
            if rc == 0 and out:
                pids.update(int(x) for x in out.split() if x.strip().isdigit())
        for name in self.kill_exact:
            rc, out, _ = run(["pgrep", "-x", name], timeout=4)
            if rc == 0 and out:
                pids.update(int(x) for x in out.split() if x.strip().isdigit())
        my_pid = os.getpid()
        clean: list[int] = []
        for p in pids:
            if p == my_pid or p == os.getppid():
                continue
            comm = self._proc_comm(p)
            if not comm or comm in self._NEVER_MATCH_COMM:
                continue
            clean.append(p)
        return clean

    def start(self) -> ActionResult:
        if not self.start_cmd:
            return ActionResult(False, f"{self.label}: no start command configured")
        argv = list(self.start_cmd)
        binary = argv[0]
        if "/" not in binary and shutil.which(binary) is None:
            return ActionResult(False, f"{self.label}: '{binary}' not found in PATH")
        try:
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, **self.start_env},
            )
        except Exception as e:
            return ActionResult(False, f"{self.label}: failed to launch — {e}")
        return ActionResult(True, f"{self.label}: launching ({' '.join(argv)})")

    def stop(self) -> ActionResult:
        for argv in self.extra_killers:
            run(list(argv), timeout=10)
        candidates = self._kill_candidates()
        if not candidates:
            return ActionResult(True, f"{self.label}: nothing to kill (already stopped)")
        for pid in candidates:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.6)
        remaining = self._kill_candidates()
        if not remaining:
            return ActionResult(True, f"{self.label}: hard-killed {len(candidates)} process(es)")
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.4)
        remaining = self._kill_candidates()
        if not remaining:
            return ActionResult(True, f"{self.label}: hard-killed (second pass)")
        return ActionResult(False, f"{self.label}: still running: {remaining}")

    def enable(self) -> ActionResult:
        if not self.autostart_desktop_basename:
            return ActionResult(False, f"{self.label}: autostart not supported")
        # Look up a system .desktop file we can copy/link into ~/.config/autostart.
        src = self._find_system_desktop()
        if src is None:
            return ActionResult(False, f"{self.label}: no .desktop file found to autostart")
        dest_dir = Path.home() / ".config" / "autostart"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{self.autostart_desktop_basename}.desktop"
        try:
            txt = src.read_text()
            # Ensure it isn't hidden.
            txt = "\n".join(
                line for line in txt.splitlines()
                if not line.startswith("Hidden=") and not line.startswith("X-GNOME-Autostart-enabled=")
            )
            if not txt.endswith("\n"):
                txt += "\n"
            dest.write_text(txt)
        except OSError as e:
            return ActionResult(False, f"{self.label}: write autostart failed: {e}")
        return ActionResult(True, f"{self.label}: autostart enabled at {dest}")

    def disable(self) -> ActionResult:
        if not self.autostart_desktop_basename:
            # No autostart file; just hard-kill the running app.
            return self.stop()
        dest = Path.home() / ".config" / "autostart" / f"{self.autostart_desktop_basename}.desktop"
        msg = []
        if dest.exists():
            try:
                dest.unlink()
                msg.append("autostart removed")
            except OSError as e:
                return ActionResult(False, f"{self.label}: could not remove autostart: {e}")
        kill = self.stop()
        msg.append(kill.message)
        return ActionResult(kill.ok, f"{self.label}: " + " — ".join(msg))

    def _find_system_desktop(self) -> Path | None:
        candidates = [
            Path("/usr/share/applications") / f"{self.autostart_desktop_basename}.desktop",
            Path.home() / ".local/share/applications" / f"{self.autostart_desktop_basename}.desktop",
        ]
        for p in candidates:
            if p.exists():
                return p
        return None


# ---------------------------------------------------------------------------
# Service catalog
# ---------------------------------------------------------------------------


def build_catalog() -> list[Service]:
    return [
        ComposeService(
            key="activepieces",
            label="Activepieces",
            category="Containers",
            description="Workflow automation stack (compose project)",
            compose_file="/home/jbaker/activepieces/docker-compose.yml",
            project_name="activepieces",
            expected_services=4,
        ),
        ComposeService(
            key="stoat",
            label="Stoat",
            category="Containers",
            description="Self-hosted chat platform (compose project)",
            compose_file="/home/jbaker/revolt/compose.yml",
            project_name="stoat",
            expected_services=14,
        ),
        SystemdComposeService(
            key="eq2emu",
            label="EQ2 Emulator",
            category="Containers",
            description="EverQuest II server emulator (eq2emu.service)",
            unit="eq2emu.service",
            compose_file="/home/jbaker/repos/eq2emu/docker/docker-compose.yaml",
            project_name="docker",  # default project = docker dir name
            expected_services=3,
        ),
        SatisfactoryVMService(
            key="satisfactory",
            label="Satisfactory Server",
            category="Game Servers",
            description="libvirt VM 'satisfactory' + in-VM systemd game unit",
            domain="satisfactory",
            ssh_alias="satisfactory",
        ),
        SystemdService(
            key="satisfactory-admin",
            label="Satisfactory Admin",
            category="Local Apps",
            description="FastAPI dashboard for the Satisfactory VM (port 8888)",
            unit="satisfactory-admin.service",
        ),
        SystemdService(
            key="poe2-tracker",
            label="PoE2 Cockpit",
            category="Local Apps",
            description="Path of Exile 2 build tracker (port 8889)",
            unit="poe2-tracker.service",
        ),
        ProcessApp(
            key="steam",
            label="Steam",
            category="Game Launchers",
            description="Hard-killed on stop",
            kill_style="hard",
            detect_patterns=(
                r"\.local/share/Steam/ubuntu12_32/steam ",
                r"\.local/share/Steam/steam\.sh",
            ),
            start_cmd=("steam",),
            kill_patterns=(
                r"\.local/share/Steam/",
                r"steamwebhelper",
                r"steam-runtime-launcher-service",
            ),
            kill_exact=("steam",),
            autostart_desktop_basename="steam",
        ),
        ProcessApp(
            key="battlenet",
            label="Battle.net",
            category="Game Launchers",
            description="Launched via Lutris; hard-killed on stop",
            kill_style="hard",
            detect_patterns=(
                r"Battle\.net Launcher\.exe",
                r"Battle\.net\.exe",
                r"Games/battlenet/.*\.exe",
            ),
            start_cmd=("lutris", "lutris:rungame/battlenet"),
            kill_patterns=(
                r"Battle\.net Launcher\.exe",
                r"Battle\.net\.exe",
                r"Battle\.net Helper\.exe",
                r"Agent\.exe",
                r"Games/battlenet/.*\.exe",
                r"umu-run.*battlenet",
            ),
            autostart_desktop_basename="net.lutris.battlenet-1",
        ),
    ]


def fmt_uptime(started_at: float) -> str:
    if not started_at:
        return "—"
    secs = max(0, int(time.time() - started_at))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    if m or h or d:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

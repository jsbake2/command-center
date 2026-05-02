"""HTTP client for the remote services-panel API.

The panel no longer touches docker/systemctl/virsh directly. Everything goes
through the API on 10.0.0.16 (the host we moved the services to).

Provides:
  - Data classes mirroring the JSON contract: State, ServiceStatus,
    ServiceMetrics, ServiceMeta, ServiceTick, SystemTick.
  - ApiClient: thin urllib wrapper. Bearer auth, JSON in/out.
  - build_catalog(client): bootstraps ServiceMeta from /services so the
    panel doesn't need to hard-code the service list.
  - fmt_uptime: shared display helper.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model (matches the API JSON)
# ---------------------------------------------------------------------------


class State(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class ServiceStatus:
    state: State
    detail: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(state=State(d.get("state", "unknown")), detail=d.get("detail", ""))


@dataclass
class ServiceMetrics:
    cpu_pct: float = 0.0
    mem_mb: float = 0.0
    started_at: float = 0.0
    n_procs: int = 0

    @classmethod
    def from_dict(cls, d):
        return cls(
            cpu_pct=float(d.get("cpu_pct", 0) or 0),
            mem_mb=float(d.get("mem_mb", 0) or 0),
            started_at=float(d.get("started_at", 0) or 0),
            n_procs=int(d.get("n_procs", 0) or 0),
        )


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


@dataclass
class ServiceMeta:
    """Static metadata about a remote service. Used to build the sidebar."""
    key: str
    label: str
    category: str = "service"
    description: str = ""
    kill_style: str = "graceful"
    supports_disable: bool = True

    # The panel expects these attributes; alias 'service' -> 'meta' is a no-op.
    @classmethod
    def from_dict(cls, d):
        return cls(
            key=d["key"],
            label=d.get("label", d["key"]),
            category=d.get("category", "service"),
            description=d.get("description", ""),
            kill_style=d.get("kill_style", "graceful"),
            supports_disable=bool(d.get("supports_disable", True)),
        )


# Back-compat alias so existing widgets/main code that referenced `Service`
# (just for typing/attr access) keeps working.
Service = ServiceMeta


@dataclass
class ServiceTick:
    """One service's current state — what the poller emits."""
    key: str
    status: ServiceStatus
    metrics: ServiceMetrics
    autostart: str

    @classmethod
    def from_dict(cls, d):
        return cls(
            key=d["key"],
            status=ServiceStatus.from_dict(d.get("status", {})),
            metrics=ServiceMetrics.from_dict(d.get("metrics", {})),
            autostart=d.get("autostart", "unknown"),
        )


@dataclass
class SystemTick:
    """Remote-host hardware snapshot."""
    hostname: str = ""
    cpu_total: float = 0.0
    cpu_per_core: list[float] = field(default_factory=list)
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    swap_used_mb: float = 0.0
    swap_total_mb: float = 0.0
    net_rx_per_s: float = 0.0
    net_tx_per_s: float = 0.0
    disk_read_per_s: float = 0.0
    disk_write_per_s: float = 0.0
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    boot_time: float = 0.0
    now: float = 0.0

    @classmethod
    def from_dict(cls, d):
        return cls(
            hostname=d.get("hostname", ""),
            cpu_total=float(d.get("cpu_total", 0) or 0),
            cpu_per_core=list(d.get("cpu_per_core", []) or []),
            mem_used_mb=float(d.get("mem_used_mb", 0) or 0),
            mem_total_mb=float(d.get("mem_total_mb", 0) or 0),
            swap_used_mb=float(d.get("swap_used_mb", 0) or 0),
            swap_total_mb=float(d.get("swap_total_mb", 0) or 0),
            net_rx_per_s=float(d.get("net_rx_per_s", 0) or 0),
            net_tx_per_s=float(d.get("net_tx_per_s", 0) or 0),
            disk_read_per_s=float(d.get("disk_read_per_s", 0) or 0),
            disk_write_per_s=float(d.get("disk_write_per_s", 0) or 0),
            load_1=float(d.get("load_1", 0) or 0),
            load_5=float(d.get("load_5", 0) or 0),
            load_15=float(d.get("load_15", 0) or 0),
            boot_time=float(d.get("boot_time", 0) or 0),
            now=float(d.get("now", time.time()) or time.time()),
        )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


DEFAULT_BASE_URL = os.environ.get("SERVICES_PANEL_API_URL", "http://10.0.0.16:9090")
DEFAULT_TOKEN_PATH = Path(
    os.environ.get(
        "SERVICES_PANEL_TOKEN_FILE",
        str(Path.home() / ".config" / "services-panel" / "token"),
    )
)


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None,
                 timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if token is None:
            try:
                token = DEFAULT_TOKEN_PATH.read_text().strip()
            except OSError as e:
                raise ApiError(
                    f"Could not read API token from {DEFAULT_TOKEN_PATH}: {e}"
                )
        if not token:
            raise ApiError(f"Token file {DEFAULT_TOKEN_PATH} is empty")
        self._token = token

    def _request(self, method: str, path: str, *, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))
                msg = err.get("error", e.reason)
            except Exception:
                msg = e.reason
            raise ApiError(f"HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise ApiError(f"network error: {e.reason}")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ApiError(f"bad JSON from API: {e}")

    # ----- endpoints -----

    def health(self) -> dict:
        return self._request("GET", "/health")

    def system(self) -> SystemTick:
        return SystemTick.from_dict(self._request("GET", "/system"))

    def services(self) -> tuple[list[ServiceMeta], list[ServiceTick]]:
        data = self._request("GET", "/services")
        metas = [ServiceMeta.from_dict(d) for d in data.get("services", [])]
        ticks = [ServiceTick.from_dict(d) for d in data.get("services", [])]
        return metas, ticks

    def service_ticks(self) -> list[ServiceTick]:
        data = self._request("GET", "/services")
        return [ServiceTick.from_dict(d) for d in data.get("services", [])]

    def action(self, key: str, action: str, *, timeout: float = 600.0) -> tuple[ActionResult, ServiceTick | None]:
        data = self._request("POST", f"/services/{key}/{action}", timeout=timeout)
        result = ActionResult(ok=bool(data.get("ok")), message=str(data.get("message", "")))
        state = data.get("state")
        tick = ServiceTick.from_dict(state) if state else None
        return result, tick


def build_catalog(client: ApiClient) -> list[ServiceMeta]:
    """One-shot: fetch service metadata from the API."""
    metas, _ = client.services()
    return metas


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


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

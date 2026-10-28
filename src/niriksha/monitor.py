"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 2A + 2B: Continuous monitor + JSONL event logger + Endpoint Migration

Architecture:
  - ServiceBrowser (zeroconf thread) populates a thread-safe registry of
    known ServiceRecords and notifies the monitor via wake_event.
  - A polling loop (main thread) calls verifier.verify_once() at configurable
    intervals or immediately upon mDNS endpoint updates.
  - State transitions, discovery events, and endpoint migrations are emitted
    as structured JSONL events to a log file AND to stdout.

State transitions tracked:
  UNKNOWN  -> any state   (first contact)
  VERIFIED -> DEGRADED    (capability drift or health regression)
  VERIFIED -> UNREACHABLE (HTTP endpoint died, ad still live)
  any      -> STALE       (mDNS advertisement withdrawn)
  UNREACHABLE -> VERIFIED (recovery / replacement verification)
  DEGRADED    -> VERIFIED (recovery / replacement verification)
  STALE       -> VERIFIED (re-advertised / replacement verification)

Migration & Timing Metrics:
  - rediscovery_latency_ms: Monotonic time from fault detection to discovery
    of the replacement endpoint via mDNS.
  - verification_latency_ms: Monotonic duration of the verification probe.
  - recovery_latency_ms: Monotonic time from fault detection to successful
    VERIFIED state at the replacement endpoint.
  - estimated_total_recovery_latency_ms: Monotonic time from last known good
    verification to successful VERIFIED state at the replacement endpoint.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
from zeroconf._services.info import ServiceInfo

from niriksha.models import ServiceRecord, ServiceState, VerificationResult
from niriksha import verifier as verifier_mod


# Ensure printable output on Windows cp1252 consoles.
if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding and \
        sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


SERVICE_TYPE = "_niriksha._tcp.local."

_DEFAULT_INTERVAL = 10      # seconds between verification polls
_DEFAULT_TIMEOUT  = 8.0     # httpx probe timeout
_DEFAULT_LOG      = "niriksha_events.jsonl"


# ---------------------------------------------------------------------------
# Shared registry (thread-safe)
# ---------------------------------------------------------------------------

class _ServiceRegistry:
    """
    Thread-safe store of currently-advertised ServiceRecords.
    Updated by the zeroconf ServiceBrowser thread; read by the poll loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ServiceRecord] = {}  # instance_name -> record
        self._stale:   dict[str, ServiceRecord] = {}  # instance_name -> removed record

    def add(self, record: ServiceRecord) -> bool:
        """Add or update record. Returns True if this is a new or changed record."""
        with self._lock:
            self._stale.pop(record.instance_name, None)
            old = self._records.get(record.instance_name)
            is_changed = (
                old is None
                or old.port != record.port
                or old.addresses != record.addresses
                or old.txt != record.txt
                or old.host != record.host
            )
            self._records[record.instance_name] = record
            return is_changed

    def remove(self, instance_name: str) -> Optional[ServiceRecord]:
        """Mark *instance_name* as stale; return the removed record or None."""
        with self._lock:
            rec = self._records.pop(instance_name, None)
            if rec is not None:
                self._stale[instance_name] = rec
            return rec

    def pop_stale(self) -> list[ServiceRecord]:
        """Return and clear all stale records (mDNS-withdrawn)."""
        with self._lock:
            stale = list(self._stale.values())
            self._stale.clear()
            return stale

    def all_records(self) -> list[ServiceRecord]:
        with self._lock:
            return list(self._records.values())


# ---------------------------------------------------------------------------
# zeroconf listener
# ---------------------------------------------------------------------------

def _decode_txt(properties: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in properties.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else (v or "")
        out[key] = val
    return out


class _MonitorListener(ServiceListener):
    """Populates registry from live mDNS events."""

    def __init__(
        self,
        zc: Zeroconf,
        registry: _ServiceRegistry,
        logger: Optional[EventLogger] = None,
        wake_event: Optional[threading.Event] = None,
    ) -> None:
        self._zc = zc
        self._registry = registry
        self._logger = logger
        self._wake_event = wake_event

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name, action="add")

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name, action="update")

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        removed = self._registry.remove(name)
        if removed:
            print(f"[MONITOR] mDNS: advertisement withdrawn for {name}", flush=True)
            if self._wake_event:
                self._wake_event.set()

    def _resolve(self, zc: Zeroconf, type_: str, name: str, action: str = "add") -> None:
        info: Optional[ServiceInfo] = zc.get_service_info(type_, name, timeout=5000)
        if info is None:
            print(f"[MONITOR] Could not resolve ServiceInfo for {name}", flush=True)
            return
        addresses = [socket.inet_ntoa(a) for a in info.addresses]
        txt = _decode_txt(info.properties)
        record = ServiceRecord(
            instance_name=name,
            service_type=type_,
            host=info.server or "",
            port=info.port,
            addresses=addresses,
            txt=txt,
        )
        changed = self._registry.add(record)
        if self._logger:
            self._logger.discovery(record, action=action)

        print(
            f"[MONITOR] mDNS: {action.upper()} {name} "
            f"at {addresses[0] if addresses else '?'}:{info.port}",
            flush=True,
        )
        if changed and self._wake_event:
            self._wake_event.set()


# ---------------------------------------------------------------------------
# JSONL event logger
# ---------------------------------------------------------------------------

class EventLogger:
    """Appends structured JSONL events to a file and echoes to stdout."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        """Write one JSON object as a line to the log file."""
        line = json.dumps(event, ensure_ascii=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(f"[EVENT] {line}", flush=True)

    def discovery(self, record: ServiceRecord, action: str = "add") -> None:
        """Emit an event when mDNS announces an addition or update."""
        self.emit({
            "event": "discovery",
            "action": action,
            "wall_clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": {
                "instance_name": record.instance_name,
                "host": record.host,
                "port": record.port,
                "addresses": record.addresses,
                "txt": record.txt,
            },
        })

    def endpoint_migration(
        self,
        record: ServiceRecord,
        old_endpoint: dict,
        new_endpoint: dict,
        rediscovery_latency_ms: Optional[float] = None,
        estimated_rediscovery_latency_ms: Optional[float] = None,
    ) -> None:
        """Emit an event when an existing service changes its advertised port or address."""
        payload: dict = {
            "event": "endpoint_migration",
            "wall_clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": {
                "instance_name": record.instance_name,
                "host": record.host,
                "port": record.port,
                "addresses": record.addresses,
                "txt": record.txt,
            },
            "old_endpoint": old_endpoint,
            "new_endpoint": new_endpoint,
            "reason": (
                f"Advertised endpoint migrated from port {old_endpoint.get('port')} "
                f"to port {new_endpoint.get('port')}"
            ),
        }
        if rediscovery_latency_ms is not None:
            payload["rediscovery_latency_ms"] = rediscovery_latency_ms
        if estimated_rediscovery_latency_ms is not None:
            payload["estimated_rediscovery_latency_ms"] = estimated_rediscovery_latency_ms
        self.emit(payload)

    def state_change(
        self,
        result: VerificationResult,
        old_state: ServiceState,
        last_verified_mono: Optional[float] = None,
        fault_detected_mono: Optional[float] = None,
        is_recovery: bool = False,
        is_migration: bool = False,
        old_endpoint: Optional[dict] = None,
        new_endpoint: Optional[dict] = None,
        recovery_latency_ms: Optional[float] = None,
        estimated_total_recovery_latency_ms: Optional[float] = None,
        rediscovery_latency_ms: Optional[float] = None,
    ) -> None:
        """Emit a state-transition event with all relevant measurement fields."""
        record = result.record
        new_state = result.state

        event: dict = {
            "event": "state_change",
            "wall_clock_utc": result.wall_clock_utc,
            "service": {
                "instance_name": record.instance_name,
                "host": record.host,
                "port": record.port,
                "addresses": record.addresses,
                "txt": record.txt,
            },
            "old_state": old_state.value if isinstance(old_state, ServiceState) else str(old_state),
            "new_state": new_state.value if isinstance(new_state, ServiceState) else str(new_state),
            "reason": result.reason,
            "measurements": {
                "http_status": result.http_status,
                "health_latency_ms": result.health_latency_ms,
                "verification_latency_ms": result.verification_latency_ms,
                "txt_fields_ok": result.txt_fields_ok,
                "health_status_ok": result.health_status_ok,
                "diag_type_ok": result.diag_type_ok,
            },
        }

        # Failure-detection latency: time from last VERIFIED to first failure.
        # Uses monotonic clock. Labelled "estimated" because the actual fault
        # moment between polls is unknown.
        if new_state in (ServiceState.UNREACHABLE, ServiceState.DEGRADED) \
                and old_state == ServiceState.VERIFIED \
                and last_verified_mono is not None:
            event["estimated_detection_latency_ms"] = round(
                (result.started_at_mono - last_verified_mono) * 1000, 2
            )

        # Recovery & Migration measurement fields
        if is_recovery:
            event["is_recovery"] = True
            if recovery_latency_ms is not None:
                event["recovery_latency_ms"] = recovery_latency_ms
            if estimated_total_recovery_latency_ms is not None:
                event["estimated_total_recovery_latency_ms"] = estimated_total_recovery_latency_ms

        if is_migration:
            event["is_migration"] = True
            if old_endpoint:
                event["old_endpoint"] = old_endpoint
            if new_endpoint:
                event["new_endpoint"] = new_endpoint
            if rediscovery_latency_ms is not None:
                event["rediscovery_latency_ms"] = rediscovery_latency_ms

        self.emit(event)

    def stale(self, record: ServiceRecord, old_state: str = "ACTIVE") -> None:
        """Emit an event for an mDNS-withdrawn service."""
        self.emit({
            "event": "state_change",
            "wall_clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": {
                "instance_name": record.instance_name,
                "host": record.host,
                "port": record.port,
                "addresses": record.addresses,
                "txt": record.txt,
            },
            "old_state": old_state,
            "new_state": ServiceState.STALE.value,
            "reason": "mDNS advertisement withdrawn (remove_service received)",
            "measurements": {},
        })

    def probe(self, result: VerificationResult) -> None:
        """Emit a routine probe event (no state change)."""
        record = result.record
        self.emit({
            "event": "probe",
            "wall_clock_utc": result.wall_clock_utc,
            "service": {"instance_name": record.instance_name, "port": record.port},
            "state": result.state.value,
            "reason": result.reason,
            "measurements": {
                "http_status": result.http_status,
                "health_latency_ms": result.health_latency_ms,
                "verification_latency_ms": result.verification_latency_ms,
            },
        })


# ---------------------------------------------------------------------------
# Per-service state machine
# ---------------------------------------------------------------------------

class _ServiceFSM:
    """Tracks state and migration lifecycle for one discovered service instance."""

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.UNKNOWN
        self.last_verified_mono: Optional[float] = None         # monotonic at last VERIFIED
        self.fault_detected_mono: Optional[float] = None        # monotonic when failure was first detected
        self.current_endpoint: Optional[tuple[str, int]] = None   # (host/ip, port)
        self.last_record: Optional[ServiceRecord] = None
        self.migration_in_progress: bool = False
        self.old_endpoint_info: Optional[dict] = None
        self.migration_discovered_mono: Optional[float] = None

    def update(self, result: VerificationResult, logger: EventLogger) -> None:
        record = result.record
        new_endpoint = (record.primary_ip or record.host, record.port)

        # 1. Detect endpoint migration
        if self.current_endpoint is not None and new_endpoint != self.current_endpoint:
            self.migration_in_progress = True
            self.migration_discovered_mono = record.discovered_at
            self.old_endpoint_info = {
                "host": self.last_record.host if self.last_record else "",
                "port": self.current_endpoint[1],
                "addresses": self.last_record.addresses if self.last_record else [],
            }
            new_endpoint_info = {
                "host": record.host,
                "port": record.port,
                "addresses": record.addresses,
            }

            rediscovery_latency_ms: Optional[float] = None
            estimated_rediscovery_latency_ms: Optional[float] = None
            if self.fault_detected_mono is not None:
                rediscovery_latency_ms = round(
                    max(0.0, (record.discovered_at - self.fault_detected_mono) * 1000), 2
                )
            if self.last_verified_mono is not None:
                estimated_rediscovery_latency_ms = round(
                    max(0.0, (record.discovered_at - self.last_verified_mono) * 1000), 2
                )

            logger.endpoint_migration(
                record=record,
                old_endpoint=self.old_endpoint_info,
                new_endpoint=new_endpoint_info,
                rediscovery_latency_ms=rediscovery_latency_ms,
                estimated_rediscovery_latency_ms=estimated_rediscovery_latency_ms,
            )
            self.current_endpoint = new_endpoint
        elif self.current_endpoint is None:
            self.current_endpoint = new_endpoint

        new_state = result.state

        # 2. Track failure onset timestamp
        if new_state in (ServiceState.UNREACHABLE, ServiceState.DEGRADED):
            if self.fault_detected_mono is None:
                self.fault_detected_mono = result.started_at_mono

        # 3. Compute recovery and migration measurements
        is_recovery = False
        recovery_latency_ms: Optional[float] = None
        estimated_total_recovery_latency_ms: Optional[float] = None
        rediscovery_latency_ms_final: Optional[float] = None

        if new_state == ServiceState.VERIFIED:
            if self.state in (ServiceState.UNREACHABLE, ServiceState.DEGRADED, ServiceState.STALE) or self.migration_in_progress:
                is_recovery = True
                if self.fault_detected_mono is not None:
                    recovery_latency_ms = round(
                        max(0.0, (result.finished_at_mono - self.fault_detected_mono) * 1000), 2
                    )
                if self.last_verified_mono is not None:
                    estimated_total_recovery_latency_ms = round(
                        max(0.0, (result.finished_at_mono - self.last_verified_mono) * 1000), 2
                    )
                if self.migration_discovered_mono is not None and self.fault_detected_mono is not None:
                    rediscovery_latency_ms_final = round(
                        max(0.0, (self.migration_discovered_mono - self.fault_detected_mono) * 1000), 2
                    )

        state_changed = (new_state != self.state)
        was_migrating = self.migration_in_progress
        saved_old_endpoint = self.old_endpoint_info

        if state_changed or was_migrating:
            logger.state_change(
                result=result,
                old_state=self.state,
                last_verified_mono=self.last_verified_mono,
                fault_detected_mono=self.fault_detected_mono,
                is_recovery=is_recovery,
                is_migration=was_migrating,
                old_endpoint=saved_old_endpoint,
                new_endpoint={
                    "host": record.host,
                    "port": record.port,
                    "addresses": record.addresses,
                } if was_migrating else None,
                recovery_latency_ms=recovery_latency_ms,
                estimated_total_recovery_latency_ms=estimated_total_recovery_latency_ms,
                rediscovery_latency_ms=rediscovery_latency_ms_final,
            )
            self.state = new_state
        else:
            logger.probe(result)

        if new_state == ServiceState.VERIFIED:
            self.last_verified_mono = result.finished_at_mono
            self.fault_detected_mono = None
            self.migration_in_progress = False
            self.old_endpoint_info = None
            self.migration_discovered_mono = None

        self.last_record = record


# ---------------------------------------------------------------------------
# Monitor (main entry point)
# ---------------------------------------------------------------------------

class Monitor:
    """
    Continuous verification monitor with automatic endpoint migration rediscovery.

    Discovers services via mDNS, verifies them on every poll tick, detects
    endpoint migration, drives state machines, and emits structured JSONL events.
    """

    def __init__(
        self,
        poll_interval: float = _DEFAULT_INTERVAL,
        http_timeout: float = _DEFAULT_TIMEOUT,
        log_path: Path = Path(_DEFAULT_LOG),
        discovery_timeout: float = 15.0,
    ) -> None:
        self._interval = poll_interval
        self._http_timeout = http_timeout
        self._logger = EventLogger(log_path)
        self._discovery_timeout = discovery_timeout
        self._registry = _ServiceRegistry()
        self._fsm_map: dict[str, _ServiceFSM] = {}   # instance_name -> FSM
        self._stop = threading.Event()
        self._wake_event = threading.Event()

    def run(self) -> None:
        """Start browsing and polling. Blocks until stop() is called or KeyboardInterrupt."""
        print(
            f"[MONITOR] Starting. poll_interval={self._interval}s  "
            f"http_timeout={self._http_timeout}s  "
            f"log={self._logger._path.resolve()}",
            flush=True,
        )

        zc = Zeroconf()
        listener = _MonitorListener(
            zc,
            self._registry,
            logger=self._logger,
            wake_event=self._wake_event,
        )
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)

        print(
            f"[MONITOR] Waiting up to {self._discovery_timeout}s for initial discovery ...",
            flush=True,
        )
        # Wait for discovery or stop
        self._wake_event.wait(timeout=self._discovery_timeout)
        self._wake_event.clear()

        try:
            self._poll_loop()
        except KeyboardInterrupt:
            print("\n[MONITOR] Interrupted by user.", flush=True)
        finally:
            browser.cancel()
            zc.close()
            print("[MONITOR] Stopped.", flush=True)

    def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()

    def _poll_loop(self) -> None:
        print("[MONITOR] Entering poll loop ...", flush=True)
        while not self._stop.is_set():
            self._tick()
            self._handle_stale()
            self._wake_event.wait(timeout=self._interval)
            self._wake_event.clear()

    def _tick(self) -> None:
        records = self._registry.all_records()
        if not records:
            print("[MONITOR] No services in registry yet.", flush=True)
            return

        for record in records:
            fsm = self._fsm_map.setdefault(record.instance_name, _ServiceFSM())
            try:
                result = verifier_mod.verify_once(record, self._http_timeout)
            except Exception as exc:
                # Guard against unexpected exceptions inside the verifier.
                print(
                    f"[MONITOR] Unexpected verifier error for {record.instance_name}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            fsm.update(result, self._logger)
            print(
                f"[MONITOR] [{result.state.value:>11}] {record.instance_name} "
                f"port={record.port} latency={result.health_latency_ms}ms "
                f"| {result.reason[:80]}",
                flush=True,
            )

    def _handle_stale(self) -> None:
        for stale in self._registry.pop_stale():
            # Retain FSM in _fsm_map so prior endpoint and fault timing survive re-advertisement
            fsm = self._fsm_map.get(stale.instance_name)
            old_state = fsm.state.value if fsm else "ACTIVE"
            self._logger.stale(stale, old_state=old_state)
            if fsm:
                fsm.state = ServiceState.STALE
                if fsm.fault_detected_mono is None:
                    fsm.fault_detected_mono = time.monotonic()

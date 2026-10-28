"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 2A: Continuous monitor + JSONL event logger

Architecture:
  - ServiceBrowser (zeroconf thread) populates a thread-safe registry of
    known ServiceRecords.
  - A polling loop (main thread) calls verifier.verify_once() at configurable
    intervals and drives the per-service state machine.
  - State transitions are emitted as structured JSONL events to a log file
    AND to stdout.

State transitions tracked:
  UNKNOWN  -> any state  (first contact)
  VERIFIED -> DEGRADED   (capability drift or health regression)
  VERIFIED -> UNREACHABLE (HTTP endpoint died, ad still live)
  any      -> STALE      (mDNS advertisement withdrawn)
  UNREACHABLE -> VERIFIED (recovery)
  DEGRADED    -> VERIFIED (recovery)

Failure-detection latency is measured from the last VERIFIED observation
to the first UNREACHABLE/DEGRADED observation using the monotonic clock.
Because the actual fault instant is unknown (it happened between polls),
we label it "estimated_detection_latency_ms" in the event.
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

    def add(self, record: ServiceRecord) -> None:
        with self._lock:
            self._stale.pop(record.instance_name, None)
            self._records[record.instance_name] = record

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

    def __init__(self, zc: Zeroconf, registry: _ServiceRegistry) -> None:
        self._zc = zc
        self._registry = registry

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        removed = self._registry.remove(name)
        if removed:
            print(f"[MONITOR] mDNS: advertisement withdrawn for {name}", flush=True)

    def _resolve(self, zc: Zeroconf, type_: str, name: str) -> None:
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
        self._registry.add(record)
        print(
            f"[MONITOR] mDNS: registered {name} "
            f"at {addresses[0] if addresses else '?'}:{info.port}",
            flush=True,
        )


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

    def state_change(
        self,
        result: VerificationResult,
        old_state: ServiceState,
        last_verified_mono: Optional[float],
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
            "old_state": old_state.value,
            "new_state": new_state.value,
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
            "service": {"instance_name": record.instance_name},
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
    """Tracks state for one discovered service instance."""

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.UNKNOWN
        self.last_verified_mono: Optional[float] = None   # monotonic at last VERIFIED

    def update(self, result: VerificationResult, logger: EventLogger) -> None:
        new_state = result.state
        if new_state == ServiceState.VERIFIED:
            self.last_verified_mono = result.finished_at_mono

        if new_state != self.state:
            logger.state_change(result, self.state, self.last_verified_mono)
            self.state = new_state
        else:
            # No state change; emit a routine probe event (verbose mode)
            logger.probe(result)


# ---------------------------------------------------------------------------
# Monitor (main entry point)
# ---------------------------------------------------------------------------

class Monitor:
    """
    Continuous verification monitor.

    Discovers services via mDNS, verifies them on every poll tick, drives
    state machines, and emits structured JSONL events.
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

    def run(self) -> None:
        """Start browsing and polling. Blocks until stop() is called or KeyboardInterrupt."""
        print(
            f"[MONITOR] Starting. poll_interval={self._interval}s  "
            f"http_timeout={self._http_timeout}s  "
            f"log={self._logger._path.resolve()}",
            flush=True,
        )

        zc = Zeroconf()
        listener = _MonitorListener(zc, self._registry)
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)

        print(
            f"[MONITOR] Waiting up to {self._discovery_timeout}s for initial discovery ...",
            flush=True,
        )
        time.sleep(self._discovery_timeout)

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

    def _poll_loop(self) -> None:
        print("[MONITOR] Entering poll loop ...", flush=True)
        while not self._stop.is_set():
            self._tick()
            self._handle_stale()
            self._stop.wait(timeout=self._interval)

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
            fsm = self._fsm_map.pop(stale.instance_name, None)
            old_state = fsm.state.value if fsm else "ACTIVE"
            self._logger.stale(stale, old_state=old_state)
            if fsm:
                fsm.state = ServiceState.STALE

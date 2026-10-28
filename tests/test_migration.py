"""
NIRIKSHA Phase 2B - Endpoint Migration & Measured Recovery Tests.

Tests:
  1. FSM detection of advertised SRV port migration.
  2. The complete fault-to-migration-recovery lifecycle (Scenario 3):
     VERIFIED (old endpoint) -> UNREACHABLE (old API stops)
     -> rediscovery of replacement endpoint -> VERIFIED (new endpoint).
  3. STALE -> replacement endpoint -> VERIFIED.
  4. Migrated endpoint with capability mismatch -> DEGRADED.
  5. Migrated endpoint unreachable -> UNREACHABLE.
  6. Real local TCP server migration E2E without mocks.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import uvicorn

from niriksha.models import ServiceRecord, ServiceState, VerificationResult
from niriksha.monitor import EventLogger, Monitor, _ServiceFSM, _ServiceRegistry
from niriksha import verifier as verifier_mod
from niriksha.service import app as diagnostic_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    port: int = 8001,
    addresses: list[str] | None = None,
    diag: str = "dns",
    discovered_at: float | None = None,
) -> ServiceRecord:
    rec = ServiceRecord(
        instance_name="niriksha-diagnostic._niriksha._tcp.local.",
        service_type="_niriksha._tcp.local.",
        host="LAPTOP-TEST.local.",
        port=port,
        addresses=addresses if addresses is not None else ["127.0.0.1"],
        txt={
            "diag": diag,
            "txtvers": "1",
            "health": "/health",
            "run": "/diagnose",
        },
    )
    if discovered_at is not None:
        rec.discovered_at = discovered_at
    return rec


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_server(port: int):
    config = uvicorn.Config(
        diagnostic_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError(f"Server did not start on port {port} within 5s")

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Unit Tests: FSM Migration Logic
# ---------------------------------------------------------------------------

class TestEndpointMigrationFSM:
    """MOCKED TESTS: verify FSM state transitions, migration detection, and timing calculations."""

    def test_fsm_detects_port_migration_event(self, tmp_path):
        """FSM must detect change from port 8001 to 8002 and emit endpoint_migration event."""
        log_file = tmp_path / "events.jsonl"
        logger = EventLogger(log_file)
        fsm = _ServiceFSM()

        rec1 = _make_record(port=8001)
        res1 = VerificationResult(
            record=rec1,
            state=ServiceState.VERIFIED,
            started_at_mono=100.0,
            finished_at_mono=100.1,
            wall_clock_utc="2026-10-28T07:00:00Z",
            http_status=200,
            health_latency_ms=10.0,
            reason="All good on 8001",
        )
        fsm.update(res1, logger)
        assert fsm.state == ServiceState.VERIFIED
        assert fsm.current_endpoint == ("127.0.0.1", 8001)

        # Now advertise on port 8002
        rec2 = _make_record(port=8002, discovered_at=105.0)
        res2 = VerificationResult(
            record=rec2,
            state=ServiceState.VERIFIED,
            started_at_mono=106.0,
            finished_at_mono=106.1,
            wall_clock_utc="2026-10-28T07:00:06Z",
            http_status=200,
            health_latency_ms=12.0,
            reason="All good on 8002",
        )
        fsm.update(res2, logger)
        assert fsm.state == ServiceState.VERIFIED
        assert fsm.current_endpoint == ("127.0.0.1", 8002)

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        migration_events = [e for e in lines if e.get("event") == "endpoint_migration"]
        assert len(migration_events) == 1
        assert migration_events[0]["old_endpoint"]["port"] == 8001
        assert migration_events[0]["new_endpoint"]["port"] == 8002
        assert "migrated from port 8001 to port 8002" in migration_events[0]["reason"]

    def test_unreachable_to_migrated_verified_lifecycle(self, tmp_path):
        """
        Scenario 3:
          1. VERIFIED at 8001.
          2. API on 8001 dies -> UNREACHABLE (advertisement still on 8001).
          3. Replacement endpoint discovered on 8002.
          4. VERIFIED at 8002 with complete recovery & rediscovery latency metrics.
        """
        log_file = tmp_path / "events.jsonl"
        logger = EventLogger(log_file)
        fsm = _ServiceFSM()

        # Step 1: VERIFIED on 8001 at mono 100.0
        rec_old = _make_record(port=8001)
        res_ok = VerificationResult(
            record=rec_old,
            state=ServiceState.VERIFIED,
            started_at_mono=99.9,
            finished_at_mono=100.0,
            wall_clock_utc="2026-10-28T07:00:00Z",
            http_status=200,
            health_latency_ms=15.0,
            reason="Healthy on 8001",
        )
        fsm.update(res_ok, logger)
        assert fsm.state == ServiceState.VERIFIED
        assert fsm.last_verified_mono == 100.0

        # Step 2: 8001 dies -> UNREACHABLE at mono 110.0
        res_fail = VerificationResult(
            record=rec_old,
            state=ServiceState.UNREACHABLE,
            started_at_mono=110.0,
            finished_at_mono=110.1,
            wall_clock_utc="2026-10-28T07:00:10Z",
            reason="Connection refused on 8001",
        )
        fsm.update(res_fail, logger)
        assert fsm.state == ServiceState.UNREACHABLE
        assert fsm.fault_detected_mono == 110.0

        # Step 3: Replacement discovered on 8002 at mono 115.0 (rediscovery latency = 5000ms)
        rec_new = _make_record(port=8002, discovered_at=115.0)
        res_recovered = VerificationResult(
            record=rec_new,
            state=ServiceState.VERIFIED,
            started_at_mono=117.8,
            finished_at_mono=118.0,
            wall_clock_utc="2026-10-28T07:00:18Z",
            http_status=200,
            health_latency_ms=14.0,
            reason="Healthy on 8002",
        )
        fsm.update(res_recovered, logger)
        assert fsm.state == ServiceState.VERIFIED
        assert fsm.current_endpoint == ("127.0.0.1", 8002)

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        state_changes = [e for e in lines if e.get("event") == "state_change"]
        recovery_events = [e for e in state_changes if e.get("is_recovery") is True]

        assert len(recovery_events) == 1
        rec_ev = recovery_events[0]
        assert rec_ev["old_state"] == "UNREACHABLE"
        assert rec_ev["new_state"] == "VERIFIED"
        assert rec_ev["is_migration"] is True
        assert rec_ev["old_endpoint"]["port"] == 8001
        assert rec_ev["new_endpoint"]["port"] == 8002
        # rediscovery_latency_ms = 115.0 - 110.0 = 5.0s = 5000.0 ms
        assert rec_ev["rediscovery_latency_ms"] == 5000.0
        # recovery_latency_ms = 118.0 - 110.0 = 8.0s = 8000.0 ms
        assert rec_ev["recovery_latency_ms"] == 8000.0
        # estimated_total_recovery_latency_ms = 118.0 - 100.0 = 18.0s = 18000.0 ms
        assert rec_ev["estimated_total_recovery_latency_ms"] == 18000.0

    def test_stale_to_migrated_verified_lifecycle(self, tmp_path):
        """Service becomes STALE on port 8001, then re-appears on port 8002 and recovers."""
        log_file = tmp_path / "events.jsonl"
        monitor = Monitor(log_path=log_file)

        rec1 = _make_record(port=8001)
        monitor._registry.add(rec1)
        res1 = VerificationResult(
            record=rec1,
            state=ServiceState.VERIFIED,
            started_at_mono=100.0,
            finished_at_mono=100.2,
            wall_clock_utc="2026-10-28T07:00:00Z",
            http_status=200,
            health_latency_ms=10.0,
            reason="OK on 8001",
        )
        fsm = monitor._fsm_map.setdefault(rec1.instance_name, _ServiceFSM())
        fsm.update(res1, monitor._logger)

        # Service withdrawn from mDNS
        monitor._registry.remove(rec1.instance_name)
        monitor._handle_stale()
        assert fsm.state == ServiceState.STALE
        assert fsm.fault_detected_mono is not None

        # Re-announced on port 8002
        rec2 = _make_record(port=8002, discovered_at=fsm.fault_detected_mono + 2.0)
        monitor._registry.add(rec2)
        res2 = VerificationResult(
            record=rec2,
            state=ServiceState.VERIFIED,
            started_at_mono=fsm.fault_detected_mono + 3.0,
            finished_at_mono=fsm.fault_detected_mono + 3.2,
            wall_clock_utc="2026-10-28T07:00:05Z",
            http_status=200,
            health_latency_ms=11.0,
            reason="OK on 8002",
        )
        fsm.update(res2, monitor._logger)
        assert fsm.state == ServiceState.VERIFIED

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        recovery_events = [e for e in lines if e.get("event") == "state_change" and e.get("is_recovery") is True]
        assert len(recovery_events) == 1
        assert recovery_events[0]["old_state"] == "STALE"
        assert recovery_events[0]["new_state"] == "VERIFIED"
        assert recovery_events[0]["is_migration"] is True
        assert recovery_events[0]["old_endpoint"]["port"] == 8001
        assert recovery_events[0]["new_endpoint"]["port"] == 8002

    def test_migrated_endpoint_degraded_capability(self, tmp_path):
        """Service migrates to port 8002, but capability cross-check fails -> DEGRADED."""
        log_file = tmp_path / "events.jsonl"
        logger = EventLogger(log_file)
        fsm = _ServiceFSM()

        rec1 = _make_record(port=8001)
        res1 = VerificationResult(
            record=rec1,
            state=ServiceState.VERIFIED,
            started_at_mono=100.0,
            finished_at_mono=100.1,
            wall_clock_utc="2026-10-28T07:00:00Z",
            http_status=200,
            health_latency_ms=10.0,
            reason="OK",
        )
        fsm.update(res1, logger)

        # Migrated to 8002, but returns mismatch
        rec2 = _make_record(port=8002, diag="http")
        res2 = VerificationResult(
            record=rec2,
            state=ServiceState.DEGRADED,
            started_at_mono=105.0,
            finished_at_mono=105.1,
            wall_clock_utc="2026-10-28T07:00:05Z",
            http_status=200,
            health_latency_ms=10.0,
            diag_type_ok=False,
            reason="Capability mismatch on 8002",
        )
        fsm.update(res2, logger)
        assert fsm.state == ServiceState.DEGRADED

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        mig_events = [e for e in lines if e.get("event") == "endpoint_migration"]
        assert len(mig_events) == 1
        state_changes = [e for e in lines if e.get("event") == "state_change"]
        assert state_changes[-1]["new_state"] == "DEGRADED"

    def test_discovery_event_logging(self, tmp_path):
        """EventLogger must record structured discovery events."""
        log_file = tmp_path / "events.jsonl"
        logger = EventLogger(log_file)
        rec = _make_record(port=8001)

        logger.discovery(rec, action="add")
        logger.discovery(rec, action="update")

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        assert len(lines) == 2
        assert lines[0]["event"] == "discovery"
        assert lines[0]["action"] == "add"
        assert lines[0]["service"]["port"] == 8001
        assert lines[1]["action"] == "update"


# ---------------------------------------------------------------------------
# Real Server E2E Migration Test (Real TCP sockets, no mock)
# ---------------------------------------------------------------------------

class TestE2EMigration:
    """REAL SERVER TESTS: starts two genuine FastAPI uvicorn servers on separate ports."""

    def test_real_server_migration_and_recovery(self, tmp_path):
        """
        REAL E2E:
          1. Start genuine FastAPI server on port A.
          2. Start genuine FastAPI server on port B.
          3. Verify against port A (VERIFIED).
          4. Stop server A -> port A closed (UNREACHABLE).
          5. Update ServiceRecord to port B -> verify against port B (VERIFIED).
          6. Assert full recovery and migration logging.
        """
        log_file = tmp_path / "migration_events.jsonl"
        logger = EventLogger(log_file)
        fsm = _ServiceFSM()

        port_a = _find_free_port()
        port_b = _find_free_port()
        assert port_a != port_b

        with _running_server(port_b):
            # Server A scope
            with _running_server(port_a):
                rec_a = _make_record(port=port_a, diag="dns")
                res_a = verifier_mod.verify_once(rec_a, http_timeout=5.0)
                assert res_a.state == ServiceState.VERIFIED
                fsm.update(res_a, logger)
                assert fsm.state == ServiceState.VERIFIED

            # Server A is now stopped
            time.sleep(0.2)
            res_fail = verifier_mod.verify_once(rec_a, http_timeout=2.0)
            assert res_fail.state == ServiceState.UNREACHABLE
            fsm.update(res_fail, logger)
            assert fsm.state == ServiceState.UNREACHABLE

            # Migrated record pointing to Server B
            rec_b = _make_record(port=port_b, diag="dns")
            res_b = verifier_mod.verify_once(rec_b, http_timeout=5.0)
            assert res_b.state == ServiceState.VERIFIED
            fsm.update(res_b, logger)
            assert fsm.state == ServiceState.VERIFIED

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        recovery_events = [e for e in lines if e.get("event") == "state_change" and e.get("is_recovery") is True]
        assert len(recovery_events) == 1
        rec_ev = recovery_events[0]
        assert rec_ev["old_state"] == "UNREACHABLE"
        assert rec_ev["new_state"] == "VERIFIED"
        assert rec_ev["is_migration"] is True
        assert rec_ev["old_endpoint"]["port"] == port_a
        assert rec_ev["new_endpoint"]["port"] == port_b
        assert rec_ev["recovery_latency_ms"] is not None
        assert rec_ev["recovery_latency_ms"] > 0

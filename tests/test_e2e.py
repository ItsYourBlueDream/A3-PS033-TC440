"""
NIRIKSHA Phase 2A - End-to-end fault demonstration tests.

NATURE OF THESE TESTS:
  These tests use a REAL local HTTP server (uvicorn/FastAPI) and REAL httpx
  HTTP calls. They do NOT use mDNS/DNS-SD (which requires multicast and
  cannot be reliably automated in a test runner without root/admin and stable
  multicast routes). Instead, they construct ServiceRecord objects with the
  real loopback address (127.0.0.1) and port of a locally started server.

  This is clearly distinguished from the Phase 1 acceptance test (which used
  REAL mDNS discovery). These tests validate the verifier's behaviour against
  a real TCP endpoint.

SCENARIOS TESTED:
  A. Real service running -> state VERIFIED.
  B. Service stopped (port closed) -> state UNREACHABLE.
  C. Wrong port in ServiceRecord (nothing listening) -> state UNREACHABLE.
  D. Real service, but TXT diag='http' while /diagnose returns 'dns_lookup'
     -> state DEGRADED.

Each test starts its own server thread (NOT a subprocess) to keep the test
suite self-contained and CI-friendly on Windows.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn

from niriksha.models import ServiceRecord, ServiceState
from niriksha import verifier as verifier_mod
from niriksha.service import app as diagnostic_app


# ---------------------------------------------------------------------------
# Real local server fixture
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Find a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_server(port: int):
    """
    Context manager: start a real uvicorn server on 127.0.0.1:{port} in a
    daemon thread and stop it on exit.
    """
    config = uvicorn.Config(
        diagnostic_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",   # suppress uvicorn access logs in test output
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until uvicorn is actually listening (up to 5 seconds)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError(f"Server did not start on port {port} within 5 seconds")

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def _make_record(port: int, diag: str = "dns", addresses: list[str] | None = None) -> ServiceRecord:
    return ServiceRecord(
        instance_name="niriksha-diagnostic._niriksha._tcp.local.",
        service_type="_niriksha._tcp.local.",
        host="localhost.local.",
        port=port,
        addresses=addresses if addresses is not None else ["127.0.0.1"],
        txt={
            "diag":    diag,
            "txtvers": "1",
            "health":  "/health",
            "run":     "/diagnose",
        },
    )


# ---------------------------------------------------------------------------
# Scenario A: Real service -> VERIFIED
# ---------------------------------------------------------------------------

class TestE2EVerified:
    """
    REAL END-TO-END: Uses a real uvicorn server on loopback.
    No mDNS. ServiceRecord is constructed manually with 127.0.0.1.
    """

    def test_real_service_is_verified(self):
        """
        REAL E2E: Start the real diagnostic service, verify it.
        Expected: VERIFIED (all checks pass including /diagnose dns_lookup).
        """
        port = _find_free_port()
        with _running_server(port):
            record = _make_record(port=port, diag="dns_lookup")
            result = verifier_mod.verify_once(record, http_timeout=8.0)

        assert result.state == ServiceState.VERIFIED, (
            f"Expected VERIFIED but got {result.state}: {result.reason}"
        )
        assert result.http_status == 200
        assert result.health_status_ok is True
        assert result.txt_fields_ok is True
        assert result.diag_type_ok is True
        assert result.health_latency_ms is not None
        assert result.health_latency_ms > 0

    def test_real_service_health_body_fields(self):
        """REAL E2E: /health body must contain 'status', 'hostname', 'uptime_seconds'."""
        port = _find_free_port()
        with _running_server(port):
            record = _make_record(port=port, diag="dns_lookup")
            result = verifier_mod.verify_once(record, http_timeout=8.0)

        assert result.health_body is not None
        assert "status" in result.health_body
        assert "hostname" in result.health_body
        assert "uptime_seconds" in result.health_body

    def test_real_service_with_phase1_diag_dns_is_verified(self):
        """REAL E2E: Real server with advertised diag='dns' (Phase 1 default) is VERIFIED."""
        port = _find_free_port()
        with _running_server(port):
            record = _make_record(port=port, diag="dns")
            result = verifier_mod.verify_once(record, http_timeout=8.0)

        assert result.state == ServiceState.VERIFIED
        assert result.diag_type_ok is True


# ---------------------------------------------------------------------------
# Scenario B: Service stopped -> UNREACHABLE
# ---------------------------------------------------------------------------

class TestE2EUnreachableServiceDown:
    """REAL END-TO-END: Server is started then stopped; verifier runs after stop."""

    def test_stopped_service_is_unreachable(self):
        """
        REAL E2E: Start server, record the port, stop server, then verify.
        Expected: UNREACHABLE (connection refused on real TCP socket).
        """
        port = _find_free_port()
        with _running_server(port):
            pass  # server stops on context exit

        # Give OS a moment to release the port
        time.sleep(0.2)

        record = _make_record(port=port)
        result = verifier_mod.verify_once(record, http_timeout=3.0)

        assert result.state == ServiceState.UNREACHABLE, (
            f"Expected UNREACHABLE but got {result.state}: {result.reason}"
        )


# ---------------------------------------------------------------------------
# Scenario C: Wrong advertised SRV port -> UNREACHABLE
# ---------------------------------------------------------------------------

class TestE2EWrongPort:
    """REAL END-TO-END: ServiceRecord advertises a port with nothing listening."""

    def test_wrong_port_is_unreachable(self):
        """
        REAL E2E: Real server on port X, but ServiceRecord has port Y (nothing there).
        Expected: UNREACHABLE.
        """
        real_port = _find_free_port()
        wrong_port = _find_free_port()
        assert real_port != wrong_port

        with _running_server(real_port):
            # Deliberately use wrong_port in the record
            record = _make_record(port=wrong_port)
            result = verifier_mod.verify_once(record, http_timeout=3.0)

        assert result.state == ServiceState.UNREACHABLE, (
            f"Expected UNREACHABLE but got {result.state}: {result.reason}"
        )


# ---------------------------------------------------------------------------
# Scenario D: TXT diag mismatch -> DEGRADED
# ---------------------------------------------------------------------------

class TestE2EDegradedDiagMismatch:
    """REAL END-TO-END: Real server running, but TXT diag is wrong."""

    def test_wrong_diag_txt_is_degraded(self):
        """
        REAL E2E: Server advertises diag='http' in TXT but /diagnose returns
        diagnostic='dns_lookup'.  Verifier must detect the mismatch.
        Expected: DEGRADED with diag_type_ok=False.
        """
        port = _find_free_port()
        with _running_server(port):
            # Inject wrong TXT diag — service is healthy but capability claim is false
            record = _make_record(port=port, diag="http")
            result = verifier_mod.verify_once(record, http_timeout=8.0)

        assert result.state == ServiceState.DEGRADED, (
            f"Expected DEGRADED but got {result.state}: {result.reason}"
        )
        assert result.diag_type_ok is False
        assert result.http_status == 200    # HTTP is fine
        assert result.health_status_ok is True  # health body is fine too

    def test_real_service_unverified_diagnose_route_is_degraded(self):
        """
        REAL E2E: Real server running and /health is healthy, but TXT run points
        to /nonexistent route (HTTP 404). Verifier must classify as DEGRADED,
        not assume capability is verified.
        """
        port = _find_free_port()
        with _running_server(port):
            record = ServiceRecord(
                instance_name="niriksha-diagnostic._niriksha._tcp.local.",
                service_type="_niriksha._tcp.local.",
                host="localhost.local.",
                port=port,
                addresses=["127.0.0.1"],
                txt={
                    "diag": "dns",
                    "txtvers": "1",
                    "health": "/health",
                    "run": "/nonexistent_diag_path",
                },
            )
            result = verifier_mod.verify_once(record, http_timeout=8.0)

        assert result.state == ServiceState.DEGRADED
        assert result.diag_type_ok is False
        assert result.http_status == 200
        assert "404" in result.reason

"""
NIRIKSHA Phase 2A - Unit tests for state classification and verifier logic.

These tests use unittest.mock to simulate HTTP responses. They are MOCKED tests:
no real network traffic is generated, no real DNS-SD discovery is performed.
Each test is clearly labelled as a mock test in its docstring.

Real end-to-end fault tests are in test_e2e.py.
"""

from __future__ import annotations

import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from niriksha.models import ServiceRecord, ServiceState
from niriksha import verifier as verifier_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    port: int = 8001,
    addresses: list[str] | None = None,
    diag: str = "dns_lookup",
    health_path: str = "/health",
    run_path: str = "/diagnose",
    extra_txt: dict | None = None,
) -> ServiceRecord:
    """Build a minimal ServiceRecord for testing."""
    txt: dict[str, str] = {
        "diag":    diag,
        "txtvers": "1",
        "health":  health_path,
        "run":     run_path,
    }
    if extra_txt:
        txt.update(extra_txt)
    return ServiceRecord(
        instance_name="niriksha-diagnostic._niriksha._tcp.local.",
        service_type="_niriksha._tcp.local.",
        host="TESTHOST.local.",
        port=port,
        addresses=addresses if addresses is not None else ["127.0.0.1"],
        txt=txt,
    )


def _make_http_response(status: int, json_body: dict | None) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = Exception("not JSON")
    return resp


# ---------------------------------------------------------------------------
# Scenario A: Healthy service -> VERIFIED
# ---------------------------------------------------------------------------

class TestVerifiedState:
    """MOCK TESTS: all HTTP calls are mocked. No real network used."""

    def test_healthy_service_is_verified(self):
        """
        MOCK: /health returns 200 + healthy JSON, /diagnose returns dns_lookup.
        Expected state: VERIFIED.
        """
        record = _make_record(diag="dns_lookup")
        health_body = {"status": "healthy", "service": "niriksha-diagnostic"}
        diagnose_body = {"diagnostic": "dns_lookup", "success": True}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, diagnose_body),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.VERIFIED
        assert result.http_status == 200
        assert result.health_status_ok is True
        assert result.txt_fields_ok is True
        assert result.diag_type_ok is True

    def test_verified_has_positive_latency(self):
        """MOCK: Verification result must report a non-negative health latency."""
        record = _make_record(diag="dns_lookup")
        health_body = {"status": "healthy"}
        diagnose_body = {"diagnostic": "dns_lookup"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, diagnose_body),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.health_latency_ms is not None
        assert result.health_latency_ms >= 0
        assert result.verification_latency_ms >= 0

    def test_healthy_service_with_phase1_diag_dns(self):
        """
        MOCK: Advertised TXT diag='dns' (Phase 1 default) matches /diagnose
        reporting diagnostic='dns_lookup'. Expected state: VERIFIED.
        """
        record = _make_record(diag="dns")
        health_body = {"status": "healthy", "service": "niriksha-diagnostic"}
        diagnose_body = {"diagnostic": "dns_lookup", "success": True}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, diagnose_body),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.VERIFIED
        assert result.diag_type_ok is True


# ---------------------------------------------------------------------------
# Scenario B: HTTP API stops, advertiser still live -> UNREACHABLE
# ---------------------------------------------------------------------------

class TestUnreachableState:
    """MOCK TESTS: simulates connection-refused and timeout conditions."""

    def test_connect_error_is_unreachable(self):
        """
        MOCK: /health raises httpx.ConnectError (connection refused).
        Expected state: UNREACHABLE.
        """
        import httpx

        record = _make_record()
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.ConnectError(
                "[WinError 10061] No connection could be made"
            )
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.UNREACHABLE
        assert "ConnectError" in result.reason or "HTTP probe failed" in result.reason

    def test_timeout_is_unreachable(self):
        """
        MOCK: /health raises httpx.ConnectTimeout.
        Expected state: UNREACHABLE.
        """
        import httpx

        record = _make_record()
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.ConnectTimeout("timed out")
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.UNREACHABLE

    def test_no_ip_address_is_unreachable(self):
        """
        MOCK: ServiceRecord with empty address list -> UNREACHABLE immediately.
        """
        record = _make_record(addresses=[])
        result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.UNREACHABLE
        assert "No IP address" in result.reason


# ---------------------------------------------------------------------------
# Scenario C: Wrong advertised SRV port -> UNREACHABLE
# ---------------------------------------------------------------------------

class TestWrongPortIsUnreachable:
    """MOCK TESTS: wrong port causes ConnectError."""

    def test_wrong_port_is_unreachable(self):
        """
        MOCK: Advertiser announces port 9999 but nothing listens there.
        Expected state: UNREACHABLE (same as scenario B at the verifier level;
        the injected port manifests as a ConnectError).
        """
        import httpx

        record = _make_record(port=9999)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.ConnectError("Connection refused on port 9999")
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.UNREACHABLE


# ---------------------------------------------------------------------------
# Scenario D: Advertised TXT diag != /diagnose diagnostic -> DEGRADED
# ---------------------------------------------------------------------------

class TestDegradedState:
    """MOCK TESTS: capability mismatches and health regressions."""

    def test_diag_mismatch_is_degraded(self):
        """
        MOCK: TXT advertises diag='http' but /diagnose returns diagnostic='dns_lookup'.
        Expected state: DEGRADED with diag_type_ok=False.
        """
        record = _make_record(diag="http")   # wrong advertised capability
        health_body = {"status": "healthy"}
        diagnose_body = {"diagnostic": "dns_lookup"}   # real observed behaviour

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, diagnose_body),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.diag_type_ok is False
        assert "mismatch" in result.reason.lower() or "disagrees" in result.reason.lower() \
            or "http" in result.reason.lower()

    def test_unhealthy_status_field_is_degraded(self):
        """
        MOCK: /health returns 200 but body status='degraded'.
        Expected state: DEGRADED.
        """
        record = _make_record()
        health_body = {"status": "degraded", "service": "niriksha-diagnostic"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = _make_http_response(200, health_body)
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.health_status_ok is False

    def test_non_200_status_is_degraded(self):
        """
        MOCK: /health returns HTTP 503. Endpoint is reachable but not healthy.
        Expected state: DEGRADED (not UNREACHABLE; TCP connected successfully).
        """
        record = _make_record()
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = _make_http_response(503, None)
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.http_status == 503

    def test_non_json_health_body_is_degraded(self):
        """
        MOCK: /health returns 200 but body is not valid JSON.
        Expected state: DEGRADED.
        """
        record = _make_record()
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = _make_http_response(200, None)
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert "not valid JSON" in result.reason or "JSON" in result.reason

    def test_missing_txt_key_is_degraded(self):
        """
        MOCK: TXT record is missing required key 'run'.
        Expected state: DEGRADED.
        """
        record = _make_record()
        del record.txt["run"]   # remove required key

        health_body = {"status": "healthy"}
        diagnose_body = {"diagnostic": "dns_lookup"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, diagnose_body),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.txt_fields_ok is False
        assert "run" in result.reason

    def test_diagnose_probe_error_is_degraded(self):
        """
        MOCK: /health succeeds, but /diagnose fails (ConnectError or timeout).
        Expected state: DEGRADED. Never assume unverified capability is correct.
        """
        import httpx
        record = _make_record()
        health_body = {"status": "healthy"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                httpx.ConnectError("Connection refused on /diagnose"),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.diag_type_ok is False
        assert "/diagnose probe failed" in result.reason

    def test_diagnose_non_200_is_degraded(self):
        """
        MOCK: /health is 200/healthy, but /diagnose returns HTTP 500.
        Expected state: DEGRADED.
        """
        record = _make_record()
        health_body = {"status": "healthy"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(500, {"error": "internal error"}),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.diag_type_ok is False
        assert "HTTP 500" in result.reason

    def test_diagnose_non_json_is_degraded(self):
        """
        MOCK: /health is 200/healthy, but /diagnose returns non-JSON.
        Expected state: DEGRADED.
        """
        record = _make_record()
        health_body = {"status": "healthy"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                _make_http_response(200, health_body),
                _make_http_response(200, None),
            ]
            result = verifier_mod.verify_once(record, http_timeout=5.0)

        assert result.state == ServiceState.DEGRADED
        assert result.diag_type_ok is False


# ---------------------------------------------------------------------------
# ServiceState enum sanity
# ---------------------------------------------------------------------------

class TestServiceStateEnum:
    def test_all_states_defined(self):
        states = {s.value for s in ServiceState}
        assert states == {"VERIFIED", "DEGRADED", "UNREACHABLE", "STALE", "UNKNOWN"}

    def test_stale_not_unreachable(self):
        """STALE and UNREACHABLE must be distinct states."""
        assert ServiceState.STALE != ServiceState.UNREACHABLE

    def test_state_is_string_comparable(self):
        """States must be comparable to plain strings (str Enum)."""
        assert ServiceState.VERIFIED == "VERIFIED"
        assert ServiceState.DEGRADED == "DEGRADED"


# ---------------------------------------------------------------------------
# ServiceRecord helpers
# ---------------------------------------------------------------------------

class TestServiceRecord:
    def test_primary_ip_returns_first(self):
        r = _make_record(addresses=["10.0.0.1", "10.0.0.2"])
        assert r.primary_ip == "10.0.0.1"

    def test_primary_ip_none_when_empty(self):
        r = _make_record(addresses=[])
        assert r.primary_ip is None

    def test_health_path_from_txt(self):
        r = _make_record(health_path="/custom/health")
        assert r.health_path == "/custom/health"

    def test_health_path_default(self):
        r = _make_record()
        del r.txt["health"]
        assert r.health_path == "/health"

    def test_advertised_diag(self):
        r = _make_record(diag="dns")
        assert r.advertised_diag == "dns"


# ---------------------------------------------------------------------------
# Monitor FSM and Timing
# ---------------------------------------------------------------------------

class TestMonitorFSMAndTiming:
    """MOCK TESTS: verifies FSM state transitions and failure-detection latency calculation."""

    def test_detection_latency_recorded_on_failure(self, tmp_path):
        import json
        from niriksha.monitor import EventLogger, _ServiceFSM

        log_file = tmp_path / "events.jsonl"
        logger = EventLogger(log_file)
        fsm = _ServiceFSM()

        record = _make_record()

        # Step 1: VERIFIED
        res_ok = verifier_mod.VerificationResult(
            record=record,
            state=ServiceState.VERIFIED,
            started_at_mono=100.0,
            finished_at_mono=100.2,
            wall_clock_utc="2026-10-28T06:00:00Z",
            http_status=200,
            health_latency_ms=10.0,
            reason="All good",
        )
        fsm.update(res_ok, logger)
        assert fsm.state == ServiceState.VERIFIED
        assert fsm.last_verified_mono == 100.2

        # Step 2: UNREACHABLE (detected at mono 110.0 -> detection latency = 110.0 - 100.2 = 9800ms)
        res_fail = verifier_mod.VerificationResult(
            record=record,
            state=ServiceState.UNREACHABLE,
            started_at_mono=110.0,
            finished_at_mono=110.1,
            wall_clock_utc="2026-10-28T06:00:10Z",
            reason="Connection refused",
        )
        fsm.update(res_fail, logger)
        assert fsm.state == ServiceState.UNREACHABLE

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        assert len(lines) == 2
        assert lines[0]["old_state"] == "UNKNOWN"
        assert lines[0]["new_state"] == "VERIFIED"
        assert lines[1]["old_state"] == "VERIFIED"
        assert lines[1]["new_state"] == "UNREACHABLE"
        assert lines[1]["estimated_detection_latency_ms"] == 9800.0

    def test_stale_event_records_prior_state(self, tmp_path):
        import json
        from niriksha.monitor import EventLogger, Monitor, _ServiceFSM

        log_file = tmp_path / "events.jsonl"
        monitor = Monitor(log_path=log_file)
        record = _make_record()

        monitor._registry.add(record)
        # Verify it once so FSM has state
        res_ok = verifier_mod.VerificationResult(
            record=record,
            state=ServiceState.VERIFIED,
            started_at_mono=100.0,
            finished_at_mono=100.2,
            wall_clock_utc="2026-10-28T06:00:00Z",
            http_status=200,
            health_latency_ms=10.0,
            reason="All good",
        )
        fsm = monitor._fsm_map.setdefault(record.instance_name, _ServiceFSM())
        fsm.update(res_ok, monitor._logger)

        # Now simulate mDNS withdrawal
        monitor._registry.remove(record.instance_name)
        monitor._handle_stale()

        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        stale_events = [e for e in lines if e.get("new_state") == "STALE"]
        assert len(stale_events) == 1
        assert stale_events[0]["old_state"] == "VERIFIED"
        assert stale_events[0]["new_state"] == "STALE"

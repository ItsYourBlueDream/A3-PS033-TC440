"""
NIRIKSHA - Automated Tests for Phase 3A Dashboard Data & Parser Engine

Validates:
- JSONL parsing robustness (missing file, empty file, corrupt/truncated trailing lines)
- Latest-state derivation across service lifecycles (VERIFIED, DEGRADED, UNREACHABLE, STALE)
- Endpoint migration tracking (port 8001 -> 8002, rediscovery, recovery)
- Measured vs estimated latency extraction
- Event timeline filtering (by type, state, text search)
- Real workspace niriksha_events.jsonl compatibility
"""

import json
from pathlib import Path

import pytest

from datetime import datetime, timezone

from niriksha.dashboard_data import (
    compute_log_freshness,
    compute_summary_counts,
    derive_services_state,
    extract_latency_timeseries,
    extract_migrations,
    extract_performance_metrics,
    filter_events,
    read_jsonl_events,
)


# ---------------------------------------------------------------------------
# 1. JSONL Parser Robustness Tests
# ---------------------------------------------------------------------------

def test_read_jsonl_nonexistent_file(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.jsonl"
    events = read_jsonl_events(non_existent)
    assert events == []


def test_read_jsonl_empty_file(tmp_path: Path):
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("", encoding="utf-8")
    events = read_jsonl_events(empty_file)
    assert events == []


def test_read_jsonl_with_blank_lines_and_truncated_last_line(tmp_path: Path):
    sample = tmp_path / "sample.jsonl"
    content = (
        '{"event": "discovery", "action": "add", "service": {"instance_name": "svc1"}}\n'
        '\n'  # blank line
        '   \n'  # whitespace line
        '{"event": "probe", "service": {"instance_name": "svc1"}, "state": "VERIFIED"}\n'
        '{"corrupt": "half_written_json_due_to_sudden_crash\n'  # malformed line
    )
    sample.write_text(content, encoding="utf-8")

    events = read_jsonl_events(sample)
    assert len(events) == 2
    assert events[0]["event"] == "discovery"
    assert events[0]["_line_number"] == 1
    assert events[1]["event"] == "probe"
    assert events[1]["_line_number"] == 4


# ---------------------------------------------------------------------------
# 2. Latest State Derivation Tests
# ---------------------------------------------------------------------------

def test_derive_services_state_complete_lifecycle():
    events = [
        # 1. Discovery
        {
            "event": "discovery",
            "action": "add",
            "wall_clock_utc": "2026-10-28T07:00:00Z",
            "service": {
                "instance_name": "svc-test._niriksha._tcp.local.",
                "host": "node1.local.",
                "port": 8001,
                "addresses": ["192.168.1.10"],
                "txt": {"diag": "dns_lookup", "health": "/health", "run": "/diagnose"},
            },
        },
        # 2. Verification
        {
            "event": "state_change",
            "wall_clock_utc": "2026-10-28T07:00:01Z",
            "service": {"instance_name": "svc-test._niriksha._tcp.local."},
            "old_state": "UNKNOWN",
            "new_state": "VERIFIED",
            "reason": "TXT diag='dns_lookup' matches /diagnose",
            "measurements": {
                "http_status": 200,
                "health_latency_ms": 150.5,
                "verification_latency_ms": 180.2,
                "txt_fields_ok": True,
                "health_status_ok": True,
                "diag_type_ok": True,
            },
        },
        # 3. Fault (Unreachable)
        {
            "event": "state_change",
            "wall_clock_utc": "2026-10-28T07:00:10Z",
            "service": {"instance_name": "svc-test._niriksha._tcp.local."},
            "old_state": "VERIFIED",
            "new_state": "UNREACHABLE",
            "reason": "Connection refused",
            "measurements": {"http_status": None, "health_latency_ms": 2000.0, "verification_latency_ms": 2000.0},
            "estimated_detection_latency_ms": 5012.3,
        },
        # 4. Migration to port 8002
        {
            "event": "endpoint_migration",
            "wall_clock_utc": "2026-10-28T07:00:15Z",
            "service": {
                "instance_name": "svc-test._niriksha._tcp.local.",
                "host": "node1.local.",
                "port": 8002,
                "addresses": ["192.168.1.10"],
            },
            "old_endpoint": {"host": "node1.local.", "port": 8001},
            "new_endpoint": {"host": "node1.local.", "port": 8002},
            "rediscovery_latency_ms": 3500.0,
            "reason": "Advertised endpoint migrated from port 8001 to port 8002",
        },
        # 5. Recovery Verification
        {
            "event": "state_change",
            "wall_clock_utc": "2026-10-28T07:00:16Z",
            "service": {"instance_name": "svc-test._niriksha._tcp.local.", "port": 8002},
            "old_state": "UNREACHABLE",
            "new_state": "VERIFIED",
            "reason": "Recovered on port 8002",
            "measurements": {
                "http_status": 200,
                "health_latency_ms": 140.0,
                "verification_latency_ms": 165.0,
                "txt_fields_ok": True,
                "health_status_ok": True,
                "diag_type_ok": True,
            },
            "is_recovery": True,
            "is_migration": True,
            "recovery_latency_ms": 4200.0,
            "estimated_total_recovery_latency_ms": 9212.3,
        },
    ]

    services = derive_services_state(events)
    assert len(services) == 1
    s = services["svc-test._niriksha._tcp.local."]
    assert s["current_state"] == "VERIFIED"
    assert s["port"] == 8002
    assert s["host"] == "node1.local."
    assert s["txt_diag"] == "dns_lookup"
    assert s["http_status"] == 200
    assert s["health_latency_ms"] == 140.0
    assert s["verification_latency_ms"] == 165.0
    assert s["last_seen_utc"] == "2026-10-28T07:00:16Z"
    assert s["last_verdict"] == "Recovered on port 8002"


def test_derive_services_state_degraded_and_stale():
    events = [
        # Service A: Degraded due to capability mismatch
        {
            "event": "state_change",
            "wall_clock_utc": "2026-10-28T07:05:00Z",
            "service": {
                "instance_name": "svc-a._niriksha._tcp.local.",
                "host": "hostA.local.",
                "port": 8001,
                "txt": {"diag": "http"},
            },
            "old_state": "VERIFIED",
            "new_state": "DEGRADED",
            "reason": "Capability mismatch: TXT diag='http' but /diagnose reports 'dns_lookup'",
            "measurements": {"http_status": 200, "diag_type_ok": False},
        },
        # Service B: Stale due to mDNS withdrawal
        {
            "event": "state_change",
            "wall_clock_utc": "2026-10-28T07:05:10Z",
            "service": {
                "instance_name": "svc-b._niriksha._tcp.local.",
                "host": "hostB.local.",
                "port": 8001,
            },
            "old_state": "VERIFIED",
            "new_state": "STALE",
            "reason": "mDNS advertisement withdrawn",
            "measurements": {},
        },
    ]

    services = derive_services_state(events)
    assert len(services) == 2
    assert services["svc-a._niriksha._tcp.local."]["current_state"] == "DEGRADED"
    assert services["svc-a._niriksha._tcp.local."]["diag_type_ok"] is False

    assert services["svc-b._niriksha._tcp.local."]["current_state"] == "STALE"
    assert "withdrawn" in services["svc-b._niriksha._tcp.local."]["last_verdict"]


# ---------------------------------------------------------------------------
# 3. Summary Counts
# ---------------------------------------------------------------------------

def test_compute_summary_counts():
    mock_services = {
        "s1": {"current_state": "VERIFIED"},
        "s2": {"current_state": "VERIFIED"},
        "s3": {"current_state": "DEGRADED"},
        "s4": {"current_state": "UNREACHABLE"},
        "s5": {"current_state": "STALE"},
        "s6": {"current_state": "UNKNOWN"},
    }
    counts = compute_summary_counts(mock_services)
    assert counts["total_discovered"] == 6
    assert counts["verified"] == 2
    assert counts["degraded"] == 1
    assert counts["unreachable"] == 1
    assert counts["stale"] == 1
    assert counts["unknown"] == 1


# ---------------------------------------------------------------------------
# 4. Performance Metrics (Measured vs Estimated)
# ---------------------------------------------------------------------------

def test_extract_performance_metrics():
    events = [
        {
            "event": "probe",
            "measurements": {"health_latency_ms": 100.0, "verification_latency_ms": 120.0},
        },
        {
            "event": "probe",
            "measurements": {"health_latency_ms": 200.0, "verification_latency_ms": 220.0},
        },
        {
            "event": "state_change",
            "estimated_detection_latency_ms": 4050.2,
            "measurements": {"health_latency_ms": 150.0, "verification_latency_ms": 170.0},
        },
        {
            "event": "endpoint_migration",
            "rediscovery_latency_ms": 3200.5,
            "estimated_rediscovery_latency_ms": 7250.7,
        },
        {
            "event": "state_change",
            "is_recovery": True,
            "recovery_latency_ms": 4100.0,
            "estimated_total_recovery_latency_ms": 8150.2,
            "measurements": {"health_latency_ms": 110.0, "verification_latency_ms": 130.0},
        },
    ]

    summary = extract_performance_metrics(events)
    # Measured
    assert summary.latest_health_latency_ms == 110.0
    assert summary.avg_health_latency_ms == 140.0  # (100+200+150+110)/4 = 560/4 = 140
    assert summary.latest_verification_latency_ms == 130.0
    assert summary.avg_verification_latency_ms == 160.0  # (120+220+170+130)/4 = 640/4 = 160
    assert summary.latest_rediscovery_latency_ms == 3200.5
    assert summary.latest_recovery_latency_ms == 4100.0

    # Estimated
    assert summary.latest_detection_latency_ms == 4050.2
    assert summary.latest_total_recovery_latency_ms == 8150.2

    # Counts
    assert summary.probe_sample_count == 4
    assert summary.migration_count == 1


# ---------------------------------------------------------------------------
# 5. Migration Sequence Extraction
# ---------------------------------------------------------------------------

def test_extract_migrations():
    events = [
        {
            "event": "endpoint_migration",
            "wall_clock_utc": "2026-10-28T07:11:31Z",
            "service": {"instance_name": "svc1"},
            "old_endpoint": {"host": "LAPTOP.local.", "port": 8001},
            "new_endpoint": {"host": "LAPTOP.local.", "port": 8002},
            "reason": "Advertised endpoint migrated from port 8001 to port 8002",
            "rediscovery_latency_ms": 3976.19,
            "estimated_rediscovery_latency_ms": 8079.95,
        },
        {
            "event": "state_change",
            "is_migration": True,
            "is_recovery": True,
            "recovery_latency_ms": 4637.18,
            "estimated_total_recovery_latency_ms": 8740.94,
        },
    ]

    migrations = extract_migrations(events)
    assert len(migrations) == 1
    m = migrations[0]
    assert m["instance_name"] == "svc1"
    assert m["old_port"] == 8001
    assert m["new_port"] == 8002
    assert m["rediscovery_latency_ms"] == 3976.19
    assert m["recovery_latency_ms"] == 4637.18
    assert m["estimated_total_recovery_latency_ms"] == 8740.94
    assert m["status"] == "Verified on New Endpoint"


# ---------------------------------------------------------------------------
# 6. Event Timeline Filtering
# ---------------------------------------------------------------------------

def test_filter_events():
    events = [
        {"event": "discovery", "action": "add", "service": {"instance_name": "alpha"}},
        {"event": "state_change", "new_state": "VERIFIED", "service": {"instance_name": "alpha"}, "reason": "matches"},
        {"event": "probe", "state": "VERIFIED", "service": {"instance_name": "alpha"}, "reason": "steady"},
        {"event": "state_change", "new_state": "UNREACHABLE", "service": {"instance_name": "beta"}, "reason": "timeout"},
    ]

    # Filter by type
    assert len(filter_events(events, event_type="probe")) == 1
    assert len(filter_events(events, event_type="state_change")) == 2

    # Filter by state
    assert len(filter_events(events, state_filter="VERIFIED")) == 2
    assert len(filter_events(events, state_filter="UNREACHABLE")) == 1

    # Filter by search
    assert len(filter_events(events, search_query="beta")) == 1
    assert len(filter_events(events, search_query="timeout")) == 1
    assert len(filter_events(events, search_query="nonexistent")) == 0


# ---------------------------------------------------------------------------
# 7. Real Log File Compatibility Test
# ---------------------------------------------------------------------------

def test_real_workspace_log_compatibility():
    real_log = Path("niriksha_events.jsonl")
    if not real_log.is_file():
        pytest.skip("niriksha_events.jsonl not present in workspace")

    events = read_jsonl_events(real_log)
    assert len(events) > 0, "Real log must contain recorded events"

    services = derive_services_state(events)
    assert len(services) >= 1, "Should derive at least one service from real log"

    # Verify primary diagnostic service was parsed
    assert "niriksha-diagnostic._niriksha._tcp.local." in services
    diag_svc = services["niriksha-diagnostic._niriksha._tcp.local."]
    assert diag_svc["port"] in (8001, 8002)
    assert diag_svc["current_state"] in ("VERIFIED", "DEGRADED", "UNREACHABLE", "STALE")

    # Verify performance metrics extraction
    perf = extract_performance_metrics(events)
    assert perf.probe_sample_count > 0
    assert perf.latest_health_latency_ms is not None
    assert perf.avg_health_latency_ms is not None

    # Verify migration extraction from the live experiment
    migrations = extract_migrations(events)
    assert len(migrations) >= 1, "Real experiment log must have migration records"
    assert migrations[0]["old_port"] == 8001
    assert migrations[0]["new_port"] == 8002
    assert migrations[0]["rediscovery_latency_ms"] is not None


# ---------------------------------------------------------------------------
# 8. Data Freshness & Age Computation Tests
# ---------------------------------------------------------------------------

def test_compute_log_freshness_empty():
    res = compute_log_freshness([])
    assert res["has_events"] is False
    assert res["is_stale"] is True
    assert res["freshness_status"] == "NO DATA"
    assert res["age_seconds"] is None


def test_compute_log_freshness_active():
    event_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    mock_now = datetime(2026, 10, 28, 12, 0, 30, tzinfo=timezone.utc)  # 30 seconds later

    events = [{"wall_clock_utc": "2026-10-28T12:00:00Z"}]
    res = compute_log_freshness(events, now_utc=mock_now, stale_threshold_seconds=120.0)

    assert res["has_events"] is True
    assert res["is_stale"] is False
    assert res["age_seconds"] == 30.0
    assert res["age_display"] == "30s ago"
    assert res["freshness_status"] == "ACTIVE STREAM"


def test_compute_log_freshness_stale():
    event_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    mock_now = datetime(2026, 10, 28, 12, 45, 0, tzinfo=timezone.utc)  # 45 minutes later

    events = [{"wall_clock_utc": "2026-10-28T12:00:00Z"}]
    res = compute_log_freshness(events, now_utc=mock_now, stale_threshold_seconds=120.0)

    assert res["has_events"] is True
    assert res["is_stale"] is True
    assert res["age_seconds"] == 2700.0
    assert "45m" in res["age_display"]
    assert res["freshness_status"] == "STALE / HISTORICAL"
    assert "Historical data" in res["freshness_message"]


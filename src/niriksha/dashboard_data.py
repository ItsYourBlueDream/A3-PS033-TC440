"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 3A: Dashboard Data Processing Engine

Robust JSONL event log parser, latest-state derivation, performance metrics
extraction, migration sequence tracking, and event filtering.
Decoupled from Streamlit to allow 100% automated test coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data Freshness & Event Age Computation
# ---------------------------------------------------------------------------

def compute_log_freshness(
    events: list[dict[str, Any]],
    now_utc: Optional[datetime] = None,
    stale_threshold_seconds: float = 120.0,
) -> dict[str, Any]:
    """
    Compute data freshness and age relative to current UTC time.
    Calculates exact elapsed time since the most recent recorded event to
    determine whether the monitor stream is actively publishing or dormant.
    """
    if not events:
        return {
            "has_events": False,
            "latest_timestamp_utc": "N/A",
            "age_seconds": None,
            "age_display": "No events",
            "is_stale": True,
            "freshness_status": "NO DATA",
            "freshness_message": "No event records found in log.",
        }

    # Find the most recent timestamp in chronological event log
    latest_ts_str = "N/A"
    latest_dt: Optional[datetime] = None

    for evt in reversed(events):
        ts = evt.get("wall_clock_utc")
        if ts:
            try:
                # Support standard UTC ISO string ending in Z or offset
                norm_ts = ts.replace("Z", "+00:00")
                parsed_dt = datetime.fromisoformat(norm_ts)
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                latest_dt = parsed_dt
                latest_ts_str = ts
                break
            except (ValueError, TypeError):
                continue

    if latest_dt is None:
        return {
            "has_events": True,
            "latest_timestamp_utc": "Unknown",
            "age_seconds": None,
            "age_display": "Timestamp missing",
            "is_stale": True,
            "freshness_status": "UNKNOWN TIMESTAMP",
            "freshness_message": "Events found but lack valid UTC timestamps.",
        }

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    diff_sec = (now_utc - latest_dt).total_seconds()
    age_seconds = max(0.0, diff_sec)

    if age_seconds < 60:
        age_display = f"{int(age_seconds)}s ago"
    elif age_seconds < 3600:
        mins = int(age_seconds // 60)
        secs = int(age_seconds % 60)
        age_display = f"{mins}m {secs}s ago" if mins < 10 else f"{mins}m ago"
    elif age_seconds < 86400:
        hrs = age_seconds / 3600
        age_display = f"{hrs:.1f}h ago"
    else:
        days = age_seconds / 86400
        age_display = f"{days:.1f}d ago"

    is_stale = age_seconds >= stale_threshold_seconds

    if is_stale:
        status_label = "STALE / HISTORICAL"
        msg = f"Historical data: last event was logged {age_display} at {latest_ts_str}. No live monitor heartbeat detected."
    else:
        status_label = "ACTIVE STREAM"
        msg = f"Live monitor active: last event logged {age_display} at {latest_ts_str}."

    return {
        "has_events": True,
        "latest_timestamp_utc": latest_ts_str,
        "age_seconds": round(age_seconds, 1),
        "age_display": age_display,
        "is_stale": is_stale,
        "freshness_status": status_label,
        "freshness_message": msg,
    }


# ---------------------------------------------------------------------------
# Robust JSONL Reader
# ---------------------------------------------------------------------------

def read_jsonl_events(log_path: Path | str) -> list[dict[str, Any]]:
    """
    Read events from a JSONL file.
    Tolerates:
      - File does not exist -> returns []
      - Empty file -> returns []
      - Blank lines -> skipped
      - Incomplete / truncated last line or corrupt line -> skipped gracefully
    Never crashes on malformed data.
    """
    path = Path(log_path)
    if not path.is_file():
        return []

    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    record = json.loads(clean_line)
                    if isinstance(record, dict):
                        # Retain original 1-based log line index for traceability
                        record["_line_number"] = line_no
                        events.append(record)
                except (json.JSONDecodeError, ValueError):
                    # Incomplete write or corrupt line — skip without aborting
                    continue
    except OSError:
        return []

    return events


# ---------------------------------------------------------------------------
# Latest State Derivation
# ---------------------------------------------------------------------------

def derive_services_state(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Derive the current real state for each unique discovered service instance.
    Iterates through historical events in order to maintain accurate latest status.

    Returns dict mapping instance_name to:
      {
        "instance_name": str,
        "host": str,
        "port": int | None,
        "addresses": list[str],
        "txt": dict[str, str],
        "txt_diag": str | None,
        "health_path": str,
        "run_path": str,
        "current_state": str ("VERIFIED" | "DEGRADED" | "UNREACHABLE" | "STALE" | "UNKNOWN"),
        "last_verdict": str,
        "last_event_type": str,
        "last_seen_utc": str,
        "http_status": int | None,
        "health_latency_ms": float | None,
        "verification_latency_ms": float | None,
        "txt_fields_ok": bool | None,
        "health_status_ok": bool | None,
        "diag_type_ok": bool | None,
      }
    """
    services: dict[str, dict[str, Any]] = {}

    for evt in events:
        svc_meta = evt.get("service") or {}
        instance_name = svc_meta.get("instance_name")
        if not instance_name:
            continue

        if instance_name not in services:
            services[instance_name] = {
                "instance_name": instance_name,
                "host": "N/A",
                "port": None,
                "addresses": [],
                "txt": {},
                "txt_diag": "N/A",
                "health_path": "/health",
                "run_path": "/diagnose",
                "current_state": "UNKNOWN",
                "last_verdict": "Discovered; awaiting verification",
                "last_event_type": evt.get("event", "unknown"),
                "last_seen_utc": evt.get("wall_clock_utc", "N/A"),
                "http_status": None,
                "health_latency_ms": None,
                "verification_latency_ms": None,
                "txt_fields_ok": None,
                "health_status_ok": None,
                "diag_type_ok": None,
            }

        s = services[instance_name]
        event_type = evt.get("event")
        utc_ts = evt.get("wall_clock_utc", s["last_seen_utc"])
        s["last_seen_utc"] = utc_ts
        s["last_event_type"] = event_type or s["last_event_type"]

        # Update endpoint / TXT metadata if present in event.service
        if "host" in svc_meta and svc_meta["host"]:
            s["host"] = svc_meta["host"]
        if "port" in svc_meta and svc_meta["port"] is not None:
            s["port"] = svc_meta["port"]
        if "addresses" in svc_meta and svc_meta["addresses"]:
            s["addresses"] = svc_meta["addresses"]
        if "txt" in svc_meta and isinstance(svc_meta["txt"], dict):
            s["txt"] = svc_meta["txt"]
            s["txt_diag"] = svc_meta["txt"].get("diag", "N/A")
            s["health_path"] = svc_meta["txt"].get("health", "/health")
            s["run_path"] = svc_meta["txt"].get("run", "/diagnose")

        # Handle specific event semantics
        if event_type == "discovery":
            action = evt.get("action", "add")
            if s["current_state"] == "UNKNOWN":
                s["last_verdict"] = f"mDNS discovery ({action})"

        elif event_type == "endpoint_migration":
            new_ep = evt.get("new_endpoint") or {}
            if new_ep.get("port"):
                s["port"] = new_ep["port"]
            if new_ep.get("host"):
                s["host"] = new_ep["host"]
            if new_ep.get("addresses"):
                s["addresses"] = new_ep["addresses"]
            s["last_verdict"] = evt.get("reason", "Endpoint migrated")

        elif event_type == "state_change":
            s["current_state"] = evt.get("new_state", s["current_state"])
            s["last_verdict"] = evt.get("reason", s["last_verdict"])
            meas = evt.get("measurements") or {}
            s["http_status"] = meas.get("http_status")
            s["health_latency_ms"] = meas.get("health_latency_ms")
            s["verification_latency_ms"] = meas.get("verification_latency_ms")
            s["txt_fields_ok"] = meas.get("txt_fields_ok")
            s["health_status_ok"] = meas.get("health_status_ok")
            s["diag_type_ok"] = meas.get("diag_type_ok")

            # Check if state_change included new_endpoint
            new_ep = evt.get("new_endpoint") or {}
            if new_ep.get("port"):
                s["port"] = new_ep["port"]
            if new_ep.get("host"):
                s["host"] = new_ep["host"]

        elif event_type == "probe":
            # Routine probe updates current verification status & measurements
            probe_state = evt.get("state")
            if probe_state:
                s["current_state"] = probe_state
            if evt.get("reason"):
                s["last_verdict"] = evt["reason"]
            meas = evt.get("measurements") or {}
            if "http_status" in meas:
                s["http_status"] = meas["http_status"]
            if "health_latency_ms" in meas:
                s["health_latency_ms"] = meas["health_latency_ms"]
            if "verification_latency_ms" in meas:
                s["verification_latency_ms"] = meas["verification_latency_ms"]

    return services


# ---------------------------------------------------------------------------
# Summary Statistics & Counts
# ---------------------------------------------------------------------------

def compute_summary_counts(services: dict[str, dict[str, Any]]) -> dict[str, int]:
    """
    Compute current operational state counts across all discovered services.
    """
    counts = {
        "total_discovered": len(services),
        "verified": 0,
        "degraded": 0,
        "unreachable": 0,
        "stale": 0,
        "unknown": 0,
    }

    for svc in services.values():
        st = svc.get("current_state", "UNKNOWN").upper()
        if st == "VERIFIED":
            counts["verified"] += 1
        elif st == "DEGRADED":
            counts["degraded"] += 1
        elif st == "UNREACHABLE":
            counts["unreachable"] += 1
        elif st == "STALE":
            counts["stale"] += 1
        else:
            counts["unknown"] += 1

    return counts


# ---------------------------------------------------------------------------
# Performance & Latency Metrics
# ---------------------------------------------------------------------------

@dataclass
class PerformanceSummary:
    # Latencies strictly measured
    latest_health_latency_ms: Optional[float]
    avg_health_latency_ms: Optional[float]
    latest_verification_latency_ms: Optional[float]
    avg_verification_latency_ms: Optional[float]
    latest_rediscovery_latency_ms: Optional[float]
    latest_recovery_latency_ms: Optional[float]

    # Latencies estimated
    latest_detection_latency_ms: Optional[float]
    latest_total_recovery_latency_ms: Optional[float]

    # Sample counts
    probe_sample_count: int
    migration_count: int


def extract_performance_metrics(events: list[dict[str, Any]]) -> PerformanceSummary:
    """
    Extract measured and estimated performance metrics from all events.
    Strictly separates direct timing measurements from poll-interval estimations.
    """
    health_latencies: list[float] = []
    verif_latencies: list[float] = []

    latest_health: Optional[float] = None
    latest_verif: Optional[float] = None
    latest_rediscovery: Optional[float] = None
    latest_recovery: Optional[float] = None
    latest_detection: Optional[float] = None
    latest_total_recovery: Optional[float] = None

    migration_count = 0

    for evt in events:
        meas = evt.get("measurements") or {}
        h_lat = meas.get("health_latency_ms")
        v_lat = meas.get("verification_latency_ms")

        if isinstance(h_lat, (int, float)) and h_lat > 0:
            health_latencies.append(float(h_lat))
            latest_health = float(h_lat)

        if isinstance(v_lat, (int, float)) and v_lat > 0:
            verif_latencies.append(float(v_lat))
            latest_verif = float(v_lat)

        if "estimated_detection_latency_ms" in evt:
            det = evt["estimated_detection_latency_ms"]
            if isinstance(det, (int, float)):
                latest_detection = float(det)

        if "rediscovery_latency_ms" in evt:
            redisc = evt["rediscovery_latency_ms"]
            if isinstance(redisc, (int, float)):
                latest_rediscovery = float(redisc)

        if "recovery_latency_ms" in evt:
            recov = evt["recovery_latency_ms"]
            if isinstance(recov, (int, float)):
                latest_recovery = float(recov)

        if "estimated_total_recovery_latency_ms" in evt:
            tot_rec = evt["estimated_total_recovery_latency_ms"]
            if isinstance(tot_rec, (int, float)):
                latest_total_recovery = float(tot_rec)

        if evt.get("event") == "endpoint_migration" or evt.get("is_migration"):
            migration_count += 1

    avg_health = round(sum(health_latencies) / len(health_latencies), 2) if health_latencies else None
    avg_verif = round(sum(verif_latencies) / len(verif_latencies), 2) if verif_latencies else None

    return PerformanceSummary(
        latest_health_latency_ms=latest_health,
        avg_health_latency_ms=avg_health,
        latest_verification_latency_ms=latest_verif,
        avg_verification_latency_ms=avg_verif,
        latest_rediscovery_latency_ms=latest_rediscovery,
        latest_recovery_latency_ms=latest_recovery,
        latest_detection_latency_ms=latest_detection,
        latest_total_recovery_latency_ms=latest_total_recovery,
        probe_sample_count=len(health_latencies),
        migration_count=migration_count,
    )


# ---------------------------------------------------------------------------
# Migration Tracking & Timeline Construction
# ---------------------------------------------------------------------------

def extract_migrations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract genuine endpoint migration occurrences with full details.
    Combines 'endpoint_migration' events and subsequent 'state_change' recovery.
    """
    migrations: list[dict[str, Any]] = []

    for evt in events:
        evt_type = evt.get("event")
        if evt_type == "endpoint_migration":
            old_ep = evt.get("old_endpoint") or {}
            new_ep = evt.get("new_endpoint") or {}
            svc = evt.get("service") or {}

            migrations.append({
                "wall_clock_utc": evt.get("wall_clock_utc", "N/A"),
                "instance_name": svc.get("instance_name", "N/A"),
                "old_port": old_ep.get("port", "N/A"),
                "new_port": new_ep.get("port", "N/A"),
                "old_host": old_ep.get("host", "N/A"),
                "new_host": new_ep.get("host", "N/A"),
                "rediscovery_latency_ms": evt.get("rediscovery_latency_ms"),
                "estimated_rediscovery_latency_ms": evt.get("estimated_rediscovery_latency_ms"),
                "recovery_latency_ms": None,
                "estimated_total_recovery_latency_ms": None,
                "reason": evt.get("reason", "Endpoint migration"),
                "status": "Migrated",
            })

        elif evt_type == "state_change" and evt.get("is_migration") and evt.get("is_recovery"):
            # Update the latest migration record with recovery measurements if matching
            if migrations:
                last_mig = migrations[-1]
                if last_mig.get("recovery_latency_ms") is None:
                    last_mig["recovery_latency_ms"] = evt.get("recovery_latency_ms")
                    last_mig["estimated_total_recovery_latency_ms"] = evt.get("estimated_total_recovery_latency_ms")
                    last_mig["status"] = "Verified on New Endpoint"

    return migrations


# ---------------------------------------------------------------------------
# Time Series for Visual Charting
# ---------------------------------------------------------------------------

def extract_latency_timeseries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract chronological points with health_latency_ms and verification_latency_ms.
    Safe for Streamlit native line_chart or Altair charts.
    """
    series: list[dict[str, Any]] = []
    for evt in events:
        meas = evt.get("measurements") or {}
        h_lat = meas.get("health_latency_ms")
        v_lat = meas.get("verification_latency_ms")
        ts = evt.get("wall_clock_utc")

        if ts and (h_lat is not None or v_lat is not None):
            series.append({
                "timestamp": ts,
                "health_latency_ms": float(h_lat) if isinstance(h_lat, (int, float)) else None,
                "verification_latency_ms": float(v_lat) if isinstance(v_lat, (int, float)) else None,
                "event": evt.get("event"),
                "state": evt.get("new_state") or evt.get("state") or "N/A",
            })
    return series


# ---------------------------------------------------------------------------
# Filterable Event Timeline
# ---------------------------------------------------------------------------

def filter_events(
    events: list[dict[str, Any]],
    event_type: Optional[str] = None,
    state_filter: Optional[str] = None,
    search_query: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Filter events by event_type, state, and text search across instance, reason, and endpoints.
    """
    filtered: list[dict[str, Any]] = []

    for evt in events:
        # 1. Filter by event type
        if event_type and event_type != "ALL":
            if evt.get("event") != event_type:
                continue

        # 2. Filter by state
        if state_filter and state_filter != "ALL":
            new_st = evt.get("new_state") or evt.get("state")
            if new_st != state_filter:
                continue

        # 3. Filter by text search
        if search_query:
            query = search_query.strip().lower()
            svc = evt.get("service") or {}
            text_corpus = (
                f"{evt.get('event', '')} "
                f"{evt.get('reason', '')} "
                f"{evt.get('old_state', '')} "
                f"{evt.get('new_state', '')} "
                f"{evt.get('state', '')} "
                f"{svc.get('instance_name', '')} "
                f"{svc.get('host', '')} "
                f"{svc.get('port', '')} "
            ).lower()
            if query not in text_corpus:
                continue

        filtered.append(evt)

    return filtered

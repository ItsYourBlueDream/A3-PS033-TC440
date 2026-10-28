"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 2A: Shared data models

ServiceState  -- the four canonical operational states
ServiceRecord -- normalised DNS-SD advertisement record (from mDNS)
VerificationResult -- outcome of one deep-verification cycle
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Operational state
# ---------------------------------------------------------------------------

class ServiceState(str, Enum):
    """
    Four mutually exclusive operational states for a discovered service.

    VERIFIED    -- endpoint reachable, /health JSON healthy, advertised metadata
                   matches observed /diagnose behaviour.
    DEGRADED    -- endpoint responds (HTTP 200 or non-200) but either the
                   health body does not indicate 'healthy', or the advertised
                   capability (TXT diag) disagrees with the /diagnose response.
    UNREACHABLE -- the advertisement is still present on mDNS but the HTTP
                   endpoint cannot be connected to or times out.
    STALE       -- the mDNS advertisement has been withdrawn (remove_service);
                   absence of the advertisement is confirmed.  Do NOT use this
                   for a dead HTTP endpoint whose advertisement is still live.
    UNKNOWN     -- initial state before the first verification attempt.
    """

    VERIFIED    = "VERIFIED"
    DEGRADED    = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"
    STALE       = "STALE"
    UNKNOWN     = "UNKNOWN"


# ---------------------------------------------------------------------------
# Advertisement record (from mDNS)
# ---------------------------------------------------------------------------

@dataclass
class ServiceRecord:
    """
    Normalised record of a DNS-SD / mDNS advertisement.
    Populated directly from zeroconf ServiceInfo — no hard-coded values.
    """

    instance_name: str          # full DNS-SD name, e.g. niriksha-diagnostic._niriksha._tcp.local.
    service_type: str           # e.g. _niriksha._tcp.local.
    host: str                   # server field from SRV record, e.g. LAPTOP.local.
    port: int                   # advertised port
    addresses: list[str]        # decoded IPv4 strings from A/AAAA records
    txt: dict[str, str]         # decoded TXT properties

    # Derived at construction time; not part of the mDNS record.
    discovered_at: float = field(default_factory=time.monotonic)

    @property
    def primary_ip(self) -> Optional[str]:
        """First advertised IPv4 address, or None if the list is empty."""
        return self.addresses[0] if self.addresses else None

    @property
    def advertised_diag(self) -> Optional[str]:
        """Value of the TXT 'diag' key, or None if absent."""
        return self.txt.get("diag")

    @property
    def health_path(self) -> str:
        """Advertised /health path from TXT, defaulting to '/health'."""
        return self.txt.get("health", "/health")

    @property
    def diagnose_path(self) -> str:
        """Advertised /diagnose path from TXT, defaulting to '/diagnose'."""
        return self.txt.get("run", "/diagnose")


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """
    Outcome of one full deep-verification cycle against a ServiceRecord.

    All timing uses time.monotonic() so it is suitable for latency measurement.
    The wall-clock timestamp (UTC ISO-8601) is also captured for log readability.
    """

    record: ServiceRecord
    state: ServiceState

    # Timing (monotonic)
    started_at_mono: float      # time.monotonic() just before the first HTTP probe
    finished_at_mono: float     # time.monotonic() just after the last probe

    # Wall-clock for log readability
    wall_clock_utc: str         # ISO-8601 UTC, e.g. "2026-10-28T06:00:00Z"

    # HTTP /health
    http_status: Optional[int] = None
    health_body: Optional[dict] = None
    health_latency_ms: Optional[float] = None

    # Capability validation
    txt_fields_ok: bool = True          # all required TXT keys present
    health_status_ok: bool = True       # body["status"] == "healthy"
    diag_type_ok: Optional[bool] = None # advertised diag matches /diagnose response
    diagnose_body: Optional[dict] = None

    # Human-readable reason for the state
    reason: str = ""

    @property
    def verification_latency_ms(self) -> float:
        """Total elapsed time for one full verification cycle (monotonic)."""
        return round((self.finished_at_mono - self.started_at_mono) * 1000, 2)

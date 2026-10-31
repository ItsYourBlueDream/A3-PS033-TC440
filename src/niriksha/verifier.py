"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 2A: Deep capability verifier

Performs a full evidence-based verification cycle against a ServiceRecord:

  1. HTTP GET /health        -- reachability + JSON validity + status field
  2. TXT metadata check      -- required keys present, values non-empty
  3. Capability cross-check  -- advertised TXT diag=<X> vs /diagnose response

Returns a VerificationResult with a ServiceState and a precise reason string.
Never assumes an unverified capability is correct.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from niriksha.models import ServiceRecord, ServiceState, VerificationResult


# Required TXT keys per the DNS-SD advertisement specification.
REQUIRED_TXT_KEYS = {"diag", "txtvers", "health", "run"}

# Default HTTP timeout for a single probe (seconds).
DEFAULT_HTTP_TIMEOUT = 8.0


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _decode_body_safe(response: httpx.Response) -> Optional[dict]:
    """Try to JSON-decode a response; return None on any error."""
    try:
        body = response.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def _check_txt_fields(txt: dict[str, str]) -> tuple[bool, str]:
    """
    Verify all required TXT keys are present and non-empty.

    Returns (ok: bool, reason: str).
    """
    missing = REQUIRED_TXT_KEYS - set(txt.keys())
    if missing:
        return False, f"TXT missing required keys: {sorted(missing)}"
    empty = [k for k in REQUIRED_TXT_KEYS if not txt.get(k)]
    if empty:
        return False, f"TXT keys have empty values: {sorted(empty)}"
    return True, ""


def _check_diag_capability(
    record: ServiceRecord,
    client: httpx.Client,
    http_timeout: float,
) -> tuple[bool, Optional[dict], str]:
    """
    Cross-check advertised TXT diag against the /diagnose endpoint response.

    Returns (match: bool, body: dict|None, reason: str).
    Never assumes an unverified capability is correct.
    """
    advertised_diag = record.advertised_diag
    if not advertised_diag:
        return False, None, "TXT 'diag' key absent; capability cross-check failed"

    ip = record.primary_ip
    if not ip:
        return False, None, "No IP address available for /diagnose probe"

    url = f"http://{ip}:{record.port}{record.diagnose_path}"
    try:
        resp = client.get(url, timeout=http_timeout)
    except httpx.HTTPError as exc:
        return False, None, f"/diagnose probe failed: {type(exc).__name__}: {exc}"

    if resp.status_code != 200:
        return False, None, f"/diagnose returned HTTP {resp.status_code} (expected 200)"

    body = _decode_body_safe(resp)
    if body is None:
        return False, None, f"/diagnose returned non-JSON (status {resp.status_code})"

    observed_diag = body.get("diagnostic")
    if observed_diag is None:
        return False, body, "/diagnose body missing 'diagnostic' field"

    match = (observed_diag == advertised_diag) or (
        advertised_diag == "dns" and observed_diag == "dns_lookup"
    )
    if not match:
        reason = (
            f"Capability mismatch: TXT advertises diag='{advertised_diag}' "
            f"but /diagnose reports diagnostic='{observed_diag}'"
        )
        return False, body, reason

    # Diagnostic type matches.  Now verify the operation actually succeeded.
    # A diagnostic that reports success=false (e.g. DNS timeout) must not
    # be accepted as VERIFIED — the *capability* is present but the
    # *operation* failed, so the service is DEGRADED.  A missing 'success'
    # field is treated the same way: never assume an unverified operation
    # succeeded.
    diag_success = body.get("success")
    if diag_success is None:
        return False, body, (
            f"TXT diag='{advertised_diag}' matches /diagnose diagnostic='{observed_diag}', "
            f"but 'success' field is missing; cannot confirm operational status"
        )
    if diag_success is not True:
        return False, body, (
            f"TXT diag='{advertised_diag}' matches /diagnose diagnostic='{observed_diag}', "
            f"but diagnostic operation reported success={diag_success}"
        )

    return True, body, (
        f"TXT diag='{advertised_diag}' matches /diagnose diagnostic='{observed_diag}', "
        f"success=True"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_once(
    record: ServiceRecord,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> VerificationResult:
    """
    Run one complete deep-verification cycle.

    State classification logic:
      - UNREACHABLE if /health cannot be reached (ConnectError, Timeout, or
        any transport-level exception).
      - DEGRADED if /health is reachable but:
          * HTTP status != 200, OR
          * Body is not valid JSON, OR
          * body["status"] != "healthy", OR
          * Required TXT keys are missing/empty, OR
          * Advertised TXT diag does NOT match /diagnose response.
      - VERIFIED if all checks pass.
    """
    wall = _utc_now()
    started = time.monotonic()

    ip = record.primary_ip
    if not ip:
        finished = time.monotonic()
        return VerificationResult(
            record=record,
            state=ServiceState.UNREACHABLE,
            started_at_mono=started,
            finished_at_mono=finished,
            wall_clock_utc=wall,
            reason="No IP address in advertisement; cannot probe",
        )

    health_url = f"http://{ip}:{record.port}{record.health_path}"

    # --- Probe 1: /health ----------------------------------------------------
    t_health = time.perf_counter()
    http_status: Optional[int] = None
    health_body: Optional[dict] = None
    health_latency_ms: Optional[float] = None

    try:
        with httpx.Client(timeout=http_timeout) as client:
            resp = client.get(health_url)
            health_latency_ms = round((time.perf_counter() - t_health) * 1000, 2)
            http_status = resp.status_code
            health_body = _decode_body_safe(resp)

            # --- Reachability check ------------------------------------------
            if http_status != 200:
                finished = time.monotonic()
                return VerificationResult(
                    record=record,
                    state=ServiceState.DEGRADED,
                    started_at_mono=started,
                    finished_at_mono=finished,
                    wall_clock_utc=wall,
                    http_status=http_status,
                    health_body=health_body,
                    health_latency_ms=health_latency_ms,
                    txt_fields_ok=False,
                    health_status_ok=False,
                    reason=f"/health returned HTTP {http_status} (expected 200)",
                )

            # --- JSON body check ---------------------------------------------
            if health_body is None:
                finished = time.monotonic()
                return VerificationResult(
                    record=record,
                    state=ServiceState.DEGRADED,
                    started_at_mono=started,
                    finished_at_mono=finished,
                    wall_clock_utc=wall,
                    http_status=http_status,
                    health_latency_ms=health_latency_ms,
                    health_status_ok=False,
                    reason="/health returned HTTP 200 but body is not valid JSON",
                )

            # --- Health status field check -----------------------------------
            health_status_ok = health_body.get("status") == "healthy"
            if not health_status_ok:
                finished = time.monotonic()
                return VerificationResult(
                    record=record,
                    state=ServiceState.DEGRADED,
                    started_at_mono=started,
                    finished_at_mono=finished,
                    wall_clock_utc=wall,
                    http_status=http_status,
                    health_body=health_body,
                    health_latency_ms=health_latency_ms,
                    health_status_ok=False,
                    reason=(
                        f"/health body status='{health_body.get('status')}' "
                        f"(expected 'healthy')"
                    ),
                )

            # --- TXT metadata check ------------------------------------------
            txt_ok, txt_reason = _check_txt_fields(record.txt)
            if not txt_ok:
                finished = time.monotonic()
                return VerificationResult(
                    record=record,
                    state=ServiceState.DEGRADED,
                    started_at_mono=started,
                    finished_at_mono=finished,
                    wall_clock_utc=wall,
                    http_status=http_status,
                    health_body=health_body,
                    health_latency_ms=health_latency_ms,
                    txt_fields_ok=False,
                    health_status_ok=True,
                    reason=txt_reason,
                )

            # --- Capability cross-check (/diagnose) --------------------------
            diag_ok, diagnose_body, diag_reason = _check_diag_capability(
                record, client, http_timeout
            )

    except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError) as exc:
        finished = time.monotonic()
        return VerificationResult(
            record=record,
            state=ServiceState.UNREACHABLE,
            started_at_mono=started,
            finished_at_mono=finished,
            wall_clock_utc=wall,
            health_latency_ms=round((time.perf_counter() - t_health) * 1000, 2),
            reason=f"HTTP probe failed: {type(exc).__name__}: {exc}",
        )

    finished = time.monotonic()

    # If diag cross-check failed or could not be verified
    if not diag_ok:
        return VerificationResult(
            record=record,
            state=ServiceState.DEGRADED,
            started_at_mono=started,
            finished_at_mono=finished,
            wall_clock_utc=wall,
            http_status=http_status,
            health_body=health_body,
            health_latency_ms=health_latency_ms,
            txt_fields_ok=True,
            health_status_ok=True,
            diag_type_ok=False,
            diagnose_body=diagnose_body,
            reason=diag_reason,
        )

    # All checks passed
    return VerificationResult(
        record=record,
        state=ServiceState.VERIFIED,
        started_at_mono=started,
        finished_at_mono=finished,
        wall_clock_utc=wall,
        http_status=http_status,
        health_body=health_body,
        health_latency_ms=health_latency_ms,
        txt_fields_ok=True,
        health_status_ok=True,
        diag_type_ok=True,
        diagnose_body=diagnose_body,
        reason=diag_reason if diag_reason else "All checks passed",
    )

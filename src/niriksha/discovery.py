"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 1: Discovery Client + HTTP Verifier

Uses python-zeroconf ServiceBrowser to discover _niriksha._tcp.local. services
from real mDNS traffic, then independently verifies the /health endpoint via httpx.

No hard-coded IPs or ports. Discovery is 100% mDNS-driven.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from threading import Event
from typing import Optional

# Ensure stdout is UTF-8 on Windows cp1252 consoles so all output is printable.
if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import httpx
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
from zeroconf._services.info import ServiceInfo


SERVICE_TYPE = "_niriksha._tcp.local."
DISCOVERY_TIMEOUT = 30          # seconds to wait for at least one service
VERIFY_TIMEOUT = 10.0           # httpx request timeout (seconds)


# ---------------------------------------------------------------------------
# ServiceListener implementation
# ---------------------------------------------------------------------------

class NirikshListener(ServiceListener):
    """
    Concrete ServiceListener for _niriksha._tcp.local. services.
    Zeroconf calls add_service / remove_service / update_service as mDNS
    announcements arrive.  No hard-coded addresses are used.
    """

    def __init__(self, zc: Zeroconf, found_event: Event) -> None:
        self._zc = zc
        self._found_event = found_event
        self.discovered: list[dict] = []

    # --- ServiceListener interface ------------------------------------------

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a new service instance is announced on the network."""
        self._resolve_and_record(zc, type_, name, verb="added")

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a service instance is removed from the network."""
        print(f"[DISCOVERY] Service removed: {name}", flush=True)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Called when a service instance updates its TXT record or address."""
        self._resolve_and_record(zc, type_, name, verb="updated")

    # --- Internal helpers ----------------------------------------------------

    def _resolve_and_record(
        self, zc: Zeroconf, type_: str, name: str, verb: str = "found"
    ) -> None:
        """Query full ServiceInfo for *name* and append to self.discovered."""
        info: Optional[ServiceInfo] = zc.get_service_info(type_, name, timeout=5000)
        if info is None:
            print(
                f"[DISCOVERY] Service {verb} but could not resolve info: {name}",
                flush=True,
            )
            return

        # Decode IPv4 addresses
        addresses = [socket.inet_ntoa(addr) for addr in info.addresses]

        # Decode TXT properties (bytes → str)
        txt_props: dict[str, str] = {}
        for k, v in info.properties.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else (v or "")
            txt_props[key] = val

        record = {
            "instance_name": name,
            "service_type": type_,
            "host": info.server,
            "port": info.port,
            "addresses": addresses,
            "txt": txt_props,
        }

        print(f"\n[DISCOVERY] [FOUND] Service {verb} via mDNS:")
        print(f"  Instance : {name}")
        print(f"  Host     : {info.server}")
        print(f"  Port     : {info.port}")
        print(f"  Addresses: {addresses}")
        print(f"  TXT      : {txt_props}", flush=True)

        self.discovered.append(record)
        self._found_event.set()


# ---------------------------------------------------------------------------
# HTTP verifier
# ---------------------------------------------------------------------------

def _verify_health(ip: str, port: int, path: str = "/health") -> None:
    """
    Perform a real HTTP GET to the discovered /health endpoint.
    Reports status, reachability, response time, and a clear verdict.
    """
    url = f"http://{ip}:{port}{path}"
    print(f"\n[VERIFIER] Sending HTTP GET to {url}")

    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=VERIFY_TIMEOUT) as client:
            response = client.get(url)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

        print(f"[VERIFIER] HTTP Status       : {response.status_code}")
        print(f"[VERIFIER] Response Time     : {elapsed_ms} ms")
        reachable = response.status_code == 200
        print(f"[VERIFIER] Reachable         : {reachable}")
        if reachable:
            try:
                print(f"[VERIFIER] Body              : {response.json()}")
            except Exception:
                print(f"[VERIFIER] Body (raw)        : {response.text[:300]}")
        verdict = (
            "[PASS] Operationally Verified"
            if reachable
            else "[FAIL] Unreachable (non-200)"
        )
        print(f"[VERIFIER] Verdict           : {verdict}", flush=True)

    except httpx.ConnectError as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        print(f"[VERIFIER] Connection error after {elapsed_ms} ms: {exc}")
        print("[VERIFIER] Verdict           : [FAIL] Unreachable (connection refused)", flush=True)
    except httpx.TimeoutException as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        print(f"[VERIFIER] Timeout after {elapsed_ms} ms: {exc}")
        print("[VERIFIER] Verdict           : [FAIL] Unreachable (timeout)", flush=True)
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        print(f"[VERIFIER] Unexpected error after {elapsed_ms} ms: {type(exc).__name__}: {exc}")
        print("[VERIFIER] Verdict           : [FAIL] Unreachable (error)", flush=True)


# ---------------------------------------------------------------------------
# Main discovery loop
# ---------------------------------------------------------------------------

def discover(timeout: int = DISCOVERY_TIMEOUT) -> None:
    """Browse for _niriksha._tcp.local. and verify the first discovered instance."""
    print(f"[DISCOVERY] Starting mDNS browse for {SERVICE_TYPE} ...")
    print(f"[DISCOVERY] Will wait up to {timeout} seconds for a service.\n", flush=True)

    zc = Zeroconf()
    found_event = Event()
    listener = NirikshListener(zc, found_event)

    browser = ServiceBrowser(zc, SERVICE_TYPE, listener)

    try:
        found = found_event.wait(timeout=float(timeout))
        if not found:
            print(
                f"\n[DISCOVERY] [NOT FOUND] No {SERVICE_TYPE} service found within {timeout} seconds.",
                flush=True,
            )
            print("  Possible causes:")
            print("  (a) The advertiser is not running - start it first.")
            print("  (b) Windows Firewall is blocking UDP 5353 (mDNS multicast).")
            print("      Fix: netsh advfirewall firewall add rule name=mDNS-UDP-In protocol=UDP dir=in localport=5353 action=allow")
            print("  (c) The active network interface does not support multicast.", flush=True)
            return

        # Verify the first discovered record
        record = listener.discovered[0]
        health_path = record["txt"].get("health", "/health")

        if record["addresses"]:
            ip = record["addresses"][0]
        else:
            # Fall back to resolving the .local. hostname via OS resolver
            host_plain = record["host"].rstrip(".")
            try:
                ip = socket.gethostbyname(host_plain)
                print(f"[DISCOVERY] Resolved {host_plain} -> {ip}", flush=True)
            except socket.gaierror as exc:
                print(
                    f"[DISCOVERY] Could not resolve host {host_plain}: {exc}",
                    flush=True,
                )
                return

        _verify_health(ip, record["port"], health_path)

    finally:
        browser.cancel()
        zc.close()
        print("\n[DISCOVERY] Browser closed.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NIRIKSHA Discovery Client")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DISCOVERY_TIMEOUT,
        help=f"Seconds to wait for a service announcement (default: {DISCOVERY_TIMEOUT})",
    )
    args = parser.parse_args()
    discover(timeout=args.timeout)

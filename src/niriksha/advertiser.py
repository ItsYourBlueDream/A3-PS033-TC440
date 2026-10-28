"""
NIRIKSHA — DNS-SD Evidence-Based Verification Platform
Phase 1: DNS-SD Advertiser

Registers the NIRIKSHA diagnostic service via mDNS/DNS-SD using python-zeroconf.
Service type : _niriksha._tcp.local.
Default port : 8001

Selects an active, non-loopback IPv4 address automatically from the OS.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import time

from zeroconf import ServiceInfo, Zeroconf


SERVICE_TYPE = "_niriksha._tcp.local."
INSTANCE_NAME = "niriksha-diagnostic"
DEFAULT_PORT = 8001


def _get_local_ipv4() -> str:
    """
    Return the preferred outbound IPv4 address by attempting a UDP connect to
    a public address (no packet is actually sent).  Falls back to socket.gethostbyname.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))          # no real traffic — just routing table lookup
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def _build_service_info(port: int, ip: str) -> ServiceInfo:
    """Build a zeroconf ServiceInfo record for the diagnostic service."""
    hostname = socket.gethostname()
    full_name = f"{INSTANCE_NAME}.{SERVICE_TYPE}"

    properties = {
        "diag": "dns",
        "txtvers": "1",
        "health": "/health",
        "run": "/diagnose",
    }

    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=full_name,
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties=properties,
        server=f"{hostname}.local.",
    )


def advertise(port: int = DEFAULT_PORT) -> None:
    """Register the service and block until SIGINT / SIGTERM."""
    ip = _get_local_ipv4()
    info = _build_service_info(port, ip)

    zc = Zeroconf()
    zc.register_service(info)

    print(f"[ADVERTISER] Service registered:")
    print(f"  Name    : {info.name}")
    print(f"  Type    : {SERVICE_TYPE}")
    print(f"  Address : {ip}")
    print(f"  Port    : {port}")
    print(f"  TXT     : {dict(info.properties)}")
    print("[ADVERTISER] Advertising ... Press Ctrl-C to stop.\n", flush=True)

    stop_flag = [False]

    def _shutdown(signum, frame):  # noqa: ANN001
        stop_flag[0] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop_flag[0]:
            time.sleep(0.5)
    finally:
        print("\n[ADVERTISER] Unregistering service …", flush=True)
        zc.unregister_service(info)
        zc.close()
        print("[ADVERTISER] Done.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NIRIKSHA DNS-SD Advertiser")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port the diagnostic HTTP service is listening on (default: {DEFAULT_PORT})"
    )
    args = parser.parse_args()
    advertise(port=args.port)

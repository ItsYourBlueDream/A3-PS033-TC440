"""
NIRIKSHA - DNS-SD Evidence-Based Verification Platform
Phase 1 + 2A + 2B: DNS-SD Advertiser

Registers the NIRIKSHA diagnostic service via mDNS/DNS-SD using python-zeroconf.
Service type : _niriksha._tcp.local.
Default port : 8001

Phase 2A fault-injection CLI flags:
  --inject-port PORT   Advertise a wrong SRV port (service still listens on 8001).
  --inject-diag VALUE  Advertise a wrong TXT diag value (e.g. "http" instead of "dns").

Phase 2B endpoint migration CLI flags:
  --migrate-to PORT    After a delay, migrate advertised endpoint to this new port.
  --migrate-delay SEC  Seconds to wait before migrating (default: 10.0).
  --migrate-mode MODE  Migration strategy: 'update' (in-place SRV update) or
                       're-register' (unregister old, register new). Default: update.

These flags are for controlled demonstrations only. Defaults preserve Phase 1 behavior.
Selects an active, non-loopback IPv4 address automatically from the OS.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import time
from pathlib import Path

from zeroconf import ServiceInfo, Zeroconf


SERVICE_TYPE = "_niriksha._tcp.local."
INSTANCE_NAME = "niriksha-diagnostic"
DEFAULT_PORT = 8001


def _get_local_ipv4() -> str:
    """
    Return the preferred outbound IPv4 address by attempting a UDP connect to
    a public address (no packet is actually sent). Falls back to gethostbyname.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def _build_service_info(
    port: int,
    ip: str,
    inject_diag: str | None = None,
) -> ServiceInfo:
    """
    Build a zeroconf ServiceInfo record.

    inject_diag: if set, override the TXT 'diag' field with this value.
    """
    hostname = socket.gethostname()
    full_name = f"{INSTANCE_NAME}.{SERVICE_TYPE}"

    properties = {
        "diag":    inject_diag if inject_diag is not None else "dns",
        "txtvers": "1",
        "health":  "/health",
        "run":     "/diagnose",
    }

    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=full_name,
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties=properties,
        server=f"{hostname}.local.",
    )


def advertise(
    port: int = DEFAULT_PORT,
    inject_port: int | None = None,
    inject_diag: str | None = None,
    migrate_to: int | None = None,
    migrate_delay: float = 10.0,
    migrate_mode: str = "update",
    migrate_trigger_file: str | None = None,
) -> None:
    """
    Register the service and block until SIGINT / SIGTERM.

    inject_port: advertise this port instead of the real one (fault scenario B/C).
    inject_diag: advertise this TXT diag value instead of 'dns' (fault scenario D).
    migrate_to:  after migrate_delay or trigger file, migrate advertised port to this value (Phase 2B).
    migrate_trigger_file: if provided, migrate when this file exists on disk.
    """
    ip = _get_local_ipv4()
    advertised_port = inject_port if inject_port is not None else port
    info = _build_service_info(advertised_port, ip, inject_diag=inject_diag)

    zc = Zeroconf()
    zc.register_service(info)

    print("[ADVERTISER] Service registered:")
    print(f"  Name           : {info.name}")
    print(f"  Type           : {SERVICE_TYPE}")
    print(f"  Address        : {ip}")
    print(f"  Advertised port: {advertised_port}", end="")
    if inject_port is not None:
        print(f"  [INJECTED - real service on {port}]", end="")
    print()
    print(f"  TXT            : {dict(info.properties)}", end="")
    if inject_diag is not None:
        print(f"  [INJECTED diag='{inject_diag}']", end="")
    print()
    if migrate_to is not None:
        if migrate_trigger_file:
            print(
                f"  [MIGRATION CONFIGURED] Will migrate to port {migrate_to} "
                f"when trigger file exists: {migrate_trigger_file} (mode: {migrate_mode})",
                flush=True,
            )
        else:
            print(
                f"  [MIGRATION SCHEDULED] Will migrate to port {migrate_to} "
                f"in {migrate_delay}s (mode: {migrate_mode})",
                flush=True,
            )
    print("[ADVERTISER] Advertising ... Press Ctrl-C to stop.\n", flush=True)

    stop_flag = [False]

    def _shutdown(signum, frame):  # noqa: ANN001
        stop_flag[0] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    migrated = False
    start_mono = time.monotonic()

    try:
        while not stop_flag[0]:
            time.sleep(0.5)
            if migrate_to is not None and not migrated:
                should_migrate = False
                if migrate_trigger_file:
                    if Path(migrate_trigger_file).is_file():
                        should_migrate = True
                elif (time.monotonic() - start_mono) >= migrate_delay:
                    should_migrate = True

                if should_migrate:
                    print(
                        f"\n[ADVERTISER] Migrating service to port {migrate_to} "
                        f"(mode: {migrate_mode}) ...",
                        flush=True,
                    )
                    new_info = _build_service_info(migrate_to, ip, inject_diag=inject_diag)
                    if migrate_mode == "update":
                        zc.update_service(new_info)
                    else:
                        zc.unregister_service(info)
                        time.sleep(0.5)
                        zc.register_service(new_info)
                    info = new_info
                    migrated = True
                    print(
                        f"[ADVERTISER] Migration complete! Now advertising on port {migrate_to}.\n",
                        flush=True,
                    )
    finally:
        print("\n[ADVERTISER] Unregistering service ...", flush=True)
        zc.unregister_service(info)
        zc.close()
        print("[ADVERTISER] Done.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NIRIKSHA DNS-SD Advertiser (Phase 1 + 2A + 2B)"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port the real HTTP service listens on (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--inject-port", type=int, default=None, dest="inject_port",
        help=(
            "Phase 2A fault injection: advertise this WRONG port in the SRV record. "
            "The real service continues on --port. "
            "Demonstrates scenario C (wrong advertised port -> UNREACHABLE)."
        ),
    )
    parser.add_argument(
        "--inject-diag", type=str, default=None, dest="inject_diag",
        help=(
            "Phase 2A fault injection: advertise this WRONG TXT diag value. "
            "Demonstrates scenario D (TXT diag mismatch -> DEGRADED)."
        ),
    )
    parser.add_argument(
        "--migrate-to", type=int, default=None, dest="migrate_to",
        help=(
            "Phase 2B endpoint migration: after --migrate-delay or trigger file, migrate advertised port to this port."
        ),
    )
    parser.add_argument(
        "--migrate-delay", type=float, default=10.0, dest="migrate_delay",
        help="Seconds to wait before performing migration (default: 10.0)",
    )
    parser.add_argument(
        "--migrate-mode", type=str, choices=["update", "re-register"], default="update", dest="migrate_mode",
        help="Migration method: 'update' (in-place SRV update) or 're-register' (unregister + register). Default: update.",
    )
    parser.add_argument(
        "--migrate-trigger-file", type=str, default=None, dest="migrate_trigger_file",
        help="Path to trigger file. When present, perform migration to --migrate-to immediately.",
    )
    args = parser.parse_args()
    advertise(
        port=args.port,
        inject_port=args.inject_port,
        inject_diag=args.inject_diag,
        migrate_to=args.migrate_to,
        migrate_delay=args.migrate_delay,
        migrate_mode=args.migrate_mode,
        migrate_trigger_file=args.migrate_trigger_file,
    )

"""
NIRIKSHA - Phase 2A: Monitor CLI entry point.

Usage:
  python -m niriksha.monitor_runner [options]

Options:
  --interval SECONDS   Poll interval (default: 10)
  --timeout  SECONDS   HTTP probe timeout (default: 8)
  --log FILE           JSONL event log path (default: niriksha_events.jsonl)
  --discovery-wait SEC Seconds to wait for initial mDNS discovery (default: 15)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from niriksha.monitor import Monitor


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NIRIKSHA Continuous Monitor (Phase 2A)"
    )
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="Poll interval in seconds between verification cycles (default: 10)"
    )
    parser.add_argument(
        "--timeout", type=float, default=8.0,
        help="HTTP probe timeout in seconds (default: 8)"
    )
    parser.add_argument(
        "--log", type=str, default="niriksha_events.jsonl",
        help="Path to the JSONL event log file (default: niriksha_events.jsonl)"
    )
    parser.add_argument(
        "--discovery-wait", type=float, default=15.0, dest="discovery_wait",
        help="Seconds to wait for initial mDNS discovery before polling (default: 15)"
    )
    args = parser.parse_args()

    monitor = Monitor(
        poll_interval=args.interval,
        http_timeout=args.timeout,
        log_path=Path(args.log),
        discovery_timeout=args.discovery_wait,
    )
    monitor.run()


if __name__ == "__main__":
    main()

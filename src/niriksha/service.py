"""
NIRIKSHA — DNS-SD Evidence-Based Verification Platform
Phase 1: Diagnostic HTTP Service

Exposes:
  GET /health   — real HTTP 200 health check
  GET /diagnose — real DNS lookup diagnostic
"""

from __future__ import annotations

import socket
import time

import dns.resolver
import dns.exception
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="NIRIKSHA Diagnostic Service", version="1.0.0")

_START_TIME = time.time()


@app.get("/health")
async def health() -> JSONResponse:
    """Return a real HTTP 200 JSON health payload."""
    uptime_seconds = round(time.time() - _START_TIME, 2)
    hostname = socket.gethostname()
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "niriksha-diagnostic",
            "hostname": hostname,
            "uptime_seconds": uptime_seconds,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


@app.get("/diagnose")
async def diagnose() -> JSONResponse:
    """
    Perform a real DNS A-record lookup for a well-known public domain
    and return the result including timing and success status.
    """
    target = "dns.google."          # well-known, reliable, globally reachable
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5.0         # seconds

    t0 = time.perf_counter()
    try:
        answers = resolver.resolve(target, "A")
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        resolved_addrs = [rdata.address for rdata in answers]
        return JSONResponse(
            status_code=200,
            content={
                "diagnostic": "dns_lookup",
                "query": target,
                "query_type": "A",
                "success": True,
                "resolved_addresses": resolved_addrs,
                "response_time_ms": elapsed_ms,
                "nameservers_used": list(resolver.nameservers),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except dns.exception.DNSException as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return JSONResponse(
            status_code=200,
            content={
                "diagnostic": "dns_lookup",
                "query": target,
                "query_type": "A",
                "success": False,
                "error": str(exc),
                "response_time_ms": elapsed_ms,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

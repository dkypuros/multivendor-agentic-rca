#!/usr/bin/env python3
"""Smoke-test a deployed sandbox through its Route, exactly as the browser drives it.

    python3 tests/smoke_route.py https://ran-sandbox-multivendor-rca.apps.<cluster-domain>

Start -> UE attach; Inject -> O-DU alarm; RCA; Heal; RCA; Stop. Leaves the RAN slice stopped
and PTP healed. Exit 0 = all checks passed. Stdlib only; TLS verification is skipped because
lab routers often use self-signed certificates.
"""
import json
import ssl
import sys
import time
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else sys.exit(__doc__)
CTX = ssl._create_unverified_context()
failures = 0


def call(method, path, timeout=90):
    req = urllib.request.Request(BASE + path, data=b"{}" if method == "POST" else None, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read() or b"{}")


def check(label, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""), flush=True)


def settle(timeout):
    deadline = time.time() + timeout
    s = call("GET", "/api/ran/status")
    while s["busy"] and time.time() < deadline:
        time.sleep(3)
        s = call("GET", "/api/ran/status")
    return s


def rca():
    r = call("POST", "/api/ran/trigger-rca")
    return r, r.get("tmf688Event", {}).get("event", {})


s = call("GET", "/api/ran/status")
print("status:")
check("sandbox answers; PTP bridge reachable", s["ptp"] is not None, s["errors"])

try:
    print("start:")
    call("POST", "/api/ran/start")
    s = settle(300)
    ue = s["ue"] or {}
    check("slice pod ready", "Ready" in (s["pod"] or ""), s["pod"])
    check("O-DU cell ACTIVE", (s["o1"] or {}).get("cellState") == "ACTIVE", s["errors"])
    check("UE registered", ue.get("registered") is True)
    check("PDU session + 3/3 GTP-U echoes", bool(ue.get("pduAddress")) and ue.get("echo") == "3/3",
          f"{ue.get('pduAddress')} {ue.get('echo')}")

    print("inject fault:")
    call("POST", "/api/ran/inject-ptp-fault")
    time.sleep(3)
    s = call("GET", "/api/ran/status")
    check("PTP FREERUN", (s["ptp"] or {}).get("port_state") == "FREERUN")
    check("O-DU cell UNAVAILABLE + CellUnavailable alarm",
          (s["o1"] or {}).get("cellState") == "UNAVAILABLE" and bool(s["alarms"]))

    print("RCA during fault:")
    r, ev = rca()
    check("NEP answered", "error" not in r, r.get("error", ""))
    for t in ev.get("evidence", []):
        print(f"        {t['plane']:9s} signals={t['signals']}  {t['evidence'][:90]}")
    check("diagnosis OC-TimingDegraded; emulated NIC not counted -> HOLD",
          ev.get("faultClass") == "OC-TimingDegraded" and ev.get("decision", "").startswith("HOLD"),
          f"{ev.get('faultClass')} {ev.get('corroboration')} {ev.get('decision')}")
    check("narrative present", bool((r.get("llmSynthesis") or {}).get("summary")), (r.get("llmSynthesis") or {}).get("model"))
finally:
    # Always leave the lab clean (PTP healed, slice stopped), even if a check above blew up.
    print("heal:")
    call("POST", "/api/ran/heal-ptp")
    time.sleep(3)
    s = call("GET", "/api/ran/status")
    check("PTP LOCKED, cell ACTIVE, no alarm",
          (s["ptp"] or {}).get("port_state") == "LOCKED" and (s["o1"] or {}).get("cellState") == "ACTIVE"
          and not s["alarms"])
    try:
        r, ev = rca()
        check("RCA after heal finds no fault", ev.get("faultClass") is None, ev.get("decision"))
    except Exception as exc:                     # never skip the Stop below
        check("RCA after heal finds no fault", False, repr(exc))

    print("stop:")
    call("POST", "/api/ran/stop")
    s = settle(120)
    check("slice stopped", s["pod"] == "none", s["pod"])

print("ALL GREEN" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)

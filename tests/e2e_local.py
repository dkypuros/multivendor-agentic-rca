#!/usr/bin/env python3
"""Local end-to-end test of the whole use case, no cluster needed.

Starts every component as a real process on high ports -- PTP bridge, CAPIF, the four MCP
servers, the MCP gateway, the NEP orchestrator and the sandbox controller -- plus a stub
Kubernetes API (tests/stub_kube.py) whose "scale" launches the real RAN slice. Then drives
the sandbox HTTP API exactly like the browser does and checks what comes back:

  start -> UE registered + PDU session + 3/3 GTP-U echoes, cell ACTIVE
  inject -> PTP FREERUN, O-DU cell UNAVAILABLE + CellUnavailable alarm
  RCA    -> OC-TimingDegraded; RAN + PTP planes corroborate (2/3), the emulated NIC agrees but is
            not counted -> HOLD; CAPIF token issued once and reused
  heal   -> cell ACTIVE, alarm cleared, PTP LOCKED; RCA finds no fault -> NO ACTION
  stop   -> slice gone

Needs python3 >= 3.11 and PyYAML (the MCP gateway reads its policy with it).
Exit 0 = all checks passed.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PY = sys.executable

SLICE_OFFSET = 20000                 # slice NFs on 27xxx (O-DU O1 on 27010, Uu on 27011/udp)
ODU = f"http://127.0.0.1:{7010 + SLICE_OFFSET}"
PTP_PORT, CAPIF_OFFSET, KUBE_PORT = 27091, 23000, 28999
CAPIF = f"http://127.0.0.1:{7027 + CAPIF_OFFSET}"
MCP = {"ocloud": 28850, "ran": 28851, "intel": 28852, "redhat": 28853}
GATEWAY_PORT, NEP_PORT, SANDBOX_PORT = 28880, 27095, 27098
SANDBOX = f"http://127.0.0.1:{SANDBOX_PORT}"

procs = []
failures = 0


def spawn(name, args, env=None, cwd=SRC):
    log = open(Path(tempfile.gettempdir()) / f"mvrca-{name}.log", "w")
    p = subprocess.Popen(args, cwd=cwd, env=dict(os.environ, PYTHONUNBUFFERED="1", **(env or {})),
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append((name, p))
    return p


def http(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def wait_http(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True          # it answered
        except Exception:
            time.sleep(0.3)
    return False


def check(label, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""), flush=True)


def status():
    return http("GET", SANDBOX + "/api/ran/status")


def wait_idle(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = status()
        if not s["busy"]:
            return s
        time.sleep(2)
    return status()


def main():
    policy = Path(tempfile.gettempdir()) / "mvrca-gateway-policy.yaml"
    text = (REPO / "deploy/openshift/gateway-policy.yaml").read_text()
    for name, port in MCP.items():
        text = text.replace(f"http://mcp-{name}:{8850 + list(MCP).index(name)}", f"http://127.0.0.1:{port}")
    policy.write_text(text)

    spawn("ptp-bridge", [PY, "services/smo/ptp_bridge.py"], {"PTP_BRIDGE_PORT": str(PTP_PORT)})
    spawn("capif", [PY, "services/core/capif/capif.py"], {"TELCO_PORT_OFFSET": str(CAPIF_OFFSET)})
    mcp_env = {"ran": {"RAN_O1_URL": ODU}, "redhat": {"TELCO_PTP_URL": f"http://127.0.0.1:{PTP_PORT}"}}
    for name, port in MCP.items():
        spawn(f"mcp-{name}", [PY, "-m", "extensions.sheldon.agentic.mcp_http", "--server", name, "--port", str(port)],
              mcp_env.get(name))
    spawn("mcp-gateway", [PY, "-m", "extensions.sheldon.agentic.gateway", "--host", "127.0.0.1",
                          "--port", str(GATEWAY_PORT), "--policy", str(policy)])
    spawn("nep", [PY, "services/orchestrator/nep_orchestrator.py"], {
        "PORT": str(NEP_PORT), "PTP_BRIDGE_URL": f"http://127.0.0.1:{PTP_PORT}", "ODU_URL": ODU,
        "CAPIF_URL": CAPIF, "MCP_GATEWAY_URL": f"http://127.0.0.1:{GATEWAY_PORT}",
        "AI_GATEWAY_URL": "http://127.0.0.1:9", "SMO_URL": "http://127.0.0.1:9"})
    spawn("stub-kube", [PY, str(REPO / "tests/stub_kube.py"), str(SRC), str(KUBE_PORT), str(SLICE_OFFSET)])
    spawn("sandbox", [PY, "services/ran/sandbox_controller.py"], {
        "PORT": str(SANDBOX_PORT), "K8S_API_URL": f"http://127.0.0.1:{KUBE_PORT}",
        "K8S_SA_DIR": "/nonexistent", "SLICE_NAMESPACE": "local", "ODU_URL": ODU,
        "SLICE_HOST": "127.0.0.1", "SLICE_PORT_OFFSET": str(SLICE_OFFSET),
        "PTP_BRIDGE_URL": f"http://127.0.0.1:{PTP_PORT}", "NEP_URL": f"http://127.0.0.1:{NEP_PORT}"})

    ups = [f"http://127.0.0.1:{PTP_PORT}/ptp", CAPIF + "/", f"http://127.0.0.1:{GATEWAY_PORT}/",
           f"http://127.0.0.1:{NEP_PORT}/nep/health", SANDBOX + "/healthz"]
    ups += [f"http://127.0.0.1:{p}/" for p in MCP.values()]
    print("components:")
    for u in ups:
        check(f"up {u}", wait_http(u))
    http("POST", f"http://127.0.0.1:{PTP_PORT}/ptp/heal", {})

    print("idle:")
    s = status()
    check("slice not running before Start", s["pod"] == "none" and s["o1"] is None, s["pod"])

    print("start:")
    http("POST", SANDBOX + "/api/ran/start")
    s = wait_idle(180)
    ue = s["ue"] or {}
    check("slice pod ready", "Ready" in (s["pod"] or ""), s["pod"])
    check("O-DU cell ACTIVE, RU CONNECTED", (s["o1"] or {}).get("cellState") == "ACTIVE"
          and s["o1"].get("ru") == "CONNECTED")
    check("UE registered (5G-AKA)", ue.get("registered") is True)
    check("PDU session address assigned", bool(ue.get("pduAddress")), ue.get("pduAddress"))
    check("3/3 echoes over GTP-U", ue.get("echo") == "3/3", ue.get("echo"))

    print("inject fault:")
    http("POST", SANDBOX + "/api/ran/inject-ptp-fault")
    time.sleep(2.5)
    s = status()
    check("PTP bridge FREERUN", s["ptp"]["port_state"] == "FREERUN", s["ptp"]["phc_offset_ns"])
    check("O-DU cell UNAVAILABLE (admin LOCKED)", s["o1"]["cellState"] == "UNAVAILABLE")
    check("O-DU raised CellUnavailable/lossOfRealTimeSynchronization",
          any(a["probableCause"] == "lossOfRealTimeSynchronization" for a in s["alarms"]))

    print("RCA during fault:")
    rca = http("POST", SANDBOX + "/api/ran/trigger-rca")
    ev = rca.get("tmf688Event", {}).get("event", {})
    planes = {t["plane"]: t for t in ev.get("evidence", [])}
    check("no error from NEP", "error" not in rca, rca.get("error", ""))
    check("CAPIF token obtained", next(sp for sp in rca["spans"] if sp["spanId"] == "span-2-capif-authz")["status"] == "OK")
    check("RAN plane testifies from O-DU O1 alarm", "du_sync_loss_alarm" in planes["ran"]["signals"],
          planes["ran"]["evidence"])
    check("PTP plane reports offset exceeded", "ptp_offset_exceeded" in planes["platform"]["signals"])
    check("NIC plane answers, labeled emulated", planes["hardware"]["emulated"] and planes["hardware"]["signals"])
    cluster = planes["cluster"]
    check("O-Cloud plane reports what it saw (no ACM here), nothing invented",
          not cluster["signals"] and ("unavailable" in cluster["evidence"] or "no OCM/ACM" in cluster["evidence"]),
          cluster["evidence"][:70])
    router = next(sp for sp in rca["spans"] if sp["spanId"] == "span-4-deterministic-router")["attributes"]
    check("diagnosis OC-TimingDegraded", ev.get("faultClass") == "OC-TimingDegraded", ev.get("faultClass"))
    check("emulated NIC not counted: 2/3 real planes -> HOLD",
          ev.get("corroboration") == "2/3" and ev.get("decision", "").startswith("HOLD")
          and router["policy.emulatedPlanesNotCounted"] == ["hardware"],
          f"{ev.get('corroboration')} {ev.get('decision')}")
    check("reply is this run's own trace", rca.get("traceId") == ev.get("traceId"))
    check("LLM narrative labeled as template when no LLM",
          rca["llmSynthesis"]["model"].startswith("none"), rca["llmSynthesis"]["summary"][:60])

    print("heal:")
    http("POST", SANDBOX + "/api/ran/heal-ptp")
    time.sleep(2.5)
    s = status()
    check("PTP LOCKED", s["ptp"]["port_state"] == "LOCKED")
    check("cell ACTIVE, alarm cleared", s["o1"]["cellState"] == "ACTIVE" and not s["alarms"])
    rca = http("POST", SANDBOX + "/api/ran/trigger-rca")
    ev = rca["tmf688Event"]["event"]
    check("RCA after heal: no fault -> NO ACTION", ev["faultClass"] is None and ev["decision"].startswith("NO ACTION"),
          ev["decision"])
    capif = next(sp for sp in rca["spans"] if sp["spanId"] == "span-2-capif-authz")["attributes"]
    check("CAPIF token reused on the second RCA (no re-onboarding)", capif.get("capif.tokenSource") == "cached")

    print("logs:")
    logs = http("GET", SANDBOX + "/api/ran/logs")
    sources = {l["source"] for l in logs}
    check("log stream carries controller + UE + real NF lines", {"K8S", "UE-SIM", "amf", "odu"} <= sources,
          ",".join(sorted(sources))[:80])
    check("no perception_emit_failed noise", not any("perception_emit_failed" in l["message"] for l in logs))

    print("stop:")
    http("POST", SANDBOX + "/api/ran/stop")
    s = wait_idle(60)
    check("slice gone", s["pod"] == "none" and s["o1"] is None, s["pod"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        failures += 1
        print(f"  FAIL  harness error: {exc!r}")
    finally:
        for name, p in reversed(procs):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _, p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
    print("ALL GREEN" if not failures else f"{failures} FAILED (component logs: {tempfile.gettempdir()}/mvrca-*.log)")
    sys.exit(1 if failures else 0)

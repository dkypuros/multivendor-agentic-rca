#!/usr/bin/env python3
"""
launch_slice.py — the PID-1 supervisor for the deployed co-located owned stack slice.

ONE image, ONE pod, the WHOLE current slice co-located (issue #61 tracks the true
per-NF microservices split; until then every NF hardcodes 127.0.0.1 in its NRF
profile, so they must share a network namespace — i.e. one pod). This process is
that pod's PID 1: it boots the slice in dependency order, gates each tier ready
before the next starts, forwards SIGTERM to every child, and reaps them — stdlib
only, like the rest of the owned core.

SOURCE OF TRUTH — NOT a hand-maintained (and therefore stale-prone) list. The set
of network functions, their scripts/ports, and the dependency edges are imported
LIVE from tools/stackctl.py (services_for / deps_for / start_service / gate_one),
the same inventory the acceptance-spec runners and the `stackctl` operator tool
use. Add an NF to stackctl and it deploys here on the next image build; nothing to
keep in sync.

PROFILE: TELCO_SLICE_PROFILE (default "full") selects the stackctl profile. "full"
is the whole current stack MINUS the P9 agents (which need the third-party
oran-agent-harness + PyYAML/jsonschema and so do not belong in the stdlib-only
image): the 5G core NRF/UDM/AMF/SMF/UPF, every harvested SBA NF (AUSF/PCF/NSSF/
NEF/CHF/UDR/BSF/SCP/SEPP/UDSF/NSSAAF/EIR/CAPIF/LMF/GMLC/TSCTSF/NWDAF/N3IWF), the
IMS voice subsystem (P-CSCF/I-CSCF/S-CSCF/IMS-HSS/MRF), the 4G EPC (MME/SGW/PGW/
HSS4G), the split RAN (O-CU-CP/O-CU-UP/O-DU/O-RU), edge, both RICs, SMO, gridctl,
NSMF, and TMF OSS/BSS(+billing).

BIND / TRACING: every NF reads its port from domain/netconfig and binds
TELCO_BIND (the ConfigMap sets 0.0.0.0) so the pod's Services are reachable
cluster-wide; TELCO_HOST=127.0.0.1 keeps co-located NFs talking on loopback.
TELCO_OTLP_ENDPOINT is inherited from the pod env into every child, so the
distributed tracer exports OTLP/HTTP spans to Jaeger.

NO internal auto-restart, by design: killing oru.py is the standing
fault-injection hook (O-RU DOWN -> SMO O1 -> TMF642 alarm -> P9 heal proposal); an
internal supervisor would race it. Pod-level self-heal is Kubernetes' job (the
liveness probe on the NRF restarts the whole pod), which is the correct blast
radius for a co-located slice.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# The image WORKDIR is /app and this file is /app/deploy/docker/launch_slice.py,
# so parents[2] is the repo/source root. Put it on the path so `import tools...`
# and the NF scripts resolve exactly as they do from a source checkout.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tools.stackctl as stackctl  # noqa: E402  (the single source of truth)

PROFILE = (os.environ.get("TELCO_SLICE_PROFILE") or "full").strip()


def _start_nf(svc, harness_path=None):
    """Start ONE NF as a child process whose stdout/stderr are INHERITED (they go to this
    PID-1's stdout = the container/pod stdout).

    This is the one place launch_slice deliberately does NOT reuse stackctl.start_service:
    the operator tool redirects each NF's output to a per-service file under .stack/logs/,
    which is right for an interactive workstation but WRONG in a pod — Grafana Alloy captures
    the pod's stdout, so NF JSON logs (and, in TELCO_TRACE=log mode, spans) must stream there,
    not into a file no one collects. start_new_session=True keeps each child a process-group
    leader so stackctl._kill_proc can TERM/KILL the whole group on shutdown."""
    env = dict(os.environ)
    if svc.get("needs_harness") and harness_path:
        env["HARNESS_PATH"] = str(harness_path)
    return subprocess.Popen([sys.executable, str(ROOT / svc["script"])],
                            stdout=None, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)


# gate_one() restarts a service via stackctl.start_service on a failed ready-gate; point that
# name at our inherit-stdout starter so retries log to the pod stdout too.
stackctl.start_service = _start_nf

# name -> Popen of every child we start (their own sessions, via stackctl.start_service).
_children = {}
_shutting_down = False


def _log(event, **kv):
    """Structured, unbuffered line so the launcher's own events land in stdout/Loki
    alongside the NFs' JSON logs. Deliberately dependency-free."""
    parts = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"[launch_slice] {event} {parts}".rstrip(), flush=True)


def _shutdown(signum, _frame):
    """Forward the pod's SIGTERM to every child process group, then exit. Children are
    started with start_new_session=True (stackctl.start_service), so each is a process
    group leader; stackctl._kill_proc TERMs the group, waits, and KILLs stragglers."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    _log("signal", signum=signum, action="terminating children", count=len(_children))
    for name, proc in list(_children.items()):
        stackctl._kill_proc(proc)
    _log("shutdown", status="complete")
    sys.exit(0)


def boot():
    """Tiered, dependency-gated, bounded-concurrency startup — the same algorithm the
    `stackctl up` operator command uses, but as a long-lived supervisor: it does NOT
    exit(1) on a straggler (a container should keep the healthy NFs — and the NRF
    liveness probe — up so the pod stays inspectable and self-heals at pod scope)."""
    otlp = os.environ.get("TELCO_OTLP_ENDPOINT") or "(unset)"
    _log("boot", profile=PROFILE, bind=os.environ.get("TELCO_BIND", "?"),
         host=os.environ.get("TELCO_HOST", "?"), otlp=otlp,
         concurrency=stackctl.CONCURRENCY)

    wanted = stackctl.services_for(PROFILE, stubs=False)
    if not wanted:
        _log("boot", error=f"no services for profile '{PROFILE}'")
        sys.exit(2)
    _log("plan", services=len(wanted))

    present = {s["name"] for s in wanted}
    by_name = {s["name"]: s for s in wanted}
    ready, failed, blocked = set(), set(), set()
    pending = set(present)

    while pending:
        newly_blocked = {n for n in pending
                         if stackctl.deps_for(by_name[n], present) & (failed | blocked)}
        if newly_blocked:
            blocked |= newly_blocked
            pending -= newly_blocked
            for n in sorted(newly_blocked):
                _log("BLOCKED", nf=n, reason="dependency failed")
            continue
        runnable = sorted(n for n in pending
                          if stackctl.deps_for(by_name[n], present) <= ready)
        if not runnable:
            _log("stall", pending=sorted(pending))
            break
        batch = runnable[:stackctl.CONCURRENCY]
        launched = {n: _start_nf(by_name[n]) for n in batch}
        for name in batch:
            svc = by_name[name]
            proc, ok, attempts = stackctl.gate_one(svc, launched[name], harness_path=None)
            _children[name] = proc
            retry = f" retry={attempts - 1}" if attempts > 1 else ""
            _log("READY" if ok else "FAIL", nf=name, pid=proc.pid,
                 port=svc["port"] or "-", extra=retry.strip())
            (ready if ok else failed).add(name)
            pending.discard(name)

    _log("boot", status="done", ready=len(ready), failed=len(failed),
         blocked=len(blocked), total=len(present))
    if failed or blocked:
        _log("boot", note="NF(s) not ready: " + ",".join(sorted(failed | blocked))
             + " — pod stays up (NRF liveness governs self-heal)")


def watch():
    """Reap children (we are PID 1) and log any exit. No restart — pod-level self-heal
    and the O-RU fault hook own that. Blocks until SIGTERM calls _shutdown()."""
    while True:
        for name, proc in list(_children.items()):
            rc = proc.poll()
            if rc is not None:
                _log("child_exit", nf=name, pid=proc.pid, rc=rc)
                del _children[name]
        time.sleep(1.0)


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    boot()
    watch()


if __name__ == "__main__":
    main()

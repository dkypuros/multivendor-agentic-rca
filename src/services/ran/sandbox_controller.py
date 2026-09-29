"""
Cloud RAN AI Sandbox Controller & Interactive NOC Console.

Every button drives a real, running thing; nothing on this page is scripted text:

  Start RAN   scales the `ran-slice` Deployment (launch_slice.py, profile "ran": the owned
              5G core + split RAN O-CU-CP/O-CU-UP/O-DU/O-RU co-located in ONE pod, the same
              slice the specs run) 0 -> 1, waits for the O-DU to report cellState=ACTIVE over
              O1, then runs services/ue_sim/ue_sim.py as a child process: RRC setup, real
              5G-AKA (MILENAGE), security mode, registration, PDU session and user-plane
              echoes over the GTP-U tunnel. Its stdout is streamed verbatim.
  Stop RAN    scales the slice 1 -> 0: the pod and every NF process in it go away.
  Inject/Heal POST the PTP bridge and set the O-DU's O1 administrativeState
              LOCKED/UNLOCKED (TS 28.541) as the protective carrier shutdown / resume, so the
              cell state and the TS 28.532 alarm on this page come from the O-DU itself.
  RCA         forwards to the NEP orchestrator agent.

Status panels read the Deployment (Kubernetes API), O-DU /o1/status + /o1/alarms and the
PTP bridge /ptp on every poll. The log stream merges this controller's own action log with
the slice pod's real stdout (Kubernetes pods/log), so what you read is what ran.

Why a co-located slice and not the per-NF pods: every NF still advertises 127.0.0.1 to the
NRF and dials its peers on loopback (upstream issue #61), so one-pod-per-NF Deployments
cannot reach each other. launch_slice.py is the supported way to run a connected stack.

Stdlib only. In-cluster it authenticates with its ServiceAccount token (see
deploy/openshift/ran-sandbox.yaml for the Role).
"""
import http.server
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PORT = int(os.environ.get("PORT", "7098"))
PTP_BRIDGE_URL = os.environ.get("PTP_BRIDGE_URL", "http://ptp-bridge:7091").rstrip("/")
NEP_URL = os.environ.get("NEP_URL", "http://nep-orchestrator:7095").rstrip("/")

SLICE_DEPLOYMENT = os.environ.get("SLICE_DEPLOYMENT", "ran-slice")
SLICE_HOST = os.environ.get("SLICE_HOST", "ran-slice")
ODU_URL = os.environ.get("ODU_URL", f"http://{SLICE_HOST}:7010").rstrip("/")
SLICE_READY_TIMEOUT = float(os.environ.get("SLICE_READY_TIMEOUT", "240"))
RCA_TIMEOUT = float(os.environ.get("RCA_TIMEOUT", "150"))       # keep below the Route timeout (180s)
UE_TIMEOUT = float(os.environ.get("UE_TIMEOUT", "120"))         # hard deadline for one ue_sim run

UE_SUPI = os.environ.get("UE_SUPI", "imsi-001010000000001")
UE_K = os.environ.get("UE_K", "465b5ce8b199b49faa5f0a2ee238a6bc")   # the seeded lab subscriber
UE_ECHOES = os.environ.get("UE_ECHOES", "3")
SLICE_PORT_OFFSET = os.environ.get("SLICE_PORT_OFFSET", "0")   # the slice's TELCO_PORT_OFFSET

SA_DIR = Path(os.environ.get("K8S_SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))
K8S_API = os.environ.get("K8S_API_URL", "https://kubernetes.default.svc").rstrip("/")


def _now():
    return datetime.now(timezone.utc)


def _hms(dt):
    return dt.strftime("%H:%M:%S.%f")[:-3]


# ----------------------------------------------------------------------------- Kubernetes
class Kube:
    """Minimal in-cluster client: scale one Deployment, read it, list its pods, tail a log."""

    def __init__(self):
        self.namespace = os.environ.get("SLICE_NAMESPACE") or self._read(SA_DIR / "namespace") or "default"
        ca = SA_DIR / "ca.crt"
        self.ctx = ssl.create_default_context(cafile=str(ca)) if ca.exists() else None

    @staticmethod
    def _read(path):
        try:
            return path.read_text().strip()
        except OSError:
            return None

    def _call(self, method, path, body=None, content_type="application/json", raw=False, timeout=8):
        headers = {"Accept": "application/json"}
        token = self._read(SA_DIR / "token")   # re-read: projected tokens rotate
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(K8S_API + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            payload = r.read().decode(errors="replace")
        return payload if raw else json.loads(payload)

    def scale(self, replicas):
        return self._call("PATCH", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{SLICE_DEPLOYMENT}/scale",
                          {"spec": {"replicas": replicas}}, content_type="application/merge-patch+json")

    def deployment(self):
        return self._call("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{SLICE_DEPLOYMENT}")

    def pods(self):
        items = self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector=app%3D{SLICE_DEPLOYMENT}")["items"]
        return sorted(items, key=lambda p: p["metadata"]["creationTimestamp"], reverse=True)

    def log(self, pod, tail=120):
        return self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods/{pod}/log?tailLines={tail}&timestamps=true",
                          raw=True)


def http_json(method, url, body=None, timeout=4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


# ----------------------------------------------------------------------------- controller
class SandboxController:
    def __init__(self):
        self.kube = Kube()
        self.lock = threading.Lock()
        self.events = []          # this controller's own action log: {ts, source, message}
        self.busy = None          # "STARTING" / "STOPPING" while an action thread runs
        self.ue = None            # last ue_sim outcome
        self.gen = 0              # bumped whenever an action finishes; stale status is never cached
        self._log_cache = (0.0, [])
        self._status_cache = (0.0, -1, None)
        self.log("SANDBOX", f"Controller up. Slice deployment={self.kube.namespace}/{SLICE_DEPLOYMENT}, "
                            f"O-DU O1={ODU_URL}, PTP bridge={PTP_BRIDGE_URL}.")

    def log(self, source, message):
        with self.lock:
            self.events.append({"ts": _now().isoformat(), "source": source, "message": message})
            del self.events[:-300]

    # -- actions (each runs in a background thread; the HTTP call returns immediately)
    def _run(self, name, fn):
        with self.lock:
            if self.busy:
                return {"accepted": False, "status": self.busy, "message": f"{self.busy.lower()} already in progress"}
            self.busy = name
        def wrapper():
            try:
                fn()
            except Exception as exc:
                self.log("SANDBOX", f"{name} failed: {exc}")
            finally:
                with self.lock:
                    self.busy = None
                    self.gen += 1
        threading.Thread(target=wrapper, daemon=True).start()
        return {"accepted": True, "status": name}

    def start(self):
        return self._run("STARTING", self._start)

    def stop(self):
        return self._run("STOPPING", self._stop)

    def _start(self):
        self.ue = None            # a new Start must never show the previous attach's result
        self.log("K8S", f"PATCH deployments/{SLICE_DEPLOYMENT}/scale replicas=1")
        self.kube.scale(1)
        deadline = time.time() + SLICE_READY_TIMEOUT
        last = None
        while time.time() < deadline:
            o1 = None
            try:
                o1 = http_json("GET", f"{ODU_URL}/o1/status", timeout=2)
            except Exception:
                pass
            phase = self._pod_phase()
            note = f"pod={phase}" + (f", O-DU cellState={o1['cellState']} ru={o1['ru']}" if o1 else ", O-DU not answering yet")
            if note != last:
                self.log("K8S", note)
                last = note
            if o1 and o1.get("cellState") == "ACTIVE":
                break
            time.sleep(3)
        else:
            self.log("SANDBOX", f"Slice not ACTIVE after {SLICE_READY_TIMEOUT:.0f}s; UE attach skipped.")
            return
        self._attach_ue()

    def _attach_ue(self):
        cmd = [sys.executable, "services/ue_sim/ue_sim.py", UE_SUPI, UE_K, "--pdu-echo", UE_ECHOES]
        env = dict(os.environ, TELCO_URL_ODU=ODU_URL, TELCO_HOST=SLICE_HOST, TELCO_PORT_OFFSET=SLICE_PORT_OFFSET)
        self.log("UE-SIM", "$ python3 " + " ".join(cmd[1:3]) + " <k> " + " ".join(cmd[4:]))
        ue = {"supi": UE_SUPI, "registered": False, "pduAddress": None, "echo": None, "rc": None}
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        watchdog = threading.Timer(UE_TIMEOUT, proc.kill)   # reading stdout blocks; this bounds it
        watchdog.start()
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line or line.startswith("{"):     # skip the obs JSON records; keep the UE's own lines
                    continue
                self.log("UE-SIM", line.strip())
                if "REGISTERED" in line:
                    ue["registered"] = True
                if "pduAddress=" in line:
                    ue["pduAddress"] = line.split("pduAddress=")[1].split()[0]
                if "ECHO" in line:
                    ue["echo"] = line.split("ECHO", 1)[1].split("replies")[0].strip()
        finally:
            watchdog.cancel()
            if proc.poll() is None:          # the loop raised: never leave a child behind
                proc.kill()
            ue["rc"] = proc.wait()
        if ue["rc"] == -9:
            self.log("UE-SIM", f"killed after {UE_TIMEOUT:.0f}s deadline")
        self.log("UE-SIM", f"exit code {ue['rc']}" + ("" if ue["rc"] == 0 else " (attach FAILED)"))
        self.ue = ue

    def _stop(self):
        self.log("K8S", f"PATCH deployments/{SLICE_DEPLOYMENT}/scale replicas=0")
        self.kube.scale(0)
        self.ue = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if not self.kube.pods():
                self.log("K8S", "slice pod terminated; its CPU and memory are released")
                return
            time.sleep(3)
        self.log("K8S", "slice pod still terminating after 120s")

    def _ptp(self, verb):
        try:
            res = http_json("POST", f"{PTP_BRIDGE_URL}/ptp/{verb}", {})
            self.log("PTP-BRIDGE", f"POST /ptp/{verb} -> lock_state={res.get('lock_state')} "
                                   f"offset={res.get('injected_offset_ns', res.get('offset_ns'))} ns")
            return res
        except Exception as exc:
            self.log("PTP-BRIDGE", f"POST /ptp/{verb} FAILED: {exc}")
            return {"error": str(exc)}

    def _o1_admin(self, state):
        try:
            res = http_json("PUT", f"{ODU_URL}/o1/config", {"administrativeState": state})
            self.log("O1", f"PUT O-DU /o1/config administrativeState={res.get('administrativeState')}")
            return res
        except Exception as exc:
            self.log("O1", f"PUT O-DU /o1/config {state} FAILED (is the slice running?): {exc}")
            return {"error": str(exc)}

    def inject(self):
        ptp = self._ptp("inject")
        o1 = self._o1_admin("LOCKED")   # protective carrier shutdown on loss of sync
        return {"ptp": ptp, "o1": o1}

    def heal(self):
        ptp = self._ptp("heal")
        o1 = self._o1_admin("UNLOCKED")
        return {"ptp": ptp, "o1": o1}

    # -- observation
    def _pod_phase(self):
        try:
            pods = self.kube.pods()
        except Exception as exc:
            return f"k8s error: {exc}"
        if not pods:
            return "none"
        p = pods[0]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in p["status"].get("conditions", []))
        return f"{p['metadata']['name']} {p['status'].get('phase')}{' Ready' if ready else ''}"

    def status(self):
        with self.lock:
            busy, ue, gen = self.busy, self.ue, self.gen
        cached_at, cached_gen, cached = self._status_cache
        if cached and cached_gen == gen and time.time() - cached_at < 2:
            return dict(cached, busy=busy, ue=ue)
        out = {"deployment": None, "pod": None, "o1": None, "alarms": [], "ptp": None, "errors": {}}
        try:
            d = self.kube.deployment()
            out["deployment"] = {"replicas": d["spec"].get("replicas", 0),
                                 "readyReplicas": d["status"].get("readyReplicas", 0)}
        except Exception as exc:
            out["errors"]["k8s"] = str(exc)
        out["pod"] = self._pod_phase()
        try:
            out["o1"] = http_json("GET", f"{ODU_URL}/o1/status", timeout=2)
            out["alarms"] = http_json("GET", f"{ODU_URL}/o1/alarms", timeout=2).get("alarms", [])
        except Exception as exc:
            out["errors"]["o1"] = str(exc)
        try:
            out["ptp"] = http_json("GET", f"{PTP_BRIDGE_URL}/ptp", timeout=2)
        except Exception as exc:
            out["errors"]["ptp"] = str(exc)
        with self.lock:
            if self.gen == gen:                  # no action finished while we probed
                self._status_cache = (time.time(), gen, out)
        # busy/ue were read BEFORE probing: a caller seeing busy=None gets probe data from after it
        return dict(out, busy=busy, ue=ue)

    def logs(self):
        """Controller events + the slice pod's real stdout, merged on timestamp."""
        cached_at, pod_lines = self._log_cache
        if time.time() - cached_at > 2:
            pod_lines = []
            try:
                pods = self.kube.pods()
                if pods:
                    pod_lines = [self._parse_pod_line(l) for l in self.kube.log(pods[0]["metadata"]["name"]).splitlines()
                                 if l.strip()]
            except Exception:
                pass
            self._log_cache = (time.time(), pod_lines)
        with self.lock:
            merged = list(self.events) + [l for l in pod_lines if l]
        merged.sort(key=lambda e: datetime.fromisoformat(e["ts"]))
        return [{"timestamp": _hms(datetime.fromisoformat(e["ts"])), "source": e["source"], "message": e["message"]}
                for e in merged[-300:]]

    @staticmethod
    def _parse_pod_line(line):
        ts, _, rest = line.partition(" ")      # kubelet prefix: 2026-09-28T15:50:23.123456789Z
        base = ts.rstrip("Z")
        if "." in base:
            head, frac = base.split(".", 1)
            base = f"{head}.{frac[:6]}"
        try:
            dt = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        source, msg = "ran-slice", rest
        if rest.startswith("{"):
            try:
                rec = json.loads(rest)
                source = str(rec.pop("nf", "ran-slice"))
                event = str(rec.pop("event", "") or "")
                for k in ("ts", "level", "trace_id", "span_id"):
                    rec.pop(k, None)
                msg = event + (" " + " ".join(f"{k}={v}" for k, v in rec.items()) if rec else "")
            except (ValueError, AttributeError):   # not a JSON object: keep the raw line
                source, msg = "ran-slice", rest
        elif rest.startswith("[launch_slice]"):
            source, msg = "launch_slice", rest[len("[launch_slice]"):].strip()
        return {"ts": dt.isoformat(), "source": source, "message": msg}


ctl = SandboxController()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Cloud RAN AI Sandbox</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .log-stream { font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace; }
  </style>
</head>
<body class="bg-[#f6f8fa] text-[#1f2328] min-h-screen p-6 antialiased">
  <div class="max-w-7xl mx-auto space-y-5">
    <header class="flex justify-between items-center bg-white border border-[#d0d7de] px-5 py-4 rounded-lg shadow-sm">
      <div>
        <div class="flex items-center gap-3">
          <h1 class="text-base font-semibold text-[#1f2328] tracking-tight">Cloud RAN AI Agentic Sandbox</h1>
          <span class="px-2 py-0.5 text-[11px] font-medium rounded-full bg-[#eff1f3] text-[#57606a] border border-[#d0d7de]">Multivendor RAN & Timing</span>
        </div>
        <p class="text-[#656d76] text-xs mt-0.5">On-demand O-RAN split RAN (O-CU-CP / O-CU-UP / O-DU / O-RU) + 5G core slice with a real UE attach, PTP fault injection, and NEP agentic root cause analysis.</p>
      </div>
      <div id="status-pill" class="px-2.5 py-1 rounded-full border text-xs font-mono font-medium flex items-center gap-1.5 bg-[#eff1f3] text-[#57606a] border-[#d0d7de]">
        <span id="status-dot" class="w-1.5 h-1.5 rounded-full bg-[#8c959f]"></span>
        <span id="status-text">LOADING</span>
      </div>
    </header>

    <div class="grid grid-cols-1 lg:grid-cols-12 gap-5 items-start">
      <div class="lg:col-span-7 bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 flex flex-col">
        <div class="flex justify-between items-center mb-3">
          <div class="flex items-center gap-2">
            <h2 class="text-xs font-semibold uppercase tracking-wider text-[#1f2328]">Live System & Telemetry Log Stream</h2>
            <span class="px-1.5 py-0.5 text-[10px] font-mono font-medium rounded bg-[#eff1f3] text-[#57606a] border border-[#d0d7de]">controller + kubectl logs ran-slice</span>
          </div>
          <button onclick="fetchLogs()" class="text-xs text-[#0969da] hover:underline font-medium">Refresh</button>
        </div>
        <div id="log-box" class="bg-[#f6f8fa] p-3 rounded-md h-[560px] overflow-y-auto log-stream text-[11px] space-y-1 border border-[#d0d7de]"></div>
      </div>

      <div class="lg:col-span-5 space-y-4">
        <div class="bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 space-y-3">
          <h2 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">RAN Controls</h2>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            <button onclick="controlRAN('start')" class="bg-[#1f883d] hover:bg-[#1a7f37] text-white p-3 rounded-lg border border-[rgba(31,35,40,0.15)] shadow-sm text-left transition active:scale-[0.99]">
              <div class="font-medium text-xs text-white">Start RAN Slice</div>
              <div class="text-[10px] text-emerald-100 mt-0.5">Scale pod up + attach UE</div>
            </button>
            <button onclick="controlRAN('stop')" class="bg-white hover:bg-[#ffebe9] hover:border-[#ffcecb] text-[#cf222e] p-3 rounded-lg border border-[#d0d7de] shadow-sm text-left transition active:scale-[0.99]">
              <div class="font-medium text-xs text-[#cf222e]">Stop RAN Slice</div>
              <div class="text-[10px] text-[#656d76] mt-0.5">Scale to 0, free CPU & RAM</div>
            </button>
            <button onclick="controlRAN('inject-fault')" class="bg-white hover:bg-[#f3f4f6] text-[#1f2328] p-3 rounded-lg border border-[#d0d7de] shadow-sm text-left transition active:scale-[0.99]">
              <div class="font-medium text-xs text-[#1f2328]">Inject Fault</div>
              <div class="text-[10px] text-[#656d76] mt-0.5">PTP FREERUN + O1 lock cell</div>
            </button>
            <button onclick="controlRAN('heal')" class="bg-white hover:bg-[#f3f4f6] text-[#0969da] p-3 rounded-lg border border-[#d0d7de] shadow-sm text-left transition active:scale-[0.99]">
              <div class="font-medium text-xs text-[#0969da]">Heal Timing</div>
              <div class="text-[10px] text-[#656d76] mt-0.5">PTP LOCKED + O1 unlock</div>
            </button>
          </div>
          <div class="text-[11px] font-mono text-[#656d76]">Pod: <span id="pod-val">-</span></div>
        </div>

        <div class="bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 space-y-2.5">
          <h2 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">UE (ue_sim)</h2>
          <div class="space-y-1.5 text-xs font-mono">
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">Registration:</span><span id="ue-reg" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">PDU session:</span><span id="ue-pdu" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">GTP-U echo:</span><span id="ue-echo" class="font-semibold text-[#656d76]">-</span></div>
          </div>
        </div>

        <div class="bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 space-y-2.5">
          <h2 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">PTP Timing Plane <span class="normal-case font-normal text-[#656d76]">(ptp-bridge /ptp)</span></h2>
          <div class="space-y-1.5 text-xs font-mono">
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">Master Offset:</span><span id="offset-val" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">Servo State:</span><span id="servo-val" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">CloudEvent:</span><span id="cloudevent-val" class="font-semibold text-[#656d76]">-</span></div>
          </div>
        </div>

        <div class="bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 space-y-2.5">
          <h2 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">O-DU 3GPP TS 28.532 Faults <span class="normal-case font-normal text-[#656d76]">(O1)</span></h2>
          <div class="space-y-1.5 text-xs font-mono">
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">Cell Status:</span><span id="cell-val" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">Active Alarm:</span><span id="alarm-val" class="font-semibold text-[#656d76]">-</span></div>
            <div class="flex justify-between bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]"><span class="text-[#656d76]">RF Carrier:</span><span id="rf-val" class="font-semibold text-[#656d76]">-</span></div>
          </div>
        </div>

        <div class="bg-white border border-[#d0d7de] rounded-lg shadow-sm p-4 space-y-3">
          <div>
            <h2 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">NEP Agentic RCA</h2>
            <p class="text-xs text-[#656d76] mt-1">4-plane corroboration across O-Cloud, RAN, PTP, and Intel NIC over CAPIF + MCP.</p>
          </div>
          <button onclick="triggerRCA()" class="w-full bg-[#0969da] hover:bg-[#0860ca] text-white font-medium py-2.5 px-3 rounded-lg text-xs transition shadow-sm text-center">
            Run 4-Plane Agentic RCA
          </button>
        </div>
      </div>
    </div>

    <div id="rca-panel" class="hidden bg-white border border-[#d0d7de] rounded-lg shadow-lg p-5 space-y-4">
      <div class="flex justify-between items-start border-b border-[#d0d7de] pb-3">
        <div>
          <div class="flex items-center gap-2.5">
            <h3 class="font-semibold text-sm text-[#1f2328]">O-RAN & TM Forum Unified Agentic Audit Trail</h3>
            <span id="rca-badge" class="px-2 py-0.5 text-[10px] font-medium rounded-full bg-[#dafbe1] text-[#1a7f37] border border-[#aceebb]">TMF688 Concluded</span>
          </div>
          <p class="text-xs text-[#656d76] mt-0.5">Trace ID: <span class="font-mono text-[#1f2328]" id="trace-id">-</span> | Run ID: <span class="font-mono text-[#1f2328]" id="mlflow-id">-</span></p>
        </div>
        <button onclick="document.getElementById('rca-panel').classList.add('hidden')" class="text-[#656d76] hover:text-[#1f2328] text-xs font-semibold px-2 py-1 bg-[#f6f8fa] border border-[#d0d7de] rounded">Close</button>
      </div>
      <div class="bg-[#f6f8fa] border border-[#d0d7de] rounded-lg p-3.5 space-y-1.5">
        <div class="flex justify-between items-center text-xs">
          <span class="font-semibold text-[#1f2328]">AI Executive Synthesis</span>
          <span class="text-[11px] text-[#656d76]" id="llm-meta">-</span>
        </div>
        <p class="text-xs text-[#1f2328] leading-relaxed" id="llm-summary">Analyzing...</p>
      </div>
      <div class="space-y-2">
        <h4 class="text-xs font-semibold text-[#1f2328] uppercase tracking-wider">Hierarchical Execution Spans</h4>
        <div class="space-y-1.5 text-xs font-mono" id="spans-list"></div>
      </div>
      <div class="space-y-1">
        <details class="text-xs">
          <summary class="text-[#0969da] cursor-pointer font-medium hover:underline">View Raw Response JSON</summary>
          <pre id="rca-output" class="mt-2 text-[11px] bg-[#f6f8fa] p-3 rounded font-mono text-[#1f2328] overflow-x-auto max-h-48 border border-[#d0d7de]"></pre>
        </details>
      </div>
    </div>
  </div>

  <script>
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const RED = 'font-semibold text-[#cf222e]', GREEN = 'font-semibold text-[#1a7f37]', GREY = 'font-semibold text-[#656d76]', INK = 'font-semibold text-[#1f2328]';
    function setVal(id, text, cls) { const el = document.getElementById(id); el.innerText = text; el.className = cls; }
    function pill(text, tone) {
      const tones = {
        grey: ['bg-[#eff1f3] text-[#57606a] border-[#d0d7de]', 'bg-[#8c959f]'],
        green: ['bg-[#dafbe1] text-[#1a7f37] border-[#aceebb]', 'bg-[#1a7f37]'],
        red: ['bg-[#ffebe9] text-[#cf222e] border-[#ffcecb]', 'bg-[#cf222e]'],
        amber: ['bg-[#fff8c5] text-[#9a6700] border-[#eac54f]', 'bg-[#9a6700]'],
      }[tone];
      document.getElementById('status-text').innerText = text;
      document.getElementById('status-pill').className = 'px-2.5 py-1 rounded-full border text-xs font-mono font-medium flex items-center gap-1.5 ' + tones[0];
      document.getElementById('status-dot').className = 'w-1.5 h-1.5 rounded-full ' + tones[1];
    }

    async function controlRAN(action) {
      const url = {'start': '/api/ran/start', 'stop': '/api/ran/stop', 'inject-fault': '/api/ran/inject-ptp-fault', 'heal': '/api/ran/heal-ptp'}[action];
      await fetch(url, { method: 'POST' });
      updateUI();
      fetchLogs();
    }

    async function triggerRCA() {
      const btn = event.target;
      btn.innerText = 'Analyzing over CAPIF...';
      try {
        const res = await fetch('/api/ran/trigger-rca', { method: 'POST' });
        const data = await res.json();
        const badge = document.getElementById('rca-badge');
        if (data.error) {
          badge.innerText = 'RCA FAILED'; badge.className = 'px-2 py-0.5 text-[10px] font-medium rounded-full bg-[#ffebe9] text-[#cf222e] border border-[#ffcecb]';
        } else {
          badge.innerText = 'TMF688 Concluded'; badge.className = 'px-2 py-0.5 text-[10px] font-medium rounded-full bg-[#dafbe1] text-[#1a7f37] border border-[#aceebb]';
        }
        const llm = data.llmSynthesis || {};
        const spans = data.spans || [];
        const tmfEvent = data.tmf688Event || data;
        document.getElementById('trace-id').innerText = data.traceId || data.event?.traceId || '-';
        document.getElementById('mlflow-id').innerText = data.mlflowRunId || data.event?.mlflowRunId || '-';
        document.getElementById('llm-summary').innerText = data.error || llm.summary || tmfEvent.event?.llmNarrative || '(no narrative returned)';
        document.getElementById('llm-meta').innerText = llm.model ? `Model: ${llm.model}` + (llm.latency_ms ? ` | Latency: ${llm.latency_ms}ms` : '') : '';
        const spansList = document.getElementById('spans-list');
        spansList.innerHTML = spans.length ? spans.map(s => `
            <div class="flex justify-between items-center bg-[#f6f8fa] p-2 rounded border border-[#d0d7de]">
              <div class="flex items-center gap-2">
                <span class="w-1.5 h-1.5 rounded-full bg-[#1a7f37]"></span>
                <span class="font-semibold text-[#1f2328]">${esc(s.name)}</span>
                <span class="text-[10px] text-[#656d76]">(${esc(s.standard)})</span>
              </div>
              <span class="text-[#1a7f37] font-semibold">${esc(s.duration_ms)}ms [${esc(s.status)}]</span>
            </div>`).join('') : '<div class="text-slate-500 text-xs p-2">No spans returned.</div>';
        document.getElementById('rca-output').innerText = JSON.stringify(tmfEvent, null, 2);
        document.getElementById('rca-panel').classList.remove('hidden');
      } finally {
        btn.innerText = 'Run 4-Plane Agentic RCA';
      }
      fetchLogs();
    }

    async function updateUI() {
      const d = await (await fetch('/api/ran/status')).json();
      const dep = d.deployment || {};
      document.getElementById('pod-val').innerText = (d.pod || '-') + (dep.replicas !== undefined ? `  (replicas ${dep.readyReplicas || 0}/${dep.replicas})` : '');

      const ptp = d.ptp;
      if (ptp) {
        const locked = ptp.port_state === 'LOCKED';
        setVal('offset-val', `${ptp.phc_offset_ns} ns` + (locked ? '' : ' (SPIKE)'), locked ? INK : RED);
        setVal('servo-val', ptp.port_state + (locked ? '' : ' (UNLOCKED)'), locked ? GREEN : RED);
        setVal('cloudevent-val', ptp.cloud_event_state, locked ? GREEN : RED);
      } else {
        ['offset-val', 'servo-val', 'cloudevent-val'].forEach(id => setVal(id, 'ptp-bridge unreachable', RED));
      }

      const o1 = d.o1;
      if (o1) {
        setVal('cell-val', o1.cellState, o1.cellState === 'ACTIVE' ? GREEN : RED);
        const a = (d.alarms || [])[0];
        setVal('alarm-val', a ? `${a.faultName} (${a.probableCause})` : 'None', a ? RED : GREY);
        const rfOn = o1.ru === 'CONNECTED' && o1.administrativeState === 'UNLOCKED';
        setVal('rf-val', rfOn ? `Active (${o1.ruId}, ${o1.ueCount} UE)` : `Off (RU ${o1.ru}, admin ${o1.administrativeState})`, rfOn ? GREEN : RED);
      } else {
        setVal('cell-val', 'OFFLINE (slice not running)', GREY);
        setVal('alarm-val', 'None', GREY);
        setVal('rf-val', 'Disabled', GREY);
      }

      const ue = d.ue;
      setVal('ue-reg', ue ? (ue.registered ? `REGISTERED ${ue.supi}` : 'FAILED') : '-', ue ? (ue.registered ? GREEN : RED) : GREY);
      setVal('ue-pdu', ue && ue.pduAddress ? ue.pduAddress : '-', ue && ue.pduAddress ? GREEN : GREY);
      setVal('ue-echo', ue && ue.echo ? ue.echo : '-', ue && ue.echo ? GREEN : GREY);

      if (d.busy) pill(d.busy + '...', 'amber');
      else if (!o1) pill('RAN STOPPED (IDLE)', 'grey');
      else if (o1.cellState !== 'ACTIVE' || (ptp && ptp.port_state !== 'LOCKED')) pill('FAULT: ' + (o1.cellState !== 'ACTIVE' ? 'CELL ' + o1.cellState : 'PTP ' + ptp.port_state), 'red');
      else pill('RAN RUNNING & LOCKED', 'green');
    }

    async function fetchLogs() {
      const logs = await (await fetch('/api/ran/logs')).json();
      const box = document.getElementById('log-box');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = logs.map(l => {
        let textStyle = 'text-[#1f2328]';
        const m = l.message;
        if (/REGISTERED|PDU SESSION|ECHO|cellState=ACTIVE|READY|lock_state=LOCKED|administrativeState=UNLOCKED/.test(m)) textStyle = 'text-[#1a7f37] font-semibold';
        if (/FAIL|ALARM|FREERUN|Traceback|error|administrativeState=LOCKED/i.test(m)) textStyle = 'text-[#cf222e] font-semibold';
        return `<div><span class="text-[#8c959f]">[${esc(l.timestamp)}]</span> <span class="text-[#57606a] font-semibold">${esc(l.source)}:</span> <span class="${textStyle}">${esc(m)}</span></div>`;
      }).join('');
      if (atBottom) box.scrollTop = box.scrollHeight;
    }

    setInterval(() => { updateUI(); fetchLogs(); }, 4000);
    updateUI();
    fetchLogs();
  </script>
</body>
</html>
"""


class SandboxHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/ran/status":
            self._send_json(200, ctl.status())
        elif self.path == "/api/ran/logs":
            self._send_json(200, ctl.logs())
        elif self.path == "/healthz":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        if self.path == "/api/ran/start":
            self._send_json(202, ctl.start())
        elif self.path == "/api/ran/stop":
            self._send_json(202, ctl.stop())
        elif self.path == "/api/ran/inject-ptp-fault":
            self._send_json(200, ctl.inject())
        elif self.path == "/api/ran/heal-ptp":
            self._send_json(200, ctl.heal())
        elif self.path == "/api/ran/trigger-rca":
            try:
                audit_trace = http_json("POST", f"{NEP_URL}/nep/rca/trigger?view=full", {}, timeout=RCA_TIMEOUT)
                ev = audit_trace.get("tmf688Event", {}).get("event", {})
                ctl.log("NEP", f"RCA {ev.get('traceId')}: {ev.get('faultClass') or 'no fault'} "
                               f"({ev.get('corroboration')}) -> {ev.get('decision')}")
                self._send_json(200, audit_trace)
            except Exception as e:
                ctl.log("NEP", f"RCA call FAILED: {e}")
                self._send_json(200, {"error": f"Failed to call NEP Orchestrator: {e}"})
        else:
            self._send_json(404, {"error": "Not Found"})

    def _send_json(self, status, payload):
        b = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), SandboxHandler)
    print(f"Cloud RAN AI Sandbox Controller running on :{PORT}...", flush=True)
    server.serve_forever()

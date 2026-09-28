"""Stub Kubernetes API for the local end-to-end test.

Implements only what the sandbox controller calls: PATCH .../deployments/ran-slice/scale,
GET the Deployment, list its pods, and read a pod's log. Scaling to 1 launches the REAL
RAN slice (launch_slice.py, profile "ran") as a child process on TELCO_PORT_OFFSET; scaling
to 0 terminates it. So the controller drives a real stack; only the Kubernetes API is faked.

Usage: python3 stub_kube.py <src-dir> <listen-port> <slice-port-offset>
"""
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

SRC, PORT, OFFSET = sys.argv[1], int(sys.argv[2]), sys.argv[3]
proc = None
lines = []   # (rfc3339 ts, text) -- what `kubectl logs --timestamps` would return


def pump(p):
    for line in p.stdout:
        lines.append((datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z"), line.rstrip()))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, raw=False):
        b = obj.encode() if raw else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_PATCH(self):
        global proc
        n = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["spec"]["replicas"]
        if n and proc is None:
            env = dict(os.environ, TELCO_PORT_OFFSET=OFFSET, TELCO_SLICE_PROFILE="ran")
            proc = subprocess.Popen([sys.executable, "deploy/docker/launch_slice.py"], cwd=SRC, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    start_new_session=True)
            threading.Thread(target=pump, args=(proc,), daemon=True).start()
        elif not n and proc is not None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(30)
            proc = None
        self._send({"spec": {"replicas": n}})

    def do_GET(self):
        up = proc is not None
        if "/deployments/" in self.path:
            self._send({"spec": {"replicas": int(up)}, "status": {"readyReplicas": int(up)}})
        elif "/log" in self.path:
            self._send("\n".join(f"{t} {l}" for t, l in lines[-120:]), raw=True)
        elif "/pods" in self.path:
            pod = {"metadata": {"name": "ran-slice-local", "creationTimestamp": "2026-01-01T00:00:00Z"},
                   "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
            self._send({"items": [pod] if up else []})
        else:
            self._send({})


def shutdown(*_):
    if proc is not None:
        os.killpg(proc.pid, signal.SIGTERM)
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

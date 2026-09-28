"""
Cluster access — the only place that touches the live Duranta namespace.

Reaches the real MySQL `oai_db` through the lab's kubectl helper (which SSHes to
the k8s control plane). SQL is base64-wrapped so it crosses the ssh -> sh -> mysql
quoting layers intact. Everything is env-configurable and every call is bounded
by a timeout; nothing here mutates the cluster except udr.py's subscriber rows.

Config (all env-overridable):
  DURANTA_KUBECTL     path to the kubectl wrapper (default: the lab helper)
  DURANTA_NAMESPACE   namespace (default: duranta)
  DURANTA_MYSQL_GREP  substring to find the mysql pod (default: mysql)
  DURANTA_DB / _USER / _PASS   database + creds (default: oai_db / root / linux)
"""

import base64
import os
import subprocess

KUBECTL = os.environ.get(
    "DURANTA_KUBECTL",
    "kubectl",
)
NAMESPACE = os.environ.get("DURANTA_NAMESPACE", "duranta")
MYSQL_GREP = os.environ.get("DURANTA_MYSQL_GREP", "mysql")
DB = os.environ.get("DURANTA_DB", "oai_db")
DB_USER = os.environ.get("DURANTA_DB_USER", "root")
DB_PASS = os.environ.get("DURANTA_DB_PASS", "linux")

TIMEOUT = int(os.environ.get("DURANTA_TIMEOUT", "30"))

_pod_cache = None


class DurantaUnavailable(RuntimeError):
    """Raised when the real cluster / MySQL pod cannot be reached. Callers treat
    this as 'federation target down', never as a provisioning failure."""


def _kubectl(args, timeout=TIMEOUT):
    try:
        out = subprocess.run([KUBECTL, "-n", NAMESPACE, *args],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DurantaUnavailable(f"kubectl helper failed: {exc}") from exc
    if out.returncode != 0:
        raise DurantaUnavailable(f"kubectl {' '.join(args)} -> rc {out.returncode}: "
                                 f"{out.stderr.strip()[:200]}")
    return out.stdout


def mysql_pod(refresh=False):
    global _pod_cache
    if _pod_cache and not refresh:
        return _pod_cache
    names = _kubectl(["get", "pods", "--no-headers", "-o",
                      "custom-columns=N:.metadata.name"])
    for line in names.splitlines():
        if MYSQL_GREP in line:
            _pod_cache = line.strip()
            return _pod_cache
    raise DurantaUnavailable(f"no pod matching {MYSQL_GREP!r} in ns {NAMESPACE}")


def sql(statement):
    """Run one SQL statement in the live oai_db; return stdout (tab-separated).
    Base64-wrapped so quotes/JSON in the statement survive the ssh/sh/mysql layers."""
    pod = mysql_pod()
    b64 = base64.b64encode(statement.encode()).decode()
    inner = (f"echo {b64} | base64 -d | "
             f"mysql -u{DB_USER} -p{DB_PASS} {DB} -N")
    return _kubectl(["exec", pod, "--", "sh", "-c", inner])


def available():
    """True when the cluster + MySQL pod + oai_db all answer. Used for health
    gating: connectors report 'unavailable' (not 'failed') when this is False."""
    try:
        sql("SELECT 1;")
        return True
    except DurantaUnavailable:
        return False


# --- read-only cluster helpers (shared by the federation rungs 1b/1c/1d) ------

def kubectl(args, timeout=TIMEOUT):
    """Public read passthrough to the lab kubectl helper (namespace prepended).
    Raises DurantaUnavailable on failure. Callers keep this READ-only unless they
    own a reversible write (e.g. a UE pod they also delete)."""
    return _kubectl(args, timeout=timeout)


def kubectl_stdin(args, text, timeout=TIMEOUT):
    """Write helper: run the kubectl wrapper with `text` piped to its stdin
    (namespace prepended). Used for `apply -f -` / `delete -f -` so a manifest is
    streamed to the control plane without needing a file readable there. Raises
    DurantaUnavailable on failure. Callers must own the reversibility of any write."""
    try:
        out = subprocess.run([KUBECTL, "-n", NAMESPACE, *args],
                             input=text, capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DurantaUnavailable(f"kubectl helper failed: {exc}") from exc
    if out.returncode != 0:
        raise DurantaUnavailable(f"kubectl {' '.join(args)} -> rc {out.returncode}: "
                                 f"{out.stderr.strip()[:300]}")
    return out.stdout


def get_pods():
    """List the namespace's pods as [{name, phase, ready, restarts, node}], parsed
    from `kubectl get pods -o json` (stdlib json). Empty list if the API is down."""
    import json
    try:
        out = _kubectl(["get", "pods", "-o", "json"])
    except DurantaUnavailable:
        return []
    pods = []
    for item in json.loads(out).get("items", []):
        st = item.get("status", {})
        cs = st.get("containerStatuses", []) or []
        pods.append({
            "name": item["metadata"]["name"],
            "phase": st.get("phase", "Unknown"),
            "ready": all(c.get("ready") for c in cs) if cs else False,
            "restarts": sum(c.get("restartCount", 0) for c in cs),
            "node": item.get("spec", {}).get("nodeName"),
        })
    return pods


def logs(pod, tail=50):
    """Recent log lines from a pod (read-only). '' if unavailable."""
    try:
        return _kubectl(["logs", pod, "--tail", str(tail)])
    except DurantaUnavailable:
        return ""


def exec_in(pod, command, timeout=TIMEOUT):
    """Run a command inside a pod (read-only diagnostics: ip/ping/status). Returns
    stdout, or raises DurantaUnavailable. `command` is a list of argv tokens."""
    return _kubectl(["exec", pod, "--", *command], timeout=timeout)

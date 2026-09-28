"""RAN / OAI evidence adapter — the RAN MCP server's reach into the real OAI cluster (Duranta).

Real gNB/DU/UE pod state and logs come from extensions.duranta.cluster (kubectl into the duranta ns).
The PTP Sync/Follow_Up forensics OAI does not natively expose are emitted as SYNTHETIC evidence,
labeled emulated, toggled by RAN_FAULT (or a per-call fault arg) so a demo can show clean vs faulted
— honest about the edge, per the fidelity discipline in docs/demo_walkthroughs/ptp_rca_remediation.
"""
import json
import os
import urllib.error
import urllib.request

# O1 mode (multivendor-agentic-rca): when RAN_O1_URL names an O-DU (the on-demand ran-slice),
# the RAN plane testifies from that O-DU's own O1 status + TS 28.532 alarms instead of kubectl.
RAN_O1_URL = os.environ.get("RAN_O1_URL", "").rstrip("/")


class RanUnavailable(RuntimeError):
    pass


def _cluster():
    try:
        from extensions.duranta import cluster
    except ImportError as exc:
        raise RanUnavailable("duranta adapter unavailable: %s" % exc)
    return cluster


def _faulted(arg):
    if arg is not None:
        return str(arg).lower() in ("1", "true", "yes")
    return os.environ.get("RAN_FAULT", "").lower() in ("1", "true", "yes")


def _o1_get(path):
    try:
        with urllib.request.urlopen(RAN_O1_URL + path, timeout=4) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RanUnavailable("O-DU O1 unreachable at %s (is the RAN slice running?): %s" % (RAN_O1_URL, exc))


def gnb_status_o1():
    """RAN-plane testimony from the O-DU's O1 surface: cell state + active alarms (real)."""
    st = _o1_get("/o1/status")
    alarms = _o1_get("/o1/alarms").get("alarms", [])
    sync_loss = any(a.get("probableCause") == "lossOfRealTimeSynchronization" for a in alarms)
    return {"plane": "ran", "source": "O-DU O1 /o1/status + /o1/alarms (real, ran-slice)",
            "cellState": st.get("cellState"), "ru": st.get("ru"),
            "administrativeState": st.get("administrativeState"), "ueCount": st.get("ueCount"),
            "alarms": [{k: a.get(k) for k in ("alarmId", "faultName", "probableCause", "perceivedSeverity")}
                       for a in alarms],
            "signal": "du_sync_loss_alarm" if sync_loss else None}


def gnb_status():
    if RAN_O1_URL:
        return gnb_status_o1()
    # gNB status only needs a pod read — NOT the SQL/oai_db health gate that available()
    # checks (that gate belongs to the subscriber-provisioning path). get_pods() returns []
    # gracefully if the API is unreachable.
    c = _cluster()
    want = ("gnb", "nr-ue", "cu", "-du", "ran")
    pods = [p for p in c.get_pods() if any(w in p.get("name", "").lower() for w in want)]
    if not pods:
        raise RanUnavailable("duranta gNB not visible (cluster unreachable or no gNB pods)")
    return {"plane": "ran", "source": "oai gNB/DU/UE (real, duranta ns)",
            "pods": [{"name": p.get("name"), "phase": p.get("phase"),
                      "ready": p.get("ready"), "restarts": p.get("restarts")} for p in pods]}


def sync_forensics(fault=None):
    """gNB RX/TX Sync/Follow_Up analysis. Emulated: OAI has no native PTP timestamping surface."""
    if not _faulted(fault):
        return {"plane": "ran", "emulated": True, "signal": None,
                "finding": "Sync/Follow_Up sequence nominal; preciseOriginTimestamp advances monotonically"}
    return {"plane": "ran", "emulated": True, "signal": "ran_sync_followup_anomaly",
            "findings": [
                "gNB RX: Follow_Up of Sync X is missing",
                "gNB RX: Follow_Up X+1 carries prior-origin timestamp",
                "GM TX: Follow_Up of Sync X never sent; X+1 preciseOriginTimestamp matches previous Sync"],
            "note": "OAI exposes no native PTP surface; synthetic evidence matching an egress HW-timestamp fault"}

"""RAN evidence adapter -- the RAN MCP server's reach into the O-DU.

gnb_status(): real RAN-plane testimony from the O-DU's O1 surface (RAN_O1_URL): cell state plus
active TS 28.532 alarms; signal du_sync_loss_alarm when an alarm cites lossOfRealTimeSynchronization.
sync_forensics(): PTP Sync/Follow_Up forensics the O-DU does not expose natively -- SYNTHETIC
evidence, labeled emulated, toggled by RAN_FAULT or a per-call fault arg (not used by the RCA).
"""
import json
import os
import urllib.error
import urllib.request

# The O-DU to testify from (the on-demand ran-slice). Required: this is the only RAN source
# in this repo; the RAN plane reports "unavailable" without it.
RAN_O1_URL = os.environ.get("RAN_O1_URL", "").rstrip("/")


class RanUnavailable(RuntimeError):
    pass


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
    if not RAN_O1_URL:
        raise RanUnavailable("RAN_O1_URL is not set (point it at the O-DU, e.g. http://ran-slice:7010)")
    return gnb_status_o1()


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

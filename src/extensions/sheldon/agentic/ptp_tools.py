"""Red Hat PTP plane — linuxptp / PTP Operator / cloud-event-proxy evidence.

Reads the REAL ptp-lab exporter (:7091, TELCO_PTP_URL) for ptp4l offset + port state, and derives
the cloud-event-proxy lock-state view an operator or agent would consume. The clock beneath is a
mock PHC (fidelity note in the PTP walkthrough), but the protocol, BMCA and notification path are
real — nothing downstream can tell.
"""
import json
import os
import urllib.error
import urllib.request

PTP_URL = os.environ.get("TELCO_PTP_URL", "http://ptp-bridge:7091").rstrip("/")
OFFSET_LIMIT_NS = int(os.environ.get("PTP_OFFSET_LIMIT_NS", "100000"))


class PtpUnavailable(RuntimeError):
    pass


def _get(path):
    try:
        with urllib.request.urlopen(PTP_URL + path, timeout=6) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PtpUnavailable("ptp exporter unreachable at %s: %s" % (PTP_URL, exc))


def ptp_operator_status(a=None):
    """ptp4l / PTP Operator: master-offset and port states (LISTENING/MASTER/SLAVE)."""
    d = _get("/ptp")
    off = d.get("phc_offset_ns")
    exceeded = off is not None and abs(off) > OFFSET_LIMIT_NS
    return {"plane": "platform", "source": "ptp4l / PTP Operator (real, mock PHC)",
            "gm_port_state": d.get("gm_port_state"), "du_port_state": d.get("du_port_state"),
            "phc_offset_ns": off, "offset_limit_ns": OFFSET_LIMIT_NS,
            "signal": "ptp_offset_exceeded" if exceeded else None}


def cloud_event_ptp_status(a=None):
    """cloud-event-proxy: the lock-state / os-clock-sync-state notification an operator consumes."""
    d = _get("/ptp")
    off = d.get("phc_offset_ns")
    locked = (d.get("du_port_state") in ("SLAVE", "UNCALIBRATED")
              and (off is None or abs(off) <= OFFSET_LIMIT_NS))
    state = "LOCKED" if locked else "FREERUN"
    return {"plane": "platform", "source": "cloud-event-proxy (derived from ptp4l)",
            "lock_state": state, "os_clock_sync_state": state, "phc_offset_ns": off,
            "signal": None if locked else "ptp_not_locked"}

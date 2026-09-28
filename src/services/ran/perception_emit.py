"""
perception_emit: the RAN's best-effort tap into the SMO perception bridge (Phase 3).

The RAN sim is the SOURCE of what happened on the radio side; the SMO telemetry store
is an OBSERVER of it. That direction is the whole design, and it dictates the single
rule this module exists to enforce:

    A PERCEPTION FAILURE MUST NEVER BE A RAN FAILURE.

So every emit is wrapped, every exception is swallowed and logged, and the caller gets
False back whatever happens -- bridge down, wrong URL, refused connection, slow socket.
If the SMO is not there, the cell still admits UEs and the bearer still comes up. This
is not sloppy error handling; it is the containment boundary, and it is why the call
sites in odu.py/ocucp.py are single unguarded lines.

The POST is synchronous but hard-bounded (EMIT_TIMEOUT_S): a refused connection returns
instantly, and a hung bridge costs one second of one RRC/F1AP request, never the process.

Target URL: SMO_BRIDGE_URL, else netconfig's smo_bridge entry. Stdlib only.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, house pattern
from domain import netconfig, obs

EMIT_TIMEOUT_S = 1.0

# The lab is one cell today and the O-DU is its NRM anchor. Overridable so a multi-zone
# demo can move events onto distinct cells without touching any call site.
CELL_ID = os.environ.get("RAN_CELL_ID", "NRCellDU=lab-cell-1")


def bridge_url():
    """Where the perception bridge listens. Env first, then the netconfig entry."""
    return (os.environ.get("SMO_BRIDGE_URL") or netconfig.url("smo_bridge")).rstrip("/")


def emit(event, cell_id=None, kpis=None, severity=None, alarm_condition=None, **attributes):
    """POST one RAN event to the perception bridge. True on 2xx, False otherwise. Never raises.

    kpis are the numeric facts (they become queryable KPIs on the R1/DME side);
    everything in **attributes rides along as VES additionalFields and stays readable
    in the stored record's source_event without pretending to be a measurement.
    """
    try:
        payload = {
            "event": event,
            "nf": obs.nf_name() or "ran",
            "cellId": cell_id or CELL_ID,
            "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attributes": {str(k): v for k, v in attributes.items() if v is not None},
        }
        if kpis:
            payload["kpis"] = kpis
        if severity:
            payload["severity"] = severity
        if alarm_condition:
            payload["alarmCondition"] = alarm_condition
        request = urllib.request.Request(
            bridge_url() + "/smo/ves", data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        corr = obs.get_corr()
        if corr:
            request.add_header(obs.HEADER, corr)   # the emit stays on the RAN request's trail
        with urllib.request.urlopen(request, timeout=EMIT_TIMEOUT_S) as response:
            ok = 200 <= response.status < 300
        obs.counter("perception_emits_total", result="ok" if ok else "rejected").inc()
        return ok
    except Exception as exc:      # noqa: BLE001 -- deliberately total; see the module docstring
        try:
            obs.counter("perception_emits_total", result="failed").inc()
            obs.log("perception_emit_failed", level="warn", ranEvent=event,
                    target=os.environ.get("SMO_BRIDGE_URL", "netconfig"), error=str(exc)[:120])
        except Exception:         # noqa: BLE001 -- even the logging must not reach the caller
            pass
        return False

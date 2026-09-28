"""Red Hat PTP MCP server — the platform (linuxptp / PTP Operator / cloud-event-proxy) boundary.

Backs the diagram's 'Red Hat' server. Reads the real ptp-lab exporter (:7091).
Serve with: python3 -m extensions.sheldon.agentic.mcp_http --server redhat --port 8853
"""
from extensions.sheldon.agentic import mcp_core
from extensions.sheldon.agentic import ptp_tools as T

SERVER = {"name": "redhat-ptp", "version": "1.0.0"}

TOOLS = {
    "ptp_operator_status": (
        "ptp4l / PTP Operator testimony: GM/DU port states and the master (phc) offset in ns; "
        "raises ptp_offset_exceeded past the limit. Reads the real :7091 exporter.",
        {"type": "object", "properties": {}},
        lambda a: T.ptp_operator_status(a),
    ),
    "ptp_cloud_event_status": (
        "cloud-event-proxy testimony: lock-state / os-clock-sync-state (LOCKED / FREERUN) as an "
        "operator or agent consumes it; raises ptp_not_locked when unsynced.",
        {"type": "object", "properties": {}},
        lambda a: T.cloud_event_ptp_status(a),
    ),
}

handle = mcp_core.make_handle(TOOLS, SERVER, T.PtpUnavailable)

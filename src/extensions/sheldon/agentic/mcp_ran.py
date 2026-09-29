"""RAN / OAI MCP server — the gNB layer's typed boundary. Backs the diagram's 'RAN Agent / NEP'.

Serve with: python3 -m extensions.sheldon.agentic.mcp_http --server ran --port 8851
"""
from extensions.sheldon.agentic import mcp_core
from extensions.sheldon.agentic import ran_tools as T

SERVER = {"name": "oai-ran", "version": "1.0.0"}

TOOLS = {
    "ran_gnb_status": (
        "RAN-plane testimony: the O-DU's O1 cell state and active TS 28.532 alarms (RAN_O1_URL).",
        {"type": "object", "properties": {}},
        lambda a: T.gnb_status(),
    ),
    "ran_sync_forensics": (
        "gNB RX/TX PTP Sync/Follow_Up forensics: missing Follow_Up, prior-origin timestamp, "
        "preciseOriginTimestamp vs previous Sync. Emulated (OAI has no native PTP surface).",
        {"type": "object", "properties": {"fault": {"type": "string",
         "description": "'1' to emit the faulted sequence; omit for nominal or set RAN_FAULT."}}},
        lambda a: T.sync_forensics(a.get("fault")),
    ),
}

handle = mcp_core.make_handle(TOOLS, SERVER, T.RanUnavailable)

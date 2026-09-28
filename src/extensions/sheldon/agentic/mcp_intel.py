"""Intel NIC MCP server — the silicon layer's typed boundary. Backs the diagram's 'Intel' server.

Serve with: python3 -m extensions.sheldon.agentic.mcp_http --server intel --port 8852
"""
from extensions.sheldon.agentic import mcp_core
from extensions.sheldon.agentic import nic_tools as T

SERVER = {"name": "intel-nic", "version": "1.0.0"}

TOOLS = {
    "nic_timestamp_counters": (
        "ethtool -S tx hardware-timestamp counters (tx_hwtstamp_timeouts, ptp_tx_carryover, "
        "ptp_ts_fifo_overflow) and the ~74 ms delta. Emulated E810.",
        {"type": "object", "properties": {"fault": {"type": "string",
         "description": "'1' to emit the egress-miss counters; omit for baseline or set NIC_FAULT."}}},
        lambda a: T.timestamp_counters(a),
    ),
    "nic_egress_timeout_log": (
        "kernel.log / ice_debug lines for the egress HW-timestamp timeout (seq=X, descriptor "
        "status=timeout, mark_miss). Emulated E810.",
        {"type": "object", "properties": {"fault": {"type": "string"}}},
        lambda a: T.egress_timeout_log(a),
    ),
}

handle = mcp_core.make_handle(TOOLS, SERVER)

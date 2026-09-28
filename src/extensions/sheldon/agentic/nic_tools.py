"""Intel NIC hardware-timestamp evidence — the Intel MCP server's plane (EMULATED).

There is no real E810 here (ethtool -T is software-only — see the fidelity note in the PTP
walkthrough). This emulates the silicon-level evidence the diagram's Intel agent contributes —
ethtool counters, ice_debug descriptor status, and the kernel.log egress HW-timestamp timeout —
toggled by NIC_FAULT (or a per-call fault arg) so a demo shows baseline vs the egress-miss fault.
Labeled emulated everywhere.
"""
import os


def _faulted(a):
    f = (a or {}).get("fault")
    if f is not None:
        return str(f).lower() in ("1", "true", "yes")
    return os.environ.get("NIC_FAULT", "").lower() in ("1", "true", "yes")


def timestamp_counters(a=None):
    """ethtool -S: the tx hardware-timestamp counters that move on an egress miss."""
    if not _faulted(a):
        return {"plane": "hardware", "emulated": True, "source": "ethtool -S (emulated E810)",
                "signal": None,
                "counters": {"tx_hwtstamp_timeouts": 0, "ptp_tx_carryover": 0, "ptp_ts_fifo_overflow": 0}}
    return {"plane": "hardware", "emulated": True, "source": "ethtool -S (emulated E810)",
            "signal": "nic_firmware_suspect",
            "counters": {"tx_hwtstamp_timeouts": 1, "ptp_tx_carryover": 1, "ptp_ts_fifo_overflow": 1},
            "delta_ms": 74.4,
            "finding": "egress HW timestamp timeout — late/stale timestamp matched to next packet window"}


def egress_timeout_log(a=None):
    """kernel.log / ice_debug lines for the egress hardware-timestamp timeout."""
    if not _faulted(a):
        return {"plane": "hardware", "emulated": True, "source": "kernel.log / ice_debug", "lines": []}
    return {"plane": "hardware", "emulated": True, "source": "kernel.log / ice_debug",
            "signal": "nic_firmware_suspect", "seq": "X", "delta_ms": 74.4,
            "lines": [
                "kernel: ice 0000:.. : egress HW timestamp timeout for seq=X; late/stale timestamp popped, matched to next packet window",
                "ice_debug: descriptor status=timeout, mark_miss, carryover_possible, delta~74.4ms"]}

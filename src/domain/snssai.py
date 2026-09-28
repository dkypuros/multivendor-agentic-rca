"""
Canonical S-NSSAI model (network slicing wave 1, epic #11).

Spec anchors: an S-NSSAI identifies one network slice and is SST (slice/service type,
0-255, with standardized values — 1 eMBB, 2 URLLC, 3 MIoT) plus an optional SD (slice
differentiator, 3 octets) per TS 23.501 section 5.15.2 / TS 23.003 section 28.4.2.
JSON shape everywhere in this stack: {"sst": <int>[, "sd": "<6 hex digits>"]}.

The canonical STRING form — "SST" or "SST:SD", e.g. "1", "2", "1:000001" — is what the
ue_sim --nssai argument parses, and what metric slice labels and the billing bySlice
rollup keys use (a dict cannot be a label or a rollup key).

Shared by: UDM (subscribed NSSAIs), AMF (NSSF-lite: requested vs subscribed -> allowed),
SMF (slice-aware UPF selection), both UPFs (per-slice session tagging) and billing
(per-slice usage dimension). Stdlib only, like everything else in the owned stack.
"""


def normalize(snssai):
    """Validated copy of one S-NSSAI dict: {"sst": int[, "sd": str]}. Raises ValueError."""
    if not isinstance(snssai, dict):
        raise ValueError(f"S-NSSAI must be an object, got {snssai!r}")
    sst = snssai.get("sst")
    if isinstance(sst, bool) or not isinstance(sst, int) or not 0 <= sst <= 255:
        raise ValueError(f"S-NSSAI sst must be an integer 0-255, got {snssai!r}")
    out = {"sst": sst}
    sd = snssai.get("sd")
    if sd is not None:
        if not (isinstance(sd, str) and len(sd) == 6
                and all(c in "0123456789abcdefABCDEF" for c in sd)):
            raise ValueError(f"S-NSSAI sd must be 6 hex digits, got {snssai!r}")
        out["sd"] = sd.lower()
    return out


def parse(text):
    """Canonical string -> S-NSSAI dict: "1" -> {"sst": 1}, "1:000001" -> {"sst": 1, "sd": ...}."""
    sst_part, _, sd_part = str(text).strip().partition(":")
    try:
        sst = int(sst_part)
    except ValueError:
        raise ValueError(f"S-NSSAI string must be 'SST' or 'SST:SD', got {text!r}")
    return normalize({"sst": sst, "sd": sd_part} if sd_part else {"sst": sst})


def key(snssai):
    """S-NSSAI dict -> canonical string ("1", "2", "1:000001"). Lenient: never raises, so
    metric/rollup paths can label malformed or legacy records instead of dying."""
    try:
        s = normalize(snssai)
    except ValueError:
        return f"invalid:{snssai!r}"
    return f"{s['sst']}:{s['sd']}" if "sd" in s else f"{s['sst']}"


def contains(nssai, snssai):
    """True when the S-NSSAI appears in the list (exact sst+sd match, normalized)."""
    return key(snssai) in {key(s) for s in (nssai or [])}


def intersect(requested, subscribed):
    """Requested S-NSSAIs that are actually subscribed, in requested order (the NSSF-lite
    core: TS 23.501 section 5.15.5 allowed-NSSAI determination at procedure fidelity).
    Malformed requested entries never match anything."""
    subscribed_keys = {key(s) for s in (subscribed or [])}
    return [normalize(s) for s in (requested or []) if key(s) in subscribed_keys]

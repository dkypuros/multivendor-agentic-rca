"""
NSSF: the standalone Network Slice Selection Function (epic #9 core hardening).

This is the NF the AMF's embedded "NSSF-lite" (network slicing wave 1, epic #11) was always a
placeholder for. Wave 1 intersected the UE's requested NSSAI with the UDM's subscribed NSSAIs
INSIDE the AMF (services/core/amf/amf.py, ledgered in procedures/network_slicing.txt). This NF
lifts that decision out into a real service the AMF discovers via the NRF and calls over
Nnssf_NSSelection — and adds the piece the embedded lite could never have: a per-tracking-area
slice-availability POLICY (which S-NSSAIs a given TAC actually supports), TS 23.501 5.15.

Spec anchors (clean-room shapes, harvested from TS 29.531 — authored, nothing copied):
  Nnssf_NSSelection            TS 29.531 section 5.2 — NSSelectionGet for a registration:
                               (requestedNssai, subscribedNssai, tai) -> AuthorizedNetworkSliceInfo
                               (allowedNssai + configuredNssai + rejectedNssai). Resource path
                               /nnssf-nsselection/v1/network-slice-information preserves the real
                               3GPP shape so the learning transfers.
  Allowed NSSAI determination  TS 23.501 section 5.15.5 — allowed = requested that is BOTH
                               subscribed AND supported in the serving TA; no requested NSSAI ->
                               the configured NSSAI (subscribed ∩ supported), i.e. all subscribed
                               slices available here.
  Configured NSSAI             TS 23.501 section 5.15.4 — the subscribed slices the network is
                               configured to serve in this area.
  S-NSSAI identity             TS 23.501 section 5.15.2 / TS 23.003 28.4.2 via domain/snssai.py.
  Nnrf_NFManagement            TS 29.510 section 5.2 — registers with the NRF as NFType NSSF,
                               serviceName nnssf-nsselection (the AMF discovers it by NF type).

SIMPLIFICATIONS (ledgered in procedures/nssf_slice_selection.txt; every fake labeled here):
  - Slice-availability POLICY is a small in-process config table SLICE_CONFIG (TAC -> supported
    S-NSSAIs), NOT provisioned over Nnssf_NSSAIAvailability from the AMFs (TS 29.531 5.3). A TAC
    absent from the table is PERMISSIVE (supports whatever is subscribed) — so an AMF that sends
    no TAC, or a TAC we have not restricted, gets exactly the wave-1 intersect result: the NSSF
    is byte-compatible with NSSF-lite by construction, and only ADDS filtering where a TAC is
    explicitly restricted.
  - No roaming: no HPLMN/VPLMN S-NSSAI mapping (TS 29.531 6.1.6.2.3), no NSI/NRF-per-slice
    selection, no target-AMF redirection. Registration-time allowed/configured/rejected only;
    PDU-session NS selection and NSSAIAvailability are later waves.
  - Override the config table with TELCO_NSSF_SLICE_CONFIG (JSON, same schema) through netconfig's
    file <- env precedence, exactly like the SMF's TELCO_UPF_SELECTION.

Run: python3 nssf.py   (listens on 127.0.0.1:7007, registers with the NRF as NSSF)
"""

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import plmn, port, url, value
from domain.snssai import contains, key as snssai_key, normalize as snssai_normalize

PORT = port("nssf")
NRF = url("nrf")
_MCC, _MNC = plmn()[:3], plmn()[3:]

# SLICE-AVAILABILITY POLICY (labeled simplification, module docstring): which S-NSSAIs each
# tracking area is configured to serve (TS 23.501 5.15.4 configured NSSAI / 5.15.5 allowed
# NSSAI determination "supported in the serving TA"). Declarative, netconfig-style, keyed by
# TAC string. A TAC that is ABSENT here is PERMISSIVE — it supports whatever the subscription
# carries — so the default deployment (and any AMF that sends no TAC) reproduces the wave-1
# NSSF-lite intersect EXACTLY. A TAC that IS listed is RESTRICTED to its S-NSSAI list: a
# subscribed slice not on the list is filtered from both allowed and configured NSSAI (this is
# the policy the embedded lite could never express). Standardized SST values per TS 23.501
# 5.15.2: 1 eMBB, 2 URLLC, 3 MIoT.
#
# The DEFAULT table restricts ONE illustrative TAC (000002, an "eMBB-only" area — only {sst:1})
# and leaves everything else permissive, so nothing in the running stack changes until a UE is
# served in that specific TA. Override with TELCO_NSSF_SLICE_CONFIG (JSON object, same schema).
DEFAULT_SLICE_CONFIG = {
    "000002": [{"sst": 1}],
}


def load_slice_config():
    raw = value("TELCO_NSSF_SLICE_CONFIG")
    table = json.loads(raw) if raw else DEFAULT_SLICE_CONFIG
    # Validate every configured S-NSSAI loudly at startup (like the SMF validates UPF labels),
    # so a malformed policy fails at boot, not silently at a UE's registration.
    return {tac: [snssai_normalize(s) for s in snssais] for tac, snssais in table.items()}


SLICE_CONFIG = load_slice_config()
app = SbiApp("nssf")


def supported_in_ta(tac):
    """The S-NSSAIs this NSSF is configured to serve in a tracking area, or None for PERMISSIVE
    (TAC absent / not given -> supports whatever is subscribed). None is the wave-1-identical path."""
    if tac is None:
        return None
    return SLICE_CONFIG.get(str(tac))   # None when the TAC is not restricted


def ns_selection_for_registration(requested, subscribed, tac):
    """The Nnssf_NSSelection registration decision (TS 29.531 5.2.2 / TS 23.501 5.15.5), authored
    clean-room. Returns (allowedNssai, configuredNssai, rejectedNssai) as lists of normalized
    S-NSSAI dicts:

      configuredNssai = subscribed slices SUPPORTED in this TA (5.15.4)
      allowedNssai    = when the UE requested slices: those requested that are BOTH subscribed
                        AND supported here (5.15.5); when it requested nothing: the configured
                        NSSAI (every subscribed+supported slice) — the pre-slicing behavior
      rejectedNssai   = requested slices dropped (unsubscribed or unsupported-in-TA), for honesty
    """
    supported = supported_in_ta(tac)
    subscribed_norm = [snssai_normalize(s) for s in (subscribed or [])]

    def available(snssai):
        return supported is None or contains(supported, snssai)

    configured = [s for s in subscribed_norm if available(s)]

    allowed, rejected = [], []
    if requested:
        for r in requested:
            try:
                rn = snssai_normalize(r)
            except ValueError:
                rejected.append(r)   # malformed request entry: reject it, never match it
                continue
            if contains(subscribed_norm, rn) and available(rn):
                allowed.append(rn)
            else:
                rejected.append(rn)
    else:
        allowed = configured
    return allowed, configured, rejected


def _select_and_reply(requested, subscribed, tai):
    tac = (tai or {}).get("tac")
    allowed, configured, rejected = ns_selection_for_registration(requested, subscribed, tac)
    obs.log("ns_selection", tac=tac,
            requested=[snssai_key(s) for s in (requested or [])],
            allowed=[snssai_key(s) for s in allowed],
            rejected=[snssai_key(s) for s in rejected])
    obs.counter("ns_selections_total").inc()
    if rejected:
        obs.counter("ns_selection_rejected_snssai_total").inc()
    return 200, {"allowedNssai": allowed, "configuredNssai": configured,
                 "rejectedNssai": rejected, "tac": tac,
                 "policy": "permissive" if supported_in_ta(tac) is None else "configured"}


@app.route("POST", "/nnssf-nsselection/v1/network-slice-information")
def ns_selection_post(params, query, body):
    """Primary surface the AMF calls. Body: {requestedNssai?, subscribedNssai, tai?}. The
    subscribedNssai is mandatory (the NSSF is not the subscription authority — the UDM is; the
    AMF forwards what it read from am-data), exactly the split TS 29.531 draws."""
    if "subscribedNssai" not in body:
        return problem(400, "Bad Request", detail="subscribedNssai is mandatory",
                       cause="MANDATORY_IE_MISSING")
    return _select_and_reply(body.get("requestedNssai"), body.get("subscribedNssai"),
                             body.get("tai"))


@app.route("GET", "/nnssf-nsselection/v1/network-slice-information")
def ns_selection_get(params, query, body):
    """Harvest-faithful GET shape (TS 29.531 6.1.3.2 NSSelectionGet): the slice-info request
    rides as a JSON query parameter. Same decision as POST, offered for spec/curl inspection."""
    raw = query.get("slice-info-request-for-registration")
    if not raw:
        return problem(400, "Bad Request",
                       detail="slice-info-request-for-registration (JSON) query param is mandatory",
                       cause="MANDATORY_QUERY_PARAM_MISSING")
    try:
        slice_info = json.loads(raw)
    except ValueError as exc:
        return problem(400, "Bad Request", detail=f"invalid JSON in query param: {exc}")
    if "subscribedNssai" not in slice_info:
        return problem(400, "Bad Request", detail="subscribedNssai is mandatory",
                       cause="MANDATORY_IE_MISSING")
    tai = json.loads(query["tai"]) if query.get("tai") else None
    return _select_and_reply(slice_info.get("requestedNssai"), slice_info["subscribedNssai"], tai)


@app.route("GET", "/nssf/slice-config")
def get_slice_config(params, query, body):
    """The configured slice-availability policy, for inspection (the viewer + specs read this)."""
    return 200, {"plmn": {"mcc": _MCC, "mnc": _MNC},
                 "permissiveDefault": True,
                 "restrictedTacs": {tac: SLICE_CONFIG[tac] for tac in SLICE_CONFIG},
                 "note": "a TAC absent here is permissive (supports whatever is subscribed); "
                         "a listed TAC is restricted to its S-NSSAI list — see "
                         "procedures/nssf_slice_selection.txt"}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2 (same shape every owned NF registers with).
    profile = {"nfType": "NSSF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nnssf-nsselection",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


if __name__ == "__main__":
    obs.init("nssf")
    register_with_nrf()
    serve(app, PORT)

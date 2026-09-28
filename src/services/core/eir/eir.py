"""
5G-EIR: 5G Equipment Identity Register — the owned core's device-identity checkpoint.

The 5G-EIR answers one question the network keeps asking about the *equipment* (not the
subscriber): "is this device allowed on the network?" A UE presents a PEI (Permanent
Equipment Identifier — an IMEI or IMEISV), and the EIR classifies it against an identity
list into WHITELISTED (allowed), BLACKLISTED (barred — e.g. a reported-stolen handset), or
GREYLISTED (allowed but tracked). During registration the AMF invokes the EIR over N5g-eir
to bar stolen/blocked equipment before it ever gets a NAS security context.

This is a STANDALONE checkpoint NF: it registers itself with the NRF (so the AMF can
discover it) and serves exactly one read service — Nnrf discovery/registration aside, no
other NF is edited. Charter axis: BREADTH. Verb: HARVEST (TS 29.511 read for shape; the ODP
reference may not carry a 5G-EIR at all, so this is authored clean-room from the spec,
stdlib only).

Spec anchors:
  N5g-eir_EquipmentIdentityCheck    TS 29.511 section 5.2 (EquipmentIdentityCheck service):
                                    GET .../equipment-status?pei=<PEI>[&supi=<SUPI>] returns
                                    EirResponseData { status: EquipmentStatus }.
  EquipmentStatus enum              TS 29.511 section 6.1.6.3.3: WHITELISTED / BLACKLISTED /
                                    GREYLISTED.
  PEI / IMEI(SV) format             TS 23.003 section 6.2 (IMEI = 15 digits, IMEISV = 16
                                    digits); PEI string form "imei-<digits>" / "imeisv-<digits>"
                                    (TS 23.003 section 28.15.2, the 5GS PEI representation).
  EIR role in registration          TS 23.502 section 4.2.2.2 step: the AMF MAY retrieve the
                                    ME identity and check it with the 5G-EIR (Identity Request /
                                    N5g-eir_EquipmentIdentityCheck_Get) to bar barred equipment.
  Nnrf_NFManagement / NFDiscovery   TS 29.510 sections 5.2 / 5.3 (register as 5G-EIR, be found).

Labeled simplifications (ledgered in procedures/eir_equipment_identity.txt):
  - The identity list is a small IN-CODE labeled table (a default-whitelist policy plus a
    seeded BLACKLISTED "stolen" PEI and a GREYLISTED example). A real EIR is provisioned from
    an operator CEIR / GSMA device database. The table is the seam; a provisioning API /
    UDR-backed store is the follow-up. No PEI classification is faked — an unlisted PEI gets
    the honest default policy, not an invented verdict.
  - The AMF actually INVOKING the EIR during registration (Identity Request -> equipment check
    -> bar on BLACKLISTED) is a FOLLOW-UP integration; today the EIR is a standalone,
    discoverable checkpoint proven in isolation. No existing NF is edited.
  - The optional supi is accepted and logged (TS 29.511 allows correlating PEI to SUPI) but
    the verdict is decided on the PEI alone — no per-subscriber allow/deny yet.

Run: python3 eir.py   (SBI on 127.0.0.1:7026, registers with the NRF as 5G-EIR)
"""

import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import port, url
from domain.statestore import open_store

PORT = port("eir")
NRF = url("nrf")
app = SbiApp("eir")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). The identity list is keyed by PEI; each record carries its label and
# a provenance note so the classification is auditable, not a bare boolean.
store = open_store("eir")
identity_list = store.collection("equipment_identity_list")

# EquipmentStatus enum (TS 29.511 6.1.6.3.3). GREYLISTED = allowed but tracked.
WHITELISTED = "WHITELISTED"
BLACKLISTED = "BLACKLISTED"
GREYLISTED = "GREYLISTED"

# Default policy for a PEI the operator has never listed. Real operators run either a
# permissive (default-whitelist) or a strict (default-greylist/deny) posture; the lab default
# is WHITELISTED so an ordinary, un-flagged handset is allowed — the honest common case.
DEFAULT_STATUS = WHITELISTED

# Seed the identity list with a small, LABELED set so the checkpoint has something to bar:
# a reported-stolen device (BLACKLISTED) and a watched device (GREYLISTED). Everything else
# falls through to DEFAULT_STATUS. This table stands in for an operator CEIR / GSMA DB.
SEED = [
    {"pei": "imei-490154203237511", "status": BLACKLISTED,
     "reason": "reported stolen (lab seed — stands in for a CEIR blacklist entry)"},
    {"pei": "imei-359881234567890", "status": GREYLISTED,
     "reason": "under observation (lab seed — e.g. a model pending type-approval)"},
]

# PEI wire form (TS 23.003 6.2 / 28.15.2): imei-<15 digits> or imeisv-<16 digits>.
_PEI_RE = re.compile(r"^(imei-\d{15}|imeisv-\d{16})$")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def seed_identity_list():
    """Idempotently load the seeded, labeled identity entries into the store on startup."""
    for entry in SEED:
        if identity_list.get(entry["pei"]) is None:
            identity_list.put(entry["pei"], {**entry, "seededAt": now_iso()})


def valid_pei(pei):
    """A PEI is well-formed iff it is an IMEI (15 digits) or IMEISV (16 digits) in 5GS string
    form (TS 23.003). Returns True/False — a malformed PEI is a client error (400), never a
    silent WHITELISTED."""
    return bool(pei) and bool(_PEI_RE.match(pei))


def classify(pei):
    """Classify a well-formed PEI against the identity list. Listed PEIs return their labeled
    status + reason; an unlisted PEI returns DEFAULT_STATUS with an honest 'not listed' note.
    Never fabricates a verdict."""
    record = identity_list.get(pei)
    if record is not None:
        return record["status"], record.get("reason", "listed")
    return DEFAULT_STATUS, "not listed — default policy applied"


# ------------------------------------------- Equipment Identity Check (N5g-eir_EquipmentIdentityCheck)
# TS 29.511 5.2: the AMF (or any authorized consumer) GETs the equipment status for a PEI. The
# EIR answers WHITELISTED / BLACKLISTED / GREYLISTED. supi is optional correlation context.

@app.route("GET", "/n5g-eir-eic/v1/equipment-status")
def equipment_status(params, query, body):
    pei = query.get("pei")
    supi = query.get("supi")
    if not pei:
        obs.counter("eir_equipment_checks_rejected_total").inc()
        return problem(400, "Bad Request", detail="query parameter 'pei' is mandatory",
                       cause="MANDATORY_QUERY_PARAM_MISSING")
    if not valid_pei(pei):
        obs.counter("eir_equipment_checks_rejected_total").inc()
        obs.log("equipment_check_rejected", pei=pei, supi=supi, reason="malformed PEI")
        return problem(400, "Bad Request",
                       detail=f"'{pei}' is not a valid PEI (expected imei-<15 digits> or "
                              f"imeisv-<16 digits>, TS 23.003)",
                       cause="INVALID_QUERY_PARAM")
    status, reason = classify(pei)
    # EirResponseData (TS 29.511 6.1.6.2.2). We carry the reason + pei/supi as honest,
    # non-normative context; a strict client reads only .status.
    response = {"status": status, "pei": pei, "supi": supi, "reason": reason,
                "checkedAt": now_iso()}
    obs.log("equipment_checked", pei=pei, supi=supi, status=status)
    obs.counter("eir_equipment_checks_total", status=status).inc()
    return 200, response


# ---------------------------------------------------- small provisioning read (lab convenience)
# Not part of N5g-eir; lets the viewer / an operator inspect the seeded list. Read-only.

@app.route("GET", "/n5g-eir-eic/v1/identity-list")
def get_identity_list(params, query, body):
    return 200, {"defaultStatus": DEFAULT_STATUS,
                 "entries": sorted(identity_list.values(), key=lambda r: r["pei"])}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. nfType 5G-EIR; the N5g-eir service name is advertised so
    # the AMF (the future consumer) can discover the equipment-check surface.
    profile = {"nfType": "5G-EIR", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "n5g-eir-eic",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("eir_identity_list_size").set(len(list(identity_list.values())))


if __name__ == "__main__":
    obs.init("eir")
    obs.on_scrape(_scrape)
    seed_identity_list()
    register_with_nrf()
    serve(app, PORT)

"""
NSSAAF: Network Slice-Specific Authentication and Authorization Function (SBA-completion wave).

The NSSAAF is the NF that runs Network Slice-Specific Authentication and Authorization (NSSAA):
for the S-NSSAIs a subscription marks as "subject to NSSAA", a UE is not merely ALLOWED the slice
on the basis of its subscription — it must additionally authenticate to a slice-owner's AAA server
(an enterprise/DN credential, distinct from the 5G primary auth the AUSF already ran). This closes
the slicing story: the NSSF decides which slices are allowed by subscription+area (Nnssf_NSSelection,
services/core/nssf/nssf.py); the NSSAAF decides which of those additionally pass a slice-specific
credential check before they land in the UE's Allowed NSSAI.

It sits at the AMF-facing Nnssaaf_NSSAA reference point and reaches "southbound" to a slice AAA
server (AAA-S), reached directly or via a AAA proxy (AAA-P). Here the AAA-S is MODELED in-process
(labeled below); the AMF-driven trigger during registration is a follow-up (ledger). No other NF is
edited — the NSSAAF is a standalone authenticator.

Spec anchors (clean-room shapes, authored from the specs — the parts depot has no NSSAAF):
  Nnssaaf_NSSAA (Authenticate)     TS 29.526 section 5.2 (Nnssaaf_NSSAA_Authenticate): given the UE
                                   identity (SUPI + GPSI) and one S-NSSAI, the NSSAAF runs the
                                   slice-specific authentication against the AAA-S and returns the
                                   result. Resource /nnssaaf-nssaa/v1/authenticate preserves the real
                                   3GPP shape so the learning transfers.
  Nnssaaf_NSSAA (Re-auth/Revoke)   TS 29.526 section 5.2 (ReAuthenticationNotification /
                                   RevocationNotification): the AAA-S, later, can trigger a
                                   re-authentication or REVOKE a UE's authorization for a slice; the
                                   NSSAAF drives that toward the AMF. Modeled here as endpoints that
                                   flip the stored per-(SUPI,S-NSSAI) status.
  NSSAA procedure                  TS 23.501 section 5.15.10 — some S-NSSAIs are subject to NSSAA per
                                   subscription (the UDM marks them); the AMF, after primary auth,
                                   triggers NSSAA per such S-NSSAI; on success the S-NSSAI is added to
                                   the Allowed NSSAI, on failure it is rejected; the result is stored
                                   and the AAA-S may re-auth/revoke at any time.
  EAP framework                    TS 33.501 Annex — NSSAA carries EAP between UE<->AMF<->NSSAAF<->AAA-S,
                                   the AMF as EAP authenticator (pass-through). Modeled at procedure
                                   fidelity as a single credential exchange (labeled).
  S-NSSAI identity                 TS 23.501 section 5.15.2 / TS 23.003 28.4.2 via domain/snssai.py.
  Nnrf_NFManagement                TS 29.510 section 5.2 — registers with the NRF as NFType NSSAAF,
                                   serviceName nnssaaf-nssaa (the AMF discovers it by NF type).

SIMPLIFICATIONS (ledgered in procedures/nssaaf_slice_auth.txt; every fake labeled here):
  - The AAA server is MODELED in-process (authenticate_at_aaa + the NSSAA_POLICY table below), NOT a
    real external AAA-S reached over RADIUS/Diameter/EAP. A subscriber's slice credential is a shared
    secret in the policy table; a real deployment runs a multi-round EAP method to a real AAA-S via a
    AAA-P (TS 23.501 5.15.10, TS 33.501). Honest at procedure fidelity: the decision (AUTHENTICATED /
    REJECTED / PENDING) and the AAA-triggered re-auth/revocation are real; the wire is not.
  - Which S-NSSAIs require NSSAA (and their authorized subscribers + credentials) is a small in-process
    config table NSSAA_POLICY, NOT read from the UDM subscription (TS 23.501 5.15.10 marks S-NSSAIs
    subject to NSSAA in the subscription). A slice ABSENT from the table is PERMISSIVE — it does NOT
    require NSSAA and is authorized without a challenge (mirrors the NSSF's permissive-default stance).
  - The AMF-driven trigger during registration is NOT wired: the AMF does not yet discover the NSSAAF
    and run NSSAA per subject-to-NSSAA S-NSSAI before writing the Allowed NSSAI. This NF is exercised
    by a direct Nnssaaf_NSSAA call today; the registration join is the follow-up (ledger).
  - Override the policy table with TELCO_NSSAAF_NSSAA_POLICY (JSON, same schema) through netconfig's
    file <- env precedence, exactly like the NSSF's TELCO_NSSF_SLICE_CONFIG.

Run: python3 nssaaf.py   (SBI on 127.0.0.1:7025, registers with the NRF as NSSAAF)
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import plmn, port, url, value
from domain.snssai import key as snssai_key, normalize as snssai_normalize
from domain.statestore import open_store

PORT = port("nssaaf")
NRF = url("nrf")
_MCC, _MNC = plmn()[:3], plmn()[3:]

# NSSAA POLICY (labeled simplification, module docstring): which S-NSSAIs are subject to NSSAA, the
# AAA server that owns each, and the authorized subscribers with their slice credential. Declarative,
# netconfig-style, keyed by the canonical S-NSSAI STRING (domain/snssai.py key(), e.g. "1", "3:000003").
# A slice ABSENT here is PERMISSIVE — it does NOT require NSSAA and is authorized without a challenge,
# so the default deployment only gates the ONE illustrative slice below. This is the modeled AAA-S:
# the "credential" is the shared secret a UE must present for that (subscriber, slice) pair.
#
# The DEFAULT table gates ONE enterprise MIoT slice (SST=3, SD=000003, "subject to NSSAA") owned by a
# modeled AAA-S, authorizing exactly ONE seed subscriber (imsi-001010000000002) with a known secret.
# SST values per TS 23.501 5.15.2: 1 eMBB, 2 URLLC, 3 MIoT. Override with TELCO_NSSAAF_NSSAA_POLICY.
DEFAULT_NSSAA_POLICY = {
    "3:000003": {
        "requiresNssaa": True,
        "aaaServer": "aaa-enterprise-miot.example.com",
        "subscribers": {
            "imsi-001010000000002": {
                "gpsi": "msisdn-14155550102",
                "credential": "slice3-enterprise-secret",
            },
        },
    },
}


def load_nssaa_policy():
    raw = value("TELCO_NSSAAF_NSSAA_POLICY")
    table = json.loads(raw) if raw else DEFAULT_NSSAA_POLICY
    # Validate every gated S-NSSAI loudly at startup (like the NSSF validates its slice config), so a
    # malformed policy fails at boot, not silently at a UE's slice-auth. The key is the canonical
    # S-NSSAI string; normalizing round-trips it and rejects a bad SST/SD.
    validated = {}
    for k, entry in table.items():
        snssai_key(snssai_normalize(_snssai_from_key(k)))  # raises on a malformed key
        validated[k] = entry
    return validated


def _snssai_from_key(k):
    """Canonical S-NSSAI string ("3" or "3:000003") -> dict, for startup validation."""
    sst_part, _, sd_part = str(k).partition(":")
    out = {"sst": int(sst_part)}
    if sd_part:
        out["sd"] = sd_part
    return out


NSSAA_POLICY = load_nssaa_policy()
app = SbiApp("nssaaf")

# Per-(SUPI, S-NSSAI) authentication context: the current NSSAA result, so a later re-auth or
# revocation can flip it and a status read reflects it. sqlite3 when TELCO_STATE_DIR is set,
# in-memory otherwise (domain/statestore.py), like every other NF.
store = open_store("nssaaf")
auth_contexts = store.collection("nssaa_auth_contexts")

# The NSSAA result values (TS 23.501 5.15.10 outcome, procedure fidelity):
#   AUTHENTICATED  the (SUPI, S-NSSAI) passed slice-specific auth -> eligible for the Allowed NSSAI
#   REJECTED       the credential check failed or the subscriber is not authorized for the slice
#   PENDING        the NSSAA is in progress (challenge issued, no credential presented yet)
#   REVOKED        the AAA-S revoked a previously-granted authorization (denied, like REJECTED)
AUTHENTICATED, REJECTED, PENDING, REVOKED = "AUTHENTICATED", "REJECTED", "PENDING", "REVOKED"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ctx_key(supi, snssai_str):
    return f"{supi}|{snssai_str}"


def authenticate_at_aaa(supi, snssai_str, credentials):
    """The MODELED AAA server (labeled simplification). Decides the NSSAA outcome for one
    (SUPI, S-NSSAI, credential) triple, authored clean-room from TS 23.501 5.15.10:

      slice not subject to NSSAA (absent from policy)  -> AUTHENTICATED, challenged=False
      subject to NSSAA, no credential presented        -> PENDING (EAP challenge outstanding)
      subject, credential matches the authorized secret-> AUTHENTICATED, challenged=True
      subject, subscriber not authorized on the slice  -> REJECTED (SUBSCRIBER_NOT_AUTHORIZED)
      subject, wrong credential                        -> REJECTED (AUTHENTICATION_FAILED)

    Returns (result, challenged, cause, aaaServer)."""
    entry = NSSAA_POLICY.get(snssai_str)
    if not entry or not entry.get("requiresNssaa"):
        return AUTHENTICATED, False, None, None            # permissive: allowed without challenge
    aaa = entry.get("aaaServer")
    sub = (entry.get("subscribers") or {}).get(supi)
    if sub is None:
        return REJECTED, True, "SUBSCRIBER_NOT_AUTHORIZED", aaa
    if credentials is None:
        return PENDING, True, "AUTHENTICATION_PENDING", aaa  # challenge issued, awaiting credential
    if str(credentials) == str(sub.get("credential")):
        return AUTHENTICATED, True, None, aaa
    return REJECTED, True, "AUTHENTICATION_FAILED", aaa


def _record(supi, gpsi, snssai_norm, snssai_str, result, challenged, cause, aaa, ctx_id=None):
    record = {
        "authCtxId": ctx_id or uuid.uuid4().hex,
        "supi": supi, "gpsi": gpsi,
        "snssai": snssai_norm, "snssaiKey": snssai_str,
        "authResult": result,
        "nssaaRequired": bool(NSSAA_POLICY.get(snssai_str, {}).get("requiresNssaa")),
        "challenged": challenged,
        "authorized": result == AUTHENTICATED,
        "aaaServer": aaa,
        "cause": cause,
        "updatedAt": now_iso(),
    }
    auth_contexts.put(ctx_key(supi, snssai_str), record)
    return record


# ------------------------------------------------------------- Nnssaaf_NSSAA (Authenticate)
# TS 29.526 5.2.2: the AMF hands the NSSAAF a UE identity and one S-NSSAI; the NSSAAF runs the
# slice-specific auth against the AAA-S and returns AUTHENTICATED / REJECTED / PENDING.

@app.route("POST", "/nnssaaf-nssaa/v1/authenticate")
def authenticate(params, query, body):
    supi = body.get("supi")
    snssai = body.get("snssai")
    if not supi or snssai is None:
        return problem(400, "Bad Request", detail="supi and snssai are mandatory",
                       cause="MANDATORY_IE_MISSING")
    try:
        snssai_norm = snssai_normalize(snssai)
    except ValueError as exc:
        return problem(400, "Bad Request", detail=f"invalid S-NSSAI: {exc}",
                       cause="INVALID_SNSSAI")
    snssai_str = snssai_key(snssai_norm)
    gpsi = body.get("gpsi")
    credentials = body.get("credentials")

    result, challenged, cause, aaa = authenticate_at_aaa(supi, snssai_str, credentials)
    record = _record(supi, gpsi, snssai_norm, snssai_str, result, challenged, cause, aaa)

    obs.log("nssaa_authenticate", supi=supi, snssai=snssai_str, result=result,
            challenged=challenged, nssaaRequired=record["nssaaRequired"], cause=cause)
    obs.counter("nssaaf_authentications_total", result=result).inc()
    return 200, record


# --------------------------------------------------- Nnssaaf_NSSAA (Re-auth / Revocation)
# TS 29.526 5.2: the AAA-S, later, triggers a re-authentication or REVOKES a UE's authorization for
# a slice; the NSSAAF drives that toward the AMF. Modeled as status flips on the stored context.

@app.route("POST", "/nnssaaf-nssaa/v1/re-authenticate")
def re_authenticate(params, query, body):
    """AAA-triggered re-authentication (TS 29.526 ReAuthenticationNotification): the prior result is
    invalidated and the (SUPI, S-NSSAI) goes PENDING until the UE re-authenticates."""
    supi, snssai = body.get("supi"), body.get("snssai")
    if not supi or snssai is None:
        return problem(400, "Bad Request", detail="supi and snssai are mandatory",
                       cause="MANDATORY_IE_MISSING")
    snssai_str = snssai_key(snssai_normalize(snssai))
    existing = auth_contexts.get(ctx_key(supi, snssai_str))
    if existing is None:
        return problem(404, "Not Found", detail="no NSSAA context for this (supi, snssai)",
                       cause="CONTEXT_NOT_FOUND")
    record = _record(supi, existing.get("gpsi"), existing["snssai"], snssai_str,
                     PENDING, True, "REAUTHENTICATION_REQUESTED",
                     existing.get("aaaServer"), ctx_id=existing.get("authCtxId"))
    obs.log("nssaa_reauthenticate", supi=supi, snssai=snssai_str)
    obs.counter("nssaaf_reauthentications_total").inc()
    return 200, record


@app.route("POST", "/nnssaaf-nssaa/v1/revoke")
def revoke(params, query, body):
    """AAA-triggered revocation (TS 29.526 RevocationNotification): a previously-granted slice
    authorization is REVOKED — the S-NSSAI must be removed from the UE's Allowed NSSAI."""
    supi, snssai = body.get("supi"), body.get("snssai")
    if not supi or snssai is None:
        return problem(400, "Bad Request", detail="supi and snssai are mandatory",
                       cause="MANDATORY_IE_MISSING")
    snssai_str = snssai_key(snssai_normalize(snssai))
    existing = auth_contexts.get(ctx_key(supi, snssai_str))
    if existing is None:
        return problem(404, "Not Found", detail="no NSSAA context for this (supi, snssai)",
                       cause="CONTEXT_NOT_FOUND")
    record = _record(supi, existing.get("gpsi"), existing["snssai"], snssai_str,
                     REVOKED, existing.get("challenged", True), "AUTHORIZATION_REVOKED",
                     existing.get("aaaServer"), ctx_id=existing.get("authCtxId"))
    obs.log("nssaa_revoke", supi=supi, snssai=snssai_str)
    obs.counter("nssaaf_revocations_total").inc()
    return 200, record


@app.route("GET", "/nnssaaf-nssaa/v1/status")
def status(params, query, body):
    """Current NSSAA status for a (supi, snssai): ?supi=<supi>&snssai=<canonical string>. Returns the
    stored context, or NOT_AUTHENTICATED when the pair has never been through NSSAA."""
    supi = query.get("supi")
    snssai_q = query.get("snssai")
    if not supi or not snssai_q:
        return problem(400, "Bad Request", detail="supi and snssai query params are mandatory",
                       cause="MANDATORY_QUERY_PARAM_MISSING")
    snssai_str = snssai_key(snssai_normalize(_snssai_from_key(snssai_q)))
    record = auth_contexts.get(ctx_key(supi, snssai_str))
    if record is None:
        return 200, {"supi": supi, "snssaiKey": snssai_str, "authResult": "NOT_AUTHENTICATED",
                     "authorized": False,
                     "nssaaRequired": bool(NSSAA_POLICY.get(snssai_str, {}).get("requiresNssaa"))}
    return 200, record


@app.route("GET", "/nssaaf/policy")
def get_policy(params, query, body):
    """The configured NSSAA policy, for inspection (the viewer + specs read this). Credentials are
    NOT echoed — only which slices require NSSAA, their AAA server, and the authorized SUPIs."""
    slices = {}
    for k, entry in NSSAA_POLICY.items():
        slices[k] = {"requiresNssaa": bool(entry.get("requiresNssaa")),
                     "aaaServer": entry.get("aaaServer"),
                     "authorizedSubscribers": sorted((entry.get("subscribers") or {}).keys())}
    return 200, {"plmn": {"mcc": _MCC, "mnc": _MNC},
                 "permissiveDefault": True,
                 "nssaaSlices": slices,
                 "note": "a slice absent here does NOT require NSSAA (authorized without challenge); "
                         "a listed slice is subject to NSSAA against its AAA server — see "
                         "procedures/nssaaf_slice_auth.txt"}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2 (same shape every owned NF registers with).
    profile = {"nfType": "NSSAAF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nnssaaf-nssaa",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("nssaaf_auth_contexts_active").set(len(list(auth_contexts.values())))


if __name__ == "__main__":
    obs.init("nssaaf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

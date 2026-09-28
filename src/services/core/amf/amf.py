"""
AMF: registration and session management brokering over an NGAP-like N2 modeled as JSON-over-HTTP.

Spec anchors:
  Registration procedure   TS 23.502 section 4.2.2.2.2
  PDU session procedure    TS 23.502 section 4.3.2.2 (AMF relays to SMF per TS 29.502 5.2.2)
  NAS messages             TS 24.501 sections 5.5.1, 5.4.1, 5.4.2, 6.4.1
  5GMM / 5GSM causes       TS 24.501 sections 9.11.3.2 and 9.11.4.2 (numeric values carried)
  N2 semantics             TS 38.413 (InitialUEMessage, Uplink/DownlinkNASTransport; PDU session
                           tunnel info travels as n2SmInfo beside the NAS payload, not inside it)
  GUAMI structure          TS 29.571 section 5.4.4.18
  RM state machine, simplified per TS 24.501 section 5.1.3:
      DEREGISTERED -> AUTH_PENDING -> SECURITY_MODE -> REGISTERED
  (AUTH_PENDING / SECURITY_MODE are internal sub-states of 5GMM-COMMON-PROCEDURE-INITIATED)

Remaining labeled simplifications: SUPI in the clear (no SUCI), fake 5G-AKA (see udm.py). NEA0/NIA0
in SecurityModeCommand are REAL null-algorithm identifiers (TS 33.501 5.11.1), legal and honest here.

Primary authentication (5G-AKA) runs THROUGH a standalone AUSF over N12. TWO code paths, chosen
at run time (discover_ausf below):

  STANDALONE AUSF (breadth axis — the real path): when a real AUSF NF is registered with the NRF
  (services/core/ausf, distinct from the UDM), the AMF initiates and confirms 5G-AKA against it —
  Nausf_UEAuthentication (TS 29.509), the AUSF pulling the vector from the UDM over N13. The AMF's
  auth_via_ausf_total proves the real AUSF served the exchange.

  AUSF-LITE FALLBACK (SIMPLIFICATION, ledgered in procedures/ausf_authentication.txt): when NO
  standalone AUSF is registered/reachable, the AMF falls back to the UDM-embedded AUSF-lite that
  always co-hosted /nausf-auth on the UDM's own port. The request/response shapes are IDENTICAL,
  so the fallback is BYTE-IDENTICAL to the pre-split path and every pre-AUSF spec
  (run_registration_spec, run_pdu_session_spec, golden, ...) runs UNEDITED.

Slice selection (network slicing, TS 23.501 section 5.15.5): the allowed NSSAI on the
registration accept. TWO code paths, chosen at run time (compute_allowed_nssai below):

  STANDALONE NSSF (epic #9 — the real path): when an NSSF is registered with the NRF, the AMF
  discovers it and calls Nnssf_NSSelection (TS 29.531) — (requestedNssai, subscribedNssai, tai)
  -> allowedNssai — so the decision (and any per-TA slice-availability policy) lives in the
  dedicated NF, not here. This is procedures/nssf_slice_selection.txt.

  NSSF-LITE FALLBACK (network slicing wave 1, epic #11 — SIMPLIFICATION, ledgered in
  procedures/network_slicing.txt): when NO NSSF is registered/reachable, the AMF falls back to
  the embedded intersect it always did — the UE's requested NSSAI ∩ the UDM's subscribed NSSAIs,
  no requested NSSAI -> the full subscribed NSSAI (byte-identical to pre-slicing). The fallback
  is EXACTLY wave 1, so every pre-NSSF spec (run_slicing_spec, run_nsmf_spec, ...) that does not
  start an NSSF is untouched.

DEEP INTEGRATION (this wave — three standalone NFs the AMF now USES, each GATED on discovery;
ledgered in procedures/amf_integrations.txt). Every step below degrades to a NO-OP when its NF
is ABSENT or unreachable, so with none of the three registered the AMF is BYTE-IDENTICAL to the
pre-integration path and every existing spec passes UNEDITED:

  5G-EIR device check (TS 23.502 4.2.2.2 / TS 29.511, check_equipment below): after successful
  primary auth, if a 5G-EIR is discoverable the AMF checks the UE's PEI. A BLACKLISTED device is
  rejected with 5GMM cause 5 "PEI not accepted" (TS 24.501 9.11.3.2). The UE-sim sends no PEI, so
  the AMF SYNTHESIZES a stable, well-formed per-SUPI IMEI (synthesize_pei) — a real UE supplies
  its own. Metric auth_eir_checks_total.

  NSSAAF slice-specific auth (TS 23.501 5.15.10 / TS 29.526, apply_nssaa below): if an NSSAAF is
  discoverable, for each NSSAA-GATED S-NSSAI in the allowed NSSAI the AMF triggers Nnssaaf_NSSAA;
  a REJECTED/REVOKED slice is removed from the allowed NSSAI. A slice not gated (permissive, per
  the NSSAAF policy) is left untouched. Metric nssaa_triggered_total.

  LMF location anchor (TS 23.273 / TS 29.572, get_ue_location below): GET
  /amf/ue-contexts/{supi}/location, if an LMF is discoverable, calls Nlmf_Location
  DetermineLocation and returns the estimate — the AMF as the GMLC/LMF location anchor. Metric
  amf_location_total. No LMF -> 404 LMF_NOT_AVAILABLE (a new endpoint, so no existing flow moves).

Run: python3 amf.py   (listens on 127.0.0.1:7002, registers with the NRF as AMF)
"""

import hashlib
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import plmn, port, url
from domain.netconfig import tac as default_tac
from domain.snssai import contains, intersect, key as snssai_key

PORT = port("amf")
NRF = url("nrf")
_MCC, _MNC = plmn()[:3], plmn()[3:]
_TAC = default_tac()   # serving Tracking Area Code; the LADN service area is a set of these
SERVING_NETWORK_NAME = (f"5G:mnc{_MNC.zfill(3)}.mcc{_MCC}"
                        ".3gppnetwork.org")   # SNN format per TS 24.501 9.12.1
GUAMI = {"plmnId": {"mcc": _MCC, "mnc": _MNC}, "amfRegionId": "CA", "amfSetId": "FE0",
         "amfPointer": "00"}
app = SbiApp("amf")
ue_contexts = {}


def discover(nf_type):
    status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                  f"?target-nf-type={nf_type}&requester-nf-type=AMF")
    instances = body.get("nfInstances", [])
    if not instances:
        raise LookupError(f"NRF has no {nf_type}")
    endpoint = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{endpoint['ipv4Address']}:{endpoint['port']}"


def discover_ausf():
    """(real_ausf_base | None, udm_base). Prefer a STANDALONE AUSF — an AUSF NF whose endpoint is
    NOT the UDM's (the UDM has always co-hosted the embedded AUSF-lite on its own port, see
    udm.py). udm_base is the fallback target for that embedded AUSF-lite. Returning None means no
    real AUSF is registered -> the AMF takes the byte-identical UDM-embedded path."""
    udm_base = discover("UDM")
    status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                  f"?target-nf-type=AUSF&requester-nf-type=AMF")
    for inst in body.get("nfInstances", []):
        ep = inst["nfServices"][0]["ipEndPoints"][0]
        base = f"http://{ep['ipv4Address']}:{ep['port']}"
        if base != udm_base:
            return base, udm_base   # a real, standalone AUSF
    return None, udm_base           # only the UDM-embedded AUSF-lite -> fallback


def auth_initiate(supi):
    """Begin 5G-AKA. Prefer the standalone AUSF; on its absence OR unreachability fall back to
    the UDM-embedded AUSF-lite (byte-identical to the pre-split path). Returns
    (status, authBody, ausfBase, viaRealAusf) — the caller keeps ausfBase so the confirmation
    hits the SAME endpoint that issued the challenge."""
    real, udm_base = discover_ausf()
    if real is not None:
        try:
            status, auth = request("POST", f"{real}/nausf-auth/v1/ue-authentications",
                                   {"supiOrSuci": supi,
                                    "servingNetworkName": SERVING_NETWORK_NAME})
            obs.counter("auth_via_ausf_total").inc()
            return status, auth, real, True
        except OSError:
            pass   # real AUSF unreachable -> FALLBACK to the UDM-embedded AUSF-lite
    # FALLBACK (labeled): the UDM-embedded AUSF-lite, exactly the pre-split direct-UDM auth.
    status, auth = request("POST", f"{udm_base}/nausf-auth/v1/ue-authentications",
                           {"supiOrSuci": supi, "servingNetworkName": SERVING_NETWORK_NAME})
    obs.counter("auth_via_udm_fallback_total").inc()
    return status, auth, udm_base, False


def _nssf_lite(requested, subscribed):
    """The embedded NSSF-lite fallback — EXACTLY network slicing wave 1 (epic #11): requested ∩
    subscribed, or the full subscribed NSSAI when the UE requested nothing. This is the pre-NSSF
    behavior, byte-identical, so a stack with no NSSF registered is unchanged."""
    return intersect(requested, subscribed) if requested else subscribed


def compute_allowed_nssai(requested, subscribed):
    """Allowed NSSAI (TS 23.501 5.15.5). Discover the standalone NSSF via the NRF and let it
    decide over Nnssf_NSSelection (TS 29.531); on ANY miss — no NSSF registered, unreachable, or
    a non-200 — fall back to the embedded NSSF-lite so the stack degrades to wave 1 exactly.
    The AMF sends the plmn but no TAC (the lab UE carries none), so the NSSF's per-TA policy is
    permissive here and its result equals the lite intersect — additive, back-compatible."""
    try:
        nssf = discover("NSSF")
    except LookupError:
        return _nssf_lite(requested, subscribed)   # no NSSF -> wave-1 embedded path
    try:
        status, info = request("POST", f"{nssf}/nnssf-nsselection/v1/network-slice-information",
                               {"requestedNssai": requested or [], "subscribedNssai": subscribed,
                                "tai": {"plmnId": {"mcc": _MCC, "mnc": _MNC}}})
    except OSError:
        return _nssf_lite(requested, subscribed)   # NSSF unreachable -> fall back
    if status != 200:
        return _nssf_lite(requested, subscribed)   # NSSF errored -> fall back
    obs.counter("nssf_selections_total").inc()
    return info.get("allowedNssai") or []


def _luhn_check_digit(digits14):
    """The IMEI check digit (TS 23.003 6.2.1 uses the Luhn algorithm over the 14 TAC+SNR digits)."""
    total = 0
    for i, ch in enumerate(reversed(digits14)):
        d = int(ch)
        if i % 2 == 0:      # rightmost of the 14 is doubled (it becomes the 2nd-from-right of 15)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def synthesize_pei(supi):
    """Default per-SUPI PEI (TS 23.003 28.15.2 5GS PEI form "imei-<15 digits>") when the UE
    presents none. A DETERMINISTIC, well-formed IMEI derived from the SUPI: a fixed lab TAC
    ("35000000") + 6 SNR digits hashed from the SUPI + a real Luhn check digit, so the same UE
    always presents the same PEI. LABELED synthesis — a real UE supplies its own IMEI/PEI, this
    is the AMF filling the Identity Request the UE-sim does not model."""
    snr = f"{int(hashlib.sha1(supi.encode()).hexdigest(), 16) % 10**6:06d}"
    digits14 = "35000000" + snr
    return "imei-" + digits14 + _luhn_check_digit(digits14)


def check_equipment(supi, pei):
    """5G-EIR device-identity check (N5g-eir_EquipmentIdentityCheck, TS 29.511; the AMF's
    Identity Request step per TS 23.502 4.2.2.2). GATED on discovery: an ABSENT or unreachable
    5G-EIR, or any non-200, returns (barred=False, status=None) so registration proceeds
    BYTE-IDENTICALLY. When the EIR answers, a BLACKLISTED verdict bars the equipment."""
    try:
        eir = discover("5G-EIR")
    except LookupError:
        return False, None                      # no EIR registered -> byte-identical, no check
    try:
        status, body = request("GET", f"{eir}/n5g-eir-eic/v1/equipment-status"
                                       f"?pei={pei}&supi={supi}")
    except OSError:
        return False, None                      # EIR unreachable -> fall through, no check
    if status != 200:
        return False, None                      # EIR errored -> fall through, no check
    verdict = body.get("status")
    obs.counter("auth_eir_checks_total", status=verdict).inc()
    obs.log("equipment_identity_checked", supi=supi, pei=pei, equipmentStatus=verdict)
    return verdict == "BLACKLISTED", verdict


def apply_nssaa(supi, gpsi, allowed):
    """Network Slice-Specific Authentication and Authorization (NSSAA, TS 23.501 5.15.10) over
    Nnssaaf_NSSAA (TS 29.526). GATED on discovery: an ABSENT or unreachable NSSAAF — or an empty
    gated set — returns `allowed` UNCHANGED, so a stack with no NSSAAF is BYTE-IDENTICAL. When an
    NSSAAF is present, for each NSSAA-gated S-NSSAI in the allowed NSSAI the AMF triggers
    slice-specific auth; a REJECTED/REVOKED slice is DROPPED from the allowed NSSAI. A slice the
    policy does not gate (permissive default) is never challenged and never dropped."""
    try:
        nssaaf = discover("NSSAAF")
    except LookupError:
        return allowed                          # no NSSAAF -> byte-identical, no NSSAA
    try:
        pstatus, policy = request("GET", f"{nssaaf}/nssaaf/policy")
    except OSError:
        return allowed                          # NSSAAF unreachable -> fall through unchanged
    if pstatus != 200:
        return allowed
    gated = set((policy.get("nssaaSlices") or {}).keys())
    if not gated:
        return allowed                          # nothing is subject to NSSAA -> unchanged
    kept = []
    for s in allowed:
        skey = snssai_key(s)
        if skey not in gated:
            kept.append(s)                      # not subject to NSSAA -> keep, no challenge
            continue
        try:
            astatus, ares = request("POST", f"{nssaaf}/nnssaaf-nssaa/v1/authenticate",
                                    {"supi": supi, "gpsi": gpsi, "snssai": s})
        except OSError:
            kept.append(s)                      # unreachable mid-flow -> fail-open, keep the slice
            continue
        result = ares.get("authResult") if astatus == 200 else None
        obs.counter("nssaa_triggered_total", result=result or "ERROR").inc()
        if result in ("REJECTED", "REVOKED"):
            obs.log("nssaa_slice_rejected", supi=supi, snssai=skey, cause=ares.get("cause"))
            continue                            # slice failed NSSAA -> drop from allowed NSSAI
        kept.append(s)
    return kept


def nas_dl(message_type, fields, extra=None):
    reply = {"procedure": "DownlinkNASTransport", "nas": {"type": message_type, **fields}}
    if extra:
        reply.update(extra)
    return 200, reply


@app.route("POST", "/n2/ue-messages")
def n2_message(params, query, body):
    nas = body.get("nas", {})
    handlers = {
        "RegistrationRequest": handle_registration_request,
        "AuthenticationResponse": handle_authentication_response,
        "SecurityModeComplete": handle_security_mode_complete,
        "PduSessionEstablishmentRequest": handle_pdu_session_request,
    }
    handler = handlers.get(nas.get("type"))
    if handler is None:
        return problem(400, "Bad Request", detail=f"unsupported NAS type {nas.get('type')}",
                       cause="UNSUPPORTED_NAS_MESSAGE")
    return handler(nas, body)


def handle_registration_request(nas, body):
    supi = nas["supi"]
    # 5G-AKA initiate over N12: through the standalone AUSF, or the byte-identical UDM-embedded
    # AUSF-lite fallback. ausfBase is remembered so the confirmation reaches the same NF.
    status, auth, ausf_base, via_real = auth_initiate(supi)
    if status != 201:
        # 5GMM cause 3 "Illegal UE" (TS 24.501 9.11.3.2) for unknown or inactive subscription
        return nas_dl("RegistrationReject", {"gmmCause": 3, "causeText": "Illegal UE"})
    # Requested NSSAI (TS 24.501 9.11.3.37) is optional in the RegistrationRequest; carried on
    # the UE context until the NSSF-lite step resolves it against the subscription (below).
    # PEI (TS 23.003 28.15.2) for the later 5G-EIR check. The UE-sim models no Identity Request,
    # so synthesize a stable per-SUPI IMEI when none is presented (labeled, synthesize_pei). This
    # is additive: with no EIR discoverable the stored PEI is never read and nothing changes.
    pei = nas.get("pei") or synthesize_pei(supi)
    ue_contexts[supi] = {"state": "AUTH_PENDING", "authCtxId": auth["authCtxId"],
                        "ausfBase": ausf_base, "authViaRealAusf": via_real,
                        "pei": pei,
                        "requestedNssai": nas.get("requestedNssai")}
    challenge = auth["5gAuthData"]
    return nas_dl("AuthenticationRequest", {"rand": challenge["rand"], "autn": challenge["autn"]})


def handle_authentication_response(nas, body):
    supi = nas["supi"]
    ctx = ue_contexts.get(supi)
    if ctx is None or ctx["state"] != "AUTH_PENDING":
        return problem(400, "Bad Request", detail=f"no authentication pending for {supi}",
                       cause="UE_CONTEXT_STATE_MISMATCH")
    # Confirm RES* against the SAME NF that issued the challenge (real AUSF or the fallback
    # UDM-embedded AUSF-lite). On success the AUSF returns KSEAF (TS 33.501 6.1.3.1).
    status, result = request(
        "PUT",
        f"{ctx['ausfBase']}/nausf-auth/v1/ue-authentications/{ctx['authCtxId']}/5g-aka-confirmation",
        {"resStar": nas.get("resStar")},
    )
    if status != 200 or result.get("authResult") != "AUTHENTICATION_SUCCESS":
        ue_contexts.pop(supi, None)
        return nas_dl("AuthenticationReject", {})
    ctx["state"] = "SECURITY_MODE"
    ctx["kseaf"] = result.get("kseaf")   # anchor key from the AUSF (None on the fallback path)
    # NEA0/NIA0: real null-algorithm identifiers per TS 33.501 5.11.1
    return nas_dl("SecurityModeCommand", {"ciphering": "NEA0", "integrity": "NIA0"})


def handle_security_mode_complete(nas, body):
    supi = nas["supi"]
    ctx = ue_contexts.get(supi)
    if ctx is None or ctx["state"] != "SECURITY_MODE":
        return problem(400, "Bad Request", detail=f"unexpected SecurityModeComplete for {supi}",
                       cause="UE_CONTEXT_STATE_MISMATCH")
    udm = discover("UDM")
    status, am = request("GET", f"{udm}/nudm-sdm/v2/{supi}/am-data")
    if status != 200:
        # 5GMM cause 7 "5GS services not allowed" (TS 24.501 9.11.3.2)
        obs.log("registration_rejected", supi=supi, gmmCause=7)
        obs.counter("registration_rejects_total").inc()
        return nas_dl("RegistrationReject", {"gmmCause": 7, "causeText": "5GS services not allowed"})
    # 5G-EIR device-identity check (TS 23.502 4.2.2.2): after successful primary auth, bar
    # BLACKLISTED equipment before it lands a NAS security context. GATED on discovery — no EIR
    # -> no check, byte-identical (check_equipment). 5GMM cause 5 "PEI not accepted"
    # (TS 24.501 9.11.3.2) is the standard reject for barred equipment.
    barred, equipment_status = check_equipment(supi, ctx.get("pei"))
    if barred:
        obs.log("registration_rejected", supi=supi, gmmCause=5, pei=ctx.get("pei"),
                equipmentStatus=equipment_status)
        obs.counter("registration_rejects_total").inc()
        ue_contexts.pop(supi, None)
        return nas_dl("RegistrationReject", {"gmmCause": 5, "causeText": "PEI not accepted"})
    if equipment_status is not None:
        ctx["equipmentStatus"] = equipment_status   # WHITELISTED/GREYLISTED — audit trail
    # Allowed NSSAI (TS 23.501 5.15.5): the standalone NSSF decides when present, else the
    # embedded NSSF-lite fallback (compute_allowed_nssai, module docstring). Nothing left
    # allowed against a nonempty request -> 5GMM cause 62 "No network slices available"
    # (TS 24.501 9.11.3.2), real behavior at procedure fidelity.
    subscribed = am.get("subscribedNssai") or am.get("nssai") or []
    requested = ctx.get("requestedNssai")
    allowed = compute_allowed_nssai(requested, subscribed)
    # NSSAA (TS 23.501 5.15.10): slice-specific auth for NSSAA-gated S-NSSAIs; a REJECTED slice is
    # dropped. GATED on discovery — no NSSAAF -> allowed unchanged, byte-identical (apply_nssaa).
    allowed = apply_nssaa(supi, am.get("gpsi"), allowed)
    if requested and not allowed:
        obs.log("registration_rejected", supi=supi, gmmCause=62,
                requestedNssai=[snssai_key(s) for s in requested])
        obs.counter("registration_rejects_total").inc()
        return nas_dl("RegistrationReject",
                      {"gmmCause": 62, "causeText": "No network slices available"})
    ctx.update(state="REGISTERED", guami=GUAMI, allowedNssai=allowed)
    obs.log("registration_accepted", supi=supi, state="REGISTERED",
            allowedNssai=[snssai_key(s) for s in allowed])
    obs.counter("registrations_total").inc()
    return nas_dl("RegistrationAccept", {"guami": GUAMI, "allowedNssai": allowed})


def handle_pdu_session_request(nas, body):
    supi = nas["supi"]
    ctx = ue_contexts.get(supi)
    if ctx is None or ctx["state"] != "REGISTERED":
        # 5GMM cause 7: session management refused while not registered
        return nas_dl("PduSessionEstablishmentReject",
                      {"gmmCause": 7, "causeText": "5GS services not allowed",
                       "cause": "UE_NOT_REGISTERED"})
    # Slice admission (wave 1): the SM message's S-NSSAI must be inside the allowed NSSAI the
    # registration accept granted, or the AMF does not forward it to the SMF — 5GMM cause 90
    # "Payload was not forwarded" (TS 24.501 9.11.3.2, UL NAS transport handling per 5.4.5).
    snssai = nas.get("sNssai") or {"sst": 1}
    if not contains(ctx.get("allowedNssai"), snssai):
        obs.log("pdu_session_rejected", supi=supi, gmmCause=90, snssai=snssai_key(snssai),
                allowedNssai=[snssai_key(s) for s in ctx.get("allowedNssai") or []])
        return nas_dl("PduSessionEstablishmentReject",
                      {"gmmCause": 90, "causeText": "Payload was not forwarded",
                       "cause": "SNSSAI_NOT_IN_ALLOWED_NSSAI"})
    gnb_gtpu = body.get("gnbGtpu") or nas.get("gnbGtpu")   # N2-level tunnel info from the gNB
    smf = discover("SMF")
    # ueLocation (TS 29.502 SmContextCreateData) is what lets the SMF honour a LADN
    # service area: the same DNN anchors at a different PSA depending on which Tracking
    # Area the subscriber is in. The RAN may report the TAC on the request; otherwise this
    # AMF serves one configured area.
    ue_tac = body.get("tac") or nas.get("tac") or ctx.get("tac") or _TAC
    status, sm = request("POST", f"{smf}/nsmf-pdusession/v1/sm-contexts",
                         {"supi": supi, "pduSessionId": nas.get("pduSessionId", 1),
                          "dnn": nas.get("dnn", "internet"),
                          "sNssai": snssai,
                          "ueLocation": {"nrLocation": {"tai": {
                              "plmnId": {"mcc": _MCC, "mnc": _MNC}, "tac": ue_tac}}},
                          "gnbGtpu": gnb_gtpu})
    if status != 201:
        # 5GSM cause 31 "Request rejected, unspecified" (TS 24.501 9.11.4.2)
        return nas_dl("PduSessionEstablishmentReject",
                      {"gsmCause": 31, "causeText": "Request rejected, unspecified"})
    ctx.setdefault("pduSessions", {})[str(sm["pduSessionId"])] = sm["seid"]
    obs.log("pdu_session_established", supi=supi, seid=sm["seid"], dnn=sm["dnn"],
            snssai=snssai_key(sm["sNssai"]))
    obs.counter("pdu_sessions_total").inc()
    # NAS accept carries the PDU address (TS 24.501 9.11.4.10); tunnel info rides as n2SmInfo
    return nas_dl("PduSessionEstablishmentAccept",
                  {"pduSessionId": sm["pduSessionId"], "pduAddress": sm["ueIp"],
                   "dnn": sm["dnn"], "sNssai": sm["sNssai"]},
                  extra={"n2SmInfo": {"ulTeid": sm["ulTeid"], "dlTeid": sm["dlTeid"],
                                      "upfGtpu": sm["upfGtpu"]}})


@app.route("GET", "/amf/ue-contexts/{supi}")
def get_ue_context(params, query, body):
    ctx = ue_contexts.get(params["supi"])
    if ctx is None:
        return problem(404, "Not Found", detail=f"no UE context for {params['supi']}")
    return 200, ctx


@app.route("GET", "/amf/ue-contexts/{supi}/location")
def get_ue_location(params, query, body):
    """The AMF as the location anchor (the GMLC/LMF path, TS 23.273): resolve the served UE's
    position by invoking Nlmf_Location DetermineLocation (TS 29.572) on a discovered LMF. GATED
    on discovery — no LMF -> 404 LMF_NOT_AVAILABLE. This is a NEW endpoint, so no existing flow
    moves; ?horizontalAccuracy=<m> forwards a LocationQoS to steer the positioning method."""
    supi = params["supi"]
    ctx = ue_contexts.get(supi)
    if ctx is None:
        return problem(404, "Not Found", detail=f"no UE context for {supi}",
                       cause="UE_CONTEXT_NOT_FOUND")
    try:
        lmf = discover("LMF")
    except LookupError:
        return problem(404, "Not Found",
                       detail="no LMF discoverable — location service unavailable",
                       cause="LMF_NOT_AVAILABLE")
    input_data = {"supi": supi}
    accuracy = query.get("horizontalAccuracy")
    if accuracy:
        try:
            input_data["locationQoS"] = {"horizontalAccuracy": float(accuracy)}
        except (TypeError, ValueError):
            pass
    try:
        status, loc = request("POST", f"{lmf}/nlmf-loc/v1/determine-location", input_data)
    except OSError:
        return problem(502, "Bad Gateway", detail="LMF unreachable", cause="LMF_UNREACHABLE")
    if status != 200:
        return status, loc                      # honest passthrough (e.g. 404 UNKNOWN_TARGET_UE)
    obs.log("amf_location", supi=supi, positioningMethod=loc.get("positioningMethod"),
            servingCell=loc.get("servingCell"))
    obs.counter("amf_location_total").inc()
    # The AMF surfaces the estimate as an Namf location result (the GMLC-facing shape).
    return 200, {"supi": supi,
                 "locationEstimate": {"latitude": loc.get("latitude"),
                                      "longitude": loc.get("longitude"),
                                      "altitude": loc.get("altitude")},
                 "accuracy": loc.get("accuracy"),
                 "positioningMethod": loc.get("positioningMethod"),
                 "ageOfLocationEstimate": loc.get("ageOfLocationEstimate"),
                 "servingCell": loc.get("servingCell"),
                 "locationSource": "LMF"}


def register_with_nrf():
    profile = {"nfType": "AMF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "namf-comm",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


if __name__ == "__main__":
    obs.init("amf")
    register_with_nrf()
    serve(app, PORT)

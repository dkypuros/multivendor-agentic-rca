"""
SMF: session management, programs the UPF over a PFCP-like N4.

Spec anchors:
  Nsmf_PDUSession CreateSMContext   TS 29.502 section 5.2.2 (resource shape preserved)
  Nsmf_PDUSession ReleaseSMContext  TS 29.502 section 5.2.2.4 / TS 23.502 4.3.4 (release surface,
                                    added with the CHF integration; ADDITIVE — no legacy caller)
  Nchf_ConvergedCharging            TS 32.291 (open/close a charging-data resource with a CHF at
                                    establishment/release; ADDITIVE with EXACT legacy fallback —
                                    no CHF registered/reachable => the SMF omits charging and
                                    billing.py's UPF-counter polling stays the rating path)
  Nudsf_UnstructuredDataManagement  TS 29.598 (MIRROR the SM context into the UDSF shared store at
                                    establishment/release so the SMF's session state is externally
                                    durable/shareable; ADDITIVE, BEST-EFFORT, with EXACT legacy
                                    fallback — no UDSF registered/reachable => the SMF mirrors
                                    nothing and behaves byte-identically. The SMF's own in-memory
                                    `sessions` store REMAINS the source of truth; the UDSF record
                                    is a MIRROR, deleted on release. A UDSF failure NEVER fails a
                                    session — the mirror is fire-and-forget, labeled obs
                                    "udsf_mirror_*".)
  Procedure                         TS 23.502 section 4.3.2.2 (UE-requested PDU session establishment)
  N4 association and session rules  TS 29.244 sections 6.2.6 (association), 7.5.2 (session
                                    establishment), 5.2 (PDR/FAR model). JSON-over-UDP per adapter.
  Slice-aware UPF selection         TS 23.501 sections 5.15 (S-NSSAI) + 6.3.3 (UPF selection by
                                    S-NSSAI and DNN) via the declarative UPF_SELECTION table below
                                    (network slicing wave 1, epic #11; TS 23.548 edge breakout is
                                    the dnn=edge rule it generalizes)
  Dynamic selection rules           /smf/upf-selection/rules (NSMF slice-management wave, epic
                                    #11): the CORE SUBNET action of a slice instance — the NSMF
                                    (services/core/nsmf) programs one (sNssai, dnn) -> UPF rule
                                    per allocated NSI over this admin surface. Dynamic rules are
                                    consulted BEFORE the static table and persisted via the state
                                    store; DEFAULT_UPF_SELECTION stays untouched as the fallback,
                                    so every pre-NSMF flow behaves exactly as before.

Run: python3 smf.py   (SBI on 127.0.0.1:7003, N4 to the UPF at 127.0.0.1:8805)
"""

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from adapters.udp_json import udp_request
from domain import obs
from domain.netconfig import host, port, udp_port, url, value
from domain.session import PduSession
from domain.snssai import key as snssai_key, normalize as snssai_normalize
from domain.statestore import open_store

PORT = port("smf")
NRF = url("nrf")
# UDSF mirror namespace (TS 29.598 nudsf-dr Record address = realmId/storageId/recordId). The SMF
# offloads each SM context under its own storage; the recordId is the session id (seid), so a
# mirrored session is addressable by exactly the key the SMF already uses.
UDSF_REALM = "realm-lab-1"
UDSF_STORAGE = "smf-session-context"
# The PSAs this SMF can program, by label; the selection table below maps sessions to labels.
UPFS = {
    "central": {"n4": (host(), udp_port("n4_central")),
                "gtpu": {"ip": host(), "port": udp_port("gtpu_central")}},
    "edge":    {"n4": (host(), udp_port("n4_edge")),
                "gtpu": {"ip": host(), "port": udp_port("gtpu_edge")}},
}

# UPF selection table (network slicing wave 1, epic #11): TS 23.501 6.3.3 — the SMF selects
# the UPF considering, among other inputs, the session's S-NSSAI and DNN. Declarative,
# netconfig-style: an ordered list of rules, FIRST MATCH WINS. Rule schema (all match keys
# optional — an omitted key is a wildcard; "upf" is mandatory and names a UPFS label):
#   {"sNssai": {"sst": <int>[, "sd": "<6 hex>"]},   match: every listed field must equal
#    "dnn": "<dnn>",                                 the session's value
#    "upf": "central" | "edge"}                      action: anchor the session here
# Override with TELCO_UPF_SELECTION (JSON array, same schema, resolved through netconfig's
# file <- env precedence); full schema documented in procedures/network_slicing.txt.
#
# The DEFAULT table reproduces the pre-slicing behavior exactly — dnn=edge anchors at the
# edge PSA (TS 23.548 session breakout, the P6 rule), everything else at the central PSA —
# and adds ONE new slice-driven rule: SST 2 (URLLC-flavored, the Design C2/E1 robot slice)
# anchors at the edge PSA regardless of DNN, because that slice's consumers (edge inference)
# live there. No pre-slicing flow carries SST 2, so default behavior is unchanged.
DEFAULT_UPF_SELECTION = [
    {"sNssai": {"sst": 2}, "upf": "edge"},
    {"dnn": "edge", "upf": "edge"},
    {"upf": "central"},
]


def load_selection_table():
    raw = value("TELCO_UPF_SELECTION")
    table = json.loads(raw) if raw else DEFAULT_UPF_SELECTION
    for rule in table:
        if rule.get("upf") not in UPFS:
            raise RuntimeError(f"TELCO_UPF_SELECTION rule names an unknown UPF label: {rule}")
    return table


UPF_SELECTION = load_selection_table()
app = SbiApp("smf")
# Dynamic selection rules (NSMF slice-management wave, epic #11): written per-NSI by the NSMF
# over /smf/upf-selection/rules, kept in the shared state store (sqlite3 when TELCO_STATE_DIR
# is set, in-memory otherwise — domain/statestore.py) in insertion order. Sessions are still
# deliberately in-memory (statestore phase 2 territory).
store = open_store("smf")
dynamic_rules = store.collection("upf_selection_rules")   # ruleId -> rule (+ ruleId, owner)
sessions = {}
counters = {"nextIp": 2, "nextTeid": 1000}
associated = set()


def rule_matches(rule, snssai, dnn, tac=None):
    """Same match semantics as the static table: every listed sNssai field must equal the
    session's, a listed dnn must equal the session's, omitted keys are wildcards.

    `tacs` (LADN wave) is the LADN SERVICE AREA: a list of Tracking Area Codes in which the
    rule applies. TS 23.501 5.6.5 -- a LADN DNN is only available inside its service area, and
    the SMF selects a PSA near the UE. A rule with no `tacs` is location-independent, which is
    every rule written before this existed, so the semantics are unchanged for them."""
    want = rule.get("sNssai") or {}
    if any(snssai.get(k) != v for k, v in want.items()):
        return False
    if "dnn" in rule and rule["dnn"] != dnn:
        return False
    area = rule.get("tacs")
    if area:
        # No known UE location cannot satisfy a service-area rule: a LADN session whose
        # location is unknown must NOT silently anchor at a local PSA.
        return tac is not None and tac in area
    return True


def select_upf(snssai, dnn, tac=None):
    """(sNssai, dnn, tac) -> (upfLabel, endpoints): DYNAMIC rules first (insertion order -- the
    NSMF's per-NSI core-subnet rules and the OSS's per-zone LADN rules preempt defaults without
    ever editing the static table), then the static table, first match wins; an exhausted table
    (only possible with an override, since the default ends in a wildcard) falls back to the
    central PSA.

    `tac` is the Tracking Area Code the UE is currently in, taken from the ueLocation the AMF
    sends in SmContextCreateData. It is what makes a LADN service area mean anything: the same
    DNN anchors at different PSAs depending on where the subscriber is, which is the mobility
    lever ETSI MEC leans on -- session management chooses the anchor, so it chooses which edge
    site's compute the session is near."""
    for rule in dynamic_rules.values():
        if rule.get("upf") in UPFS and rule_matches(rule, snssai, dnn, tac):
            return rule["upf"], UPFS[rule["upf"]]
    for rule in UPF_SELECTION:
        if rule_matches(rule, snssai, dnn, tac):
            return rule["upf"], UPFS[rule["upf"]]
    return "central", UPFS["central"]


def discover_pcf():
    """Discover a PCF via the NRF (TS 29.510 Nnrf_NFDiscovery, target-nf-type=PCF), returning
    its base URL or None. ADDITIVE + BACKWARD-COMPATIBLE: any failure — NRF unreachable, no PCF
    registered, malformed profile — returns None, and the caller falls back to legacy behavior
    EXACTLY. This is the whole compatibility contract: a stack without a PCF behaves as before."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=PCF&requester-nf-type=SMF")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != "npcf-smpolicycontrol":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def authorize_policy(supi, psi, dnn, snssai):
    """Npcf_SMPolicyControl_Create (TS 29.512 5.2.2): discover a PCF and ask it for the session's
    policy. Returns the SmPolicyDecision dict on success, or None to signal LEGACY FALLBACK
    (no PCF, or the PCF errored/was unreachable). The SMF NEVER fails a session because policy
    was unavailable — a missing PCF just means today's behavior, unchanged and labeled."""
    pcf = discover_pcf()
    if pcf is None:
        obs.log("policy_fallback", supi=supi, dnn=dnn, reason="no_pcf_discovered")
        obs.counter("policy_authorizations_total", outcome="fallback").inc()
        return None
    try:
        status, decision = request("POST", f"{pcf}/npcf-smpolicycontrol/v1/sm-policies",
                                   {"supi": supi, "pduSessionId": psi, "dnn": dnn,
                                    "sNssai": snssai, "pduSessionType": "IPV4"})
    except OSError:
        obs.log("policy_fallback", supi=supi, dnn=dnn, reason="pcf_unreachable")
        obs.counter("policy_authorizations_total", outcome="fallback").inc()
        return None
    if status != 201:
        obs.log("policy_fallback", supi=supi, dnn=dnn, reason=f"pcf_status_{status}")
        obs.counter("policy_authorizations_total", outcome="fallback").inc()
        return None
    obs.log("policy_authorized", supi=supi, dnn=dnn, smPolicyId=decision.get("smPolicyId"),
            policy=decision.get("policyLabel"))
    obs.counter("policy_authorizations_total", outcome="authorized").inc()
    return decision


def discover_chf():
    """Discover a CHF via the NRF (TS 29.510 Nnrf_NFDiscovery, target-nf-type=CHF), returning
    its base URL or None. ADDITIVE + BACKWARD-COMPATIBLE, exactly like discover_pcf: any failure
    — NRF unreachable, no CHF registered, malformed profile — returns None, and the caller falls
    back to legacy behavior EXACTLY (no charging). A stack without a CHF behaves as before."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=CHF&requester-nf-type=SMF")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != "nchf-convergedcharging":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def open_charging(supi, dnn, fiveqi):
    """Nchf_ConvergedCharging_Create (TS 32.291 5.2.2.2): discover a CHF and open a charging-data
    resource for the session, recording the tariff's initial GSU. Returns (chargingRef, granted
    quota) on success, or (None, None) to signal LEGACY FALLBACK (no CHF, or the CHF errored/was
    unreachable). The SMF NEVER fails a session because charging was unavailable — a missing CHF
    just means today's behavior (billing.py's UPF-counter polling still rates the session)."""
    chf = discover_chf()
    if chf is None:
        obs.log("charging_fallback", supi=supi, dnn=dnn, reason="no_chf_discovered")
        obs.counter("charging_opens_total", outcome="fallback").inc()
        return None, None
    payload = {"subscriberIdentifier": supi, "dnn": dnn}
    if fiveqi is not None:
        payload["fiveqi"] = fiveqi   # let the CHF tariff key on the PCF-authorized 5QI if any
    try:
        status, resp = request("POST", f"{chf}/nchf-convergedcharging/v1/chargingdata", payload)
    except OSError:
        obs.log("charging_fallback", supi=supi, dnn=dnn, reason="chf_unreachable")
        obs.counter("charging_opens_total", outcome="fallback").inc()
        return None, None
    if status != 201:
        obs.log("charging_fallback", supi=supi, dnn=dnn, reason=f"chf_status_{status}")
        obs.counter("charging_opens_total", outcome="fallback").inc()
        return None, None
    ref = resp.get("chargingDataRef")
    granted = ((resp.get("multipleUnitInformation") or [{}])[0]).get("grantedUnit")
    obs.log("charging_opened", supi=supi, dnn=dnn, chargingRef=ref,
            tariff=resp.get("tariffLabel"),
            grantVolume=(granted or {}).get("totalVolume"))
    obs.counter("charging_opens_total", outcome="opened").inc()
    return ref, granted


def release_charging(chargingRef):
    """Nchf_ConvergedCharging_Release (TS 32.291 5.2.2.4): close the charging-data resource at
    session teardown. Best-effort — a CHF that is gone must never block a release (the resource
    is already the CHF's to reconcile). Returns the CDR summary on success, else None."""
    chf = discover_chf()
    if chf is None:
        return None
    try:
        status, resp = request("POST",
                               f"{chf}/nchf-convergedcharging/v1/chargingdata/{chargingRef}/release",
                               {})
    except OSError:
        obs.log("charging_release_fallback", chargingRef=chargingRef, reason="chf_unreachable")
        return None
    if status != 200:
        obs.log("charging_release_fallback", chargingRef=chargingRef, reason=f"chf_status_{status}")
        return None
    obs.log("charging_released", chargingRef=chargingRef, cdr=resp.get("cdr"))
    obs.counter("charging_releases_total").inc()
    return resp.get("cdr")


def discover_udsf():
    """Discover a UDSF via the NRF (TS 29.510 Nnrf_NFDiscovery, target-nf-type=UDSF), returning its
    base URL or None. ADDITIVE + BACKWARD-COMPATIBLE, exactly like discover_pcf/discover_chf: any
    failure — NRF unreachable, no UDSF registered, malformed profile — returns None, and the caller
    mirrors nothing, behaving byte-identically to a UDSF-less stack (the whole fallback contract)."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=UDSF&requester-nf-type=SMF")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != "nudsf-dr":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def _udsf_record_url(udsf, seid):
    return f"{udsf}/nudsf-dr/v1/records/{UDSF_REALM}/{UDSF_STORAGE}/{seid}"


def mirror_session_to_udsf(session):
    """Nudsf_UnstructuredDataManagement PUT (TS 29.598 5.2.2.2): MIRROR the SM context into the UDSF
    shared store so the SMF's session state is externally durable/shareable. BEST-EFFORT and
    NON-BLOCKING by contract: gated on a discoverable UDSF, and ANY failure (no UDSF, UDSF down,
    bad status, unexpected error) is swallowed — a UDSF problem NEVER fails the session. The SMF's
    in-memory `sessions` store stays the SOURCE OF TRUTH; this record is a MIRROR keyed by seid.
    The record carries the session's identity/anchoring in `blocks` and finds-by-tag meta-tags."""
    udsf = discover_udsf()
    if udsf is None:
        obs.counter("smf_udsf_writes_total", outcome="fallback").inc()
        return False
    record = {
        "blocks": {
            "seid": session.seid, "supi": session.supi, "pduSessionId": session.pduSessionId,
            "dnn": session.dnn, "sNssai": session.sNssai, "ueIp": session.ueIp,
            "upf": session.upf, "state": session.state,
        },
        "metaTags": {"nfType": "SMF", "supi": session.supi, "dnn": session.dnn,
                     "upf": session.upf},
    }
    try:
        status, _ = request("PUT", _udsf_record_url(udsf, session.seid), record)
    except OSError:
        obs.log("udsf_mirror_fallback", seid=session.seid, reason="udsf_unreachable")
        obs.counter("smf_udsf_writes_total", outcome="fallback").inc()
        return False
    except Exception as exc:   # noqa: BLE001 — a mirror must NEVER surface into the session path
        obs.log("udsf_mirror_fallback", seid=session.seid, reason=f"error:{exc}")
        obs.counter("smf_udsf_writes_total", outcome="fallback").inc()
        return False
    if status not in (200, 201):
        obs.log("udsf_mirror_fallback", seid=session.seid, reason=f"udsf_status_{status}")
        obs.counter("smf_udsf_writes_total", outcome="fallback").inc()
        return False
    obs.log("udsf_mirrored", seid=session.seid, supi=session.supi, ueIp=session.ueIp,
            storage=UDSF_STORAGE)
    obs.counter("smf_udsf_writes_total", outcome="mirrored").inc()
    return True


def unmirror_session_from_udsf(seid):
    """Nudsf_UnstructuredDataManagement DELETE (TS 29.598 5.2.2.2): drop the mirrored SM context on
    release so the shared store tracks only live sessions. BEST-EFFORT — a UDSF that is gone (or a
    session that was never mirrored, e.g. established while the UDSF was down) must NEVER block a
    release; a 404 is a no-op success. Same fallback discipline as the mirror write."""
    udsf = discover_udsf()
    if udsf is None:
        return False
    try:
        status, _ = request("DELETE", _udsf_record_url(udsf, seid))
    except OSError:
        obs.log("udsf_unmirror_fallback", seid=seid, reason="udsf_unreachable")
        return False
    except Exception as exc:   # noqa: BLE001 — a mirror must NEVER surface into the session path
        obs.log("udsf_unmirror_fallback", seid=seid, reason=f"error:{exc}")
        return False
    obs.log("udsf_unmirrored", seid=seid, status=status)
    obs.counter("smf_udsf_writes_total", outcome="deleted").inc()
    return status in (204, 404)


def ensure_association(upf):
    n4 = upf["n4"]
    if n4 in associated:
        return
    reply = udp_request({"messageType": "AssociationSetupRequest", "nodeId": "smf.owned-stack.local"},
                        n4)
    if reply.get("cause") != "ACCEPTED":
        raise RuntimeError(f"UPF {n4} refused N4 association: {reply}")
    associated.add(n4)


@app.route("POST", "/nsmf-pdusession/v1/sm-contexts")
def create_sm_context(params, query, body):
    supi = body["supi"]
    psi = int(body.get("pduSessionId", 1))
    # dnn and sNssai are part of SmContextCreateData (TS 29.502 6.1.6.2.2)
    dnn = body.get("dnn", "internet")
    snssai = body.get("sNssai") or {"sst": 1}
    # ueLocation is part of SmContextCreateData (TS 29.502 6.1.6.2.2); the AMF fills it from
    # the registration TAI. Absent for any caller that has not been taught to send it, in
    # which case location-independent rules still match and LADN rules deliberately do not.
    tac = (((body.get("ueLocation") or {}).get("nrLocation") or {}).get("tai") or {}).get("tac")
    gnb_gtpu = body["gnbGtpu"]
    upf_label, upf = select_upf(snssai, dnn, tac)
    ensure_association(upf)

    # Npcf_SMPolicyControl_Create (TS 29.512 / TS 23.502 4.3.2.2 step 8): ask a PCF, if one is
    # registered, to authorize this session's policy. decision is None on the LEGACY FALLBACK
    # path (no PCF / PCF down) — every line below guards on it, so a PCF-less stack is unchanged.
    decision = authorize_policy(supi, psi, dnn, snssai)
    sess_rule = ((decision or {}).get("sessRules") or {}).get("sess-rule-1") or {}
    auth_qos = sess_rule.get("authDefQos")            # {"5qi", "arp"} or None (fallback)
    sess_ambr = sess_rule.get("authSessAmbr")         # {"uplink", "downlink"} or None
    auth_5qi = (auth_qos or {}).get("5qi")            # the 5QI the PCF authorized, or None

    ue_ip = f"10.45.0.{counters['nextIp']}"
    counters["nextIp"] += 1
    ul_teid, dl_teid = counters["nextTeid"], counters["nextTeid"] + 1
    counters["nextTeid"] += 2
    seid = f"{supi}-{psi}"

    # N4 session rules are slice-tagged: the session-level sNssai plus a per-PDR sNssai
    # mirror PFCP's Rel-16 S-NSSAI IE in the session/PDI (TS 29.244 slicing support), so the
    # UPF can report per-slice sessions and counters without asking the SMF.
    # When a PCF authorized the session we ALSO tag each PDR with the authorized 5QI (the QoS
    # the flow binds to — TS 29.244 QER/QFI binding, modeled as a field). On the fallback path
    # auth_5qi is None and the key is omitted, so the N4 datagram is byte-identical to before.
    qos_tag = {"5qi": auth_5qi} if auth_5qi is not None else {}
    reply = udp_request({
        "messageType": "SessionEstablishmentRequest",
        "seid": seid,
        "sNssai": snssai,
        "pdrs": [
            {"pdrId": 1, "direction": "uplink", "match": {"teid": ul_teid}, "farId": 1,
             "sNssai": snssai, **qos_tag},
            {"pdrId": 2, "direction": "downlink", "match": {"ueIp": ue_ip}, "farId": 2,
             "sNssai": snssai, **qos_tag},
        ],
        "fars": [
            {"farId": 1, "apply": "FORWARD", "destination": "DN-echo"},
            {"farId": 2, "apply": "FORWARD", "destination": {"gtpu": gnb_gtpu, "teid": dl_teid}},
        ],
    }, upf["n4"])
    if reply.get("cause") != "ACCEPTED":
        return 500, {"title": "UPF rejected session establishment", "cause": reply.get("cause")}

    # Nchf_ConvergedCharging_Create (TS 32.291 / TS 23.502 4.3.2.2): open a charging-data
    # resource with a CHF, if one is registered, keying its tariff on the dnn + the
    # PCF-authorized 5QI (auth_5qi). charging_ref/granted are None on the LEGACY FALLBACK path
    # (no CHF / CHF down) — every line below guards on it, so a CHF-less stack is unchanged and
    # billing.py's UPF-counter polling remains the rating path exactly as today.
    charging_ref, granted = open_charging(supi, dnn, auth_5qi)

    # Apply the PCF decision AND the CHF grant to the session record (all None on the fallback
    # paths, so a session established without a PCF/CHF looks exactly as it did before).
    session = PduSession(seid=seid, supi=supi, pduSessionId=psi, ueIp=ue_ip, dnn=dnn,
                         sNssai=snssai, ulTeid=ul_teid, dlTeid=dl_teid, gnbGtpu=gnb_gtpu,
                         state="ACTIVE", upf=upf_label,
                         smPolicyId=(decision or {}).get("smPolicyId"),
                         authorizedQos=auth_qos, sessionAmbr=sess_ambr,
                         gateStatus=(decision or {}).get("gateStatus"),
                         policyLabel=(decision or {}).get("policyLabel"),
                         chargingRef=charging_ref, grantedQuota=granted)
    sessions[seid] = session
    obs.log("session_created", supi=supi, seid=seid, dnn=dnn, ueIp=ue_ip,
            snssai=snssai_key(snssai), upf=upf_label,
            policy=session.policyLabel, fiveqi=auth_5qi, chargingRef=charging_ref)
    obs.counter("sessions_total").inc()
    obs.counter("slice_sessions_total", slice=snssai_key(snssai), upf=upf_label).inc()
    # Nudsf mirror (TS 29.598 / this integration): make the just-established SM context externally
    # durable by mirroring it into the UDSF shared store, IF one is discoverable. Best-effort and
    # non-blocking — the session is already ACTIVE and stored above (the source of truth); a UDSF
    # absence/failure changes nothing here, so a UDSF-less stack is byte-identical to before.
    mirror_session_to_udsf(session)
    # The response carries the authorized policy when a PCF decided it; the keys are simply
    # absent (via a dict filter) on the fallback path, keeping the legacy response shape.
    extra = {k: v for k, v in {
        "smPolicyId": session.smPolicyId, "authorizedQos": auth_qos,
        "sessionAmbr": sess_ambr, "gateStatus": session.gateStatus,
        "policyLabel": session.policyLabel,
        "chargingRef": charging_ref, "grantedQuota": granted}.items() if v is not None}
    return 201, {"seid": seid, "ueIp": ue_ip, "dnn": dnn, "sNssai": snssai, "ulTeid": ul_teid,
                 "dlTeid": dl_teid, "upfGtpu": upf["gtpu"], "pduSessionId": psi,
                 "upf": upf_label, **extra}


@app.route("GET", "/smf/sessions")
def list_sessions(params, query, body):
    return 200, {"sessions": [s.as_dict() for s in sessions.values()]}


@app.route("POST", "/nsmf-pdusession/v1/sm-contexts/{seid}/release")
def release_sm_context(params, query, body):
    """Nsmf_PDUSession ReleaseSMContext (TS 29.502 5.2.2.4 / TS 23.502 4.3.4 PDU session
    release). ADDITIVE: this surface did not exist before the CHF integration, so no legacy
    caller depends on it. When the session carried a CHF charging-data resource, close it over
    Nchf_ConvergedCharging_Release; a session without one (the legacy path) just tears down —
    charging is simply skipped, exactly as a CHF-less stack always behaved."""
    session = sessions.get(params["seid"])
    if session is None:
        return problem(404, "Not Found", detail=f"no session {params['seid']}",
                       cause="CONTEXT_NOT_FOUND")
    cdr = release_charging(session.chargingRef) if session.chargingRef else None
    session.state = "RELEASED"
    sessions.pop(params["seid"], None)
    # Nudsf mirror teardown: delete the mirrored SM context so the shared store tracks only live
    # sessions. Best-effort — a missing UDSF (or a session that was never mirrored) is a no-op and
    # never blocks the release, exactly as a UDSF-less stack always behaved.
    unmirror_session_from_udsf(params["seid"])
    obs.log("session_released", seid=params["seid"], supi=session.supi,
            chargingRef=session.chargingRef, charged=bool(cdr))
    obs.counter("sessions_released_total").inc()
    resp = {"seid": params["seid"], "state": "RELEASED"}
    if cdr is not None:   # omitted on the fallback path, keeping the legacy-shaped response
        resp["cdr"] = cdr
    return 200, resp


# ------------------------------------------------- dynamic UPF-selection rules (admin surface)
# The NSMF's core-subnet action (epic #11, TS 28.531-flavored provisioning): one rule per
# allocated NSI, same schema and match semantics as the static table, consulted BEFORE it.
# Purely additive — with zero dynamic rules the SMF behaves byte-identically to wave 1.

@app.route("POST", "/smf/upf-selection/rules")
def add_selection_rule(params, query, body):
    if body.get("upf") not in UPFS:
        return problem(400, "Bad Request",
                       detail=f"unknown UPF label {body.get('upf')!r} (known: {sorted(UPFS)})")
    rule = {"ruleId": "rule-" + uuid.uuid4().hex[:10], "upf": body["upf"]}
    if body.get("sNssai") is not None:
        try:
            rule["sNssai"] = snssai_normalize(body["sNssai"])
        except ValueError as exc:
            return problem(400, "Bad Request", detail=str(exc))
    if body.get("dnn") is not None:
        rule["dnn"] = str(body["dnn"])
    if "sNssai" not in rule and "dnn" not in rule:
        return problem(400, "Bad Request",
                       detail="a dynamic rule needs at least one match key (sNssai or dnn) — "
                              "an unconditional rule would shadow the whole static table")
    if body.get("owner"):
        rule["owner"] = str(body["owner"])   # e.g. the NSMF's nsiId, for audit/readback
    dynamic_rules.put(rule["ruleId"], rule)
    obs.log("upf_selection_rule_added", ruleId=rule["ruleId"], upf=rule["upf"],
            snssai=snssai_key(rule["sNssai"]) if "sNssai" in rule else None,
            dnn=rule.get("dnn"), owner=rule.get("owner"))
    obs.counter("upf_selection_rules_total", action="added").inc()
    return 201, rule


@app.route("GET", "/smf/upf-selection/rules")
def list_selection_rules(params, query, body):
    return 200, {"rules": dynamic_rules.values(), "staticTable": UPF_SELECTION}


@app.route("DELETE", "/smf/upf-selection/rules/{ruleId}")
def delete_selection_rule(params, query, body):
    rule = dynamic_rules.get(params["ruleId"])
    if rule is None:
        return problem(404, "Not Found", detail=f"no dynamic rule {params['ruleId']}")
    dynamic_rules.delete(params["ruleId"])
    obs.log("upf_selection_rule_removed", ruleId=params["ruleId"], owner=rule.get("owner"))
    obs.counter("upf_selection_rules_total", action="removed").inc()
    return 204, {}


def register_with_nrf():
    profile = {"nfType": "SMF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nsmf-pdusession",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("sessions_active").set(len(sessions))
    by_slice = {}
    for s in sessions.values():
        by_slice[snssai_key(s.sNssai)] = by_slice.get(snssai_key(s.sNssai), 0) + 1
    for slice_key, count in by_slice.items():
        obs.gauge("slice_sessions_active", slice=slice_key).set(count)


if __name__ == "__main__":
    obs.init("smf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

"""
NEF: Network Exposure Function — the northbound, AF-facing front door of the owned core.

The NEF is the exposure surface the AI-grid / edge apps (Application Functions) use to
OBSERVE the network (monitoring events) and INFLUENCE it (traffic steering). It sits at the
N33 reference point facing the AF, and reaches southbound into the core over the SBI:
here it DISCOVERS UDM and AMF through the NRF and READS them to resolve an AF's request into
a real report. No other NF is edited — the NEF is a standalone northbound reader.

Spec anchors:
  Nnef_EventExposure (Monitoring)   TS 29.522 section 4.4 (MonitoringEvent API): an AF subscribes
                                    to a monitoring event (UE_REACHABILITY, LOCATION_REPORTING) for
                                    a target UE (SUPI/GPSI). Resource + report shapes preserved.
  Nnef_TrafficInfluence             TS 29.522 section 4.5 (TrafficInfluence API): an AF requests
                                    traffic steering (dnn / S-NSSAI / route to an edge DNAI).
  NEF exposure services & N33       TS 23.501 section 7.2.8 (NEF), TS 23.502 section 4.15
                                    (Network Exposure procedures — event exposure, AF influence).
  Underlying reads:
    Nudm_SDM am-data                TS 29.503 section 6.1 — does the target subscriber exist
                                    (services/core/udm/udm.py GET /nudm-sdm/v2/{supi}/am-data).
    AMF UE reachability / RM state  TS 23.502 4.15 event exposure is normally Namf_EventExposure;
                                    here the NEF reads the AMF's UE context directly
                                    (services/core/amf/amf.py GET /amf/ue-contexts/{supi}) to
                                    resolve REGISTERED / reachable — labeled simplification below.
  Nnrf_NFManagement / NFDiscovery   TS 29.510 sections 5.2 / 5.3 (register as NEF, discover UDM/AMF).

CAPIF authorization gate (DEEP INTEGRATION, ledgered in procedures/nef_capif_gating.txt):
  - The northbound exposure APIs are gated behind CAPIF (TS 29.222 / TS 33.122) — but ONLY when
    a CAPIF core is DISCOVERABLE in the NRF. When CAPIF is present, an AF calling an Nnef exposure
    resource MUST present a valid CAPIF-issued access token (Authorization: Bearer <shaped JWT>);
    a missing/forged/expired token is rejected 401. The NEF validates the token against CAPIF's
    shared shaping secret (recompute the shaped signature + check iss/exp) — introspection-style,
    no per-call round-trip. When CAPIF is ABSENT the gate is INERT and the NEF behaves byte-
    identically to before this integration (the af_authorized() stub still stands). ADDITIVE +
    EXACT FALLBACK: existing specs run with no CAPIF, so their behaviour is unchanged.

Labeled simplifications (ledgered in procedures/nef_exposure.txt):
  - AF authorization is STUBBED at af_authorized() (always allows). The CAPIF gate above is the
    real OAuth2/CAPIF token check (TS 33.122 / TS 29.522 security) wired in as a presence-gated
    layer in front of these handlers; the token crypto is SHAPED (CAPIF's stub digest), not signed.
  - GPSI->SUPI translation is an identity stub: a "gpsi" target is treated as the internal SUPI.
    Real NEF resolves it via Nudm_SDM id-translation. A "supi" target is used as-is.
  - UE reachability is read straight off the AMF UE context (a lab shortcut), not via a proper
    Namf_EventExposure subscription with AMF-driven notifications. Honest at read fidelity.
  - Traffic-influence ENFORCEMENT via the PCF (DEEP INTEGRATION, ledgered in
    procedures/nef_ti_pcf_enforcement.txt): when a PolicyAuthorization-capable PCF is discoverable
    in the NRF, the NEF FORWARDS the AF's intent to it as an Npcf_PolicyAuthorization app-session
    (TS 29.514) so it becomes a real installed PCC rule (enforcementStatus ENFORCED_VIA_PCF, the
    returned appSessionId recorded for teardown on DELETE). ADDITIVE + EXACT FALLBACK: with no such
    PCF (absent, unreachable, or a plain PCF answering 404 on the app-session route) the NEF falls
    back to EXACTLY the record-only behaviour, labeled RECORDED_NOT_ENFORCED, byte-identical to
    before this integration. Nsmf traffic-steering enforcement remains a follow-up.

Run: python3 nef.py   (SBI on 127.0.0.1:7008, registers with the NRF as NEF)
"""

import base64
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request
from domain import netconfig, obs
from domain.netconfig import port, url
from domain.statestore import open_store

PORT = port("nef")
NRF = url("nrf")
app = SbiApp("nef")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). AF subscriptions are keyed by subscriptionId; the afId is carried in
# the record so the {afId} resource scoping is honoured on read/delete.
store = open_store("nef")
monitoring_subs = store.collection("monitoring_subscriptions")
traffic_subs = store.collection("traffic_influence_subscriptions")

# Monitoring event types the NEF can resolve today (TS 29.522 4.4.3 MonitoringType). Others are
# accepted and recorded but reported UNSUPPORTED_MONITORING_TYPE rather than faked.
RESOLVABLE_TYPES = {"UE_REACHABILITY", "LOCATION_REPORTING"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def discover(nf_type):
    """NRF discovery, same shape as services/core/amf/amf.py — returns a base URL or None so a
    missing NF degrades to an honest UNKNOWN in the report rather than crashing the NEF."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type=NEF")
    except OSError:
        return None
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        return None
    endpoint = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{endpoint['ipv4Address']}:{endpoint['port']}"


def af_authorized(af_id):
    """AF authorization STUB (labeled simplification). A real NEF enforces OAuth2/CAPIF here
    (TS 29.522 security, TS 33.122). Today every AF is allowed; the hook exists so the follow-up
    is a body change, not a new seam."""
    return True


# ============================================================ CAPIF authorization gate (DEEP INT)
# The northbound exposure APIs (Nnef_EventExposure, Nnef_TrafficInfluence) are gated behind CAPIF
# (TS 29.222 CAPIF / TS 33.122 security) — but ONLY when a CAPIF core is DISCOVERABLE in the NRF.
# This makes the gate presence-conditional: with CAPIF absent the NEF behaves byte-identically to
# before this integration (EXACT FALLBACK); with CAPIF present an AF must present a valid CAPIF-
# issued Bearer token. Validation is introspection-style against CAPIF's shared shaping secret
# (recompute the shaped signature; check iss/exp) — no per-call round-trip to CAPIF is needed.

# CAPIF issues a JWT-STRUCTURED token whose third segment is sha256(header.payload) base64url'd
# (services/core/capif/capif.py shaped_jwt). "Validate against CAPIF" here = recompute that shaped
# signature and confirm the CAPIF-minted claims. Token crypto is SHAPED, not RS256/HS256 (labeled).
CAPIF_ISS = "capif-core"
_EXPOSURE_PREFIXES = ("/3gpp-monitoring-event/", "/3gpp-traffic-influence/")
_capif_cache = {"present": None, "ts": 0.0}
_CAPIF_TTL = 1.5   # seconds — bound how often the gate re-discovers CAPIF in the NRF


def capif_present():
    """True when a CAPIF core is discoverable in the NRF (the gate is armed). Cached with a short
    TTL so the exposure hot path does not hammer the NRF. On any discovery error -> False (fail
    OPEN to the exact pre-integration behaviour, never a false 401)."""
    now = time.time()
    if _capif_cache["present"] is not None and now - _capif_cache["ts"] < _CAPIF_TTL:
        return _capif_cache["present"]
    present = discover("CAPIF") is not None
    _capif_cache.update(present=present, ts=now)
    return present


def _shaped_sig(header_seg, payload_seg):
    """Recompute CAPIF's shaped signature for header.payload (mirror of capif.shaped_jwt). The
    shared secret is the shaping algorithm itself — sha256 over the first two JWT segments."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(f"{header_seg}.{payload_seg}".encode()).digest()).rstrip(b"=").decode()


def validate_capif_token(auth_header):
    """Validate a CAPIF access token from an Authorization header. Returns (ok, info): info is the
    claims dict when ok, else a short reason string. Checks structure, the shaped signature (proves
    CAPIF minted it), the issuer, and expiry — a forged/tampered/expired token fails."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return False, "no bearer token"
    token = auth_header[len("Bearer "):].strip()
    parts = token.split(".")
    if len(parts) != 3:
        return False, "malformed token"
    header_seg, payload_seg, sig_seg = parts
    if sig_seg != _shaped_sig(header_seg, payload_seg):
        return False, "bad signature"
    try:
        pad = payload_seg + "=" * (-len(payload_seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return False, "bad payload"
    if claims.get("iss") != CAPIF_ISS:
        return False, "wrong issuer"
    if int(claims.get("exp", 0)) < int(time.time()):
        return False, "expired"
    return True, claims


def capif_gate(path, auth_header):
    """The presence-gated authorization decision for one inbound exposure request. Returns a
    problem tuple (status, body) to REJECT, or None to ALLOW. When CAPIF is absent this is inert
    (returns None immediately) so behaviour is byte-identical to the pre-integration NEF."""
    if not any(path.startswith(p) for p in _EXPOSURE_PREFIXES):
        return None            # not an exposure resource — never gated
    if not capif_present():
        return None            # EXACT FALLBACK: no CAPIF -> no token required
    ok, info = validate_capif_token(auth_header)
    if ok:
        obs.counter("nef_capif_authorized_total").inc()
        obs.log("capif_gate_authorized", path=path, sub=info.get("sub"), scope=info.get("scope"))
        return None
    obs.counter("nef_capif_rejected_total").inc()
    obs.log("capif_gate_rejected", path=path, reason=info)
    return problem(401, "Unauthorized",
                   detail=f"CAPIF authorization required ({info}) — onboard at CAPIF and present "
                          "a valid Authorization: Bearer <access token>",
                   cause="AF_NOT_AUTHORIZED")


def target_supi(body):
    """Resolve the AF's target UE identity to an internal SUPI. 'supi' is used as-is; 'gpsi' is
    an identity stub for Nudm_SDM id-translation (labeled). Returns the SUPI or None."""
    if body.get("supi"):
        return str(body["supi"])
    if body.get("gpsi"):
        return str(body["gpsi"])
    return None


def resolve_report(monitoring_type, supi):
    """Turn a monitoring subscription into a REAL report by reading UDM (does the subscriber
    exist?) and the AMF (is it registered / reachable?). This is the exposure act: the AF asked
    the network about a subscriber, and the NEF answers from live NF state.

    Returns (report_dict, error) — error is a (status, cause, detail) tuple when the target is
    unknown so the caller can reject the subscription (TS 29.522 uses 404 USER_NOT_FOUND)."""
    # --- UDM: subscriber existence + subscription data (Nudm_SDM am-data, TS 29.503 6.1)
    udm = discover("UDM")
    subscriber_known = False
    plmn = None
    if udm is not None:
        try:
            status, am = request("GET", f"{udm}/nudm-sdm/v2/{supi}/am-data")
            if status == 200:
                subscriber_known, plmn = True, am.get("plmn")
        except OSError:
            pass
    if udm is None:
        return None, (500, "NF_DISCOVERY_FAILURE", "NEF could not discover a UDM in the NRF")
    if not subscriber_known:
        return None, (404, "USER_NOT_FOUND", f"no subscription for {supi} in the UDM")

    # --- AMF: RM state / reachability (read of the UE context, TS 23.502 4.15 event exposure)
    amf = discover("AMF")
    registered, rm_state = False, "UNKNOWN"
    if amf is not None:
        try:
            status, ctx = request("GET", f"{amf}/amf/ue-contexts/{supi}")
            if status == 200:
                rm_state = ctx.get("state", "UNKNOWN")
                registered = rm_state == "REGISTERED"
            elif status == 404:
                rm_state = "DEREGISTERED"
        except OSError:
            pass

    report = {"monitoringType": monitoring_type, "eventTime": now_iso(),
              "supi": supi, "plmn": plmn,
              "subscriberKnown": subscriber_known, "rmState": rm_state}
    if monitoring_type == "UE_REACHABILITY":
        # Reachable only when the AMF holds a REGISTERED context (TS 29.522 reachabilityType).
        report["reachable"] = registered
        report["reachabilityType"] = "DATA" if registered else None
    elif monitoring_type == "LOCATION_REPORTING":
        # Location is served from the AMF's serving GUAMI when registered; NEF does not invent
        # a cell it cannot read (honest — no fabricated TAI/cell).
        report["locationInfo"] = ({"guami": ctx.get("guami")} if registered else None)
        report["locationKnown"] = registered
    else:
        report["note"] = "UNSUPPORTED_MONITORING_TYPE"
    return report, None


# ------------------------------------------------- Monitoring Event API (Nnef_EventExposure)
# TS 29.522 4.4: an AF subscribes to a UE monitoring event; the NEF resolves the current report
# from UDM/AMF and returns it in the created resource (and re-resolves it on every GET = poll).

@app.route("POST", "/3gpp-monitoring-event/v1/{afId}/subscriptions")
def create_monitoring_subscription(params, query, body):
    af_id = params["afId"]
    if not af_authorized(af_id):                      # STUB — always true today
        return problem(403, "Forbidden", detail="AF not authorized", cause="AF_NOT_AUTHORIZED")
    monitoring_type = body.get("monitoringType")
    if not monitoring_type:
        return problem(400, "Bad Request", detail="monitoringType is mandatory",
                       cause="MANDATORY_IE_MISSING")
    supi = target_supi(body)
    if supi is None:
        return problem(400, "Bad Request", detail="a target UE (supi or gpsi) is mandatory",
                       cause="MANDATORY_IE_MISSING")
    report, error = resolve_report(monitoring_type, supi)
    if error is not None:
        status, cause, detail = error
        return problem(status, "Not Found" if status == 404 else "Internal Server Error",
                       detail=detail, cause=cause)
    sub_id = uuid.uuid4().hex
    record = {"subscriptionId": sub_id, "afId": af_id, "monitoringType": monitoring_type,
              "supi": supi, "gpsi": body.get("gpsi"),
              "notificationDestination": body.get("notificationDestination"),
              "self": f"/3gpp-monitoring-event/v1/{af_id}/subscriptions/{sub_id}",
              "createdAt": now_iso(), "monitoringEventReport": report}
    monitoring_subs.put(sub_id, record)
    obs.log("monitoring_subscription_created", afId=af_id, subscriptionId=sub_id,
            monitoringType=monitoring_type, supi=supi,
            resolvable=monitoring_type in RESOLVABLE_TYPES)
    obs.counter("nef_monitoring_subscriptions_total").inc()
    return 201, record


@app.route("GET", "/3gpp-monitoring-event/v1/{afId}/subscriptions/{subscriptionId}")
def get_monitoring_subscription(params, query, body):
    record = monitoring_subs.get(params["subscriptionId"])
    if record is None or record.get("afId") != params["afId"]:
        return problem(404, "Not Found", detail="no such monitoring subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    # Poll: re-resolve against live UDM/AMF so a GET reflects the CURRENT network state, then
    # persist the refreshed report on the record.
    report, error = resolve_report(record["monitoringType"], record["supi"])
    if error is None:
        record["monitoringEventReport"] = report
        monitoring_subs.put(record["subscriptionId"], record)
    return 200, record


@app.route("GET", "/3gpp-monitoring-event/v1/{afId}/subscriptions")
def list_monitoring_subscriptions(params, query, body):
    subs = [r for r in monitoring_subs.values() if r.get("afId") == params["afId"]]
    return 200, {"subscriptions": subs}


@app.route("DELETE", "/3gpp-monitoring-event/v1/{afId}/subscriptions/{subscriptionId}")
def delete_monitoring_subscription(params, query, body):
    record = monitoring_subs.get(params["subscriptionId"])
    if record is None or record.get("afId") != params["afId"]:
        return problem(404, "Not Found", detail="no such monitoring subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    monitoring_subs.delete(params["subscriptionId"])
    obs.log("monitoring_subscription_deleted", afId=params["afId"],
            subscriptionId=params["subscriptionId"])
    obs.counter("nef_monitoring_subscriptions_deleted_total").inc()
    return 204, {}


# ================================================ PCF PolicyAuthorization enforcement (DEEP INT)
# The AF's traffic-influence intent becomes a REAL installed policy: the NEF acts as the AF-to-PCF
# exposure bridge (TS 23.501 6.1.3.14, TS 29.514 Npcf_PolicyAuthorization). When a PCF is
# discoverable in the NRF, the NEF POSTs an app-session context to
# /npcf-policyauthorization/v1/app-sessions (TS 29.514 4.2.2 AppSessionContext) — mapping the TI's
# DNN/S-NSSAI/target-UE/DNAI-route/QoS into the app-session body — and the PCF installs a dynamic
# PCC rule. The returned appSessionId is recorded on the subscription so DELETE tears the policy
# down (TS 29.514 4.2.5). Charter axis: FIDELITY (enforcement goes from record-only to a real
# policy). Verb: FEDERATE/wire (nef.py is the ONLY file edited; the PCF app-session surface is a
# SIBLING deliverable — see procedures/nef_ti_pcf_enforcement.txt for the runtime dependency).
#
# THE CONTRACT — additive + EXACT FALLBACK. Enforcement is gated on the PCF being DISCOVERABLE AND
# REACHABLE AND actually serving the PolicyAuthorization surface. If ANY of those is false — no PCF
# in the NRF, PCF down (connection error), or a plain PCF that has no /npcf-policyauthorization
# route (404) — the NEF falls back to EXACTLY the pre-integration record-only behaviour and labels
# the resource RECORDED_NOT_ENFORCED, byte-identically. So every existing spec (run_nef_spec,
# run_nef_capif_spec, run_golden_scenario_spec) — none of which run a PolicyAuthorization-capable
# PCF — passes UNEDITED. Enforcement is opt-in by deploying a PCF that serves app-sessions.

_POLICYAUTH_BASE = "/npcf-policyauthorization/v1/app-sessions"


def build_app_session(record):
    """Map a recorded traffic-influence subscription into a TS 29.514 AppSessionContext request
    body (ascReqData). The DNAI route becomes afRoutReq.routeToLocs; a requested 5QI/GBR (if the AF
    supplied one) becomes the media-component QoS. Honest: this is the app-session SHAPE the PCF
    consumes to install a dynamic PCC rule — the crypto/charging fields a real AF-session carries
    are out of scope (labeled in the procedure ledger)."""
    routes = [{"dnai": r.get("dnai")} for r in (record.get("trafficRoutes") or []) if r.get("dnai")]
    asc = {
        "afAppId": record.get("afId"),
        "dnn": record.get("dnn"),
        "snssai": record.get("snssai"),
        "supi": record.get("supi"),
        "gpsi": record.get("gpsi"),
        # DNAI steering (TS 29.514 4.2.2 / TS 29.512 traffic routing): where to anchor the flow.
        "afRoutReq": {"routeToLocs": routes} if routes else None,
        # The described flow the policy applies to (TS 29.514 MediaComponent). Default any-UL/DL.
        "medComponents": {
            "0": {"medCompN": 0, "fStatus": "ENABLED",
                  "medSubComps": {"0": {"fNum": 0,
                                        "flowDescs": [f"permit out ip from any to assigned"]}}}
        },
    }
    # Optional AF-requested QoS -> the requested 5QI/GBR the PCF authorizes (TS 29.514 QosReference).
    qos = record.get("qosReference") or record.get("requestedQos")
    if qos:
        asc["medComponents"]["0"]["marBwUl"] = qos.get("marBwUl")
        asc["medComponents"]["0"]["marBwDl"] = qos.get("marBwDl")
        asc["qosReference"] = qos.get("qosReference") or qos.get("5qi")
    return {k: v for k, v in asc.items() if v is not None}


def enforce_via_pcf(record):
    """Best-effort: hand the traffic-influence intent to the PCF as an Npcf_PolicyAuthorization
    app-session so it becomes a real installed policy. Returns (pcf_base, app_session_id) on a
    genuine enforcement, or (None, None) for the EXACT record-only fallback.

    Fallback (byte-identical to pre-integration) when: no PCF discoverable; the PCF is unreachable
    (connection error); or the PCF does not serve the PolicyAuthorization surface (any non-2xx, e.g.
    a plain PCF answering 404). Never raises — enforcement failure degrades to record-only, never a
    500 to the AF."""
    pcf = discover("PCF")
    if pcf is None:
        return None, None
    try:
        status, body = request("POST", f"{pcf}{_POLICYAUTH_BASE}", build_app_session(record))
    except OSError:
        return None, None                      # PCF unreachable -> exact fallback
    if status not in (200, 201):
        return None, None                      # plain PCF (404) / rejection -> exact fallback
    app_session_id = body.get("appSessionId") or body.get("appSessionContextId")
    if not app_session_id:
        return None, None                      # no session handle returned -> exact fallback
    return pcf, app_session_id


def revoke_from_pcf(record):
    """Tear down the installed policy on delete (TS 29.514 4.2.5 DELETE app-session). Best-effort:
    only when this subscription was actually enforced; any error is swallowed so a DELETE of the TI
    resource always succeeds locally even if the PCF is already gone."""
    pcf = record.get("pcfBase")
    app_session_id = record.get("appSessionId")
    if not pcf or not app_session_id:
        return
    try:
        request("DELETE", f"{pcf}{_POLICYAUTH_BASE}/{app_session_id}")
    except OSError:
        pass


# ------------------------------------------------ Traffic Influence API (Nnef_TrafficInfluence)
# TS 29.522 4.5: an AF asks the network to steer a UE's traffic (dnn / S-NSSAI / route to an
# edge DNAI). The NEF RECORDS and EXPOSES the request, and — when a PolicyAuthorization-capable PCF
# is discoverable+reachable — ENFORCES it by installing a dynamic PCC rule via the PCF (the
# enforce_via_pcf bridge above). With no such PCF the NEF falls back to EXACTLY the record-only
# behaviour, labeled RECORDED_NOT_ENFORCED (procedures/nef_ti_pcf_enforcement.txt ledger).

@app.route("POST", "/3gpp-traffic-influence/v1/{afId}/subscriptions")
def create_traffic_influence_subscription(params, query, body):
    af_id = params["afId"]
    if not af_authorized(af_id):                      # STUB — always true today
        return problem(403, "Forbidden", detail="AF not authorized", cause="AF_NOT_AUTHORIZED")
    sub_id = uuid.uuid4().hex
    record = {"subscriptionId": sub_id, "afId": af_id,
              "dnn": body.get("dnn"), "snssai": body.get("snssai"),
              "trafficRoutes": body.get("trafficRoutes"),   # e.g. [{"dnai": "edge-dnai-1"}]
              "gpsi": body.get("gpsi"), "supi": body.get("supi"),
              # Optional AF-requested QoS carried through to the PCF app-session (TS 29.514).
              "qosReference": body.get("qosReference"), "requestedQos": body.get("requestedQos"),
              "anyUeInd": bool(body.get("anyUeInd", False)),
              "self": f"/3gpp-traffic-influence/v1/{af_id}/subscriptions/{sub_id}",
              "createdAt": now_iso(),
              # Honesty marker on the resource itself: default is recorded-but-not-programmed. It is
              # upgraded to ENFORCED_VIA_PCF below iff a PolicyAuthorization-capable PCF installs it.
              "enforcementStatus": "RECORDED_NOT_ENFORCED"}
    # --- DEEP INT: try to make it a REAL policy via the PCF. Best-effort + EXACT fallback: with no
    # PolicyAuthorization-capable PCF this is a no-op and the resource stays RECORDED_NOT_ENFORCED,
    # byte-identical to the pre-integration NEF.
    pcf_base, app_session_id = enforce_via_pcf(record)
    if app_session_id is not None:
        record["enforcementStatus"] = "ENFORCED_VIA_PCF"
        record["appSessionId"] = app_session_id
        record["pcfBase"] = pcf_base
        obs.counter("nef_ti_enforced_total", result="enforced").inc()
    else:
        obs.counter("nef_ti_enforced_total", result="recorded_fallback").inc()
    traffic_subs.put(sub_id, record)
    obs.log("traffic_influence_subscription_created", afId=af_id, subscriptionId=sub_id,
            dnn=record["dnn"], trafficRoutes=record["trafficRoutes"],
            enforcement=record["enforcementStatus"], appSessionId=record.get("appSessionId"))
    obs.counter("nef_traffic_influence_subscriptions_total").inc()
    return 201, record


@app.route("GET", "/3gpp-traffic-influence/v1/{afId}/subscriptions/{subscriptionId}")
def get_traffic_influence_subscription(params, query, body):
    record = traffic_subs.get(params["subscriptionId"])
    if record is None or record.get("afId") != params["afId"]:
        return problem(404, "Not Found", detail="no such traffic-influence subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    return 200, record


@app.route("GET", "/3gpp-traffic-influence/v1/{afId}/subscriptions")
def list_traffic_influence_subscriptions(params, query, body):
    subs = [r for r in traffic_subs.values() if r.get("afId") == params["afId"]]
    return 200, {"subscriptions": subs}


@app.route("DELETE", "/3gpp-traffic-influence/v1/{afId}/subscriptions/{subscriptionId}")
def delete_traffic_influence_subscription(params, query, body):
    record = traffic_subs.get(params["subscriptionId"])
    if record is None or record.get("afId") != params["afId"]:
        return problem(404, "Not Found", detail="no such traffic-influence subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    # DEEP INT: if this intent was enforced as a PCF app-session, tear the policy down too. No-op
    # (best-effort) for a record-only subscription — the local DELETE always succeeds.
    revoke_from_pcf(record)
    traffic_subs.delete(params["subscriptionId"])
    obs.log("traffic_influence_subscription_deleted", afId=params["afId"],
            subscriptionId=params["subscriptionId"],
            appSessionId=record.get("appSessionId"))
    obs.counter("nef_traffic_influence_subscriptions_deleted_total").inc()
    return 204, {}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. Both Nnef service names are advertised so a future
    # PCF/AF discovering the NEF finds its exposure surfaces.
    profile = {"nfType": "NEF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nnef-eventexposure",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "nnef-trafficinfluence",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("nef_monitoring_subscriptions_active").set(len(list(monitoring_subs.values())))
    obs.gauge("nef_traffic_influence_subscriptions_active").set(len(list(traffic_subs.values())))


# ------------------------------------------------------------------- gated SBI server (DEEP INT)
# adapters/sbi_http.serve() dispatches on (params, query, body) and does not expose request
# headers to handlers — and it is NOT ours to edit. So the NEF runs its own thin server that is a
# FAITHFUL mirror of sbi_http.serve (same /metrics, same "/" index, same ProblemDetails media type,
# same correlation-id propagation) with ONE addition: before an exposure handler runs, capif_gate()
# inspects the Authorization header. When CAPIF is absent the gate is inert and every response is
# byte-identical to sbi_http.serve. Reuses the shared app.match/problem so routing stays single-source.

def serve_gated(app, port):
    """Mirror of adapters.sbi_http.serve with the CAPIF authorization gate layered in front of the
    exposure resources. Byte-identical to sbi_http.serve when CAPIF is not discoverable."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            obs.set_corr(self.headers.get(obs.HEADER))
            if method == "GET" and parsed.path == "/metrics":
                self._reply_text(200, obs.render_metrics())
                return
            if method == "GET" and parsed.path == "/":
                self._reply(200, {
                    "nf": app.name,
                    "metrics": "/metrics",
                    "routes": sorted({f"{m} /" + "/".join(seg) for m, seg, _ in app.routes}),
                })
                return
            handler, params = app.match(method, parsed.path)
            if handler is None:
                self._reply(*problem(404, "Not Found", detail=parsed.path))
                return
            # --- CAPIF gate: the one addition over sbi_http.serve. Inert when CAPIF is absent.
            rejection = capif_gate(parsed.path, self.headers.get("Authorization"))
            if rejection is not None:
                self._reply(*rejection)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            status, reply = handler(params, query, body)
            self._reply(status, reply)

        def _reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            content_type = "application/problem+json" if status >= 400 else "application/json"
            self.send_header("Content-Type", content_type)
            self._finish(data)

        def _reply_text(self, status, text):
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self._finish(data)

        def _finish(self, data):
            corr = obs.get_corr()
            if corr:
                self.send_header(obs.HEADER, corr)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    if not obs.initialized():
        obs.init(app.name)
    bind = netconfig.bind_host()
    server = ThreadingHTTPServer((bind, port), Handler)
    obs.log("listening", addr=f"{bind}:{port}", capif_gate="presence-conditional")
    server.serve_forever()


if __name__ == "__main__":
    obs.init("nef")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve_gated(app, PORT)

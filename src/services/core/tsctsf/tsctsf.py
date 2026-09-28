"""
TSCTSF: Time-Sensitive Communication and Time Synchronization Function — the owned core's
deterministic-networking control point for the URLLC / industrial / robot story.

The TSCTSF is the 5G core function an Application Function talks to when a flow is not just
"fast" but DETERMINISTIC: a robot-cell control loop, a PLC, a motion controller. It does two
distinct jobs (TS 23.501 clause 5.27 TSC + time synchronization; API TS 29.565 Ntsctsf):

  1. TIME SYNCHRONIZATION (Ntsctsf_TimeSynchronization) — configure 5G access-stratum time
     distribution (the network broadcasts a synchronized clock, SIB9 / gPTP grandmaster
     relay) so a GROUP of UEs on a shop floor share one time base. This is the 5G system
     acting as an IEEE 802.1AS (gPTP) time-aware relay between a TSN grandmaster and the
     end stations behind the UEs.

  2. QoS + TSC ASSISTANCE (Ntsctsf_QoSandTSCAssistance) — an AF describes a periodic,
     deterministic flow with a TSC Assistance Container (burst arrival time, periodicity,
     survival time, flow direction, burst size). The TSCTSF is the function that TURNS that
     timing description into 5G QoS requirements: a delay-critical GBR 5QI + a Packet Delay
     Budget (+ derived GFBR), and the TSCAI (TSC Assistance Information) the RAN scheduler
     needs. It would then hand those requirements to the PCF (services/core/pcf/pcf.py) as a
     policy authorization so the PCC/QoS actually gets installed on the session.

This is a STANDALONE control-plane function: no other NF is edited. Charter axis: BREADTH.
Verb: HARVEST (TS 29.565 / TS 23.501 5.27 read for the surface + the TSC->QoS mapping shape;
authored clean-room, stdlib only).

Spec anchors:
  Ntsctsf_TimeSynchronization       TS 29.565 clause 5 — AF configures 5G access-stratum time
                                    distribution / PTP instance for a group of UEs.
  Ntsctsf_QoSandTSCAssistance       TS 29.565 clause 5 — AF requests QoS for a flow, carrying a
                                    TSC Assistance Container; the TSCTSF derives the QoS.
  TSC + time synchronization        TS 23.501 clause 5.27 (5.27.1 time sync / gPTP, 5.27.2 TSC
                                    Assistance Information (TSCAI), 5.27.4 traffic patterns).
  Delay-critical GBR 5QIs / PDB     TS 23.501 clause 5.7.4, table 5.7.4-1 (5QI 82/83/84/85: the
                                    standardized delay-critical GBR characteristics used for
                                    industrial / URLLC deterministic flows).
  Npcf_PolicyAuthorization (peer)   TS 29.514 — the surface the TSCTSF would use to hand the
                                    derived QoS to the PCF (services/core/pcf/pcf.py). Modeled
                                    as a derivation + honesty marker here (see ledger).
  Nnrf_NFManagement registration    TS 29.510 clause 5.2 (registers as nfType TSCTSF).

LABELED SIMPLIFICATIONS (ledgered in procedures/tsctsf_time_sensitive.txt):
  - NO real gPTP / TSN bridge or grandmaster: the time-synch configuration is RECORDED and
    described (clock domain, SIB9 distribution, spatialValidity) but there is no SIB9 broadcast
    and no IEEE 802.1AS relay. The configuration is honest at procedure fidelity, marked
    distributionStatus = MODELED_NO_GPTP_HW.
  - The TSC -> QoS mapping is a LABELED, declarative model (QOS_MAP below): the standardized
    delay-critical 5QIs and their PDBs are real (TS 23.501 table 5.7.4-1), the selection rule
    that maps a traffic pattern onto one of them is authored for this lab.
  - PCF HANDOFF (DEEP INTEGRATION, ledgered in procedures/tsctsf_pcf_install.txt): when a PCF that
    advertises Npcf_PolicyAuthorization (TS 29.514) is DISCOVERABLE in the NRF, the TSCTSF now POSTs
    the derived QoS to /npcf-policyauthorization/v1/app-sessions so the deterministic QoS is really
    INSTALLED as a dynamic PCC rule (pcfHandoff = INSTALLED + appSessionId; DELETEd on teardown).
    ADDITIVE + EXACT FALLBACK: with no such PCF discoverable OR the endpoint unavailable (a plain
    PCF that does not advertise policy-authorization — the sibling pol-pcf branch not yet merged —
    returns 404), the QoS stays EXACTLY pcfHandoff = DERIVED_NOT_INSTALLED, as before this wiring.
    Metric: tsctsf_qos_installed_total{result=installed|derived_fallback}.

Run: python3 tsctsf.py   (SBI on 127.0.0.1:7038, registers with the NRF as nfType TSCTSF)
"""

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

PORT = port("tsctsf")
NRF = url("nrf")
app = SbiApp("tsctsf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise.
store = open_store("tsctsf")
timesynch_configs = store.collection("timesynch_configurations")   # configId -> record
tsc_subs = store.collection("qos_tsc_subscriptions")               # subId    -> record

# --------------------------------------------------------------------- the TSC -> QoS mapping
# The standardized delay-critical GBR 5QIs (TS 23.501 table 5.7.4-1): the 5QI value, its Packet
# Delay Budget in ms, its Packet Error Rate, and its priority level are the REAL normative
# characteristics. Ordered tightest PDB first. The SELECTION rule (pick_qos below) is the
# authored, labeled model that maps a traffic pattern onto one of these — this is the single
# place the TSC->QoS policy lives (a PCF/TSN-config UI would author it in a later pass).
#
#   82  Discrete Automation                     PDB 10ms  PER 1e-4  priority 19
#   83  Discrete Automation (higher throughput) PDB 10ms  PER 1e-4  priority 22
#   84  Intelligent Transport Systems           PDB 30ms  PER 1e-5  priority 24
#   85  Electricity Distribution (high volt)    PDB  5ms  PER 1e-5  priority 21
QOS_MAP = [
    {"fiveqi": 85, "pdbMs": 5,  "per": "1e-5", "priorityLevel": 21,
     "label": "delay-critical-5ms",  "use": "high-voltage / motion control"},
    {"fiveqi": 82, "pdbMs": 10, "per": "1e-4", "priorityLevel": 19,
     "label": "discrete-automation", "use": "discrete automation / robot cell"},
    {"fiveqi": 84, "pdbMs": 30, "per": "1e-5", "priorityLevel": 24,
     "label": "intelligent-transport", "use": "ITS / looser deterministic"},
]
# Loosest PDB in the table — anything requiring more than this is not a delay-critical flow.
MAX_DC_PDB_MS = max(q["pdbMs"] for q in QOS_MAP)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _positive_int(body, key):
    """Return int(body[key]) if it is a positive number, else None (used for validation)."""
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v > 0 else None


def derive_qos(pattern):
    """The core act: turn a TSC traffic pattern into 5G QoS requirements.

    Input (a TSC Assistance Container, TS 23.501 5.27.2 / 5.27.4):
      periodicityMicros    the flow period in microseconds (deterministic burst cadence)
      survivalTimeMicros   how long the app survives without a correctly received message
      burstArrivalTime     (optional) offset of the burst within the period, microseconds
      flowDirection        UPLINK | DOWNLINK | BIDIRECTIONAL
      maxBurstSizeBytes    (optional) bytes per burst -> derives the GFBR

    Returns (qos_dict, tscai_dict, error) where error is a (detail, cause) tuple on a malformed
    pattern. The 5QI/PDB come from QOS_MAP (real characteristics); the selection is the model.

    Selection rule (labeled): the flow's latency bound is the tighter of its period and its
    survival time (a message later than either is useless / breaches survival). We pick the
    standardized delay-critical 5QI with the LARGEST PDB that still fits inside that bound —
    the least-stringent class that honestly meets the requirement, leaving scheduler margin.
    A bound below the tightest PDB is still served by the tightest class, flagged with negative
    margin so the honesty is on the resource, not hidden.
    """
    periodicity = _positive_int(pattern, "periodicityMicros")
    survival = _positive_int(pattern, "survivalTimeMicros")
    if periodicity is None:
        return None, None, ("periodicityMicros must be a positive number of microseconds",
                            "MANDATORY_IE_INCORRECT")
    if survival is None:
        return None, None, ("survivalTimeMicros must be a positive number of microseconds",
                            "MANDATORY_IE_INCORRECT")
    direction = pattern.get("flowDirection", "DOWNLINK")
    if direction not in ("UPLINK", "DOWNLINK", "BIDIRECTIONAL"):
        return None, None, ("flowDirection must be UPLINK, DOWNLINK or BIDIRECTIONAL",
                            "MANDATORY_IE_INCORRECT")

    # Latency bound the flow can tolerate, in ms (tighter of period and survival time).
    bound_ms = min(periodicity, survival) / 1000.0

    # Pick the least-stringent delay-critical 5QI whose PDB fits the bound; fall back to the
    # tightest class (QOS_MAP[0]) when the bound is tighter than anything standardized.
    fitting = [q for q in QOS_MAP if q["pdbMs"] <= bound_ms]
    chosen = max(fitting, key=lambda q: q["pdbMs"]) if fitting else QOS_MAP[0]
    margin_ms = round(bound_ms - chosen["pdbMs"], 3)

    # Guaranteed Flow Bit Rate from the burst size and period, when the AF gave a burst size
    # (bits per burst / period). Honest arithmetic, not a fabricated headline number.
    gfbr_kbps = None
    burst = _positive_int(pattern, "maxBurstSizeBytes")
    if burst is not None:
        gfbr_kbps = round(burst * 8 / (periodicity / 1_000_000.0) / 1000.0, 3)

    qos = {
        "5qi": chosen["fiveqi"],
        "resourceType": "DELAY_CRITICAL_GBR",
        "packetDelayBudgetMs": chosen["pdbMs"],
        "packetErrorRate": chosen["per"],
        "priorityLevel": chosen["priorityLevel"],
        "qosLabel": chosen["label"],
        "latencyBoundMs": round(bound_ms, 3),
        "pdbMarginMs": margin_ms,          # >= 0 means the class meets the bound with margin
        "meetsDeterministicBound": margin_ms >= 0,
        "gfbrKbps": gfbr_kbps,
        # The requirement the TSCTSF would hand to the PCF as an Npcf_PolicyAuthorization
        # app-session (TS 29.514). Marked so nobody mistakes derivation for installation.
        "pcfHandoff": "DERIVED_NOT_INSTALLED",
    }
    # TSC Assistance Information (TS 23.501 5.27.2): what the SMF/RAN scheduler would receive to
    # align the radio grant with the deterministic burst.
    tscai = {
        "flowDirection": direction,
        "periodicity": periodicity,
        "burstArrivalTime": pattern.get("burstArrivalTime"),
        "survivalTime": survival,
    }
    return qos, tscai, None


# =========================================== PCF policy-authorization install (DEEP INTEGRATION)
# The derivation above computes the deterministic QoS the PCF would enforce. This block WIRES that
# derivation to the PCF: when a PCF advertising Npcf_PolicyAuthorization (TS 29.514) is discoverable
# in the NRF, the TSCTSF POSTs an app-session carrying the derived 5QI/PDB/GFBR so the QoS is really
# installed as a dynamic PCC rule on the flow. Presence-gated + EXACT FALLBACK, mirroring the way
# the SMF discovers and calls the PCF (services/core/smf/smf.py): any miss falls back byte-for-byte
# to the pre-integration DERIVED_NOT_INSTALLED behaviour — the whole compatibility contract.

def discover_pcf_policyauth():
    """Discover a PCF that advertises Npcf_PolicyAuthorization (TS 29.510 Nnrf_NFDiscovery,
    target-nf-type=PCF, service npcf-policyauthorization), returning its base URL or None.
    ADDITIVE + EXACT FALLBACK: any failure — NRF unreachable, no PCF registered, or a PCF that does
    NOT advertise policy-authorization (a plain PCF: the sibling pol-pcf branch is not yet on main)
    — returns None, and the caller falls back to DERIVED_NOT_INSTALLED exactly as before."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=PCF&requester-nf-type=TSCTSF")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != "npcf-policyauthorization":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def _appsession_body(af_id, flow, qos, tscai):
    """Map the derived deterministic QoS into an Npcf_PolicyAuthorization AppSessionContext
    (TS 29.514 5.6.2.2 AppSessionContextReqData): the flow's DNN/slice plus a media component
    carrying the requested delay-critical GBR 5QI, its PDB and priority, and the GFBR as the
    max guaranteed bandwidth (steered per flow direction), plus the TSC assistance — everything
    the PCF needs to install a dynamic PCC rule with the deterministic treatment."""
    gbr = qos.get("gfbrKbps")
    gbr_str = f"{gbr} Kbps" if isinstance(gbr, (int, float)) else None
    direction = tscai.get("flowDirection", "DOWNLINK")
    med = {
        "medCompN": 0,
        "fStatus": "ENABLED",
        "medType": "CONTROL",
        "des5qi": qos["5qi"],                        # requested delay-critical GBR 5QI
        "resourceType": qos["resourceType"],
        "packetDelayBudgetMs": qos["packetDelayBudgetMs"],
        "priorityLevel": qos.get("priorityLevel"),
    }
    if gbr_str and direction in ("UPLINK", "BIDIRECTIONAL"):
        med["marBwUl"] = gbr_str
    if gbr_str and direction in ("DOWNLINK", "BIDIRECTIONAL"):
        med["marBwDl"] = gbr_str
    return {
        "ascReqData": {
            "afAppId": af_id,
            "dnn": flow.get("dnn"),
            "sliceInfo": flow.get("snssai"),
            "medComponents": {"0": med},
            # TSC assistance carried alongside the QoS so the PCF/SMF can align the PCC rule with
            # the deterministic burst (TS 23.501 5.27.2 TSCAI).
            "tscQosReq": {
                "flowDirection": direction,
                "periodicity": tscai.get("periodicity"),
                "burstArrivalTime": tscai.get("burstArrivalTime"),
                "survivalTime": tscai.get("survivalTime"),
            },
        }
    }


def install_qos_at_pcf(af_id, flow, qos, tscai):
    """POST the derived QoS to the PCF as an Npcf_PolicyAuthorization app-session (TS 29.514
    5.6.2.2) so the deterministic QoS is actually installed as a dynamic PCC rule. Returns
    (appSessionId, pcfBase) on success, or (None, None) to signal EXACT FALLBACK to
    DERIVED_NOT_INSTALLED. Best-effort: the TSCTSF NEVER fails a subscription because the PCF was
    absent, unreachable, or legacy (a plain PCF returns 404) — a miss just means today's behaviour,
    unchanged and labeled. Metric tsctsf_qos_installed_total{result=installed|derived_fallback}."""
    pcf = discover_pcf_policyauth()
    if pcf is None:
        obs.log("qos_install_fallback", afId=af_id, reason="no_pcf_policyauth_discovered")
        obs.counter("tsctsf_qos_installed_total", result="derived_fallback").inc()
        return None, None
    try:
        status, resp = request("POST", f"{pcf}/npcf-policyauthorization/v1/app-sessions",
                               _appsession_body(af_id, flow, qos, tscai))
    except OSError:
        obs.log("qos_install_fallback", afId=af_id, reason="pcf_unreachable")
        obs.counter("tsctsf_qos_installed_total", result="derived_fallback").inc()
        return None, None
    if status != 201:
        obs.log("qos_install_fallback", afId=af_id, reason=f"pcf_status_{status}")
        obs.counter("tsctsf_qos_installed_total", result="derived_fallback").inc()
        return None, None
    app_session_id = (resp.get("appSessionId")
                      or (resp.get("ascRespData") or {}).get("appSessionId"))
    obs.log("qos_installed", afId=af_id, fiveqi=qos["5qi"], pdbMs=qos["packetDelayBudgetMs"],
            appSessionId=app_session_id, pcf=pcf)
    obs.counter("tsctsf_qos_installed_total", result="installed").inc()
    return app_session_id, pcf


def teardown_qos_at_pcf(pcf, app_session_id):
    """DELETE the Npcf_PolicyAuthorization app-session at teardown (TS 29.514 5.6.2.5). Best-effort:
    a PCF that is gone must never block a subscription delete (the app-session is the PCF's to
    reconcile). No-op when the subscription was never installed (fallback path)."""
    if not pcf or not app_session_id:
        return
    try:
        request("DELETE", f"{pcf}/npcf-policyauthorization/v1/app-sessions/{app_session_id}")
    except OSError:
        obs.log("qos_teardown_fallback", appSessionId=app_session_id, reason="pcf_unreachable")
        return
    obs.log("qos_uninstalled", appSessionId=app_session_id)
    obs.counter("tsctsf_qos_uninstalled_total").inc()


# ------------------------------------------------- Ntsctsf_TimeSynchronization (TS 29.565 cl.5)
# An AF configures 5G access-stratum time distribution (SIB9 / gPTP) so a GROUP of UEs shares
# one synchronized time base. Modeled: the configuration is recorded and fully described, but
# there is no real SIB9 broadcast or IEEE 802.1AS grandmaster relay (labeled).

@app.route("POST", "/ntsctsf-timesynch/v1/configurations")
def create_timesynch_configuration(params, query, body):
    group = body.get("targetUeGroup") or {}
    if not group.get("groupId") and not group.get("supis"):
        return problem(400, "Bad Request",
                       detail="targetUeGroup with a groupId or a supis list is mandatory",
                       cause="MANDATORY_IE_MISSING")
    time_domain = body.get("timeDomain", "gPTP")
    if time_domain not in ("gPTP", "PTP"):
        return problem(400, "Bad Request", detail="timeDomain must be gPTP or PTP",
                       cause="MANDATORY_IE_INCORRECT")
    config_id = "tsync-" + uuid.uuid4().hex[:12]
    record = {
        "configurationId": config_id,
        "timeDomain": time_domain,
        "gptpDomainNumber": body.get("gptpDomainNumber", 0),
        "distributionMethod": body.get("distributionMethod", "SIB9"),   # SIB9 | USER_PLANE
        "targetUeGroup": group,
        # 5G access-stratum time distribution parameters (TS 23.501 5.27.1): the clock the
        # network would relay to the UEs, and its advertised quality.
        "asTimeDistribution": {
            "referenceTimeSource": body.get("referenceTimeSource", "TSN-GRANDMASTER"),
            "clockQualityAccuracy": body.get("clockQualityAccuracy", "WITHIN_1US"),
            "sib9": body.get("distributionMethod", "SIB9") == "SIB9",
        },
        "self": f"/ntsctsf-timesynch/v1/configurations/{config_id}",
        "createdAt": now_iso(),
        # Honesty marker on the resource itself: the config is real at procedure fidelity, but
        # no gPTP grandmaster / SIB9 broadcast / TSN bridge hardware exists.
        "distributionStatus": "MODELED_NO_GPTP_HW",
    }
    timesynch_configs.put(config_id, record)
    obs.log("timesynch_configuration_created", configurationId=config_id, timeDomain=time_domain,
            groupId=group.get("groupId"), method=record["distributionMethod"])
    obs.counter("tsctsf_timesynch_configurations_total").inc()
    return 201, record


@app.route("GET", "/ntsctsf-timesynch/v1/configurations/{configurationId}")
def get_timesynch_configuration(params, query, body):
    record = timesynch_configs.get(params["configurationId"])
    if record is None:
        return problem(404, "Not Found", detail="no such time-synch configuration",
                       cause="CONFIGURATION_NOT_FOUND")
    return 200, record


@app.route("GET", "/ntsctsf-timesynch/v1/configurations")
def list_timesynch_configurations(params, query, body):
    return 200, {"configurations": list(timesynch_configs.values())}


@app.route("DELETE", "/ntsctsf-timesynch/v1/configurations/{configurationId}")
def delete_timesynch_configuration(params, query, body):
    if timesynch_configs.get(params["configurationId"]) is None:
        return problem(404, "Not Found", detail="no such time-synch configuration",
                       cause="CONFIGURATION_NOT_FOUND")
    timesynch_configs.delete(params["configurationId"])
    obs.log("timesynch_configuration_deleted", configurationId=params["configurationId"])
    obs.counter("tsctsf_timesynch_configurations_deleted_total").inc()
    return 204, {}


# ---------------------------------------------- Ntsctsf_QoSandTSCAssistance (TS 29.565 cl.5)
# An AF requests deterministic / bounded-latency treatment for a flow, carrying a TSC
# Assistance Container. The TSCTSF derives the 5G QoS (5QI + PDB + GFBR) and the TSCAI, and
# would hand the QoS to the PCF (Npcf_PolicyAuthorization). Derivation is real; installation is
# the labeled follow-up.

@app.route("POST", "/ntsctsf-qos-tsc/v1/subscriptions")
def create_qos_tsc_subscription(params, query, body):
    af_id = body.get("afId", "af-unknown")
    flow = body.get("flowDescription") or {}          # {dnn, snssai, direction, ...} — echoed
    pattern = body.get("tscAssistanceContainer") or body.get("trafficPattern") or {}
    qos, tscai, error = derive_qos(pattern)
    if error is not None:
        detail, cause = error
        obs.counter("tsctsf_qos_tsc_rejected_total").inc()
        return problem(400, "Bad Request", detail=detail, cause=cause)
    sub_id = "tscqos-" + uuid.uuid4().hex[:12]
    # DEEP INTEGRATION: install the derived deterministic QoS at the PCF as an
    # Npcf_PolicyAuthorization app-session (TS 29.514). Best-effort + EXACT FALLBACK — with no
    # policy-authorization PCF discoverable/reachable, qos stays DERIVED_NOT_INSTALLED unchanged.
    app_session_id, pcf_base = install_qos_at_pcf(af_id, flow, qos, tscai)
    if app_session_id is not None:
        qos["pcfHandoff"] = "INSTALLED"
        qos["appSessionId"] = app_session_id
    record = {
        "subscriptionId": sub_id,
        "afId": af_id,
        "flowDescription": flow,
        "tscAssistanceContainer": pattern,
        "derivedQos": qos,
        "derivedTscai": tscai,
        # Teardown handles for the installed PCC rule (None on the fallback path).
        "pcfAppSessionId": app_session_id,
        "pcfBase": pcf_base,
        "self": f"/ntsctsf-qos-tsc/v1/subscriptions/{sub_id}",
        "createdAt": now_iso(),
    }
    tsc_subs.put(sub_id, record)
    obs.log("qos_tsc_subscription_created", subscriptionId=sub_id, afId=af_id,
            fiveqi=qos["5qi"], pdbMs=qos["packetDelayBudgetMs"],
            meets=qos["meetsDeterministicBound"], pcfHandoff=qos["pcfHandoff"])
    obs.counter("tsctsf_qos_tsc_subscriptions_total", fiveqi=str(qos["5qi"])).inc()
    return 201, record


@app.route("GET", "/ntsctsf-qos-tsc/v1/subscriptions/{subscriptionId}")
def get_qos_tsc_subscription(params, query, body):
    record = tsc_subs.get(params["subscriptionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such QoS/TSC subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    return 200, record


@app.route("GET", "/ntsctsf-qos-tsc/v1/subscriptions")
def list_qos_tsc_subscriptions(params, query, body):
    return 200, {"subscriptions": list(tsc_subs.values())}


@app.route("DELETE", "/ntsctsf-qos-tsc/v1/subscriptions/{subscriptionId}")
def delete_qos_tsc_subscription(params, query, body):
    record = tsc_subs.get(params["subscriptionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such QoS/TSC subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    # DEEP INTEGRATION: release the installed PCC rule at the PCF (no-op on the fallback path).
    teardown_qos_at_pcf(record.get("pcfBase"), record.get("pcfAppSessionId"))
    tsc_subs.delete(params["subscriptionId"])
    obs.log("qos_tsc_subscription_deleted", subscriptionId=params["subscriptionId"])
    obs.counter("tsctsf_qos_tsc_subscriptions_deleted_total").inc()
    return 204, {}


@app.route("GET", "/tsctsf/qos-map")
def get_qos_map(params, query, body):
    # Lab introspection: the standardized delay-critical 5QIs the TSC->QoS mapping selects from
    # (the future TSN-config UI reads this).
    return 200, {"qosMap": QOS_MAP, "maxDelayCriticalPdbMs": MAX_DC_PDB_MS}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. Both Ntsctsf services are advertised so an AF (or the
    # PCF) discovering the TSCTSF finds both the time-synch and QoS/TSC-assistance surfaces.
    profile = {"nfType": "TSCTSF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "ntsctsf-timesynch",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "ntsctsf-qos-tsc",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("tsctsf_timesynch_configurations_active").set(len(list(timesynch_configs.values())))
    obs.gauge("tsctsf_qos_tsc_subscriptions_active").set(len(list(tsc_subs.values())))


if __name__ == "__main__":
    obs.init("tsctsf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

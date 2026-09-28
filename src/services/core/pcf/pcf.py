"""
PCF: Policy Control Function — Npcf_SMPolicyControl (SMF-facing) AND Npcf_PolicyAuthorization
(AF-facing, N5/Rx) services for the owned 5G core.

Clean-room (charter HARVEST verb): the open-digital-platform-2_0 PCF was read for the shape
of the Npcf surface only; every line here is authored for this stack, stdlib-only like the
rest of the owned core.

Spec anchors:
  Npcf_SMPolicyControl              TS 29.512 (session-management policy control service)
  Npcf_PolicyAuthorization          TS 29.514 (AF/NEF/TSCTSF-facing N5/Rx application-session
                                    policy authorization — the AF requests QoS for a described
                                    flow, the PCF derives + installs a dynamic PCC rule)
  PCF concept + policy framework    TS 23.503 (PCC rules, authorized session-AMBR, default QoS,
                                    dynamic PCC rules for AF sessions, service data flow filters)
  Nnrf_NFManagement registration    TS 29.510 section 5.2 (PCF registers as nfType PCF)
  Resource shapes                   TS 29.512 5.6.2: SmPolicyContextData (request),
                                    SmPolicyDecision (response) — sessRules, pccRules, qosDecs.
                                    TS 29.514 5.6.2: AppSessionContext / AppSessionContextReqData,
                                    MediaComponent / MediaSubComponent (flow descriptions + QoS).
  Nbsf_Management RegisterBinding   TS 29.521 5.2.2.2 (PCF publishes its session binding to a BSF)

POLICY AUTHORIZATION (N5/Rx, TS 29.514 — additive AF-facing surface, procedures/pcf_policy_authorization.txt)
  An AF — reached via the NEF (Nnef_AFsessionWithQoS / traffic-influence) or the TSCTSF (time-
  sensitive comms) — POSTs an AppSessionContext describing a media flow (flow descriptions +
  a requested 5QI / GBR / MBR, or a TSC QoS) to /npcf-policyauthorization/v1/app-sessions. The
  PCF creates an App Session Context (Sd/N5 resource), DERIVES a dynamic PCC rule (5QI + GBR/MBR
  bound to the flow filters) per media component, and INSTALLS it into its policy state keyed by
  the target SUPI. The install is LIVE: the SMF's next Npcf_SMPolicyControl_Create for that
  (supi, dnn) MERGES the installed dynamic rules into the SmPolicyDecision it returns — so the
  derived rule is visible to the SMF/policy state, not merely recorded. This is PURELY ADDITIVE:
  with no app session for a SUPI the SmPolicyDecision is BYTE-IDENTICAL to before, so every
  existing SMPolicyControl spec (run_pcf_spec, run_pdu_session_spec, run_golden_scenario_spec)
  passes unedited. PATCH/PUT re-derives the rule; DELETE revokes it (removes it from the state).

BSF BINDING (additive integration, best-effort — procedures/pcf_bsf_integration.txt)
  When the PCF creates an SM policy association it also PUBLISHES a PCF binding to the BSF, so a
  later NEF/AF that holds only the UE's IP can DISCOVER this PCF as the session's serving PCF
  (TS 29.521, the Nbsf reference point). This is gated on a BSF being discoverable via the NRF AND
  reachable, and it is BEST-EFFORT / NON-BLOCKING: any BSF fault is swallowed and NEVER fails the
  SM policy create. It also requires the UE IPv4 in the SmPolicyContextData (TS 29.512 6.1.6.2.3
  ipv4Address) — with no IP there is nothing to bind, so registration is simply skipped. With NO
  BSF (or no ipv4Address) the PCF behaves BYTE-IDENTICALLY to before: the decision is unchanged and
  no binding side effect occurs. The policy DELETE deregisters the binding, also best-effort.

WHAT IT DOES
  The SMF, during PDU session establishment (TS 23.502 4.3.2.2 step 8), calls
  Npcf_SMPolicyControl_Create with the session context (supi, dnn, S-NSSAI, ...). The PCF
  answers with an SmPolicyDecision: the AUTHORIZED session-AMBR, a DEFAULT QoS rule carrying
  a 5QI + ARP for the DNN/slice, a default PCC rule that binds "all traffic" to that QoS, and
  the gate status. The SMF applies those to the session and tags its N4 rules (see smf.py).

  Policy is DATA-DRIVEN: POLICY_TABLE below is a small labeled, first-match-wins table keyed
  by DNN and/or S-NSSAI (same match semantics as the SMF's UPF-selection table). It is the
  single place a policy decision is authored today; a TMF/PCF policy-authoring UI writes this
  table in a later pass (labeled here, ledgered in procedures/pcf_policy_control.txt).

Run: python3 pcf.py   (SBI on 127.0.0.1:7006, registers with the NRF as nfType PCF)
"""

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import host, port, url, value
from domain.statestore import open_store

PORT = port("pcf")
NRF = url("nrf")

# This PCF's own locator, published into every BSF binding so a discoverer that holds only the
# UE's IP can name THIS PCF as the session's serving PCF (TS 29.521 PcfBinding: pcfId + pcfFqdn).
# Deterministic per (host, port) and overridable — the spec reads it back at GET /pcf/nf-instance
# to confirm the very PCF it drove is the one the BSF returns. Not shifted by TELCO_PORT_OFFSET so
# it stays a stable identity string; the FQDN is the standards-shaped 3gppnetwork.org form.
PCF_ID = value("TELCO_PCF_ID") or f"pcf-{host()}-{PORT}"
PCF_FQDN = value("TELCO_PCF_FQDN") or f"pcf-{PORT}.5gc.mnc001.mcc001.3gppnetwork.org"

# ------------------------------------------------------------------- policy table
# Labeled, declarative, first-match-wins. Match keys are optional (an omitted key is a
# wildcard); "policy" is the decision applied on a match. Same match shape as the SMF's
# UPF-selection table so the two read alike. This is the ONE authoring surface for policy
# today — a TMF/PCF policy UI (later pass) will write this table instead of it being in-code.
#
# Each policy carries:
#   fiveqi        5G QoS Identifier of the default QoS flow (TS 23.501 5.7.4 / table 5.7.4-1)
#   sessionAmbr   authorized session-AMBR {uplink, downlink} (TS 23.501 5.7.2.6)
#   arp           allocation/retention priority (TS 23.501 5.7.2.2): priorityLevel 1..15
#                 (1 = highest), preemption capability/vulnerability
#   gateStatus    "OPEN" | "CLOSED" — whether the default flow may pass traffic (TS 29.512
#                 flow status, modeled at the session-default level)
#   label         human string for the UI / logs (which policy fired)
#
# The DEFAULT table below is authored for this lab's DNNs (internet/ims/edge) plus one
# slice-driven rule: SST 2 (URLLC-flavored, the robot slice used elsewhere in the stack)
# gets a low-latency 5QI and a tighter ARP regardless of DNN. No pre-PCF flow is affected —
# the SMF only consults the PCF when one is registered, and falls back to legacy otherwise.
DEFAULT_POLICY_TABLE = [
    {"sNssai": {"sst": 2}, "policy": {
        "label": "urllc-sst2", "fiveqi": 83, "gateStatus": "OPEN",
        "sessionAmbr": {"uplink": "50 Mbps", "downlink": "100 Mbps"},
        "arp": {"priorityLevel": 3, "preemptCap": "MAY_PREEMPT",
                "preemptVuln": "NOT_PREEMPTABLE"}}},
    {"dnn": "ims", "policy": {
        "label": "ims-signalling", "fiveqi": 5, "gateStatus": "OPEN",
        "sessionAmbr": {"uplink": "256 Kbps", "downlink": "256 Kbps"},
        "arp": {"priorityLevel": 1, "preemptCap": "MAY_PREEMPT",
                "preemptVuln": "NOT_PREEMPTABLE"}}},
    {"dnn": "edge", "policy": {
        "label": "edge-breakout", "fiveqi": 80, "gateStatus": "OPEN",
        "sessionAmbr": {"uplink": "200 Mbps", "downlink": "500 Mbps"},
        "arp": {"priorityLevel": 5, "preemptCap": "NOT_PREEMPT",
                "preemptVuln": "PREEMPTABLE"}}},
    {"dnn": "internet", "policy": {
        "label": "internet-default", "fiveqi": 9, "gateStatus": "OPEN",
        "sessionAmbr": {"uplink": "100 Mbps", "downlink": "200 Mbps"},
        "arp": {"priorityLevel": 8, "preemptCap": "NOT_PREEMPT",
                "preemptVuln": "PREEMPTABLE"}}},
    {"policy": {   # catch-all: any DNN the table does not name gets best-effort
        "label": "best-effort-default", "fiveqi": 9, "gateStatus": "OPEN",
        "sessionAmbr": {"uplink": "50 Mbps", "downlink": "100 Mbps"},
        "arp": {"priorityLevel": 9, "preemptCap": "NOT_PREEMPT",
                "preemptVuln": "PREEMPTABLE"}}},
]


def load_policy_table():
    """DEFAULT_POLICY_TABLE unless TELCO_PCF_POLICY overrides it (JSON array, same schema,
    netconfig file<-env precedence) — the house pattern for a declarative table."""
    raw = value("TELCO_PCF_POLICY")
    table = json.loads(raw) if raw else DEFAULT_POLICY_TABLE
    for rule in table:
        if "policy" not in rule or "fiveqi" not in rule["policy"]:
            raise RuntimeError(f"TELCO_PCF_POLICY rule missing policy.fiveqi: {rule}")
    return table


POLICY_TABLE = load_policy_table()
app = SbiApp("pcf")
# Policy associations survive a restart when TELCO_STATE_DIR is set (house persistence
# pattern); in-memory dict otherwise — same as every other NF.
store = open_store("pcf")
policies = store.collection("sm_policies")   # smPolicyId -> {context, decision}
# App Session Contexts (Npcf_PolicyAuthorization, TS 29.514) — appSessionId -> {asc, derived rules,
# state}. Additive: an empty collection means the SMPolicyControl path is byte-identical to before.
app_sessions = store.collection("app_sessions")


def match_policy(dnn, snssai):
    """(dnn, sNssai) -> policy dict, first-match-wins. Every listed sNssai field must equal
    the session's; a listed dnn must equal the session's; omitted keys are wildcards."""
    snssai = snssai or {}
    for rule in POLICY_TABLE:
        want = rule.get("sNssai") or {}
        if any(snssai.get(k) != v for k, v in want.items()):
            continue
        if "dnn" in rule and rule["dnn"] != dnn:
            continue
        return rule["policy"]
    return POLICY_TABLE[-1]["policy"]   # the catch-all always matches; belt-and-braces


def build_decision(sm_policy_id, dnn, snssai, policy):
    """Author an SmPolicyDecision (TS 29.512 5.6.2.5) from the matched policy: a session rule
    carrying the authorized session-AMBR + default QoS, one QoS decision (the 5QI/ARP), and a
    default PCC rule binding all traffic to that QoS. Simplified to the fields the SMF applies
    and the lab surfaces; the resource SHAPE is preserved so the learning transfers."""
    qos_id = f"qos-{policy['label']}"
    pcc_id = f"pcc-default-{dnn}"
    return {
        "smPolicyId": sm_policy_id,
        "policyLabel": policy["label"],
        "gateStatus": policy["gateStatus"],
        # sessRules: authorized session-AMBR + default QoS (TS 29.512 SessionRule)
        "sessRules": {
            "sess-rule-1": {
                "sessRuleId": "sess-rule-1",
                "authSessAmbr": policy["sessionAmbr"],
                "authDefQos": {"5qi": policy["fiveqi"], "arp": policy["arp"]},
            }
        },
        # qosDecs: the QoS data the PCC rule references (TS 29.512 QosData)
        "qosDecs": {
            qos_id: {"qosId": qos_id, "5qi": policy["fiveqi"], "arp": policy["arp"]}
        },
        # pccRules: default rule permitting all traffic, bound to the QoS decision above
        # (TS 29.512 PccRule, TS 23.503 6.3 default PCC rule)
        "pccRules": {
            pcc_id: {
                "pccRuleId": pcc_id,
                "precedence": 1000,
                "pccRuleStatus": "ACTIVE",
                "flowInfos": [{"flowDescription": "permit out ip from any to assigned"},
                              {"flowDescription": "permit in ip from any to assigned"}],
                "refQosData": [qos_id],
            }
        },
    }


# ------------------------------------------------------- BSF binding (best-effort integration)
# Mirrors the SMF's discover_pcf/discover_chf pattern exactly: discovery + call are guarded so a
# missing or broken BSF is invisible to the policy path. The WHOLE compatibility contract lives in
# these three functions returning early (None / no-op) on every failure or when no IP is present.

def discover_bsf():
    """Discover a BSF via the NRF (TS 29.510 Nnrf_NFDiscovery, target-nf-type=BSF), returning its
    base URL or None. ADDITIVE + BEST-EFFORT: any failure — NRF unreachable, no BSF registered,
    malformed profile — returns None and the PCF skips the binding. A stack without a BSF is
    unchanged."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=BSF&requester-nf-type=PCF")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != "nbsf-management":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def register_binding_with_bsf(supi, dnn, snssai, ipv4addr):
    """Nbsf_Management RegisterBinding (TS 29.521 5.2.2.2): publish 'THIS PCF serves THIS session'
    so a later NEF/AF holding only the UE IP can find us. BEST-EFFORT / NON-BLOCKING — gated on a
    BSF being discoverable AND reachable; ANY failure is swallowed and returns None so a BSF fault
    NEVER fails the SM policy create. Needs the UE ipv4Addr (TS 29.512 6.1.6.2.3 ipv4Address) — with
    none there is nothing to bind, so we skip silently (this is the lab SMF's path today, which is
    why every existing spec is byte-identical). Returns (bsfBase, bindingId) on success."""
    if not ipv4addr:
        return None                                  # no UE IP in the context -> nothing to bind
    bsf = discover_bsf()
    if bsf is None:
        obs.log("bsf_binding_skip", supi=supi, dnn=dnn, reason="no_bsf_discovered")
        obs.counter("pcf_bsf_registrations_total", outcome="fallback").inc()
        return None
    binding = {"supi": supi, "dnn": dnn, "snssai": snssai, "ipv4Addr": ipv4addr,
               "pcfId": PCF_ID, "pcfFqdn": PCF_FQDN}
    try:
        status, resp = request("POST", f"{bsf}/nbsf-management/v1/pcfBindings", binding)
    except OSError:
        obs.log("bsf_binding_skip", supi=supi, dnn=dnn, reason="bsf_unreachable")
        obs.counter("pcf_bsf_registrations_total", outcome="fallback").inc()
        return None
    if status != 201:
        obs.log("bsf_binding_skip", supi=supi, dnn=dnn, reason=f"bsf_status_{status}")
        obs.counter("pcf_bsf_registrations_total", outcome="fallback").inc()
        return None
    binding_id = resp.get("bindingId")
    obs.log("bsf_binding_registered", supi=supi, dnn=dnn, ipv4Addr=ipv4addr,
            bindingId=binding_id, pcfId=PCF_ID)
    obs.counter("pcf_bsf_registrations_total", outcome="registered").inc()
    return bsf, binding_id


def deregister_binding_from_bsf(bsf_base, binding_id):
    """Nbsf_Management DeregisterBinding (TS 29.521 5.2.2.5): drop the binding when the policy
    association ends. BEST-EFFORT — never raises, so a BSF fault never fails the policy delete."""
    if not bsf_base or not binding_id:
        return
    try:
        request("DELETE", f"{bsf_base}/nbsf-management/v1/pcfBindings/{binding_id}")
    except OSError:
        obs.log("bsf_deregister_skip", bindingId=binding_id, reason="bsf_unreachable")
        return
    obs.log("bsf_binding_deregistered", bindingId=binding_id)
    obs.counter("pcf_bsf_registrations_total", outcome="deregistered").inc()


@app.route("GET", "/pcf/nf-instance")
def get_nf_instance(params, query, body):
    # Lab introspection (additive): this PCF's own locator, so a driver can confirm the BSF returns
    # the SAME pcfId/pcfFqdn it published. No effect on the Npcf policy path.
    return 200, {"pcfId": PCF_ID, "pcfFqdn": PCF_FQDN}


@app.route("POST", "/npcf-smpolicycontrol/v1/sm-policies")
def create_sm_policy(params, query, body):
    # SmPolicyContextData (TS 29.512 5.6.2.3): supi + dnn are the minimum the decision keys on.
    if "supi" not in body or "dnn" not in body:
        return problem(400, "Bad Request", detail="supi and dnn are mandatory",
                       cause="MANDATORY_IE_MISSING")
    supi = body["supi"]
    dnn = body["dnn"]
    snssai = body.get("sNssai") or {"sst": 1}
    policy = match_policy(dnn, snssai)
    sm_policy_id = "smpol-" + uuid.uuid4().hex[:12]
    decision = build_decision(sm_policy_id, dnn, snssai, policy)
    # Npcf_PolicyAuthorization install point (TS 29.514 / TS 23.503 dynamic PCC rules): merge any
    # AF-installed dynamic PCC rules for this SUPI into the decision the SMF receives. When no app
    # session targets this SUPI extra_pcc is empty and the decision is BYTE-IDENTICAL to before —
    # this single guarded line is the whole compatibility contract for the SMPolicyControl path.
    extra_pcc, extra_qos = installed_rules_for(supi, dnn, snssai)
    if extra_pcc:
        decision["pccRules"].update(extra_pcc)
        decision["qosDecs"].update(extra_qos)
        decision["dynamicPccRules"] = sorted(extra_pcc)     # lab: which rules the AF installed
    record = {
        "smPolicyId": sm_policy_id, "supi": supi, "dnn": dnn, "sNssai": snssai,
        "pduSessionId": body.get("pduSessionId"), "decision": decision}
    # Publish the serving-PCF binding to a BSF, best-effort (TS 29.521 5.2.2.2). The UE IPv4 comes
    # from the SmPolicyContextData (TS 29.512 6.1.6.2.3 ipv4Address; ipv4Addr accepted as an alias).
    # register_binding_with_bsf swallows every failure and returns None when no BSF/IP is present,
    # so this line NEVER changes the decision below and NEVER fails the create.
    ipv4addr = body.get("ipv4Address") or body.get("ipv4Addr")
    binding = register_binding_with_bsf(supi, dnn, snssai, ipv4addr)
    if binding:                                      # remember it so DELETE can deregister
        record["bsfBinding"] = {"base": binding[0], "bindingId": binding[1]}
    policies.put(sm_policy_id, record)
    obs.log("sm_policy_created", smPolicyId=sm_policy_id, supi=supi, dnn=dnn,
            policy=policy["label"], fiveqi=policy["fiveqi"], gate=policy["gateStatus"])
    obs.counter("sm_policies_created_total", policy=policy["label"]).inc()
    # 201 Created; the smPolicyId is the resource id the SMF uses for GET/DELETE (real SBI
    # returns it in Location — TS 29.512 5.6.2.4; here it rides in the body too, lab-friendly).
    return 201, decision


@app.route("GET", "/npcf-smpolicycontrol/v1/sm-policies/{smPolicyId}")
def get_sm_policy(params, query, body):
    row = policies.get(params["smPolicyId"])
    if row is None:
        return problem(404, "Not Found", detail=f"no policy {params['smPolicyId']}",
                       cause="POLICY_NOT_FOUND")
    return 200, row["decision"]


@app.route("DELETE", "/npcf-smpolicycontrol/v1/sm-policies/{smPolicyId}")
def delete_sm_policy(params, query, body):
    row = policies.get(params["smPolicyId"])
    if row is None:
        return problem(404, "Not Found", detail=f"no policy {params['smPolicyId']}",
                       cause="POLICY_NOT_FOUND")
    # Deregister the BSF binding if this association registered one (best-effort, never raises).
    # Absent on every legacy/no-BSF policy, so this is a no-op there and the delete is unchanged.
    bsf_binding = row.get("bsfBinding")
    if bsf_binding:
        deregister_binding_from_bsf(bsf_binding.get("base"), bsf_binding.get("bindingId"))
    policies.delete(params["smPolicyId"])
    obs.log("sm_policy_deleted", smPolicyId=params["smPolicyId"])
    obs.counter("sm_policies_deleted_total").inc()
    return 204, {}


# ============================================================ Npcf_PolicyAuthorization (N5/Rx)
# AF-facing application-session policy authorization (TS 29.514). NEF (Nnef_AFsessionWithQoS /
# traffic-influence) and TSCTSF (time-sensitive comms) are the northbound callers; the request/
# response shape below is the contract they target — documented in procedures/pcf_policy_authorization.txt.
#
# REQUEST  POST /npcf-policyauthorization/v1/app-sessions   body = AppSessionContext:
#   { "ascReqData": {                    (TS 29.514 AppSessionContextReqData; the top level is also
#                                         accepted directly for lab-friendliness)
#       "supi": "imsi-001010000000001",  target UE (install key); "ueIpv4Addr" accepted as an alias
#       "dnn": "internet", "sNssai": {"sst": 1},   scope the install (both optional)
#       "afAppId": "af-edge-inference-1",           the requesting AF/app (optional, echoed as appId)
#       "notifUri": "http://af.local/notify",       AF notification sink (recorded, not called in-lab)
#       "mediaComponents": [             one PCC rule is derived per media component
#         { "medCompN": 1,
#           "fiveqi": 3,                 requested 5QI  (or "5qi"); "qosReference" recorded alongside
#           "gbrUl": "5 Mbps", "gbrDl": "20 Mbps",   guaranteed bit rate (GBR flow)
#           "mbrUl": "10 Mbps", "mbrDl": "40 Mbps",  maximum bit rate (also accepts marBwUl/marBwDl)
#           "arp": {...},                optional ARP override
#           "fStatus": "ENABLED",        gate for the flow (default ENABLED)
#           "fDescs": ["permit out ip from 10.60.0.0/16 to assigned",
#                      "permit in ip from assigned to 10.60.0.0/16"] } ],  IPFilterRule flow descriptions
#       "tscQosReq": {...} }             optional TSC QoS (TSCTSF path, TS 29.514 5.6.2.9) — recorded }
#
# RESPONSE 201 Created, body = the created AppSessionContext:
#   { "appSessionId": "appsess-xxxx",    the resource id (real SBI also returns it in Location)
#     "ascReqData": {...normalized...},  what the PCF stored
#     "installedPccRules": { "pcc-app-...": {pccRuleId, precedence, flowInfos, refQosData, appId,...} },
#     "installedQosDecs":   { "qos-app-...": {qosId, 5qi, gbrUl, gbrDl, mbrUl, mbrDl, arp} },
#     "state": "ACTIVE" }
# Errors are ProblemDetails (TS 29.500 5.2.7): 400 on a malformed/empty flow or no requested QoS.

_FLOW_LIMIT = 32   # belt-and-braces cap on flow descriptions per media component


def _normalize_asc(body):
    """AppSessionContext(ReqData) -> a normalized dict, tolerant of the TS map form and lab aliases.
    Raises ValueError(cause, detail) on a malformed flow so the route can answer 400."""
    asc = body.get("ascReqData") if isinstance(body.get("ascReqData"), dict) else body
    supi = asc.get("supi")
    ue_ipv4 = asc.get("ueIpv4Addr") or asc.get("ueIpv4Address") or asc.get("ipv4Addr")
    comps = asc.get("mediaComponents") or asc.get("medComponents")
    if isinstance(comps, dict):                # TS map form: {medCompN -> MediaComponent}
        comps = [dict(v, medCompN=k) for k, v in comps.items()]
    if not isinstance(comps, list) or not comps:
        raise ValueError(("MANDATORY_IE_MISSING", "at least one media component is required"))
    norm_comps = []
    any_qos = bool(asc.get("tscQosReq"))
    for i, comp in enumerate(comps):
        if not isinstance(comp, dict):
            raise ValueError(("MANDATORY_IE_INCORRECT", f"media component {i} is not an object"))
        # Gather flow descriptions: top-level fDescs, or TS medSubComps map each carrying fDescs.
        fdescs = list(comp.get("fDescs") or comp.get("flowDescriptions") or [])
        for sub in (comp.get("medSubComps") or {}).values():
            if isinstance(sub, dict):
                fdescs += list(sub.get("fDescs") or sub.get("flowDescriptions") or [])
        if not fdescs:
            raise ValueError(("MANDATORY_IE_MISSING",
                              f"media component {i} has no flow description"))
        if len(fdescs) > _FLOW_LIMIT:
            raise ValueError(("MANDATORY_IE_INCORRECT",
                              f"media component {i} exceeds {_FLOW_LIMIT} flow descriptions"))
        for d in fdescs:
            if not isinstance(d, str) or not d.strip():
                raise ValueError(("MANDATORY_IE_INCORRECT",
                                  f"media component {i} has a malformed flow description: {d!r}"))
        fiveqi = comp.get("fiveqi", comp.get("5qi"))
        if fiveqi is not None and not isinstance(fiveqi, int):
            raise ValueError(("MANDATORY_IE_INCORRECT", f"media component {i} 5qi must be an integer"))
        gbr_ul, gbr_dl = comp.get("gbrUl"), comp.get("gbrDl")
        mbr_ul = comp.get("mbrUl") or comp.get("marBwUl")
        mbr_dl = comp.get("mbrDl") or comp.get("marBwDl")
        if any(v is not None for v in (fiveqi, gbr_ul, gbr_dl, mbr_ul, mbr_dl)):
            any_qos = True
        norm_comps.append({
            "medCompN": comp.get("medCompN", i + 1),
            "fiveqi": fiveqi, "qosReference": comp.get("qosReference"),
            "gbrUl": gbr_ul, "gbrDl": gbr_dl, "mbrUl": mbr_ul, "mbrDl": mbr_dl,
            "arp": comp.get("arp"), "fStatus": comp.get("fStatus", "ENABLED"),
            "fDescs": [d.strip() for d in fdescs]})
    if not any_qos:
        raise ValueError(("MANDATORY_IE_MISSING",
                          "no requested QoS (need a 5QI, GBR/MBR, or tscQosReq)"))
    return {
        "supi": supi, "ueIpv4Addr": ue_ipv4, "dnn": asc.get("dnn"),
        "sNssai": asc.get("sNssai") or asc.get("snssai"),
        "afAppId": asc.get("afAppId") or asc.get("afId"),
        "notifUri": asc.get("notifUri") or asc.get("notificationUri"),
        "tscQosReq": asc.get("tscQosReq"), "mediaComponents": norm_comps}


def _derive_rules(app_session_id, norm):
    """Derive a dynamic PCC rule + its QoS data per media component (TS 29.514 -> TS 23.503 dynamic
    PCC rule): 5QI + GBR/MBR + ARP bound to the flow's service-data-flow filters (the flow
    descriptions). Returns (pccRules, qosDecs) keyed by their ids."""
    pcc, qos = {}, {}
    default_arp = {"priorityLevel": 2, "preemptCap": "MAY_PREEMPT", "preemptVuln": "NOT_PREEMPTABLE"}
    for i, comp in enumerate(norm["mediaComponents"]):
        cid = str(comp.get("medCompN", i + 1))
        qos_id = f"qos-app-{app_session_id}-{cid}"
        rule_id = f"pcc-app-{app_session_id}-{cid}"
        qd = {"qosId": qos_id, "arp": comp.get("arp") or default_arp}
        for k, src in (("5qi", "fiveqi"), ("gbrUl", "gbrUl"), ("gbrDl", "gbrDl"),
                       ("maxbrUl", "mbrUl"), ("maxbrDl", "mbrDl"), ("qosReference", "qosReference")):
            if comp.get(src) is not None:
                qd[k] = comp[src]
        qos[qos_id] = qd
        pcc[rule_id] = {
            "pccRuleId": rule_id, "precedence": 200 + i,
            "pccRuleStatus": "ACTIVE", "appId": norm.get("afAppId"),
            "flowInfos": [{"flowDescription": d} for d in comp["fDescs"]],
            "flowStatus": comp.get("fStatus", "ENABLED"),
            "refQosData": [qos_id]}
    return pcc, qos


def installed_rules_for(supi, dnn, snssai):
    """Every ACTIVE AF-installed dynamic PCC rule targeting this SUPI, for merge into the SMF's
    SmPolicyDecision (the LIVE install point). Only SUPI-keyed app sessions install into SM policy;
    an app session that named a dnn/sNssai only installs when they match the session. Returns
    ({} , {}) when nothing matches — the byte-identical case for the SMPolicyControl path."""
    pcc, qos = {}, {}
    if not supi:
        return pcc, qos
    for row in app_sessions.values():
        if row.get("state") != "ACTIVE":
            continue
        n = row.get("norm") or {}
        if n.get("supi") != supi:
            continue
        if n.get("dnn") and dnn and n["dnn"] != dnn:
            continue
        if n.get("sNssai") and snssai and n["sNssai"] != snssai:
            continue
        pcc.update(row.get("pccRules") or {})
        qos.update(row.get("qosDecs") or {})
    return pcc, qos


def _asc_view(row):
    """The AppSessionContext resource as returned to the AF (TS 29.514 AppSessionContext)."""
    return {"appSessionId": row["appSessionId"], "ascReqData": row["norm"],
            "installedPccRules": row.get("pccRules", {}),
            "installedQosDecs": row.get("qosDecs", {}), "state": row.get("state")}


@app.route("POST", "/npcf-policyauthorization/v1/app-sessions")
def create_app_session(params, query, body):
    # TS 29.514 5.2.2.2: an AF/NEF/TSCTSF requests policy for a described application flow.
    try:
        norm = _normalize_asc(body)
    except ValueError as exc:
        cause, detail = exc.args[0]
        obs.counter("pcf_app_sessions_total", outcome="rejected").inc()
        return problem(400, "Bad Request", detail=detail, cause=cause)
    app_session_id = "appsess-" + uuid.uuid4().hex[:12]
    pcc, qos = _derive_rules(app_session_id, norm)
    row = {"appSessionId": app_session_id, "norm": norm, "pccRules": pcc,
           "qosDecs": qos, "state": "ACTIVE"}
    app_sessions.put(app_session_id, row)
    obs.log("app_session_created", appSessionId=app_session_id, supi=norm.get("supi"),
            dnn=norm.get("dnn"), afAppId=norm.get("afAppId"),
            pccRules=sorted(pcc), fiveqi=[c.get("fiveqi") for c in norm["mediaComponents"]])
    obs.counter("pcf_app_sessions_total", outcome="created").inc()
    return 201, _asc_view(row)


def _modify_app_session(params, query, body):
    app_session_id = params["appSessionId"]
    row = app_sessions.get(app_session_id)
    if row is None:
        return problem(404, "Not Found", detail=f"no app session {app_session_id}",
                       cause="APP_SESSION_CONTEXT_NOT_FOUND")
    # Re-derive from the update body (TS 29.514 5.2.3 AppSessionContextUpdateData / full replace).
    # ascUpdateData is accepted as the update container; otherwise the body IS the update.
    upd = body.get("ascUpdateData") if isinstance(body.get("ascUpdateData"), dict) else body
    try:
        norm = _normalize_asc(upd)
    except ValueError as exc:
        cause, detail = exc.args[0]
        return problem(400, "Bad Request", detail=detail, cause=cause)
    pcc, qos = _derive_rules(app_session_id, norm)
    row.update({"norm": norm, "pccRules": pcc, "qosDecs": qos, "state": "ACTIVE"})
    app_sessions.put(app_session_id, row)
    obs.log("app_session_modified", appSessionId=app_session_id, supi=norm.get("supi"),
            pccRules=sorted(pcc), fiveqi=[c.get("fiveqi") for c in norm["mediaComponents"]])
    obs.counter("pcf_app_sessions_total", outcome="modified").inc()
    return 200, _asc_view(row)


@app.route("PATCH", "/npcf-policyauthorization/v1/app-sessions/{appSessionId}")
def patch_app_session(params, query, body):
    return _modify_app_session(params, query, body)


@app.route("PUT", "/npcf-policyauthorization/v1/app-sessions/{appSessionId}")
def put_app_session(params, query, body):
    return _modify_app_session(params, query, body)


@app.route("DELETE", "/npcf-policyauthorization/v1/app-sessions/{appSessionId}")
def delete_app_session(params, query, body):
    # TS 29.514 5.2.4: revoke the app session — the derived dynamic PCC rule is removed from the
    # policy state, so a subsequent SMPolicyControl decision no longer carries it.
    app_session_id = params["appSessionId"]
    row = app_sessions.get(app_session_id)
    if row is None:
        return problem(404, "Not Found", detail=f"no app session {app_session_id}",
                       cause="APP_SESSION_CONTEXT_NOT_FOUND")
    app_sessions.delete(app_session_id)
    obs.log("app_session_deleted", appSessionId=app_session_id,
            supi=(row.get("norm") or {}).get("supi"))
    obs.counter("pcf_app_sessions_total", outcome="deleted").inc()
    return 204, {}


@app.route("GET", "/npcf-policyauthorization/v1/app-sessions/{appSessionId}")
def get_app_session(params, query, body):
    row = app_sessions.get(params["appSessionId"])
    if row is None:
        return problem(404, "Not Found", detail=f"no app session {params['appSessionId']}",
                       cause="APP_SESSION_CONTEXT_NOT_FOUND")
    return 200, _asc_view(row)


@app.route("GET", "/npcf-policyauthorization/v1/app-sessions")
def list_app_sessions(params, query, body):
    # Lab introspection (additive): summarize the live app sessions and the rules they installed.
    rows = app_sessions.values()
    items = [{"appSessionId": r["appSessionId"], "supi": (r.get("norm") or {}).get("supi"),
              "dnn": (r.get("norm") or {}).get("dnn"), "afAppId": (r.get("norm") or {}).get("afAppId"),
              "state": r.get("state"), "pccRules": sorted(r.get("pccRules") or {})}
             for r in rows]
    return 200, {"appSessions": items, "count": len(items)}


@app.route("GET", "/pcf/policy-table")
def get_policy_table(params, query, body):
    # Lab introspection: the authored policy table (the future PCF-UI reads/writes this).
    return 200, {"policyTable": POLICY_TABLE}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2, mirroring udm/smf: nfStatus mandatory, service named
    # npcf-smpolicycontrol so an SMF discovers this PCF with target-nf-type=PCF.
    # Two services on the one PCF instance: npcf-smpolicycontrol (SMF-facing, unchanged) and
    # npcf-policyauthorization (AF-facing, so a NEF/TSCTSF discovers this surface via the NRF).
    # Additive: existing discovery (target-nf-type=PCF, serviceName npcf-smpolicycontrol) is intact.
    profile = {"nfType": "PCF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "npcf-smpolicycontrol",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                              {"serviceName": "npcf-policyauthorization",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("sm_policies_active").set(len(policies.values()))
    obs.gauge("pcf_app_sessions_active").set(len(app_sessions.values()))


if __name__ == "__main__":
    obs.init("pcf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

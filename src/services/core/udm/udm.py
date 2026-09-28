"""
UDM + embedded AUSF-lite: one process for P1. The AUSF SPLITS OUT into its own NF
(services/core/ausf) on the breadth axis; the AUSF-lite co-hosted here stays as the
EXACT fallback the AMF uses when no standalone AUSF is reachable (see amf.py).

Spec anchors:
  Nausf_UEAuthentication  TS 29.509 section 5.2 (resource shapes preserved below) — the
                          embedded AUSF-lite; a real standalone AUSF now sits in front of it.
  Nudm_UEAuthentication   TS 29.503 section 6.1: generate-auth-data — the auth VECTOR served
                          to the standalone AUSF over N13 (additive endpoint, added for the
                          AUSF split; the embedded AUSF-lite above never calls it — it has k
                          in-process — so pre-split behavior is byte-identical).
  Nudm_SDM am-data        TS 29.503 section 6.1
  Nudr_DataRepository     TS 29.504 section 5.2: subscriber provisioning writes (driven by the P7
                          OSS on a TMF641 service order). The UDM writes into an EMBEDDED store; a
                          standalone UDR (services/core/udr/udr.py) is the DATA LAYER it fronts.

DATA-LAYER INTEGRATION (additive + EXACT fallback — the make-or-break contract):
  When a UDR is DISCOVERABLE via the NRF and REACHABLE, the UDM becomes a FRONT for the data
  layer: it READS a provisioned subscriber's am-data THROUGH the UDR (Nudr QUERY), and on
  provisioning WRITES that subscriber's am-data / sm-data / smf-selection-subscription-data
  THROUGH the UDR (Nudr CREATE). When the UDR is ABSENT or unreachable, every read/write falls
  back to the EMBEDDED store byte-for-byte — run_registration_spec, run_golden_scenario_spec,
  run_pdu_session_spec and every existing spec take the exact pre-integration path. The embedded
  store is written UNCONDITIONALLY (it stays the fallback copy); the UDR mirror is best-effort.
  Metrics: udm_udr_reads_total / udm_udr_writes_total (served/mirrored via the data layer),
  udm_embedded_fallback_total (served from the embedded store because no UDR was usable).
  This mirrors the SMF's discover_pcf/discover_chf additive+fallback seam (services/core/smf).
  5G-AKA                  TS 33.501 section 6.1.3.2, NOW REAL (fidelity axis #51). The UDM mints
                          the authentication VECTOR with REAL MILENAGE (f1-f5, TS 35.206) and the
                          REAL TS 33.501 Annex A KDF: AUTN = (SQN^AK)||AMF||MAC-A carries a genuine
                          f1 MAC, XRES* is the Annex A.4 KDF over CK||IK, and KAUSF is the Annex A.2
                          KDF. The sha256(k||rand) stand-in is RETIRED — see adapters/milenage.py.

Run: python3 udm.py   (listens on 127.0.0.1:7001, registers with the NRF as UDM and AUSF)
"""

import secrets
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3]))
from adapters import milenage
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.snssai import key as snssai_key
from domain.statestore import open_store
from domain.netconfig import plmn, port, url
from domain.subscriber import Subscriber, load_subscribers

PORT = port("udm")
NRF = url("nrf")
app = SbiApp("udm")
# Persistence (issue #23 phase 1): ordered/provisioned subscribers live in the shared state
# store (sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise - see domain/statestore.py).
# The seed subscribers.json stays the read-only bootstrap set it always was. auth_contexts are
# ephemeral session state - phase 2 territory, intentionally in-memory.
store = open_store("udm")
provisioned = store.collection("subscribers")
seed_subscribers = load_subscribers(HERE.parent / "subscribers.json")
auth_contexts = {}


# ------------------------------------------------------- Nudr data-layer seam (additive + fallback)
# The UDM fronts the standalone UDR (services/core/udr/udr.py) when one is discoverable+reachable.
# Every helper below is best-effort: on ANY failure it yields to the embedded store, so a stack
# without a UDR behaves byte-for-byte like the pre-integration UDM (the make-or-break contract).
UDR_SERVICE = "nudr-dr"
DEFAULT_DNN = "internet"


def discover_udr():
    """Discover a UDR (the owned-core DATA LAYER, TS 29.510 Nnrf_NFDiscovery target-nf-type=UDR),
    returning its base URL or None. ADDITIVE + EXACT-FALLBACK, mirroring the SMF's discover_pcf/
    discover_chf: any failure — NRF unreachable, no UDR registered, malformed profile — returns
    None, and every caller takes the embedded path. This is the whole compatibility contract:
    with no UDR reachable the UDM is exactly its pre-integration self."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=UDR&requester-nf-type=UDM")
    except OSError:
        return None
    if status != 200:
        return None
    for profile in body.get("nfInstances", []):
        for svc in profile.get("nfServices", []):
            if svc.get("serviceName") != UDR_SERVICE:
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def _udr_sub_url(base, supi, kind):
    """Nudr subscription-data resource for a subscriber's provisioned data (TS 29.504 5.2.3). The
    servingPlmnId segment is the home PLMN and is used identically on read and write, so a row
    always round-trips to the same key."""
    return f"{base}/nudr-dr/v2/subscription-data/{supi}/{plmn()}/provisioned-data/{kind}"


def _udr_read_am_data(base, supi):
    """Read a provisioned subscriber's am-data document from the UDR (Nudr QUERY). Returns the
    stored subscriber record (UDR bookkeeping envelope stripped) or None on any miss/failure —
    None means the caller falls back to the embedded store, so an unreachable UDR is
    indistinguishable from the pre-integration UDM."""
    try:
        status, doc = request("GET", _udr_sub_url(base, supi, "am-data"))
    except OSError:
        return None
    if status != 200 or not isinstance(doc, dict):
        return None
    return {k: v for k, v in doc.items() if k != "_udr"}


def _sm_data(sub):
    """SessionManagementSubscriptionData (TS 29.503 6.1 / TS 29.505), one entry per subscribed
    S-NSSAI carrying a default DNN configuration. Written through the UDR so the SMF's
    session-management view of the subscriber round-trips via the data layer."""
    return {snssai_key(s): {"dnnConfigurations":
                            {DEFAULT_DNN: {"pduSessionTypes": {"defaultSessionType": "IPV4"}}}}
            for s in (sub.subscribedNssai or [{"sst": 1}])}


def _smf_selection(sub):
    """SmfSelectionSubscriptionData (TS 29.503 6.1.6.2 / TS 29.505): the SMF-selection view —
    subscribed S-NSSAI infos with their allowed DNNs, derived from the subscription's S-NSSAIs."""
    return {"subscribedSnssaiInfos": {snssai_key(s): {"dnnInfos": [{"dnn": DEFAULT_DNN}]}
                                      for s in (sub.subscribedNssai or [{"sst": 1}])}}


def udr_provision(supi, sub):
    """On provisioning, WRITE the subscriber's am-data / sm-data / smf-selection-subscription-data
    THROUGH the UDR (Nudr CREATE) so the data layer holds the source of truth for reads. Best-
    effort + additive: if no UDR is reachable this is a no-op and the embedded store (written
    unconditionally by the caller) remains the sole copy — the exact pre-integration behavior.
    Returns True iff the data layer accepted the write."""
    udr = discover_udr()
    if udr is None:
        return False
    docs = {"am-data": asdict(sub), "sm-data": _sm_data(sub),
            "smf-selection-subscription-data": _smf_selection(sub)}
    wrote = False
    for kind, doc in docs.items():
        try:
            status, _ = request("PUT", _udr_sub_url(udr, supi, kind), doc)
        except OSError:
            break
        wrote = wrote or status in (200, 201)
    if wrote:
        obs.counter("udm_udr_writes_total").inc()
        obs.log("subscriber_provisioned_via_udr", supi=supi, udr=udr)
    return wrote


def find_subscriber(supi):
    """Provisioned subscribers first, then the seed set. When a UDR (the owned-core data layer) is
    discoverable+reachable the provisioned read is served THROUGH the UDR (Nudr am-data); otherwise
    — and for the seed set, which was never provisioned into the UDR — the EMBEDDED store answers,
    byte-identical to the pre-integration UDM. from_record accepts subscribedNssai or legacy nssai."""
    udr = discover_udr()
    if udr is not None:
        rec = _udr_read_am_data(udr, supi)
        if rec is not None:
            obs.counter("udm_udr_reads_total").inc()
            return Subscriber.from_record(rec)
        # UDR reachable but no row for this supi (e.g. a seed subscriber) -> embedded fallback.
    row = provisioned.get(supi)
    if row is not None:
        obs.counter("udm_embedded_fallback_total").inc()
        return Subscriber.from_record(row)
    sub = seed_subscribers.get(supi)
    if sub is not None:
        obs.counter("udm_embedded_fallback_total").inc()
    return sub


# SQN state per subscriber (TS 33.102 6.3.2): the home network's sequence-number counter, kept in
# the vector's AUTN as SQN^AK. Ephemeral/in-memory — the UE does not enforce SQN freshness (no
# resync in this pass, ledgered in procedures/ausf_authentication.txt), so a UDM restart is safe.
_sqn_state = {}


def _next_sqn(supi):
    v = _sqn_state.get(supi, 0x000000000000) + 0x20   # step by the IND-range spacing (TS 33.102)
    _sqn_state[supi] = v
    return v.to_bytes(6, "big")


def auth_vector(sub, snn):
    """A REAL 5G-AKA authentication vector (TS 33.501 6.1.3.2). The UDM owns K and OP, so it runs
    MILENAGE f1-f5 (TS 35.206) over a fresh RAND + the subscriber's SQN and derives XRES* / KAUSF
    with the TS 33.501 Annex A KDF (Annex A.4 / A.2). Returns {rand, autn, xresStar, kausf} as hex
    — the AUSF gets the vector but never K (TS 33.501 6.1, home-network secret)."""
    rand = secrets.token_bytes(16)
    v = milenage.network_vector(sub.k, rand, _next_sqn(sub.supi), milenage.DEFAULT_AMF, snn=snn)
    return {"rand": v["rand"], "autn": v["autn"], "xresStar": v["xres_star"], "kausf": v["kausf"]}


@app.route("POST", "/nausf-auth/v1/ue-authentications")
def start_authentication(params, query, body):
    # AuthenticationInfo requires supiOrSuci and servingNetworkName (TS 29.509 6.1.6.2.2)
    if "servingNetworkName" not in body:
        return problem(400, "Bad Request", detail="servingNetworkName is mandatory",
                       cause="MANDATORY_IE_MISSING")
    supi = body.get("supiOrSuci")
    sub = find_subscriber(supi)
    if sub is None or sub.status != "ACTIVE":
        return problem(404, "Not Found", detail=f"no active subscription for {supi}",
                       cause="USER_NOT_FOUND")
    ctx_id = str(uuid.uuid4())
    # Embedded AUSF-lite holds K in-process: mint the REAL vector now, store XRES* for the confirm.
    av = auth_vector(sub, body["servingNetworkName"])
    auth_contexts[ctx_id] = {"supi": supi, "xresStar": av["xresStar"]}
    return 201, {
        "authCtxId": ctx_id,
        "authType": "5G_AKA",
        "5gAuthData": {"rand": av["rand"], "autn": av["autn"]},
    }


@app.route("PUT", "/nausf-auth/v1/ue-authentications/{authCtxId}/5g-aka-confirmation")
def confirm_authentication(params, query, body):
    ctx = auth_contexts.pop(params["authCtxId"], None)
    if ctx is None:
        return problem(404, "Not Found", cause="CONTEXT_NOT_FOUND")
    # Both outcomes are 200 with ConfirmationDataResponse.authResult (TS 29.509 6.1.6.2.5).
    # RES* was computed against the stored XRES* (real TS 33.501 A.4 KDF) minted at start.
    if body.get("resStar") != ctx["xresStar"]:
        obs.log("authentication_result", supi=ctx["supi"], authResult="AUTHENTICATION_FAILURE")
        obs.counter("auth_failures_total").inc()
        return 200, {"authResult": "AUTHENTICATION_FAILURE"}
    obs.log("authentication_result", supi=ctx["supi"], authResult="AUTHENTICATION_SUCCESS")
    obs.counter("auth_successes_total").inc()
    return 200, {"authResult": "AUTHENTICATION_SUCCESS", "supi": ctx["supi"]}


@app.route("POST", "/nudm-ueau/v1/{supi}/security-information/generate-auth-data")
def generate_auth_data(params, query, body):
    """Nudm_UEAuthentication_Get (TS 29.503 6.1, N13): the standalone AUSF asks the UDM for a
    5G-AKA authentication VECTOR. The UDM owns K (never leaves the home network, TS 33.501 6.1),
    so it mints RAND, computes the expected RES* (xresStar) and derives KAUSF here; the AUSF
    gets the vector but not K. ADDITIVE — the embedded AUSF-lite (start/confirm above) does NOT
    use this path, so pre-split registration is byte-identical.

    Request  AuthenticationInfoRequest: {servingNetworkName, ausfInstanceId?} (TS 29.503 6.1.6.2.2)
    Response AuthenticationInfoResult:   authType + av (rand/autn/xresStar/kausf) (6.1.6.2.6)."""
    if "servingNetworkName" not in body:
        return problem(400, "Bad Request", detail="servingNetworkName is mandatory",
                       cause="MANDATORY_IE_MISSING")
    sub = find_subscriber(params["supi"])
    if sub is None or sub.status != "ACTIVE":
        # DAI_NOT_FOUND (TS 29.503) — the AUSF turns this into an auth reject to the AMF
        return problem(404, "Not Found", detail=f"no active subscription for {params['supi']}",
                       cause="USER_NOT_FOUND")
    # REAL 5G-AKA vector (MILENAGE + TS 33.501 Annex A KDF): AUTN carries a genuine f1 MAC-A,
    # xresStar is the Annex A.4 KDF, kausf the Annex A.2 KDF. The AUSF compares UE RES* against
    # xresStar and derives KSEAF from kausf; it never sees K.
    av = auth_vector(sub, body["servingNetworkName"])
    obs.log("auth_vector_generated", supi=params["supi"],
            servingNetworkName=body["servingNetworkName"])
    obs.counter("auth_vectors_generated_total").inc()
    return 200, {"authType": "5G_AKA",
                 "supi": params["supi"], "authenticationVector": av}


@app.route("PUT", "/nudr-dr/v2/subscription-data/{supi}/provisioned-data/am-data")
def provision_subscriber(params, query, body):
    # Nudr_DataRepository provisioning shape (TS 29.504 section 5.2 / TS 29.505 subscription data).
    # The UDR lives inside the UDM process for now, exactly like the AUSF does (module docstring);
    # it splits into its own NF in a later phase. Written by the P7 OSS on a TMF641 service order.
    if "k" not in body:
        return problem(400, "Bad Request", detail="permanent key k is mandatory",
                       cause="MANDATORY_IE_MISSING")
    created = find_subscriber(params["supi"]) is None
    # Subscribed S-NSSAIs (slicing wave 1, epic #11): the payload may carry subscribedNssai
    # or the legacy nssai key (the OSS udm-subscriber connector sends nssai from order
    # characteristics); absent -> today's default [{"sst": 1}], so every pre-slicing order
    # provisions exactly the subscription it always did.
    try:
        sub = Subscriber.from_record({
            "supi": params["supi"], "k": body["k"], "plmn": body.get("plmn", plmn()),
            "subscribedNssai": body.get("subscribedNssai") or body.get("nssai"),
            "status": body.get("status", "ACTIVE")})
    except ValueError as exc:
        return problem(400, "Bad Request", detail=str(exc), cause="INVALID_NSSAI")
    # Persistence (issue #23 phase 1): the write lands in the state store, so with
    # TELCO_STATE_DIR set an ordered subscriber survives a UDM restart; without it the store
    # is dict-backed and a restart forgets ordered subscribers, exactly as before.
    provisioned.put(params["supi"], asdict(sub))
    # Additive data-layer mirror: when a UDR is discoverable+reachable, write am-data / sm-data /
    # smf-selection THROUGH it (Nudr CREATE). No UDR reachable -> no-op, and the embedded put above
    # remains the sole copy: byte-identical to the pre-integration UDM.
    udr_provision(params["supi"], sub)
    obs.log("subscriber_provisioned", supi=params["supi"], status=sub.status, created=created,
            subscribedNssai=[snssai_key(s) for s in sub.subscribedNssai])
    obs.counter("subscribers_provisioned_total").inc()
    return (201 if created else 200), {"supi": params["supi"], "status": sub.status}


@app.route("GET", "/nudm-sdm/v2/{supi}/am-data")
def am_data(params, query, body):
    sub = find_subscriber(params["supi"])
    if sub is None:
        return problem(404, "Not Found", cause="USER_NOT_FOUND")
    # subscribedNssai is the canonical key (TS 23.501 5.15.3 subscribed S-NSSAIs); "nssai"
    # stays as a legacy alias with the same value so pre-slicing readers keep working.
    return 200, {"supi": sub.supi, "plmn": sub.plmn,
                 "subscribedNssai": sub.subscribedNssai, "nssai": sub.subscribedNssai,
                 "status": sub.status}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2: nfStatus mandatory, addresses and services as arrays
    for nf_type, service in (("UDM", "nudm-sdm"), ("AUSF", "nausf-auth")):
        profile = {"nfType": nf_type, "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
                   "nfServices": [{"serviceName": service,
                                   "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
        for _ in range(25):
            try:
                request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
                break
            except OSError:
                time.sleep(0.2)


if __name__ == "__main__":
    obs.init("udm")
    register_with_nrf()
    serve(app, PORT)

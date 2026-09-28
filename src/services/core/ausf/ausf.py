"""
AUSF: Authentication Server Function — Nausf_UEAuthentication for the owned 5G core.

Clean-room (charter HARVEST verb): the open-digital-platform-2_0 AUSF was read for the shape
of the Nausf surface and the 5G-AKA flow only; every line here is authored for this stack,
stdlib-only like the rest of the owned core.

WHY THIS NF EXISTS
  5G-AKA used to run INSIDE the UDM process (services/core/udm registered as nfType AUSF and
  served /nausf-auth itself — the "embedded AUSF-lite"). This is the real split: a standalone
  AUSF sits on the N12 (AMF<->AUSF) and N13 (AUSF<->UDM) interfaces where 3GPP puts it. The
  home network's secret K never reaches the AUSF — the UDM mints the vector and keeps K
  (TS 33.501 6.1). The AUSF holds the anchor keys and confirms the response.

  DELICATE by construction: the AUSF speaks the EXACT request/response shapes the AMF already
  used against the UDM-embedded AUSF-lite, so the AMF's fallback (AUSF absent/unreachable ->
  direct UDM-embedded auth) is byte-identical and every pre-split spec runs unedited.

Spec anchors:
  Nausf_UEAuthentication            TS 29.509 section 5.2 (UE authentication service)
    POST /nausf-auth/v1/ue-authentications                         6.1.3.2 (initiate, N12)
    PUT  .../{authCtxId}/5g-aka-confirmation                       6.1.3.5 (confirm RES*, N12)
  5G-AKA procedure + keys           TS 33.501 section 6.1.3.2 (challenge/response),
                                    Annex A.6 (KSEAF = KDF(KAUSF, SNN))
  Nudm_UEAuthentication_Get         TS 29.503 section 6.1 (auth vector from the UDM, N13)
  Nnrf_NFManagement registration    TS 29.510 section 5.2 (AUSF registers as nfType AUSF)

REAL CRYPTO (fidelity axis #51): RAND/AUTN/XRES*/KAUSF come from the UDM's REAL MILENAGE vector.
XRES* is the TS 33.501 Annex A.4 KDF over CK||IK (the exact value the UE recomputes, so the AMF
path is identical), and KSEAF is the REAL Annex A.6 KDF HMAC-SHA256(KAUSF, FC=0x6C || SNN). The
sha256 stand-in is RETIRED (adapters/milenage.py). EAP-AKA' and SUCI deconcealment are follow-ups
(procedures/ausf_authentication.txt ledger).

Run: python3 ausf.py   (SBI on 127.0.0.1:7015, registers with the NRF as nfType AUSF)
"""

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters import milenage
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import port, url

PORT = port("ausf")
NRF = url("nrf")
app = SbiApp("ausf")
# Auth contexts are ephemeral 5G-AKA session state (challenge issued, awaiting RES*) — in-memory
# by design, exactly like the UDM-embedded AUSF-lite kept them.
auth_contexts = {}   # authCtxId -> {supi, rand, xresStar, kausf, servingNetworkName}
app_instance_id = str(uuid.uuid4())   # this AUSF's NF instance id (NRF registration + N13 calls)


def discover(nf_type):
    status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                  f"?target-nf-type={nf_type}&requester-nf-type=AUSF")
    instances = body.get("nfInstances", [])
    if not instances:
        raise LookupError(f"NRF has no {nf_type}")
    endpoint = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{endpoint['ipv4Address']}:{endpoint['port']}"


def derive_kseaf(kausf, serving_network_name):
    """KSEAF (TS 33.501 Annex A.6): the security-anchor key = KDF(KAUSF, serving network name),
    REAL HMAC-SHA256 with FC=0x6C and P0=SNN. The AUSF derives it from the KAUSF the UDM supplied
    and hands it to the serving network (SEAF/AMF) on a successful confirmation."""
    return milenage.derive_kseaf(bytes.fromhex(kausf), serving_network_name).hex()


@app.route("POST", "/nausf-auth/v1/ue-authentications")
def start_authentication(params, query, body):
    # AuthenticationInfo requires supiOrSuci and servingNetworkName (TS 29.509 6.1.6.2.2).
    # Same guard, same shape as the UDM-embedded AUSF-lite so the AMF sees an identical surface.
    if "servingNetworkName" not in body:
        return problem(400, "Bad Request", detail="servingNetworkName is mandatory",
                       cause="MANDATORY_IE_MISSING")
    supi = body.get("supiOrSuci")
    snn = body["servingNetworkName"]
    # N13: fetch the 5G-AKA vector from the UDM (Nudm_UEAuthentication_Get). The UDM owns K;
    # the AUSF gets RAND/AUTN/XRES*/KAUSF but never K.
    try:
        udm = discover("UDM")
    except LookupError:
        return problem(500, "Internal Server Error", detail="no UDM registered",
                       cause="UDM_UNREACHABLE")
    try:
        status, av_result = request(
            "POST", f"{udm}/nudm-ueau/v1/{supi}/security-information/generate-auth-data",
            {"servingNetworkName": snn, "ausfInstanceId": app_instance_id})
    except OSError:
        return problem(504, "Gateway Timeout", detail="UDM unreachable for auth vector",
                       cause="UDM_UNREACHABLE")
    if status != 200:
        # The UDM said the subscriber is unknown/inactive; surface it as the AMF expects
        # (non-201 -> RegistrationReject), preserving the embedded-path outcome.
        obs.counter("auth_vector_failures_total").inc()
        return problem(status, "Not Found", detail=f"no auth vector for {supi}",
                       cause="USER_NOT_FOUND")
    av = av_result["authenticationVector"]
    ctx_id = str(uuid.uuid4())
    auth_contexts[ctx_id] = {"supi": supi, "rand": av["rand"], "xresStar": av["xresStar"],
                             "kausf": av["kausf"], "servingNetworkName": snn}
    obs.log("authentication_initiated", supi=supi, authCtxId=ctx_id, servingNetworkName=snn)
    obs.counter("auth_initiations_total").inc()
    # 201 UeAuthenticationCtx (TS 29.509 6.1.3.2.3): the 5gAuthData carries the challenge; the
    # body shape (authCtxId + 5gAuthData{rand, autn}) is IDENTICAL to the UDM-embedded AUSF-lite.
    return 201, {
        "authCtxId": ctx_id,
        "authType": "5G_AKA",
        "5gAuthData": {"rand": av["rand"], "autn": av["autn"]},
        "_links": {"5g-aka": {"href":
                   f"/nausf-auth/v1/ue-authentications/{ctx_id}/5g-aka-confirmation"}},
    }


@app.route("PUT", "/nausf-auth/v1/ue-authentications/{authCtxId}/5g-aka-confirmation")
def confirm_authentication(params, query, body):
    ctx = auth_contexts.pop(params["authCtxId"], None)
    if ctx is None:
        return problem(404, "Not Found", cause="CONTEXT_NOT_FOUND")
    # RES* verification (TS 33.501 6.1.3.2.2): compare the UE's RES* against the UDM's XRES*.
    # Both outcomes are 200 with ConfirmationDataResponse.authResult (TS 29.509 6.1.6.2.5),
    # exactly like the embedded path — so the AMF branches identically.
    if body.get("resStar") != ctx["xresStar"]:
        obs.log("authentication_result", supi=ctx["supi"], authResult="AUTHENTICATION_FAILURE")
        obs.counter("auth_failures_total").inc()
        return 200, {"authResult": "AUTHENTICATION_FAILURE"}
    # Success: derive the anchor key KSEAF for the serving network (TS 33.501 6.1.3.1).
    kseaf = derive_kseaf(ctx["kausf"], ctx["servingNetworkName"])
    obs.log("authentication_result", supi=ctx["supi"], authResult="AUTHENTICATION_SUCCESS")
    obs.counter("auth_successes_total").inc()
    return 200, {"authResult": "AUTHENTICATION_SUCCESS", "supi": ctx["supi"], "kseaf": kseaf}


@app.route("GET", "/nausf-auth/v1/ue-authentications/{authCtxId}")
def get_authentication(params, query, body):
    ctx = auth_contexts.get(params["authCtxId"])
    if ctx is None:
        return problem(404, "Not Found", cause="CONTEXT_NOT_FOUND")
    return 200, {"authCtxId": params["authCtxId"], "authType": "5G_AKA",
                 "supi": ctx["supi"], "status": "ONGOING"}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2, mirroring udm/pcf: nfStatus mandatory, service named
    # nausf-auth so an AMF discovers this AUSF with target-nf-type=AUSF.
    profile = {"nfType": "AUSF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nausf-auth",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{app_instance_id}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("auth_contexts_active").set(len(auth_contexts))


if __name__ == "__main__":
    obs.init("ausf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

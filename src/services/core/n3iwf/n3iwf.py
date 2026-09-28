"""
N3IWF: Non-3GPP InterWorking Function — the untrusted non-3GPP (Wi-Fi) access door into the core.

A UE that reaches the 5G core over untrusted non-3GPP access (a plain Wi-Fi / internet path, no
gNB, no licensed radio) does NOT speak NGAP to the AMF itself. It first builds an IPsec tunnel to
the N3IWF with IKEv2, and the N3IWF then relays the UE's NAS to the AMF over N2 — playing exactly
the role a gNB plays for 3GPP access. The N3IWF is the non-3GPP analogue of the gNB: same N2/NGAP
southbound face to the AMF, a Wi-Fi + IPsec northbound face to the UE instead of the Uu radio.

Spec anchors:
  Non-3GPP interworking / N3IWF     TS 23.501 section 4.2.8.2 (untrusted non-3GPP access reference
                                    architecture; N3IWF terminates N2/N3 for non-3GPP access).
  Registration via untrusted        TS 23.502 section 4.12.2 (registration via untrusted non-3GPP
  non-3GPP access                   access: IKEv2/IPsec setup, then NAS relayed to the AMF over N2).
  UE <-> N3IWF signalling           TS 24.502 (access to the 5GC via non-3GPP access networks:
                                    IKEv2, IPsec SA, NAS over the signalling IPsec SA).
  N2 semantics (as a gNB)           TS 38.413 (InitialUEMessage / Uplink/Downlink NASTransport). The
                                    N3IWF drives the AMF's EXISTING N2 interface (POST /n2/ue-messages)
                                    exactly as services/ran/ (the gNB) does — the AMF is not edited.
  Access type                       TS 29.571 section 5.4.3.4 AccessType = NON_3GPP_ACCESS: the N3IWF
                                    stamps every UE it serves as reaching the core over non-3GPP access.
  Nnrf_NFManagement (register)      TS 29.510 section 5.2 (register as N3IWF), 5.3 (discover the AMF).

The relayed NAS is the ordinary 5GMM registration (TS 24.501 5.5.1) + REAL 5G-AKA (the same
MILENAGE RES* the UDM/ue_sim compute — TS 35.206 f1-f5 + TS 33.501 Annex A.4, adapters/milenage.py):
RegistrationRequest -> AuthenticationRequest ->
AuthenticationResponse -> SecurityModeCommand -> SecurityModeComplete -> RegistrationAccept. The
N3IWF holds K only to answer the AMF's authentication challenge on the UE's behalf inside the
IKEv2-EAP exchange — the untrusted-access analogue of the UE answering over the radio.

Labeled simplifications (ledgered in procedures/n3iwf_non3gpp_access.txt):
  - IKEv2 / IPsec is SHAPED, NOT REAL CRYPTO. establish_ipsec() records an IKE_SA_INIT + IKE_AUTH +
    CHILD_SA state walk and mints labeled SPIs / an inner IP, but performs no Diffie-Hellman, no
    ESP, no real packet protection. It is the interface, not the idea. Real IKEv2 = RFC 7296.
  - The signalling IPsec SA is modeled; the user-plane child SA (the IPsec tunnel that would carry
    a PDU session's data over non-3GPP access, TS 23.501 4.2.8.2 N3) is a FOLLOW-UP: this pass
    proves control-plane attach (NAS registration) only.
  - The N3IWF answers 5G-AKA on the UE's behalf from a provided K (the EAP-AKA' peer is folded in),
    rather than tunnelling EAP end-to-end to a separate UE process. Honest at procedure fidelity.

Run: python3 n3iwf.py   (SBI on 127.0.0.1:7024, registers with the NRF as N3IWF)
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters import milenage
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import plmn, port, url

PORT = port("n3iwf")
NRF = url("nrf")
_MCC, _MNC = plmn()[:3], plmn()[3:]
# The serving network name (TS 24.501 9.12.1) RES* binds to — identical to the AMF's constant.
SERVING_NETWORK_NAME = f"5G:mnc{_MNC.zfill(3)}.mcc{_MCC}.3gppnetwork.org"
# AccessType per TS 29.571 5.4.3.4 — every UE the N3IWF serves reaches the core over non-3GPP access.
ACCESS_TYPE = "NON_3GPP_ACCESS"
app = SbiApp("n3iwf")

# N3IWF-held UE contexts (keyed by SUPI): the IPsec tunnel + the relayed-registration outcome.
ue_contexts = {}
_ip_pool = [10, 60, 0, 0]   # inner-IP allocator for the shaped IPsec tunnel (labeled)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def discover(nf_type):
    """NRF discovery, same shape as services/core/amf/amf.py — returns a base URL or raises."""
    status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                  f"?target-nf-type={nf_type}&requester-nf-type=N3IWF")
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        raise LookupError(f"NRF has no {nf_type}")
    endpoint = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{endpoint['ipv4Address']}:{endpoint['port']}"


def res_star(k, rand, autn):
    """The SAME REAL 5G-AKA RES* the UDM/ue_sim compute (MILENAGE + TS 33.501 Annex A.4). The N3IWF
    answers the AMF's challenge on the UE's behalf inside the (shaped) IKEv2-EAP exchange: it runs
    MILENAGE over RAND+AUTN with the UE's K, recovers SQN, and returns RES* bound to the SNN. On a
    wrong key the MAC fails but it still returns a (wrong) RES* so the AMF/UDM rejects it."""
    uev = milenage.ue_vector(k, rand, autn, snn=SERVING_NETWORK_NAME, raise_on_mac=False)
    return uev["res_star"]


def allocate_inner_ip():
    """Allocate a UE inner tunnel address (shaped — labeled, not a real IPsec-assigned address)."""
    _ip_pool[3] += 1
    if _ip_pool[3] > 254:
        _ip_pool[3] = 1
        _ip_pool[2] += 1
    return ".".join(str(o) for o in _ip_pool)


def establish_ipsec(supi, ue_outer_ip):
    """SHAPED IKEv2 / IPsec setup (labeled — NO REAL CRYPTO). Records the IKE_SA_INIT -> IKE_AUTH
    -> CHILD_SA state walk of TS 24.502 / RFC 7296 and mints labeled SPIs + an inner IP, but
    performs no Diffie-Hellman and no ESP. Returns the tunnel record. See the module ledger."""
    tunnel = {
        "tunnelId": uuid.uuid4().hex,
        "shaped": True,   # honesty marker on the resource itself: not real crypto
        "ikeStateWalk": ["IKE_SA_INIT", "IKE_AUTH", "CHILD_SA_ESTABLISHED"],
        "state": "ESTABLISHED",
        "spiInitiator": uuid.uuid4().hex[:16],
        "spiResponder": uuid.uuid4().hex[:16],
        # Labeled shaped algorithm names (what real IKEv2 would negotiate) — recorded, not applied.
        "ikeProposal": {"encr": "ENCR_AES_CBC", "integ": "AUTH_HMAC_SHA2_256_128",
                        "prf": "PRF_HMAC_SHA2_256", "dhGroup": 14},
        "ueOuterIp": ue_outer_ip,
        "ueInnerIp": allocate_inner_ip(),
        "n3iwfInnerIp": "10.60.0.254",
        "createdAt": now_iso(),
    }
    obs.log("ipsec_tunnel_shaped", supi=supi, tunnelId=tunnel["tunnelId"], shaped=True)
    obs.counter("n3iwf_ipsec_tunnels_total").inc()
    return tunnel


def relay_nas(amf, nas, extra=None):
    """Relay one NAS message to the AMF over its EXISTING N2 interface (POST /n2/ue-messages),
    exactly as a gNB does. extra carries N2-level info beside the NAS (e.g. accessType). Returns
    the AMF's DownlinkNASTransport reply's inner NAS dict."""
    body = {"nas": nas}
    if extra:
        body.update(extra)
    status, reply = request("POST", f"{amf}/n2/ue-messages", body)
    return status, reply.get("nas", {})


def register_over_n2(supi, k, requested_nssai):
    """Drive the UE's NAS registration THROUGH the AMF's N2 interface, N3IWF playing the gNB role.
    The N3IWF answers 5G-AKA on the UE's behalf (res_star). Returns (ok, outcome_dict)."""
    amf = discover("AMF")
    # N2-level access-type stamp (TS 29.571): the AMF's InitialUEMessage arrives over non-3GPP
    # access. Carried beside the NAS as a gNB would carry its RAT/location — additive, the AMF's
    # existing N2 surface ignores unknown N2 fields, so the AMF is not edited.
    n2 = {"accessType": ACCESS_TYPE, "anType": "NON_3GPP_ACCESS"}

    reg = {"type": "RegistrationRequest", "supi": supi}
    if requested_nssai:
        reg["requestedNssai"] = requested_nssai
    status, nas = relay_nas(amf, reg, n2)
    if nas.get("type") != "AuthenticationRequest":
        return False, {"stage": "registration",
                       "gmmCause": nas.get("gmmCause"), "causeText": nas.get("causeText")}

    answer = res_star(k, nas["rand"], nas["autn"])
    status, nas = relay_nas(amf, {"type": "AuthenticationResponse", "supi": supi,
                                  "resStar": answer}, n2)
    if nas.get("type") != "SecurityModeCommand":
        return False, {"stage": "authentication", "reply": nas.get("type")}

    status, nas = relay_nas(amf, {"type": "SecurityModeComplete", "supi": supi}, n2)
    if nas.get("type") != "RegistrationAccept":
        return False, {"stage": "security_mode", "gmmCause": nas.get("gmmCause"),
                       "reply": nas.get("type")}

    return True, {"guami": nas.get("guami"), "allowedNssai": nas.get("allowedNssai")}


# --------------------------------------------------------------- non-3GPP UE attach (control plane)
# TS 23.502 4.12.2: a Wi-Fi UE (1) does IKEv2/IPsec setup with the N3IWF, then (2) the N3IWF relays
# its NAS registration to the AMF over N2. One POST drives the whole untrusted-access attach.

@app.route("POST", "/n3iwf/ue-attach")
def ue_attach(params, query, body):
    supi = body.get("supi")
    k = body.get("k")
    if not supi or not k:
        return problem(400, "Bad Request", detail="supi and k are mandatory",
                       cause="MANDATORY_IE_MISSING")
    ue_outer_ip = body.get("ueOuterIp", "192.0.2.10")   # the UE's Wi-Fi/internet-side address
    requested_nssai = body.get("requestedNssai")

    obs.log("ue_attach_start", supi=supi, accessType=ACCESS_TYPE)
    # (1) SHAPED IKEv2 / IPsec setup (labeled — no real crypto).
    tunnel = establish_ipsec(supi, ue_outer_ip)

    # (2) Relay NAS registration to the AMF over N2 (N3IWF as the gNB for non-3GPP access).
    ok, outcome = register_over_n2(supi, k, requested_nssai)

    record = {"supi": supi, "accessType": ACCESS_TYPE, "tunnel": tunnel,
              "registered": ok, "attachedAt": now_iso()}
    if ok:
        record.update(state="REGISTERED", guami=outcome.get("guami"),
                      allowedNssai=outcome.get("allowedNssai"))
        obs.log("ue_attached", supi=supi, accessType=ACCESS_TYPE, state="REGISTERED")
        obs.counter("n3iwf_ue_attach_total").inc()
        ue_contexts[supi] = record
        return 201, record
    record["state"] = "ATTACH_FAILED"
    record["failure"] = outcome
    obs.log("ue_attach_failed", supi=supi, stage=outcome.get("stage"))
    obs.counter("n3iwf_ue_attach_failures_total").inc()
    ue_contexts[supi] = record
    # 403: the non-3GPP access attempt was refused by the core (auth/registration reject).
    return 403, record


@app.route("GET", "/n3iwf/ue-contexts/{supi}")
def get_ue_context(params, query, body):
    ctx = ue_contexts.get(params["supi"])
    if ctx is None:
        return problem(404, "Not Found", detail=f"no non-3GPP UE context for {params['supi']}")
    return 200, ctx


@app.route("GET", "/n3iwf/status")
def status(params, query, body):
    registered = [c for c in ue_contexts.values() if c.get("registered")]
    return 200, {
        "nf": "N3IWF",
        "accessType": ACCESS_TYPE,
        "plmn": {"mcc": _MCC, "mnc": _MNC},
        "ipsec": "SHAPED (labeled — no real crypto; see procedures/n3iwf_non3gpp_access.txt)",
        "ueContexts": len(ue_contexts),
        "registeredUes": len(registered),
        "tunnels": sum(1 for c in ue_contexts.values() if c.get("tunnel")),
    }


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. N3IWF advertises its non-3GPP interworking service so a
    # UE/ePDG-selection function could discover it. Same registration shape as the other NFs.
    profile = {"nfType": "N3IWF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "n3iwf-non3gpp-access",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("n3iwf_ue_contexts_active").set(len(ue_contexts))
    obs.gauge("n3iwf_registered_ues").set(sum(1 for c in ue_contexts.values()
                                              if c.get("registered")))


if __name__ == "__main__":
    obs.init("n3iwf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

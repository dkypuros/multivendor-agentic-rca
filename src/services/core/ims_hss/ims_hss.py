"""
IMS-HSS: the IMS Home Subscriber Server — IMS subscriber data over a Cx-shaped surface.

The IMS-HSS is the master database for the voice (IMS) subsystem: for every subscriber it holds
the private identity (IMPI), the public identities (IMPUs — a sip: AoR and a tel: URI), the
long-term key K, and a service profile (the iFC set the S-CSCF evaluates). It answers the Cx
reference point that the I-CSCF and S-CSCF interrogate during registration and call setup. This
is the IMS analogue of the 5G UDM/UDR (services/core/udm/udm.py) and stays a STANDALONE NF — no
5GC NF is touched.

Spec anchors:
  Cx UAR/UAA  TS 29.228 6.1.1 / TS 29.229 6.1.1 (User-Authorization): I-CSCF asks whether the
              user may register and which S-CSCF serves / should serve it.
  Cx MAR/MAA  TS 29.228 6.3 / TS 29.229 6.3 (Multimedia-Auth): S-CSCF pulls an authentication
              vector to challenge the UE.
  Cx SAR/SAA  TS 29.228 6.1.2 / TS 29.229 6.1.2 (Server-Assignment): S-CSCF registers/clears its
              assignment and pulls the user profile (iFCs).
  Cx LIR/LIA  TS 29.228 6.1.4 / TS 29.229 6.1.4 (Location-Info): I-CSCF finds the S-CSCF serving a
              called party for a terminating request.
  IMS ident.  TS 23.003 13 (IMPI/IMPU/home domain), TS 23.228 5 (IMS registration/session).
  Nnrf_NFMgmt TS 29.510 5.2 (register as an IMS-HSS so the CSCFs discover this Cx surface).

Labeled simplifications (ledgered in procedures/ims_vonr.txt):
  - Cx is carried as JSON over HTTP (adapters/sip_json.py transport note), not Diameter.
  - IMS-AKA is an honest FAKE: the auth vector's XRES is sha256(K||RAND) truncated with
    fake_crypto=true (same construction as the 5G-AKA RES* in the UDM). No Milenage / AUTN MAC.
  - Registration state (assigned S-CSCF) is in-memory session state, seeded fresh at boot.

Run: python3 ims_hss.py   (SBI on 127.0.0.1:7036, registers with the NRF as IMS-HSS)
"""

import secrets
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from adapters.sip_json import (
    DIAMETER_ERROR_IDENTITY_NOT_REGISTERED, DIAMETER_ERROR_USER_UNKNOWN,
    DIAMETER_FIRST_REGISTRATION, DIAMETER_SUBSEQUENT_REGISTRATION, DIAMETER_SUCCESS,
    SAT_REGISTRATION, SAT_RE_REGISTRATION, SAT_UNREGISTERED_USER, SAT_USER_DEREGISTRATION,
    fake_res, home_domain,
)
from domain import obs
from domain.netconfig import port, url

PORT = port("ims_hss")
NRF = url("nrf")
app = SbiApp("ims_hss")


def _seed():
    """Two IMS subscribers, provisioned exactly like a real HSS would hold them: an IMPI, a
    default sip: IMPU and an associated tel: IMPU (the implicit registration set), a key K, and a
    default service profile. The iFC is an illustrative originating INVITE trigger toward a
    telephony AS — recorded, never invoked (no AS in this subsystem)."""
    d = home_domain()
    users = [
        {"name": "alice", "k": "00112233445566778899aabbccddeeff", "tel": "tel:+15551000001"},
        {"name": "bob",   "k": "ffeeddccbbaa99887766554433221100", "tel": "tel:+15551000002"},
    ]
    subs = {}
    for u in users:
        impi = f"{u['name']}@{d}"
        impu = f"sip:{u['name']}@{d}"
        profile = {
            "profileId": uuid.uuid4().hex,
            "publicIdentities": [impu, u["tel"]],
            "initialFilterCriteria": [{
                "priority": 0,
                "triggerPoint": {"conditionType": "originating", "sipMethod": ["INVITE"]},
                "applicationServer": f"sip:telephony-as.{d}",
                "defaultHandling": "SESSION_CONTINUED",
            }],
        }
        subs[impi] = {
            "impi": impi, "k": u["k"],
            "publicIdentities": [impu, u["tel"]], "defaultImpu": impu,
            "serviceProfile": profile,
            "scscfName": None, "regState": "NOT_REGISTERED",
        }
    return subs


subscriptions = _seed()                                   # IMPI -> subscription record
impu_index = {impu: impi for impi, s in subscriptions.items() for impu in s["publicIdentities"]}


def by_impu(impu):
    return subscriptions.get(impu_index.get(impu))


def by_impi(impi):
    return subscriptions.get(impi)


# --------------------------------------------------------- Cx: User-Authorization (I-CSCF -> HSS)
@app.route("POST", "/cx/uar")
def user_authorization(params, query, body):
    """UAR/UAA (TS 29.228 6.1.1): may this IMPU register, and which S-CSCF? On a first
    registration the HSS returns capabilities and lets the I-CSCF select; on a subsequent one it
    returns the already-assigned S-CSCF so the same server keeps the user."""
    impu, impi = body.get("publicIdentity"), body.get("privateIdentity")
    sub = by_impu(impu)
    if sub is None:
        obs.log("cx_uar", impu=impu, result="USER_UNKNOWN")
        return 200, {"resultCode": DIAMETER_ERROR_USER_UNKNOWN}
    obs.counter("ims_hss_uar_total").inc()
    if sub["scscfName"] and sub["regState"] == "REGISTERED":
        return 200, {"resultCode": DIAMETER_SUBSEQUENT_REGISTRATION, "serverName": sub["scscfName"]}
    return 200, {"resultCode": DIAMETER_FIRST_REGISTRATION,
                 "serverCapabilities": {"mandatoryCapabilities": [], "optionalCapabilities": [1]},
                 "serverName": None}


# ---------------------------------------------------------- Cx: Multimedia-Auth (S-CSCF -> HSS)
@app.route("POST", "/cx/mar")
def multimedia_auth(params, query, body):
    """MAR/MAA (TS 29.228 6.3): return an authentication vector for the IMPI. The HSS owns K, so
    it mints RAND and computes the expected XRES = fake_res(K, RAND) (fake_crypto). The S-CSCF
    challenges the UE with RAND and later compares the UE's RES against this XRES."""
    impi, impu = body.get("privateIdentity"), body.get("publicIdentity")
    sub = by_impi(impi) or by_impu(impu)
    if sub is None:
        return 200, {"resultCode": DIAMETER_ERROR_USER_UNKNOWN,
                     "publicIdentity": impu, "privateIdentity": impi}
    rand = secrets.token_hex(16)
    av = {"itemNumber": 0, "authScheme": "Digest-AKAv1-MD5", "fake_crypto": True,
          "rand": rand, "autn": "FAKE-AUTN", "xres": fake_res(sub["k"], rand)}
    obs.log("cx_mar", impi=impi)
    obs.counter("ims_hss_mar_total").inc()
    return 200, {"resultCode": DIAMETER_SUCCESS, "publicIdentity": sub["defaultImpu"],
                 "privateIdentity": sub["impi"], "authVectors": [av]}


# ---------------------------------------------------------- Cx: Server-Assignment (S-CSCF -> HSS)
@app.route("POST", "/cx/sar")
def server_assignment(params, query, body):
    """SAR/SAA (TS 29.228 6.1.2): the S-CSCF records (or clears) its assignment for the user and
    pulls the service profile. On (re-)registration the HSS binds scscfName and returns the iFCs."""
    impu = body.get("publicIdentity")
    sat = body.get("serverAssignmentType")
    sub = by_impu(impu)
    if sub is None:
        return 200, {"resultCode": DIAMETER_ERROR_USER_UNKNOWN}
    if sat in (SAT_REGISTRATION, SAT_RE_REGISTRATION):
        sub["scscfName"], sub["regState"] = body.get("serverName"), "REGISTERED"
    elif sat == SAT_UNREGISTERED_USER:
        sub["scscfName"], sub["regState"] = body.get("serverName"), "UNREGISTERED"
    elif sat == SAT_USER_DEREGISTRATION:
        sub["scscfName"], sub["regState"] = None, "NOT_REGISTERED"
    obs.log("cx_sar", impu=impu, sat=sat, regState=sub["regState"])
    obs.counter("ims_hss_sar_total").inc()
    return 200, {"resultCode": DIAMETER_SUCCESS,
                 "userProfile": {"privateIdentity": sub["impi"],
                                 "publicIdentities": sub["publicIdentities"],
                                 "serviceProfile": sub["serviceProfile"]},
                 "associatedIdentities": sub["publicIdentities"]}


# ---------------------------------------------------------- Cx: Location-Info (I-CSCF -> HSS)
@app.route("POST", "/cx/lir")
def location_info(params, query, body):
    """LIR/LIA (TS 29.228 6.1.4): for a terminating request the I-CSCF asks which S-CSCF serves
    the called party. Unknown IMPU -> USER_UNKNOWN; known but not currently served -> NOT
    REGISTERED (the I-CSCF maps these to SIP 404 / 480)."""
    impu = body.get("publicIdentity")
    sub = by_impu(impu)
    if sub is None:
        obs.log("cx_lir", impu=impu, result="USER_UNKNOWN")
        return 200, {"resultCode": DIAMETER_ERROR_USER_UNKNOWN}
    if not sub["scscfName"]:
        return 200, {"resultCode": DIAMETER_ERROR_IDENTITY_NOT_REGISTERED}
    obs.log("cx_lir", impu=impu, serverName=sub["scscfName"])
    obs.counter("ims_hss_lir_total").inc()
    return 200, {"resultCode": DIAMETER_SUCCESS, "serverName": sub["scscfName"]}


# ------------------------------------------------------------------- management / inspection
@app.route("GET", "/ims-hss/subscriptions")
def list_subscriptions(params, query, body):
    return 200, {"subscriptions": [
        {"impi": s["impi"], "publicIdentities": s["publicIdentities"],
         "regState": s["regState"], "scscfName": s["scscfName"]}
        for s in subscriptions.values()]}


@app.route("GET", "/ims-hss/subscriptions/{impi}")
def get_subscription(params, query, body):
    sub = by_impi(params["impi"])
    if sub is None:
        return problem(404, "Not Found", cause="USER_UNKNOWN")
    return 200, {k: v for k, v in sub.items() if k != "k"}   # never expose K


def register_with_nrf():
    profile = {"nfType": "IMS-HSS", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "cx",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("ims_hss_subscriptions").set(len(subscriptions))
    obs.gauge("ims_hss_registered").set(
        sum(1 for s in subscriptions.values() if s["regState"] == "REGISTERED"))


if __name__ == "__main__":
    obs.init("ims_hss")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

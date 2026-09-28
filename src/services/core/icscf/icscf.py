"""
I-CSCF: the Interrogating Call Session Control Function — the IMS entry query point.

The I-CSCF is the network's entry point for registration and for terminating calls. It holds no
subscriber state; it INTERROGATES the IMS-HSS over Cx to decide where a request goes:
  - on REGISTER it sends a User-Authorization-Request (UAR) to learn / select the S-CSCF, then
    forwards the REGISTER to that S-CSCF;
  - on a terminating INVITE it sends a Location-Info-Request (LIR) to find the S-CSCF serving the
    called party, then forwards the INVITE there.
It is a STANDALONE IMS NF; no 5GC NF is touched.

Spec anchors:
  Cx UAR/UAA   TS 29.228 6.1.1 / TS 29.229 (S-CSCF selection on registration).
  Cx LIR/LIA   TS 29.228 6.1.4 / TS 29.229 (S-CSCF location on a terminating request).
  SIP routing  TS 24.229 5.3 (I-CSCF procedures): forward REGISTER / INVITE to the S-CSCF.
  Nnrf disc.   TS 29.510 5.3 — discover the IMS-HSS (Cx) and the S-CSCF (to forward to).

Labeled simplifications (ledgered in procedures/ims_vonr.txt):
  - Single S-CSCF pool: the UAA server capabilities are honoured trivially; the I-CSCF forwards to
    the one S-CSCF it discovers via the NRF (the Cx serverName is carried but not resolved to a
    distinct host). The location step (LIR) is still real.
  - SIP legs are now real SIP (adapters/sip_real.py, RFC 3261): the I-CSCF prepends a real Via and
    strips it off responses, and flags the terminating leg with a real header. Cx (UAR/LIR) stays
    Diameter-over-JSON (adapters/sip_json.py transport note), not real Diameter/Dx — out of scope here.

Run: python3 icscf.py   (SBI on 127.0.0.1:7034, registers with the NRF as I-CSCF)
"""

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, request
from adapters.sip_json import (
    DIAMETER_ERROR_IDENTITY_NOT_REGISTERED, DIAMETER_ERROR_USER_UNKNOWN,
    DIAMETER_FIRST_REGISTRATION, DIAMETER_SUBSEQUENT_REGISTRATION, DIAMETER_SUCCESS,
    home_domain,
)
from adapters import sip_real as sip
from domain import obs
from domain.netconfig import port, url

PORT = port("icscf")
NRF = url("nrf")
app = SbiApp("icscf")
ICSCF_HOST = f"icscf.{home_domain()}"


def discover(nf_type):
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type=ICSCF")
    except OSError:
        return None
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        return None
    ep = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{ep['ipv4Address']}:{ep['port']}"


def cx(path, body):
    hss = discover("IMS-HSS")
    if hss is None:
        return {}
    try:
        _s, ans = request("POST", f"{hss}{path}", body)
        return ans
    except OSError:
        return {}


def impi_of(req):
    digest = sip.parse_digest(req.get("Authorization"))
    if digest.get("username"):
        return digest["username"]
    impu = sip.header_uri(req.get("To")) or sip.header_uri(req.get("From")) or ""
    return impu[len("sip:"):] if impu.startswith("sip:") else impu


def _err(req, status, reason, cause):
    return [sip.with_reason(sip.make_response(req, status, reason), cause)]


def handle_register(req):
    """REGISTER: UAR to the HSS to select/locate the S-CSCF, then forward the REGISTER on."""
    impu = sip.header_uri(req.get("To")) or sip.header_uri(req.get("From"))
    uaa = cx("/cx/uar", {"publicIdentity": impu, "privateIdentity": impi_of(req),
                        "visitedNetwork": req.get("P-Visited-Network-ID") or ""})
    rc = uaa.get("resultCode")
    if rc == DIAMETER_ERROR_USER_UNKNOWN:
        obs.log("register_user_unknown", impu=impu)
        return _err(req, 404, "Not Found", "USER_UNKNOWN")
    if rc not in (DIAMETER_FIRST_REGISTRATION, DIAMETER_SUBSEQUENT_REGISTRATION):
        return _err(req, 500, "Server Internal Error", "S_CSCF_SELECTION_FAILED")
    scscf = discover("S-CSCF")
    if scscf is None:
        return _err(req, 500, "Server Internal Error", "NO_SCSCF")
    obs.log("register_forwarded", impu=impu, serverName=uaa.get("serverName"))
    obs.counter("icscf_uar_total").inc()
    try:
        return sip.relay_forward(req, scscf, ICSCF_HOST)
    except OSError:
        return _err(req, 500, "Server Internal Error", "SCSCF_UNREACHABLE")


def handle_invite(req):
    """Terminating INVITE: LIR to the HSS to find the serving S-CSCF, then forward the terminating
    leg to it. Unknown callee -> 404; known but not registered -> 480."""
    callee = sip.header_uri(req.get("To"))
    lia = cx("/cx/lir", {"publicIdentity": callee, "originatingRequest": False})
    rc = lia.get("resultCode")
    if rc == DIAMETER_ERROR_USER_UNKNOWN:
        obs.log("invite_user_unknown", callee=callee)
        return _err(req, 404, "Not Found", "USER_UNKNOWN")
    if rc == DIAMETER_ERROR_IDENTITY_NOT_REGISTERED:
        return _err(req, 480, "Temporarily Unavailable", "NOT_REGISTERED")
    if rc != DIAMETER_SUCCESS:
        return _err(req, 500, "Server Internal Error", "LOCATION_FAILED")
    scscf = discover("S-CSCF")
    if scscf is None:
        return _err(req, 500, "Server Internal Error", "NO_SCSCF")
    obs.log("invite_forwarded", callee=callee, serverName=lia.get("serverName"))
    obs.counter("icscf_lir_total").inc()
    term = req.copy()
    term.set("P-Leg-Hint", "terminating")   # tells the S-CSCF this is the terminating leg (fork to UE)
    try:
        return sip.relay_forward(term, scscf, ICSCF_HOST)
    except OSError:
        return _err(req, 500, "Server Internal Error", "SCSCF_UNREACHABLE")


def on_sip(req):
    method = (req.method or "").upper()
    if method == "REGISTER":
        return handle_register(req)
    if method == "INVITE":
        return handle_invite(req)
    return [sip.make_response(req, 405, "Method Not Allowed")]


def register_with_nrf():
    profile = {"nfType": "I-CSCF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "sip",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


if __name__ == "__main__":
    obs.init("icscf")
    register_with_nrf()
    sip.serve(app, PORT, on_sip)

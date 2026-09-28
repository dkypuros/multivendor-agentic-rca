"""
P-CSCF: the Proxy Call Session Control Function — the UE's entry point into IMS.

The P-CSCF is the first SIP hop for a UE: every REGISTER and every INVITE from the UE enters IMS
here. It is a stateful proxy — it adds itself to the Path so responses and later terminating
requests route back through it, it stores the registration binding on a successful 200, and it
asserts the caller's identity (P-Asserted-Identity) on originating requests before forwarding them
to the S-CSCF along the Service-Route the S-CSCF handed out at registration. It is a STANDALONE
IMS NF; no 5GC NF is touched.

Spec anchors:
  SIP REGISTER proxy   TS 24.229 5.2.2 (P-CSCF registration): add Path, forward to the I-CSCF,
                       absorb the 200 OK, store the contact + Service-Route.
  SIP originating      TS 24.229 5.2.6 (P-CSCF session flows): enforce registration, assert
                       identity, forward INVITE/ACK/BYE to the S-CSCF.
  Nnrf discovery       TS 29.510 5.3 — discover the I-CSCF (registration) and S-CSCF (origination).
  Dedicated bearer     TS 23.228 / TS 23.501 5.7.4 (5QI=1 voice): the P-CSCF is the IMS Application
                       Function (AF) on the Rx/N5 leg — it OWNS the media/bearer coupling. When a
                       VoNR call is answered it requests a dedicated GBR voice bearer from the PCF's
                       Npcf_PolicyAuthorization (TS 29.514) and releases it on BYE. See below.

DEDICATED VOICE BEARER (Rx/N5 -> PCF Npcf_PolicyAuthorization, TS 29.514 — procedures/vonr_dedicated_bearer.txt)
  The P-CSCF is the correct owner of this coupling: it sits on the media/signalling border as the
  IMS AF, so the Rx/N5 leg to the PCF lives HERE, not on the S-CSCF (which owns pure session
  routing). When an originating INVITE is answered 200 OK with a VoNR SDP answer (5QI=1 + a media
  GBR — the GBR is authored by the S-CSCF from the negotiated codec, see scscf.py), the P-CSCF POSTs
  an AppSessionContext to the PCF (des5qi=1 + gbrUl/gbrDl bound to the media flow), the PCF DERIVES
  and INSTALLS a dynamic GBR PCC rule, and the P-CSCF holds the returned appSessionId keyed by the
  Call-ID. On BYE it DELETEs the app session, releasing the bearer.

  CONTRACT (make-or-break): GATED on a PolicyAuthorization-capable PCF being discoverable via the
  NRF, and BEST-EFFORT. With NO such PCF (or any PCF fault) the P-CSCF behaves EXACTLY as before —
  the 5QI=1 bearer is NOTED, no bearer installed — so run_ims_spec / run_ims_mrf_spec /
  run_golden_scenario_spec pass UNEDITED. Metric ims_dedicated_bearer_total{installed|noted_fallback}.

Labeled simplifications (ledgered in procedures/vonr_dedicated_bearer.txt):
  - Real SIP/SDP on the wire now (adapters/sip_real.py, RFC 3261/4566): the P-CSCF prepends a real
    Via (branch), a Record-Route on INVITE and a Path on REGISTER, and strips its Via off responses.
    Still procedure-level, not bit-level: no IPSec SA, no NAT/Via rewriting, and no RTP is carried.
  - No IMPU->IMSI (SUPI) mapping in this subsystem: the caller IMPU is used as the app-session
    install key (a real P-CSCF/PCRF resolves the subscriber's SUPI). No real RTP flows, so the
    Rx service-data-flow filters are procedure-level (permit the audio media both ways).

Run: python3 pcscf.py   (SBI on 127.0.0.1:7033, registers with the NRF as P-CSCF)
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, request
from adapters.sip_json import home_domain
from adapters import sip_real as sip
from domain import obs
from domain.netconfig import port, url

PORT = port("pcscf")
NRF = url("nrf")
app = SbiApp("pcscf")
PCSCF_HOST = f"pcscf.{home_domain()}"
PCSCF_URI = f"sip:{PCSCF_HOST}"

contacts = {}   # AoR (IMPU) -> {contact, serviceRoute, state}
bearers = {}    # Call-ID -> {pcf, appSessionId, 5qi} — the dedicated voice bearers this AF installed

DEDICATED_5QI = 1   # TS 23.501 5.7.4 table 5.7.4-1: 5QI 1 = conversational voice (GBR)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------- dedicated voice bearer (Rx/N5 -> PCF, TS 29.514)
def discover_policyauth_pcf():
    """Discover a PCF that advertises Npcf_PolicyAuthorization (TS 29.510 5.3, target-nf-type=PCF,
    serviceName npcf-policyauthorization). Returns its base URL or None. THIS IS THE GATE: a plain
    PCF (only npcf-smpolicycontrol) or no PCF at all yields None, and the P-CSCF falls back to
    NOTING the 5QI=1 bearer — byte-identical to before this integration."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                       "?target-nf-type=PCF&requester-nf-type=PCSCF")
    except OSError:
        return None
    if status != 200:
        return None
    for inst in body.get("nfInstances", []):
        for svc in inst.get("nfServices", []):
            if svc.get("serviceName") != "npcf-policyauthorization":
                continue
            ep = (svc.get("ipEndPoints") or [{}])[0]
            if ep.get("ipv4Address") and ep.get("port"):
                return f"http://{ep['ipv4Address']}:{ep['port']}"
    return None


def install_dedicated_bearer(call_id, caller, callee, sdp):
    """Npcf_PolicyAuthorization_Create (TS 29.514 5.2.2.2): the IMS AF asks the PCF to install a
    dedicated GBR voice bearer for the answered media — des5qi=1 + the media GBR the S-CSCF authored
    into the SDP answer, bound to the media flow's service-data-flow filters. BEST-EFFORT: any PCF
    fault degrades to the noted fallback and NEVER changes the SIP answer already returned upstream."""
    gbr = (sdp or {}).get("mediaGbr") or {}
    pcf = discover_policyauth_pcf()
    if pcf is None:
        obs.log("dedicated_bearer_noted", callId=call_id, reason="no_policyauth_pcf",
                fiveqi=DEDICATED_5QI, note="Rx/N5 to PCF unavailable — 5QI=1 noted, not installed")
        obs.counter("ims_dedicated_bearer_total", outcome="noted_fallback").inc()
        return
    asc = {"ascReqData": {
        # supi = the caller IMPU (labeled: no IMPU->SUPI map here); dnn 'ims' scopes the install.
        "supi": caller, "dnn": "ims", "afAppId": "pcscf-vonr",
        "notifUri": f"{PCSCF_URI}/rx-notify",           # AF sink (recorded by the PCF, not called)
        "mediaComponents": [{
            "medCompN": 1, "fiveqi": DEDICATED_5QI,      # des5qi=1 (TS 29.514 MediaComponent)
            "gbrUl": gbr.get("ul"), "gbrDl": gbr.get("dl"),
            # procedure-level SDF filters for the audio media (no real RTP 5-tuple in this model)
            "fDescs": ["permit out 17 from any to any", "permit in 17 from any to any"]}]}}
    try:
        status, resp = request("POST", f"{pcf}/npcf-policyauthorization/v1/app-sessions", asc)
    except OSError:
        obs.log("dedicated_bearer_noted", callId=call_id, reason="pcf_unreachable",
                fiveqi=DEDICATED_5QI)
        obs.counter("ims_dedicated_bearer_total", outcome="noted_fallback").inc()
        return
    if status != 201:
        obs.log("dedicated_bearer_noted", callId=call_id, reason=f"pcf_status_{status}",
                fiveqi=DEDICATED_5QI)
        obs.counter("ims_dedicated_bearer_total", outcome="noted_fallback").inc()
        return
    app_session_id = resp.get("appSessionId")
    bearers[call_id] = {"pcf": pcf, "appSessionId": app_session_id, "5qi": DEDICATED_5QI}
    obs.log("dedicated_bearer_installed", callId=call_id, appSessionId=app_session_id,
            fiveqi=DEDICATED_5QI, gbrUl=gbr.get("ul"), gbrDl=gbr.get("dl"))
    obs.counter("ims_dedicated_bearer_total", outcome="installed").inc()


def release_dedicated_bearer(call_id):
    """Npcf_PolicyAuthorization_Delete (TS 29.514 5.2.4) on BYE: revoke the app session so the PCF
    removes the derived GBR PCC rule. ADDITIVE: a call with no installed bearer is a no-op."""
    b = bearers.pop(call_id, None)
    if not b:
        return
    try:
        request("DELETE", f"{b['pcf']}/npcf-policyauthorization/v1/app-sessions/{b['appSessionId']}")
    except OSError:
        obs.log("dedicated_bearer_release_skip", callId=call_id, reason="pcf_unreachable")
        return
    obs.log("dedicated_bearer_released", callId=call_id, appSessionId=b["appSessionId"])
    obs.counter("ims_dedicated_bearer_released_total").inc()


def discover(nf_type):
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type=PCSCF")
    except OSError:
        return None
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        return None
    ep = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{ep['ipv4Address']}:{ep['port']}"


def handle_register(req):
    """Add a real Path header, forward to the I-CSCF, and on 200 OK store the binding + Service-Route."""
    icscf = discover("I-CSCF")
    if icscf is None:
        return [sip.with_reason(sip.make_response(req, 500, "Server Internal Error"), "NO_ICSCF")]
    fwd = req.copy()
    fwd.add("Path", f"<{PCSCF_URI};lr>")                  # RFC 3327 Path: terminating requests route back
    try:
        responses = sip.relay_forward(fwd, icscf, PCSCF_HOST)
    except OSError:
        return [sip.with_reason(sip.make_response(req, 500, "Server Internal Error"),
                                "ICSCF_UNREACHABLE")]
    final = sip.final(responses)
    if final is not None and final.status == 200:
        aor = sip.header_uri(req.get("To")) or sip.header_uri(req.get("From"))
        contacts[aor] = {"contact": req.get("Contact") or req.get("From"),
                         "serviceRoute": final.get_all("Service-Route"),
                         "state": "REGISTERED", "registeredAt": now_iso()}
        obs.log("registration_stored", aor=aor)
        obs.counter("pcscf_registrations_total").inc()
    return responses


def registered(aor):
    c = contacts.get(aor)
    return c if c and c.get("state") == "REGISTERED" else None


def handle_originating(req):
    """Enforce registration, assert identity, note the dedicated bearer, forward to the S-CSCF."""
    caller = sip.header_uri(req.get("From"))
    if registered(caller) is None:
        return [sip.with_reason(sip.make_response(req, 403, "Forbidden"), "NOT_REGISTERED")]
    scscf = discover("S-CSCF")
    if scscf is None:
        return [sip.with_reason(sip.make_response(req, 500, "Server Internal Error"), "NO_SCSCF")]
    method = (req.method or "").upper()
    fwd = req.copy()
    fwd.set("P-Asserted-Identity", f"<{caller}>")         # assert the identity authenticated at register
    record_route = PCSCF_URI if method == "INVITE" else None
    if method == "INVITE":
        obs.counter("pcscf_invites_total").inc()
    try:
        responses = sip.relay_forward(fwd, scscf, PCSCF_HOST, record_route=record_route)
    except OSError:
        return [sip.with_reason(sip.make_response(req, 500, "Server Internal Error"),
                                "SCSCF_UNREACHABLE")]
    # Rx/N5 dedicated bearer (TS 29.514): the P-CSCF is the IMS AF. On a VoNR answer (5QI=1 + a media
    # GBR the S-CSCF authored into the real SDP), install the bearer; on BYE, release it. GATED on a
    # PolicyAuthorization PCF and BEST-EFFORT, so with no such PCF this is a pure counter++ and the
    # SIP answer already relayed upstream is unchanged.
    final = sip.final(responses)
    if method == "INVITE" and final is not None and final.status == 200:
        sdp = final.sdp()
        if sdp.get("5qi") == DEDICATED_5QI and sdp.get("mediaGbr") and not sdp.get("conference"):
            install_dedicated_bearer(req.get("Call-ID"), caller, sip.header_uri(req.get("To")), sdp)
    elif method == "BYE":
        release_dedicated_bearer(req.get("Call-ID"))
    return responses


def on_sip(req):
    """Real SIP entry (adapters/sip_real.serve): req is a parsed SipMessage, return a list of
    SipMessage responses (1xx provisionals then the final)."""
    method = (req.method or "").upper()
    if method == "REGISTER":
        return handle_register(req)
    if method in ("INVITE", "ACK", "BYE"):
        return handle_originating(req)
    return [sip.make_response(req, 405, "Method Not Allowed")]


@app.route("GET", "/pcscf/contacts")
def list_contacts(params, query, body):
    return 200, {"contacts": [{"aor": k, "state": v.get("state")} for k, v in contacts.items()]}


@app.route("GET", "/pcscf/bearers")
def list_bearers(params, query, body):
    # The dedicated 5QI=1 voice bearers this AF currently holds at the PCF (spec / viewer proof).
    return 200, {"bearers": [{"callId": k, "appSessionId": v.get("appSessionId"), "5qi": v.get("5qi")}
                             for k, v in bearers.items()]}


def register_with_nrf():
    profile = {"nfType": "P-CSCF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "sip",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("pcscf_contacts").set(sum(1 for c in contacts.values() if c.get("state") == "REGISTERED"))
    obs.gauge("pcscf_dedicated_bearers").set(len(bearers))


if __name__ == "__main__":
    obs.init("pcscf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    sip.serve(app, PORT, on_sip)

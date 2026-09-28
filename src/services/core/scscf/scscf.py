"""
S-CSCF: the Serving Call Session Control Function — IMS registrar and call control.

The S-CSCF is the heart of the IMS voice subsystem. It is the SIP registrar: it authenticates the
UE (IMS-AKA over Cx MAR), stores the registration binding (which contact/Path reaches the UE), and
records its assignment in the HSS (Cx SAR). It is also the session router: it applies the user's
iFCs and routes INVITEs — originating requests toward the terminating I-CSCF, terminating requests
to the registered callee. It is a STANDALONE IMS NF; no 5GC NF is touched.

Spec anchors:
  SIP REGISTER + IMS-AKA   TS 24.229 5.4.1 (S-CSCF registration), TS 33.203 (IMS-AKA): first
                           REGISTER -> 401 challenge (nonce = RAND from the HSS AV); second
                           REGISTER with the response -> verified against XRES -> 200 OK.
  SIP session control      TS 24.229 5.4.3 (S-CSCF session flows): INVITE / 100 / 180 / 200 / ACK,
                           BYE / 200. TS 23.228 5.4a-5.7 (IMS registration & session procedures).
  Cx MAR / SAR             TS 29.228 6.3 / 6.1.2 — auth vector + server assignment / user profile.
  Nnrf discovery           TS 29.510 5.3 — discover the IMS-HSS (Cx) and the I-CSCF (terminating).
  SDP / dedicated bearer   RFC 4566 offer/answer; the voice media is 5QI=1 (TS 23.501 5.7.4). The
                           S-CSCF (session control) authors the media GBR from the negotiated codec
                           into the SDP answer (RFC 4566 b=AS bandwidth, TS 26.114 codec bit rates)
                           so the P-CSCF — the IMS AF on the Rx/N5 leg — can request a real dedicated
                           GBR bearer from the PCF (see pcscf.py / procedures/vonr_dedicated_bearer.txt).
                           No RTP is carried here; the bearer install lives on the P-CSCF, best-effort.
  MRF in-call media        TS 24.147 (conference factory join) + TS 23.228 4.7 (media resource) +
                           TS 23.218 7 (announcement): when the callee is the conference-factory
                           URI (sip:conf@<home>) the S-CSCF, acting as the conference AS, discovers
                           the MRF (Nnrf) and drives its media-resource surface — allocate a
                           conference media session, admit each caller as a participant, release on
                           the last BYE. GATED on MRF discovery; ADDITIVE with EXACT fallback (see
                           the labeled note below and procedures/ims_mrf_integration.txt).

Labeled simplifications (ledgered in procedures/ims_vonr.txt):
  - IMS-AKA verification is the honest FAKE: the UE's RES must equal the HSS XRES = sha256(K||RAND)
    truncated (fake_crypto). No Milenage, no IPSec SA, no integrity protection.
  - iFCs are evaluated (originating INVITE trigger recognised) but no Application Server is invoked
    (there is none in this subsystem) — the trigger is logged, the request continues.
  - The terminating UE's provisional/final responses (100/180/200) are SYNTHESISED here from the
    stored binding: the callee is a registered contact, not a live listening socket. The dedicated
    voice bearer (5QI=1) is noted, never programmed (Rx to the PCF is a follow-up).
  - Single S-CSCF: originating and terminating legs are served by this same instance; the leg is
    still routed out through the I-CSCF so the Cx LIR location step is real.
  - MRF media path is taken ONLY for the dedicated media URIs (conference sip:conf@<home>,
    announcement sip:announce@<home>). Every other INVITE — the normal 2-party VoNR call — routes
    to the I-CSCF byte-for-byte as before. With the MRF ABSENT (undiscoverable/unreachable) the
    conference INVITE degrades HONESTLY (488) and normal calls are entirely unaffected: the MRF
    coupling is behaviour-neutral for the whole rest of the subsystem. The conference leg is FULLY
    WIRED; the announcement leg is a LABELED FOLLOW-UP stub (501) — real vs. stub is ledgered in
    procedures/ims_mrf_integration.txt.

Run: python3 scscf.py   (SBI on 127.0.0.1:7035, registers with the NRF as S-CSCF)
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, request
from adapters.sip_json import (
    DIAMETER_SUCCESS, SAT_REGISTRATION, SAT_RE_REGISTRATION, SAT_USER_DEREGISTRATION,
    home_domain, realm, scscf_uri,
)
from adapters import sip_real as sip
from domain import obs
from domain.netconfig import port, url

PORT = port("scscf")
NRF = url("nrf")
app = SbiApp("scscf")
SCSCF_HOST = f"scscf.{home_domain()}"

# --- MRF in-call media integration (TS 24.147 / TS 23.228 4.7 / TS 23.218 7) ----------------------
# The S-CSCF acts as the conference AS: a callee whose URI user part is CONFERENCE_USER is the
# conference-factory URI (sip:conf@<home>); the S-CSCF drives the MRF's media-resource surface for
# it. ANNOUNCE_USER (sip:announce@<home>) is the labeled announcement follow-up stub. Every other
# request-URI is untouched, so the normal call flow is byte-identical whether or not the MRF exists.
MRF_BASE_PATH = "/mrf/v1/media-sessions"
CONFERENCE_USER = "conf"
ANNOUNCE_USER = "announce"
# UE offers a bare codec token (e.g. "AMR-WB"); the MRF wants the RFC 3551/TS 26.114 label. Unknown
# codecs fall back to a supported default so the MRF never rejects the allocation with a 400.
_MRF_CODECS = {"AMR-WB": "AMR-WB/16000", "AMR": "AMR/8000", "PCMU": "PCMU/8000",
               "PCMA": "PCMA/8000", "G729": "G729/8000"}
_MRF_DEFAULT_CODEC = "AMR-WB/16000"

# Media GBR per codec for the VoNR SDP answer (TS 26.114 typical VoLTE/VoNR audio bit rates, incl.
# IP/UDP/RTP overhead — modeled as a round "b=AS" figure). This is the number the P-CSCF hands the
# PCF as the des5qi=1 flow's guaranteed bit rate. Unknown codecs fall back to the AMR-WB default.
_VONR_MEDIA_GBR = {"AMR-WB": "24 Kbps", "AMR": "12 Kbps", "PCMU": "64 Kbps",
                   "PCMA": "64 Kbps", "G729": "8 Kbps"}
_VONR_DEFAULT_GBR = "24 Kbps"


def media_gbr(codec):
    """The dedicated-bearer GBR {ul, dl} for a VoNR audio codec (symmetric for conversational voice)."""
    rate = _VONR_MEDIA_GBR.get(codec, _VONR_DEFAULT_GBR)
    return {"ul": rate, "dl": rate}

# Registrar state (Kamailio ims_usrloc_scscf shape, kept in-memory — registration is session
# state, seeded empty at boot exactly like the 5G AMF UE contexts).
bindings = {}        # IMPU -> {impi, contact, path[], serviceProfile, pendingXres, state}
dialogs = {}         # callId -> {caller, callee, state, [conference: mediaSessionId]}
conferences = {}     # conference AoR (sip:conf@<home>) -> {mediaSessionId, base, resourceUri, callers}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def discover(nf_type):
    """NRF discovery (TS 29.510 5.3), same shape as services/core/nef/nef.py: a base URL or None."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type=SCSCF")
    except OSError:
        return None
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        return None
    ep = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{ep['ipv4Address']}:{ep['port']}"


def cx(path, body):
    """One Cx round-trip to the IMS-HSS (JSON over HTTP). Returns the answer dict or {} if the
    HSS is unreachable/undiscoverable — the caller turns {} into a SIP 5xx."""
    hss = discover("IMS-HSS")
    if hss is None:
        return {}
    try:
        _s, ans = request("POST", f"{hss}{path}", body)
        return ans
    except OSError:
        return {}


def impi_of(req):
    """Private identity: from the Authorization username when present, else derived from the IMPU
    (a labeled simplification — real UEs always send the IMPI in Authorization)."""
    digest = sip.parse_digest(req.get("Authorization"))
    if digest.get("username"):
        return digest["username"]
    impu = sip.header_uri(req.get("To")) or sip.header_uri(req.get("From")) or ""
    return impu[len("sip:"):] if impu.startswith("sip:") else impu


def _err(req, status, reason, cause):
    return [sip.with_reason(sip.make_response(req, status, reason, to_tag=sip.new_tag()), cause)]


# --------------------------------------------------------------------- REGISTER (registrar leg)
def handle_register(req):
    impu = sip.header_uri(req.get("To")) or sip.header_uri(req.get("From"))
    impi = impi_of(req)
    auth = sip.parse_digest(req.get("Authorization"))

    if not auth:
        # First REGISTER: pull an auth vector (Cx MAR) and challenge the UE with 401 (TS 33.203).
        maa = cx("/cx/mar", {"publicIdentity": impu, "privateIdentity": impi,
                             "authScheme": "Digest-AKAv1-MD5"})
        vectors = maa.get("authVectors") if maa.get("resultCode") == DIAMETER_SUCCESS else None
        if not vectors:
            return _err(req, 403, "Forbidden", "AUTH_VECTOR_UNAVAILABLE")
        av = vectors[0]
        b = bindings.get(impu, {"impu": impu})
        b.update({"impi": impi, "pendingXres": av["xres"], "state": "AUTH_PENDING"})
        bindings[impu] = b
        obs.log("register_challenge", impu=impu, impi=impi)
        obs.counter("scscf_register_challenges_total").inc()
        # A real WWW-Authenticate header: nonce carries RAND so the UE computes RES = fake_res(K, RAND);
        # algorithm AKAv1-MD5 signals IMS-AKA (the RES/XRES is the honest FAKE, ledgered).
        challenge = sip.make_response(req, 401, "Unauthorized", to_tag=sip.new_tag())
        challenge.add("WWW-Authenticate",
                      sip.build_digest(realm=realm(), nonce=av["rand"], algorithm="AKAv1-MD5"))
        return [challenge]

    # Second REGISTER: verify the UE response against the XRES the HSS gave us.
    b = bindings.get(impu)
    if not b or "pendingXres" not in b:
        stale = sip.make_response(req, 401, "Unauthorized", to_tag=sip.new_tag())
        stale.add("WWW-Authenticate", sip.build_digest(realm=realm(), stale="true"))
        return [sip.with_reason(stale, "STALE_OR_UNKNOWN_CHALLENGE")]
    if auth.get("response") != b["pendingXres"]:
        obs.log("register_auth_failure", impu=impu)
        obs.counter("scscf_register_failures_total").inc()
        return _err(req, 403, "Forbidden", "AUTHENTICATION_FAILURE")

    # Authenticated: record the assignment (Cx SAR) and pull the service profile / iFCs.
    reregistration = b.get("state") == "REGISTERED"
    saa = cx("/cx/sar", {"publicIdentity": impu, "privateIdentity": impi,
                        "serverName": scscf_uri(),
                        "serverAssignmentType": SAT_RE_REGISTRATION if reregistration
                        else SAT_REGISTRATION})
    if saa.get("resultCode") != DIAMETER_SUCCESS:
        return _err(req, 500, "Server Internal Error", "SAR_FAILED")
    profile = saa.get("userProfile", {})
    b.update({"contact": req.get("Contact") or req.get("From"),
              "path": req.get_all("Path"), "serviceProfile": profile.get("serviceProfile"),
              "publicIdentities": profile.get("publicIdentities", [impu]),
              "state": "REGISTERED", "registeredAt": now_iso()})
    b.pop("pendingXres", None)
    bindings[impu] = b
    obs.log("registered", impu=impu, impi=impi, reregistration=reregistration)
    obs.counter("scscf_registrations_total").inc()
    ok = sip.make_response(req, 200, "OK", to_tag=sip.new_tag())
    ok.set("Contact", b["contact"])
    ok.set("Expires", req.get("Expires") or 600000)
    ok.add("Service-Route", f"<{scscf_uri()};lr;orig>")
    for u in b["publicIdentities"]:
        ok.add("P-Associated-URI", f"<{u}>")
    return [ok]


# ------------------------------------------------------------------------- INVITE (call control)
def registered_binding(impu):
    b = bindings.get(impu)
    return b if b and b.get("state") == "REGISTERED" else None


def _ringing_chain(req, ok):
    """The real 100 Trying + 180 Ringing provisional messages the callee UA would emit before its
    final response, pipelined ahead of `ok` on the same carrier (RFC 3261 provisional responses).
    The 180 shares the final's To-tag (same early dialog)."""
    to_tag = sip.header_param(ok.get("To"), "tag")
    trying = sip.make_response(req, 100, "Trying")
    ringing = sip.make_response(req, 180, "Ringing", to_tag=to_tag)
    return [trying, ringing, ok]


def synth_terminating_answer(req, callee):
    """The terminating leg: the callee is a REGISTERED contact. Real IMS forwards to the UE and
    relays its 100/180/200; here we synthesise that chain from the binding (labeled). The 200 OK
    carries a real SDP answer with the 5QI=1 dedicated bearer + media GBR NOTED (not programmed)."""
    caller = sip.header_uri(req.get("From"))
    callee_uri = sip.header_uri(req.get("To"))
    codec = req.sdp().get("codec") or "AMR-WB"
    # The SDP answer carries the media GBR (RFC 4566 b=AS + 3GPP attrs) so the P-CSCF (IMS AF) can
    # request a real dedicated 5QI=1 GBR bearer from the PCF over Rx/N5. Additive: 5qi=1 is unchanged;
    # whether a bearer is actually installed is the P-CSCF's gated, best-effort call (no RTP here).
    answer_sdp = sip.build_sdp(codec=codec, five_qi=1, media_gbr=media_gbr(codec))
    dialogs[req.get("Call-ID")] = {"caller": caller, "callee": callee_uri,
                                   "state": "CONFIRMED", "startedAt": now_iso()}
    obs.log("call_answered", callId=req.get("Call-ID"), caller=caller, callee=callee_uri)
    obs.counter("scscf_calls_answered_total").inc()
    ok = sip.make_response(req, 200, "OK", to_tag=sip.new_tag())
    ok.set("Contact", callee["contact"])
    ok.set("P-Asserted-Identity", req.get("To"))
    ok.set_sdp(answer_sdp)
    return _ringing_chain(req, ok)


# ------------------------------------------------------- MRF in-call media (conference / announce)
def _uri_user(uri):
    """The user part of a SIP URI (sip:conf@ims -> 'conf'); '' if not a sip: URI. Params/domain
    stripped so sip:conf@ims and sip:conf@<home>;lr both resolve to the same user token."""
    if not uri or not uri.startswith("sip:"):
        return ""
    return uri[len("sip:"):].split("@", 1)[0].split(";", 1)[0].lower()


def is_conference_uri(uri):
    """True for the IMS conference-factory URI (TS 24.147, user part 'conf'). This is the ONLY
    request the MRF conference path is taken for; every other INVITE routes exactly as before."""
    return _uri_user(uri) == CONFERENCE_USER


def is_announcement_uri(uri):
    """True for the announcement URI (user part 'announce') — the labeled follow-up stub."""
    return _uri_user(uri) == ANNOUNCE_USER


def _mrf(method, base, path, body=None):
    """One MRF media-resource control round-trip (JSON over HTTP). Returns (status, dict); a dead
    MRF yields (None, {}) so the caller degrades HONESTLY instead of raising — the whole point of
    gating on discovery is that the S-CSCF never fakes a bridge it could not reach."""
    try:
        return request(method, base.rstrip("/") + path, body)
    except OSError:
        return None, {}


def handle_conference_invite(req, caller):
    """MRF-backed conferencing (TS 24.147 conference factory + TS 23.228 4.7 media resource control).
    GATED on MRF discovery: with the MRF undiscoverable/unreachable the INVITE degrades HONESTLY to
    488 (no faked bridge), and — because this branch is entered only for the conference URI — the
    rest of the subsystem is untouched. Otherwise the S-CSCF, acting as the conference AS, allocates
    an MRF conference media session for the factory URI ONCE and admits each caller as a participant,
    then answers 200 OK with the MRFP anchor as the SDP media endpoint (MODELLED — no RTP)."""
    conf_uri = sip.header_uri(req.get("To"))
    call_id = req.get("Call-ID")
    mrf = discover("MRF")
    if mrf is None:
        obs.log("conference_mrf_unavailable", conference=conf_uri, cause="MRF_UNDISCOVERABLE")
        obs.counter("scscf_mrf_unavailable_total").inc()
        return _err(req, 488, "Not Acceptable Here", "MRF_UNAVAILABLE")

    conf = conferences.get(conf_uri)
    if conf is None:
        # Allocate the conference bridge once, on the first INVITE to the factory URI (TS 24.147).
        status, session = _mrf("POST", mrf, MRF_BASE_PATH,
                               {"resourceType": "conference", "name": _uri_user(conf_uri),
                                "codec": _MRF_CODECS.get(req.sdp().get("codec"),
                                                         _MRF_DEFAULT_CODEC)})
        if status != 201:
            obs.log("conference_alloc_failed", conference=conf_uri, mrfStatus=status)
            obs.counter("scscf_mrf_unavailable_total").inc()
            return _err(req, 503, "Service Unavailable", "MRF_ALLOC_FAILED")
        conf = {"mediaSessionId": session["mediaSessionId"], "base": mrf,
                "resourceUri": session.get("resourceUri"), "callers": {}}
        conferences[conf_uri] = conf
        obs.log("conference_allocated", conference=conf_uri, mediaSessionId=conf["mediaSessionId"])
        obs.counter("scscf_mrf_sessions_total").inc()

    # Admit the caller as a participant on the MRFP (a MODELLED media endpoint per party, TS 24.147).
    status, joined = _mrf("POST", mrf, f"{MRF_BASE_PATH}/{conf['mediaSessionId']}/participants",
                          {"uri": caller, "displayName": _uri_user(caller)})
    if status != 201:
        obs.log("conference_join_failed", conference=conf_uri, uri=caller, mrfStatus=status)
        obs.counter("scscf_mrf_unavailable_total").inc()
        return _err(req, 503, "Service Unavailable", "MRF_PARTICIPANT_REJECTED")
    participant = joined.get("participant", {})
    conf["callers"][call_id] = participant.get("participantId")
    obs.counter("scscf_mrf_participants_total").inc()

    # Dialog recorded so ACK/BYE work exactly like a 2-party call; the last BYE tears the bridge down.
    dialogs[call_id] = {"caller": caller, "callee": conf_uri, "state": "CONFIRMED",
                        "conference": conf["mediaSessionId"], "startedAt": now_iso()}
    obs.log("conference_joined", conference=conf_uri, caller=caller,
            mediaSessionId=conf["mediaSessionId"], participantCount=joined.get("participantCount"))
    obs.counter("scscf_calls_answered_total").inc()

    # Real SDP answer: the conference marker + MRFP anchor + session/participant ids ride as 3GPP
    # SDP attribute lines (MODELLED media endpoint, no RTP), so the UA reads them off genuine SDP.
    answer_sdp = sip.build_sdp(
        codec=req.sdp().get("codec") or "AMR-WB", five_qi=1,
        conference={"mediaSessionId": conf["mediaSessionId"],
                    "mrfEndpoint": participant.get("mediaEndpoint"),
                    "participantId": participant.get("participantId"),
                    "participantCount": joined.get("participantCount")})
    ok = sip.make_response(req, 200, "OK", to_tag=sip.new_tag())
    ok.set("Contact", conf.get("resourceUri") or conf_uri)
    ok.set("P-Asserted-Identity", req.get("To"))
    ok.set_sdp(answer_sdp)
    return _ringing_chain(req, ok)


def handle_announcement_invite(req, caller):
    """LABELED FOLLOW-UP (stub, not wired to the MRF): an early-media/announcement leg to
    sip:announce@<home> would allocate an MRF 'announcement' media session and POST .../play a prompt
    (TS 23.218 7). Only the conference media path is fully wired in this pass; the announcement path
    returns an honest 501 so it is never silently faked. Removal path: mirror
    handle_conference_invite against resourceType='announcement' + the MRF /play route."""
    obs.log("announcement_followup_stub", callee=req.get("To"), caller=caller)
    obs.counter("scscf_mrf_announcement_stub_total").inc()
    return _err(req, 501, "Not Implemented", "ANNOUNCEMENT_FOLLOWUP")


def _release_conference_leg(call_id, dlg):
    """When a conference participant hangs up, drop its leg; when the last party leaves, release the
    whole MRF media session (TS 23.228 4.7) so the MODELLED port pairs return to the MRFP pool.
    ADDITIVE: a non-conference dialog has no 'conference' key and this is a no-op."""
    conf_id = dlg.get("conference")
    if not conf_id:
        return
    for conf_uri, conf in list(conferences.items()):
        if conf["mediaSessionId"] != conf_id:
            continue
        conf["callers"].pop(call_id, None)
        if not conf["callers"]:
            _mrf("DELETE", conf["base"], f"{MRF_BASE_PATH}/{conf_id}")
            conferences.pop(conf_uri, None)
            obs.log("conference_released", conference=conf_uri, mediaSessionId=conf_id)
            obs.counter("scscf_mrf_sessions_released_total").inc()
        return


def handle_invite(req):
    caller, callee = sip.header_uri(req.get("From")), sip.header_uri(req.get("To"))
    from_leg = req.get("P-Leg-Hint")   # set by the I-CSCF when it loops the leg back (terminating)

    if from_leg == "terminating":
        # Terminating leg (arrived back from the I-CSCF after Cx LIR): fork to the callee binding.
        b = registered_binding(callee)
        if b is None:
            return _err(req, 480, "Temporarily Unavailable", "NOT_REGISTERED")
        return synth_terminating_answer(req, b)

    # Originating leg: the caller must be registered here (came in via the P-CSCF service route).
    if registered_binding(caller) is None:
        return _err(req, 403, "Forbidden", "CALLER_NOT_REGISTERED")
    # Evaluate originating iFCs (recognised, no AS invoked — logged then continue).
    b = bindings.get(caller, {})
    ifcs = ((b.get("serviceProfile") or {}).get("initialFilterCriteria")) or []
    for ifc in ifcs:
        tp = ifc.get("triggerPoint", {})
        if tp.get("conditionType") == "originating" and "INVITE" in tp.get("sipMethod", []):
            obs.log("ifc_triggered", caller=caller, applicationServer=ifc.get("applicationServer"),
                    note="AS not invoked in this subsystem")
    # MRF in-call media (TS 24.147 conference / TS 23.218 announcement). Taken ONLY for the
    # dedicated media request-URIs; every other INVITE (the normal 2-party VoNR call) falls straight
    # through to the I-CSCF routing below, byte-for-byte unchanged and independent of the MRF.
    if is_conference_uri(callee):
        return handle_conference_invite(req, caller)
    if is_announcement_uri(callee):
        return handle_announcement_invite(req, caller)
    # Proxy the terminating leg out through the I-CSCF (real Cx LIR location step). The INVITE is
    # relayed unchanged (request-URI = callee, From/To preserved) with a real Via prepended; the
    # 100/180/200 stream comes back and is relayed up with our Via stripped (RFC 3261 16.6/16.7).
    icscf = discover("I-CSCF")
    if icscf is None:
        return _err(req, 500, "Server Internal Error", "NO_ICSCF")
    obs.counter("scscf_calls_originated_total").inc()
    try:
        return sip.relay_forward(req, icscf, SCSCF_HOST)
    except OSError:
        return _err(req, 500, "Server Internal Error", "ICSCF_UNREACHABLE")


# --------------------------------------------------------------------------- ACK / BYE
def handle_ack(req):
    dlg = dialogs.get(req.get("Call-ID"))
    if dlg:
        dlg["state"] = "ESTABLISHED"
    obs.log("call_established", callId=req.get("Call-ID"))
    # ACK has no response in real SIP; we return a 200 as a transport acknowledgement (the UA ignores it).
    return [sip.make_response(req, 200, "OK")]


def handle_bye(req):
    dlg = dialogs.get(req.get("Call-ID"))
    if dlg is None:
        return [sip.make_response(req, 481, "Call/Transaction Does Not Exist")]
    dlg["state"] = "TERMINATED"
    _release_conference_leg(req.get("Call-ID"), dlg)   # no-op unless this is a conference dialog
    obs.log("call_terminated", callId=req.get("Call-ID"))
    obs.counter("scscf_calls_terminated_total").inc()
    return [sip.make_response(req, 200, "OK")]


def on_sip(req):
    method = (req.method or "").upper()
    handler = {"REGISTER": handle_register, "INVITE": handle_invite,
               "ACK": handle_ack, "BYE": handle_bye}.get(method)
    if handler is None:
        return [sip.make_response(req, 405, "Method Not Allowed")]
    return handler(req)


# Inspection surfaces (used by the spec / viewer to prove both parties are bound).
@app.route("GET", "/scscf/bindings")
def list_bindings(params, query, body):
    return 200, {"bindings": [{"impu": k, "state": v.get("state"), "contact": v.get("contact")}
                              for k, v in bindings.items()]}


@app.route("GET", "/scscf/dialogs")
def list_dialogs(params, query, body):
    return 200, {"dialogs": dialogs}


@app.route("GET", "/scscf/conferences")
def list_conferences(params, query, body):
    """MRF-backed conferences this S-CSCF holds (for the spec / viewer to prove the media bridge)."""
    return 200, {"conferences": [
        {"aor": uri, "mediaSessionId": c["mediaSessionId"], "resourceUri": c.get("resourceUri"),
         "participants": len(c["callers"])} for uri, c in conferences.items()]}


def register_with_nrf():
    profile = {"nfType": "S-CSCF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "sip",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("scscf_bindings").set(sum(1 for b in bindings.values() if b.get("state") == "REGISTERED"))
    obs.gauge("scscf_dialogs").set(len(dialogs))
    obs.gauge("scscf_conferences").set(len(conferences))


if __name__ == "__main__":
    obs.init("scscf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    sip.serve(app, PORT, on_sip)

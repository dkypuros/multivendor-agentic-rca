"""
SEPP: Security Edge Protection Proxy — the roaming edge of the owned core (TS 29.573, TS 33.501).

The SEPP is the ONLY 5G core NF that sits on the PLMN boundary: every inter-PLMN SBI message
between a visited (serving) network and a home network crosses a pair of SEPPs over the N32
reference point. It is the security gateway that lets an operator interconnect with a roaming
partner without exposing its internal SBI to the public internet. This is the ROAMING domain —
previously a gap on THE EVERYTHING MAP — opened here at procedure fidelity.

Two SEPP roles are modelled by ONE configurable process (env SEPP_ROLE): the VISITED-PLMN SEPP
(cSEPP, the sender edge in front of a roaming AMF/SMF) and the HOME-PLMN SEPP (pSEPP, the
receiver edge in front of the UDM/AUSF). A hermetic pair (visited 00102 -> home 00101) proves the
whole N32 crossing in one process image, configured twice.

N32 has two legs (TS 29.573 clause 5):
  N32-c  the control leg. Two SEPPs run a SECURITY CAPABILITY HANDSHAKE (SecNegotiate) to agree a
         protection mechanism (TLS vs PRINS) and, here, to ESTABLISH an N32-f context: a shared
         context id + a per-context secret both edges hold. TS 29.573 clause 5.2.
           POST /n32c-handshake/v1/exchange-capability
  N32-f  the forwarding leg. An inter-PLMN SBI message from a local NF is REFORMATTED into a
         message-protected envelope (JOSE-shaped: a JWS-style protected header + payload +
         integrity tag, TS 29.573 clause 5.3 / clause 6.2), carried to the peer SEPP, VERIFIED,
         unwrapped, and delivered. TS 29.573 clause 5.3.
           POST /n32f-forward/v1/roaming-egress   (local NF -> this SEPP, sender side)
           POST /n32f-forward/v1/n32f-message     (peer SEPP -> this SEPP, receiver side)

THE ROAMING ACT
  "A visited-PLMN NF spoke to a home-PLMN NF, and neither PLMN's internal SBI was ever exposed on
  the wire between them — only a protected N32-f envelope crossed." The envelope's INTEGRITY is a
  real HMAC-SHA256 over the JOSE-shaped protected-header + payload, keyed by the N32-f context
  secret established at the N32-c handshake — so a tampered or unauthorized message is genuinely
  rejected (a green check, not an assertion). What is SHAPED, not crypto-grade, is labelled below.

Labelled simplifications (ledgered in procedures/sepp_roaming.txt):
  - JOSE ENVELOPE IS SHAPED, NOT RFC/crypto-grade. The protected header + payload + signature
    mimic the JWS/JWE serialisation of TS 33.501 PRINS, and the integrity tag is a real
    HMAC-SHA256 (stdlib) so tamper detection is honest — but there is NO confidentiality
    (JWE encryption): the payload is base64url of cleartext, not ciphertext. Real ES256/RSA-OAEP
    signing+encryption per RFC 7515/7516 and TS 33.501 is a FIDELITY follow-up.
  - THE CONTEXT KEY IS EXCHANGED IN THE CLEAR at N32-c handshake, not derived from a TLS exporter
    or a PRINS key-agreement. Real N32-c runs over mutually-authenticated TLS (TS 33.501 clause
    13.1) and derives session keys; here the receiver mints the secret and returns it. Labelled.
  - TLS TRANSPORT IS ABSENT. N32 mandates TLS 1.2+ between SEPPs (TS 33.501 13.1); this lab runs
    the JSON envelope over plain HTTP/1.1 like every other owned NF (procedure-level fidelity).
  - THE PEER PLMN'S CORE IS MODELLED, NOT REAL. There is no second running 5G core: the home SEPP
    DELIVERS a roaming message by reconstructing and returning it (delivered=true, the SBI request
    echoed intact). Wiring N32-f delivery into a real second-PLMN UDM/AUSF is a BREADTH follow-up.

Run: python3 sepp.py            (home role,    PLMN 00101, SBI on 127.0.0.1:7022, registers as SEPP)
     SEPP_ROLE=visited python3 sepp.py   (visited role, PLMN 00102; point SEPP_PEER_URL at the home SEPP)

Config (env; all optional, sensible role defaults):
  SEPP_ROLE        home (default) | visited
  SEPP_PLMN        PLMN id, default home=00101 visited=00102 (mccmnc, mcc 3 digits + mnc 2-3)
  SEPP_ID          this SEPP's id, default sepp-<role>-<plmn>
  SEPP_PEER_URL    base URL of the peer SEPP (sender side; enables /roaming-egress auto-handshake)
  SEPP_PORT        listen port override (default netconfig port('sepp')=7022, honours TELCO_PORT_*)
"""

import base64
import hashlib
import hmac
import json
import os
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

# ----------------------------------------------------------------- role / identity config
ROLE = os.environ.get("SEPP_ROLE", "home").lower()
_DEFAULT_PLMN = {"home": "00101", "visited": "00102"}.get(ROLE, "00101")
PLMN = os.environ.get("SEPP_PLMN", _DEFAULT_PLMN)
SEPP_ID = os.environ.get("SEPP_ID", f"sepp-{ROLE}-{PLMN}")
PEER_URL = os.environ.get("SEPP_PEER_URL")           # sender side; None on a pure receiver
PORT = int(os.environ.get("SEPP_PORT") or port("sepp"))
NRF = url("nrf")

# Security capabilities this SEPP offers on N32-c, in preference order (TS 29.573 6.1.5.3.2).
# PRINS (PRotocol for N32 INterconnect Security) enables application-layer message protection
# across IPX intermediaries; TLS is end-to-end SEPP-to-SEPP. We advertise both; PRINS is what
# drives the N32-f envelope here.
SUPPORTED_CAPABILITIES = ["PRINS", "TLS"]

app = SbiApp("sepp")

# Persistence mirrors the other NFs (domain/statestore.py): sqlite3 when TELCO_STATE_DIR is set,
# in-memory otherwise. An N32 context is the established security association with one peer PLMN.
store = open_store(f"sepp-{ROLE}")
contexts = store.collection("n32_contexts")   # n32ContextId -> {peerPlmn, peerSeppId, secret, ...}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def plmn_obj(plmn_str):
    """Split a PLMN id (mccmnc) into {mcc, mnc} per TS 23.003. mcc is 3 digits; mnc is the rest."""
    return {"mcc": plmn_str[:3], "mnc": plmn_str[3:]}


# ----------------------------------------------------------------- JOSE-shaped N32-f protection
# TS 29.573 clause 5.3 + TS 33.501 clause 13.2: an inter-PLMN SBI message is REFORMATTED into a
# protected envelope before it leaves the PLMN. Real PRINS uses JWE (confidentiality) + JWS
# (integrity) per RFC 7516/7515. Here the envelope is JOSE-SHAPED: a base64url protected header,
# a base64url payload, and a real HMAC-SHA256 integrity tag keyed by the N32-f context secret.
# Integrity is genuine (tamper is caught); confidentiality is NOT present (payload is cleartext,
# base64url only) — labelled in the module docstring and the procedure ledger.

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def protect(sbi_message: dict, ctx: dict) -> dict:
    """Reformat an SBI message into a JOSE-shaped, integrity-protected N32-f envelope.

    Shape mirrors JWS Flattened JSON Serialization (RFC 7515): {protected, payload, signature}.
    The signature is HMAC-SHA256(context_secret, protected + '.' + payload) — real integrity,
    shaped confidentiality (payload is base64url cleartext, not JWE ciphertext)."""
    header = {"typ": "N32-f", "alg": "HS256-SHAPED", "cty": "application/json",
              "n32fContextId": ctx["n32ContextId"],
              "src": {"seppId": SEPP_ID, "plmnId": plmn_obj(PLMN)}}
    protected = _b64u(json.dumps(header).encode())
    payload = _b64u(json.dumps(sbi_message).encode())
    signing_input = f"{protected}.{payload}".encode()
    signature = hmac.new(ctx["secret"].encode(), signing_input, hashlib.sha256).hexdigest()
    return {"protected": protected, "payload": payload, "signature": signature}


def unprotect(envelope: dict, ctx: dict):
    """Verify + unwrap a JOSE-shaped N32-f envelope against the context secret.

    Returns (sbi_message, None) on success or (None, reason) if the integrity tag does not
    verify (tamper) or the envelope is malformed. Uses hmac.compare_digest (constant time)."""
    try:
        protected = envelope["protected"]
        payload = envelope["payload"]
        signature = envelope["signature"]
    except (KeyError, TypeError):
        return None, "MALFORMED_N32F_ENVELOPE"
    signing_input = f"{protected}.{payload}".encode()
    expected = hmac.new(ctx["secret"].encode(), signing_input, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        return None, "INTEGRITY_CHECK_FAILED"
    try:
        sbi_message = json.loads(_b64u_dec(payload))
    except (ValueError, json.JSONDecodeError):
        return None, "MALFORMED_N32F_PAYLOAD"
    return sbi_message, None


# ----------------------------------------------------------------- N32-c: capability handshake
# TS 29.573 clause 5.2.2 (Security Capability Negotiation). The initiating SEPP POSTs its offered
# capabilities + its PLMN/SEPP identity; the responder selects the first mutually-supported
# capability and, when PRINS is selected, ESTABLISHES an N32-f context: a shared context id +
# secret that both edges then hold and use to protect/verify N32-f traffic. The responder returns
# the context so the initiator can mirror it (labelled: real N32-c derives keys from TLS/PRINS).

@app.route("POST", "/n32c-handshake/v1/exchange-capability")
def exchange_capability(params, query, body):
    sender_sepp = body.get("senderSeppId")
    sender_plmn = body.get("senderPlmnId")   # {"mcc","mnc"} or a "mccmnc" string
    offered = body.get("securityCapabilities") or body.get("secCapList") or []
    if not sender_sepp or not sender_plmn:
        return problem(400, "Bad Request", cause="MANDATORY_IE_MISSING",
                       detail="senderSeppId and senderPlmnId are mandatory (TS 29.573 5.2.2)")
    selected = next((c for c in SUPPORTED_CAPABILITIES if c in offered), None)
    if selected is None:
        # No common capability -> no secure interconnect can be formed (TS 33.501 13.1).
        obs.counter("sepp_handshake_rejected_total", role=ROLE).inc()
        return problem(406, "Not Acceptable", cause="NEGOTIATION_FAILED",
                       detail=f"no common security capability; offered={offered}, "
                              f"supported={SUPPORTED_CAPABILITIES}")
    ctx_id = "n32c-" + uuid.uuid4().hex[:16]
    # PRINS needs a shared secret for the N32-f integrity tag. Minted here and returned in the
    # clear (labelled simplification); real N32-c derives it from the TLS exporter / PRINS keys.
    secret = uuid.uuid4().hex + uuid.uuid4().hex if selected == "PRINS" else ""
    record = {"n32ContextId": ctx_id, "role": ROLE, "localSeppId": SEPP_ID, "localPlmn": PLMN,
              "peerSeppId": sender_sepp, "peerPlmn": sender_plmn,
              "selectedSecCapability": selected, "secret": secret,
              "status": "ESTABLISHED", "establishedAt": now_iso()}
    contexts.put(ctx_id, record)
    obs.log("n32c_handshake_established", role=ROLE, n32ContextId=ctx_id, peerSeppId=sender_sepp,
            selected=selected)
    obs.counter("sepp_handshake_established_total", role=ROLE).inc()
    return 200, {"n32ContextId": ctx_id, "receiverSeppId": SEPP_ID,
                 "receiverPlmnId": plmn_obj(PLMN), "selectedSecCapability": selected,
                 "secret": secret, "supportedFeatures": "0x1"}


def _ensure_context(peer_url):
    """Sender side: return an existing ESTABLISHED context for the peer, or run the N32-c
    handshake against peer_url to create one. Returns (context, None) or (None, reason)."""
    for c in contexts.values():
        if c.get("status") == "ESTABLISHED" and c.get("peerUrl") == peer_url:
            return c, None
    try:
        status, resp = request("POST", f"{peer_url}/n32c-handshake/v1/exchange-capability",
                               {"senderSeppId": SEPP_ID, "senderPlmnId": plmn_obj(PLMN),
                                "securityCapabilities": SUPPORTED_CAPABILITIES})
    except OSError as e:
        return None, f"peer SEPP unreachable: {e}"
    if status != 200:
        return None, f"handshake rejected by peer: {status} {resp.get('cause')}"
    ctx = {"n32ContextId": resp["n32ContextId"], "role": ROLE, "localSeppId": SEPP_ID,
           "localPlmn": PLMN, "peerSeppId": resp.get("receiverSeppId"),
           "peerPlmn": resp.get("receiverPlmnId"), "peerUrl": peer_url,
           "selectedSecCapability": resp.get("selectedSecCapability"),
           "secret": resp.get("secret", ""), "status": "ESTABLISHED", "establishedAt": now_iso()}
    contexts.put(ctx["n32ContextId"], ctx)
    obs.log("n32c_handshake_initiated", role=ROLE, n32ContextId=ctx["n32ContextId"],
            peerSeppId=ctx["peerSeppId"], selected=ctx["selectedSecCapability"])
    return ctx, None


# ----------------------------------------------------------------- N32-f: sender (local -> peer)
# TS 29.573 clause 5.3. A local (visited-PLMN) NF hands the SEPP a roaming SBI message; the SEPP
# protects it into a JOSE-shaped envelope and forwards it to the peer (home) SEPP over N32-f, then
# returns the peer's delivery result. This is the "egress" edge of the visited PLMN.

@app.route("POST", "/n32f-forward/v1/roaming-egress")
def roaming_egress(params, query, body):
    peer_url = body.get("peerUrl") or PEER_URL
    sbi_message = body.get("sbiMessage")
    if not peer_url:
        return problem(400, "Bad Request", cause="MANDATORY_IE_MISSING",
                       detail="peerUrl (or SEPP_PEER_URL env) is required for egress")
    if not isinstance(sbi_message, dict):
        return problem(400, "Bad Request", cause="MANDATORY_IE_MISSING",
                       detail="sbiMessage (the inter-PLMN SBI request object) is mandatory")
    ctx, reason = _ensure_context(peer_url)
    if ctx is None:
        return problem(502, "Bad Gateway", cause="N32C_HANDSHAKE_FAILED", detail=reason)
    envelope = protect(sbi_message, ctx)
    obs.log("n32f_egress", role=ROLE, n32ContextId=ctx["n32ContextId"],
            targetPlmn=ctx.get("peerPlmn"))
    obs.counter("sepp_n32f_egress_total", role=ROLE).inc()
    try:
        status, resp = request("POST", f"{peer_url}/n32f-forward/v1/n32f-message",
                               {"n32ContextId": ctx["n32ContextId"], "envelope": envelope})
    except OSError as e:
        return problem(502, "Bad Gateway", cause="N32F_FORWARD_FAILED",
                       detail=f"peer SEPP unreachable: {e}")
    return status, resp


# ----------------------------------------------------------------- N32-f: receiver (peer -> local)
# TS 29.573 clause 5.3.2. The peer SEPP delivers a protected N32-f envelope. This SEPP looks up the
# N32 context (authorization: no context => unknown/unauthorized peer), verifies the integrity tag
# (tamper => reject), unwraps the SBI message, and DELIVERS it. Delivery to the local core is
# MODELLED (no real second-PLMN core): the reconstructed SBI request is returned intact.

@app.route("POST", "/n32f-forward/v1/n32f-message")
def n32f_message(params, query, body):
    ctx_id = body.get("n32ContextId")
    envelope = body.get("envelope")
    ctx = contexts.get(ctx_id) if ctx_id else None
    if ctx is None or ctx.get("status") != "ESTABLISHED":
        # No established N32 context for this id => the peer never handshook, or is spoofing an
        # id we do not hold. Reject as unauthorized (TS 33.501 13: only established SEPP peers).
        obs.log("n32f_rejected", role=ROLE, reason="NO_N32_CONTEXT", n32ContextId=ctx_id,
                level="warning")
        obs.counter("sepp_n32f_rejected_total", role=ROLE, reason="unauthorized").inc()
        return problem(403, "Forbidden", cause="UNAUTHORIZED_PEER",
                       detail=f"no established N32 context for id {ctx_id!r}")
    sbi_message, reason = unprotect(envelope, ctx)
    if reason is not None:
        # Integrity tag did not verify (tamper) or the envelope is malformed.
        obs.log("n32f_rejected", role=ROLE, reason=reason, n32ContextId=ctx_id, level="warning")
        obs.counter("sepp_n32f_rejected_total", role=ROLE, reason="integrity").inc()
        return problem(400, "Bad Request", cause=reason,
                       detail="N32-f envelope failed verification (TS 29.573 5.3)")
    obs.log("n32f_delivered", role=ROLE, n32ContextId=ctx_id, peerSeppId=ctx.get("peerSeppId"))
    obs.counter("sepp_n32f_delivered_total", role=ROLE).inc()
    # Deliver = reconstruct + return the SBI request intact (peer core MODELLED, labelled).
    return 200, {"delivered": True, "n32ContextId": ctx_id,
                 "receivedByPlmn": PLMN, "fromPeerSeppId": ctx.get("peerSeppId"),
                 "sbiMessage": sbi_message, "deliveredAt": now_iso()}


# ----------------------------------------------------------------- status surface (for the viewer)
@app.route("GET", "/sepp/status")
def sepp_status(params, query, body):
    ctxs = list(contexts.values())
    return 200, {"seppId": SEPP_ID, "role": ROLE, "plmn": PLMN, "plmnId": plmn_obj(PLMN),
                 "supportedCapabilities": SUPPORTED_CAPABILITIES,
                 "n32ContextCount": len(ctxs),
                 "peers": [{"n32ContextId": c["n32ContextId"], "peerSeppId": c.get("peerSeppId"),
                            "peerPlmn": c.get("peerPlmn"), "status": c.get("status"),
                            "selectedSecCapability": c.get("selectedSecCapability")}
                           for c in ctxs]}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. Both N32 services (n32-c, n32-f) are advertised, plus the
    # seppInfo the NRF carries for a SEPP (TS 29.510 6.1.6.2.30). One instance id per role so a
    # hermetic home+visited pair both appear in the NRF without colliding.
    instance_id = f"sepp-{ROLE}-{uuid.uuid4()}"
    profile = {"nfType": "SEPP", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "plmnList": [plmn_obj(PLMN)],
               "seppInfo": {"seppId": SEPP_ID, "seppPrefix": "/sepp",
                            "remotePlmnList": [], "remoteSeppList": []},
               "nfServices": [
                   {"serviceName": "n32-c",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "n32-f",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{instance_id}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("sepp_n32_contexts_active", role=ROLE).set(
        len([c for c in contexts.values() if c.get("status") == "ESTABLISHED"]))


if __name__ == "__main__":
    obs.init(f"sepp-{ROLE}")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

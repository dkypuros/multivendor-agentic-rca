"""
sip_json: a labeled SIP-over-JSON transport for the owned IMS voice subsystem.

Real IMS signalling is SIP (TS 24.229 profiling of RFC 3261) carried over UDP/TCP/TLS, with
SDP (RFC 4566) offer/answer describing RTP media. The owned stack is a PROCEDURE-LEVEL model, not a
bit-level one (design stance 1 in docs/stack/end_to_end_stack_master_plan.txt), so — exactly as the
5G SBI is modelled as JSON-over-HTTP by adapters/sbi_http.py — SIP is modelled here as a JSON
ENVELOPE carried over the same HTTP/1.1 transport. Every SIP request and response is a dict; the
method, headers and SDP bodies preserve their real names so the learning transfers. Each CSCF
serves a single POST /sip endpoint that consumes and emits these envelopes.

HONESTY (ledgered in procedures/ims_vonr.txt):
  - This is SIP-over-JSON, not real SIP: no wire parsing, no Via/branch transaction matching, no
    retransmissions, no real SDP negotiation and NO RTP media. The 5QI=1 dedicated bearer for
    voice is NOTED in the SDP, never programmed (Rx to the PCF / PCSCF-in-the-data-path is a
    follow-up). The application-level SIP status lives in the envelope's "status" field; the HTTP
    status is just the transport (always 200 when a CSCF handled the message).
  - IMS-AKA is an honest FAKE, like the 5G-AKA in services/core/udm/udm.py: the challenge RES* is
    sha256(K || RAND) truncated, carried with fake_crypto=true. No Milenage, no IPSec SA.

Stdlib only. Envelopes are built with the helpers below so the four CSCFs agree on the shape.
"""

import hashlib
import uuid

from adapters.sbi_http import request
from domain.netconfig import plmn

# ---- Cx/Dx Diameter result codes (TS 29.229 6.2 / 3GPP mappings) modelled as ints.
DIAMETER_SUCCESS = 2001
DIAMETER_FIRST_REGISTRATION = 2001
DIAMETER_SUBSEQUENT_REGISTRATION = 2002
DIAMETER_UNREGISTERED_SERVICE = 2003
DIAMETER_ERROR_USER_UNKNOWN = 5001
DIAMETER_ERROR_IDENTITIES_DONT_MATCH = 5002
DIAMETER_ERROR_IDENTITY_NOT_REGISTERED = 5003
DIAMETER_ERROR_ROAMING_NOT_ALLOWED = 5004

# ---- Cx Server-Assignment-Types (TS 29.229 6.3.15) — the subset the S-CSCF drives.
SAT_REGISTRATION = 1
SAT_RE_REGISTRATION = 2
SAT_UNREGISTERED_USER = 3
SAT_USER_DEREGISTRATION = 5


def home_domain():
    """The IMS home network domain per TS 23.003 13.2, derived from the owned PLMN so the IMS
    identities live in the same network as the 5G core (PLMN 00101 -> mnc001.mcc001)."""
    p = plmn()
    mcc, mnc = p[:3], p[3:]
    return f"ims.mnc{int(mnc):03d}.mcc{int(mcc):03d}.3gppnetwork.org"


def realm():
    """The Digest/authentication realm the S-CSCF challenges with (TS 24.229 = home domain)."""
    return home_domain()


def scscf_uri():
    """Canonical SIP URI of the (single) owned S-CSCF, used as the Cx Server-Name."""
    return f"sip:scscf.{home_domain()}"


def fake_res(k, rand):
    """IMS-AKA challenge response, honest FAKE (fake_crypto): sha256(K || RAND) truncated —
    identical construction to the 5G-AKA RES* in services/core/udm/udm.py. The HSS holds K and
    computes the expected XRES the same way, so the S-CSCF can compare them."""
    return hashlib.sha256(bytes.fromhex(k) + bytes.fromhex(rand)).hexdigest()[:16]


def new_call_id():
    return uuid.uuid4().hex + "@" + home_domain()


def sip_request(method, request_uri, from_uri, to_uri, **headers):
    """Build a SIP request envelope. Named SIP headers ride as top-level keys; unknown kwargs are
    carried verbatim so a proxy can add Path / Route / P-Asserted-Identity without a schema change."""
    env = {
        "transport": "SIP-over-JSON",   # the honesty label travels on every message
        "kind": "request",
        "method": method,
        "requestUri": request_uri,
        "from": from_uri,
        "to": to_uri,
        "callId": headers.pop("callId", None) or new_call_id(),
        "cseq": headers.pop("cseq", 1),
        "via": headers.pop("via", []),
        "route": headers.pop("route", []),
        "path": headers.pop("path", []),
    }
    env.update(headers)
    return env


def sip_response(req, status, reason, **headers):
    """Build a SIP response envelope that echoes the dialog-identifying fields of `req` (RFC 3261
    8.2.6: a response copies From/To/Call-ID/CSeq/Via). `status`/`reason` are the SIP status line."""
    env = {
        "transport": "SIP-over-JSON",
        "kind": "response",
        "status": status,
        "reason": reason,
        "method": req.get("method"),
        "from": req.get("from"),
        "to": req.get("to"),
        "callId": req.get("callId"),
        "cseq": req.get("cseq"),
        "via": req.get("via", []),
    }
    env.update(headers)
    return env


def sip_send(base_url, envelope):
    """Forward a SIP envelope to another CSCF's POST /sip surface. Returns the response envelope
    (a dict with its own SIP "status"); the HTTP status is transport-only and discarded here.
    A dead next hop raises OSError, exactly like adapters/sbi_http.request."""
    _http_status, reply = request("POST", base_url.rstrip("/") + "/sip", envelope)
    return reply

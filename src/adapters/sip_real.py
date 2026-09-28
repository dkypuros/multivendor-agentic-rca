"""
sip_real: a REAL SIP (RFC 3261) + SDP (RFC 4566) codec for the owned IMS voice subsystem.

This RETIRES the labeled SIP-over-JSON simplification (adapters/sip_json.py): the message ON THE
WIRE is now genuine SIP text — a real request/status line, real CRLF-framed headers (Via with a
z9hG4bK branch, From/To with tags, Call-ID, CSeq, Max-Forwards, Contact, Route/Record-Route,
Content-Type/Content-Length), and a real SDP body (v=/o=/s=/c=/t=/m=/a=/b= lines) for the
offer/answer. The owned stack still carries the SIP text over its HTTP/1.1 carrier (the SIP message is
the HTTP body, Content-Type: application/sip) exactly as RFC 3261 permits SIP over any reliable
transport — but there is NO JSON envelope any more: parse this stream with any generic RFC 3261
parser and you get the method and headers back.

WHAT IS REAL NOW (fidelity axis #53):
  - request/status line + CRLF header framing + Content-Length body framing (RFC 3261 7);
  - Via with branch (z9hG4bK, RFC 3261 8.1.1.7), prepended on forward + stripped on the response
    by each proxy (RFC 3261 16.6/16.7); Max-Forwards decrement;
  - From/To tags (8.1.1.3), Call-ID (8.1.1.4), CSeq num+method (8.1.1.5), Contact, Route,
    Record-Route (the P-CSCF/S-CSCF stay in the path), Service-Route (registration), Path;
  - the IMS-AKA challenge as a real WWW-Authenticate header and the response as a real
    Authorization header (Digest-AKAv1-MD5), TS 24.229 5.4.1 / RFC 3261 22;
  - the INVITE/100/180/200/ACK/BYE dialog with a real SDP offer/answer (RFC 4566), the 5QI and the
    media GBR carried as real SDP bandwidth (b=AS) + 3GPP attribute lines.

WHAT STAYS MODELLED (labeled, ledgered in procedures/ims_vonr.txt):
  - RTP media is NOT carried (the m=/c= describe media that is never sent) — #53's RTP follow-up;
  - IMS-AKA stays the honest FAKE (RES = sha256(K||RAND) truncated, no Milenage, no IPSec SA);
  - the 100/180 provisional responses are emitted as real SIP messages but pipelined in the same
    HTTP response as the final 200 (the carrier is request/response, not a live socket);
  - the Cx leg (CSCF<->IMS-HSS) is Diameter and stays JSON-labelled (adapters/sip_json.py) — only
    the SIP legs became real bits here.

Stdlib only. Identity/Diameter helpers (home_domain, realm, scscf_uri, fake_res, the Cx codes) stay
in adapters/sip_json.py; this module owns the SIP/SDP WIRE.
"""

import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib.request
from urllib.parse import parse_qs, urlparse

from adapters.sbi_http import problem
from domain import netconfig, obs

CRLF = "\r\n"
SIP_VERSION = "SIP/2.0"
BRANCH_MAGIC = "z9hG4bK"          # RFC 3261 8.1.1.7 magic cookie
DEFAULT_MAX_FORWARDS = 70
SIP_CONTENT_TYPE = "application/sip"
SDP_CONTENT_TYPE = "application/sdp"

# Compact header forms (RFC 3261 20) accepted on parse; we always serialise the long form.
_COMPACT = {"v": "Via", "f": "From", "t": "To", "i": "Call-ID", "m": "Contact",
            "c": "Content-Type", "l": "Content-Length", "s": "Subject", "k": "Supported"}


def new_branch():
    return BRANCH_MAGIC + uuid.uuid4().hex


def new_tag():
    return uuid.uuid4().hex[:12]


def new_call_id(home):
    return uuid.uuid4().hex + "@" + home


# --------------------------------------------------------------------------- URI / header params
def header_uri(value):
    """The SIP/tel URI inside a name-addr or addr-spec header value: '"Al" <sip:a@d>;tag=1' -> sip:a@d."""
    if value is None:
        return None
    v = value.strip()
    if "<" in v and ">" in v:
        return v[v.index("<") + 1:v.index(">")].split(";", 1)[0]
    return v.split(";", 1)[0].strip()


def header_param(value, name):
    """A header parameter value: header_param('<sip:a@d>;tag=xyz', 'tag') -> 'xyz'; None if absent."""
    if value is None:
        return None
    for part in value.split(";")[1:]:
        k, _, val = part.strip().partition("=")
        if k.strip().lower() == name.lower():
            return val.strip()
    return None


def via_branch(via_value):
    """The branch of a Via header value (Via: SIP/2.0/UDP host;branch=z9hG4bK...)."""
    return header_param(via_value, "branch")


# --------------------------------------------------------- Digest auth (RFC 3261 22 / RFC 2617)
def build_digest(**params):
    """Serialise a Digest challenge/credentials header value (RFC 2617). Quoted params (realm,
    nonce, username, response, uri) are quoted; token params (algorithm, stale) are bare."""
    bare = {"algorithm", "stale", "qop"}
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        parts.append(f"{k}={v}" if k in bare else f'{k}="{v}"')
    return "Digest " + ", ".join(parts)


def parse_digest(value):
    """Parse a WWW-Authenticate / Authorization Digest header value into a param dict."""
    if not value:
        return {}
    body = value.split(" ", 1)[1] if " " in value else value
    out = {}
    for part in body.split(","):
        k, _, v = part.strip().partition("=")
        out[k.strip()] = v.strip().strip('"')
    return out


# ============================================================================ the SIP message
class SipMessage:
    """A parsed/serialisable RFC 3261 message. Headers are an ordered list of [name, value] so
    Via/Route multiplicity and order are preserved exactly (RFC 3261 7.3.1)."""

    def __init__(self, is_request=True):
        self.is_request = is_request
        self.method = None
        self.request_uri = None
        self.version = SIP_VERSION
        self.status = None
        self.reason = None
        self.headers = []          # list of [name, value]
        self.body = ""

    # -- header access (case-insensitive on the name) -----------------------------------------
    def get(self, name):
        low = name.lower()
        for n, v in self.headers:
            if n.lower() == low:
                return v
        return None

    def get_all(self, name):
        low = name.lower()
        return [v for n, v in self.headers if n.lower() == low]

    def add(self, name, value):
        self.headers.append([name, str(value)])
        return self

    def set(self, name, value):
        low = name.lower()
        self.headers = [[n, v] for n, v in self.headers if n.lower() != low]
        self.headers.append([name, str(value)])
        return self

    def remove(self, name):
        low = name.lower()
        self.headers = [[n, v] for n, v in self.headers if n.lower() != low]
        return self

    # -- Via stack (RFC 3261 16.6 add on forward / 16.7 strip on response) ---------------------
    def prepend_via(self, host, transport="UDP", branch=None):
        via = f"{SIP_VERSION}/{transport} {host};branch={branch or new_branch()}"
        self.headers.insert(0, ["Via", via])
        return self

    def top_via(self):
        for n, v in self.headers:
            if n.lower() == "via":
                return v
        return None

    def pop_top_via(self):
        for i, (n, v) in enumerate(self.headers):
            if n.lower() == "via":
                del self.headers[i]
                return v
        return None

    def decrement_max_forwards(self):
        mf = self.get("Max-Forwards")
        mf = int(mf) if mf is not None else DEFAULT_MAX_FORWARDS
        self.set("Max-Forwards", max(mf - 1, 0))
        return mf - 1

    # -- CSeq ----------------------------------------------------------------------------------
    def cseq_number(self):
        c = self.get("CSeq") or "0"
        return int(c.split()[0])

    def cseq_method(self):
        c = (self.get("CSeq") or "").split()
        return c[1] if len(c) > 1 else None

    def copy(self):
        m = SipMessage(self.is_request)
        m.method, m.request_uri, m.version = self.method, self.request_uri, self.version
        m.status, m.reason, m.body = self.status, self.reason, self.body
        m.headers = [[n, v] for n, v in self.headers]
        return m

    # -- SDP body ------------------------------------------------------------------------------
    def sdp(self):
        if self.body and (self.get("Content-Type") or "").lower().startswith(SDP_CONTENT_TYPE):
            return parse_sdp(self.body)
        return {}

    def set_sdp(self, text):
        self.body = text
        self.set("Content-Type", SDP_CONTENT_TYPE)
        return self

    # -- serialise (RFC 3261 7) ----------------------------------------------------------------
    def serialize(self):
        if self.is_request:
            start = f"{self.method} {self.request_uri} {self.version}"
        else:
            start = f"{self.version} {self.status} {self.reason}"
        body_bytes = (self.body or "").encode("utf-8")
        # Content-Length is authored fresh on the wire (RFC 3261 20.14) so it is always correct.
        headers = [[n, v] for n, v in self.headers if n.lower() != "content-length"]
        lines = [start] + [f"{n}: {v}" for n, v in headers]
        lines.append(f"Content-Length: {len(body_bytes)}")
        return CRLF.join(lines) + CRLF + CRLF + (self.body or "")

    def encode(self):
        return self.serialize().encode("utf-8")

    def __repr__(self):
        head = (f"{self.method} {self.request_uri}" if self.is_request
                else f"{self.status} {self.reason}")
        return f"<SipMessage {head}>"


# ============================================================================ parsing the wire
def parse_message(text):
    """Parse ONE RFC 3261 message from text (no Content-Length framing of a stream)."""
    return parse_messages(text.encode("utf-8") if isinstance(text, str) else text)[0]


def parse_messages(data):
    """Parse one-or-more length-framed SIP messages from a byte stream (a UAS may pipeline
    100/180/200). Returns a list of SipMessage. This is a GENERIC RFC 3261 parser — nothing here
    knows about IMS — so a spec can use it to prove the wire really is SIP."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    msgs = []
    i = 0
    n = len(data)
    while i < n:
        while i < n and data[i:i + 2] == b"\r\n":     # skip inter-message CRLFs
            i += 2
        if i >= n:
            break
        sep = data.find(b"\r\n\r\n", i)
        if sep < 0:
            break
        head = data[i:sep].decode("utf-8", "replace")
        lines = head.split(CRLF)
        start = lines[0].split(" ", 2)
        msg = SipMessage()
        if start[0].startswith("SIP/"):
            msg.is_request = False
            msg.version = start[0]
            msg.status = int(start[1]) if len(start) > 1 else 0
            msg.reason = start[2] if len(start) > 2 else ""
        else:
            msg.is_request = True
            msg.method = start[0]
            msg.request_uri = start[1] if len(start) > 1 else ""
            msg.version = start[2] if len(start) > 2 else SIP_VERSION
        for hl in lines[1:]:
            if not hl.strip():
                continue
            name, _, val = hl.partition(":")
            name = name.strip()
            name = _COMPACT.get(name.lower(), name)
            msg.headers.append([name, val.strip()])
        clen = int(msg.get("Content-Length") or 0)
        bstart = sep + 4
        msg.body = data[bstart:bstart + clen].decode("utf-8", "replace")
        i = bstart + clen
        msgs.append(msg)
    return msgs


# ============================================================================ message builders
def make_request(method, request_uri, from_uri, to_uri, call_id, cseq,
                 via_host="ue.invalid", from_tag=None, to_tag=None, contact=None,
                 max_forwards=DEFAULT_MAX_FORWARDS, expires=None):
    """Build an RFC 3261 request with the mandatory header set (8.1.1)."""
    m = SipMessage(is_request=True)
    m.method = method
    m.request_uri = request_uri
    m.prepend_via(via_host)
    m.add("Max-Forwards", max_forwards)
    frm = f"<{from_uri}>;tag={from_tag or new_tag()}"
    to = f"<{to_uri}>" + (f";tag={to_tag}" if to_tag else "")
    m.add("From", frm)
    m.add("To", to)
    m.add("Call-ID", call_id)
    m.add("CSeq", f"{cseq} {method}")
    if contact:
        m.add("Contact", f"<{contact}>" if not contact.startswith("<") else contact)
    if expires is not None:
        m.add("Expires", expires)
    return m


def make_response(req, status, reason, to_tag=None):
    """Build a response that copies the dialog-identifying headers of `req` (RFC 3261 8.2.6):
    Via stack, From, To (a tag is added for a final/UAS response), Call-ID and CSeq are echoed."""
    m = SipMessage(is_request=False)
    m.status = status
    m.reason = reason
    for n, v in req.headers:
        if n.lower() == "via":
            m.add("Via", v)
    m.set("From", req.get("From"))
    to = req.get("To")
    if to and "tag=" not in to and to_tag:
        to = f"{to};tag={to_tag}"
    m.set("To", to)
    m.set("Call-ID", req.get("Call-ID"))
    m.set("CSeq", req.get("CSeq"))
    return m


def with_reason(msg, cause):
    """Attach an RFC 3326 Reason header carrying the machine cause token (USER_UNKNOWN, ...),
    so the honest error cause travels on real SIP instead of a JSON field."""
    if cause is not None:
        msg.set("Reason", f'SIP;cause={msg.status};text="{cause}"')
    return msg


def reason_cause(msg):
    """Extract the cause token from an RFC 3326 Reason header, or None."""
    r = msg.get("Reason")
    if not r:
        return None
    lo = r.find('text="')
    if lo < 0:
        return None
    return r[lo + 6:].split('"', 1)[0] or None


# ============================================================================ SDP (RFC 4566)
_RTPMAP = {"AMR-WB": ("96", "AMR-WB/16000"), "AMR": ("96", "AMR/8000"),
           "PCMU": ("0", "PCMU/8000"), "PCMA": ("8", "PCMA/8000"), "G729": ("18", "G729/8000")}


def _gbr_kbps(gbr):
    """Best-effort integer kbps for b=AS from a {'ul','dl'} of '24 Kbps' strings; 0 if unknown."""
    for key in ("dl", "ul"):
        v = (gbr or {}).get(key)
        if v:
            digits = "".join(ch for ch in str(v) if ch.isdigit())
            if digits:
                return int(digits)
    return 0


def build_sdp(codec="AMR-WB", five_qi=None, media_gbr=None, ip="127.0.0.1", port=49170,
              conference=None, session="VoNR call"):
    """Serialise a real RFC 4566 SDP offer/answer. The 5QI and the media GBR ride as real SDP:
    b=AS bandwidth (RFC 4566 5.8) + 3GPP attribute lines (a=3gpp-*), so the P-CSCF (IMS AF) can
    read the QoS off genuine SDP. No RTP is carried — the m=/c= describe media that is modelled."""
    pt, rtpmap = _RTPMAP.get(codec, _RTPMAP["AMR-WB"])
    lines = [
        "v=0",
        f"o=- {uuid.uuid4().int % (10 ** 10)} 1 IN IP4 {ip}",
        f"s={session}",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {pt}",
    ]
    gbr_kbps = _gbr_kbps(media_gbr)
    if gbr_kbps:                                     # b= precedes a= within a media block (RFC 4566)
        lines.append(f"b=AS:{gbr_kbps}")
    lines.append(f"a=rtpmap:{pt} {rtpmap}")
    if five_qi is not None:
        lines.append(f"a=3gpp-qos-5qi:{five_qi}")
    if media_gbr:
        lines.append(f"a=3gpp-media-gbr:ul={media_gbr.get('ul')};dl={media_gbr.get('dl')}")
    if conference:
        lines.append("a=3gpp-conference:1")
        for key, attr in (("mediaSessionId", "3gpp-media-session-id"),
                          ("mrfEndpoint", "3gpp-mrf-endpoint"),
                          ("participantId", "3gpp-participant-id"),
                          ("participantCount", "3gpp-participant-count")):
            if conference.get(key) is not None:
                lines.append(f"a={attr}:{conference[key]}")
    return CRLF.join(lines) + CRLF


def parse_sdp(text):
    """Parse a real SDP body back into the field view the IMS handlers use. Returns {} for no body."""
    if not text:
        return {}
    out = {"media": None, "codec": None, "5qi": None, "mediaGbr": None,
           "conference": False, "mrfEndpoint": None, "mediaSessionId": None,
           "participantId": None, "participantCount": None}
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or "=" not in line:
            continue
        typ, val = line[0], line[2:]
        if typ == "m":
            out["media"] = val.split()[0] if val.split() else None
        elif typ == "a":
            attr, _, aval = val.partition(":")
            if attr == "rtpmap":
                out["codec"] = aval.split()[1].split("/")[0] if len(aval.split()) > 1 else None
            elif attr == "3gpp-qos-5qi":
                out["5qi"] = int(aval) if aval.isdigit() else aval
            elif attr == "3gpp-media-gbr":
                gbr = {}
                for part in aval.split(";"):
                    k, _, v = part.partition("=")
                    if k in ("ul", "dl"):
                        gbr[k] = v
                out["mediaGbr"] = gbr or None
            elif attr == "3gpp-conference":
                out["conference"] = True
            elif attr == "3gpp-mrf-endpoint":
                out["mrfEndpoint"] = aval
            elif attr == "3gpp-media-session-id":
                out["mediaSessionId"] = aval
            elif attr == "3gpp-participant-id":
                out["participantId"] = aval
            elif attr == "3gpp-participant-count":
                out["participantCount"] = int(aval) if aval.isdigit() else aval
    return out


# ============================================================================ transport
def final(msgs):
    """The definitive response from a pipelined [1xx..., final] stream (RFC 3261 = the last one)."""
    return msgs[-1] if msgs else None


def provisional(msgs):
    """The provisional (1xx) responses in the stream, in order."""
    return [m for m in msgs if m.status and 100 <= m.status < 200]


def response_view(msgs):
    """A read-only dict view of a real SIP response stream for a UA/test: the final status, the
    parsed SDP, the 1xx provisionals, and the dialog/QoS fields lifted off real headers + SDP. The
    real SipMessage stays available under '_final' for anyone that wants the wire object."""
    f = final(msgs)
    sdp = f.sdp() if f else {}
    conf = {}
    if sdp.get("conference"):
        conf = {"mediaSessionId": sdp.get("mediaSessionId"),
                "participantId": sdp.get("participantId"),
                "participantCount": sdp.get("participantCount")}
    return {
        "status": f.status if f else None,
        "reason": f.reason if f else None,
        "sdp": sdp,
        "provisional": [{"status": m.status, "reason": m.reason} for m in provisional(msgs)],
        "serviceRoute": f.get_all("Service-Route") if f else [],
        "pAssociatedUri": f.get_all("P-Associated-URI") if f else [],
        "conference": conf,
        "cause": reason_cause(f) if f else None,
        "_final": f,
    }


def send(base_url, msg, timeout=5):
    """Forward a real SIP message to a peer CSCF's POST /sip surface and return the parsed list of
    response messages (1xx provisional first, then the final). The SIP TEXT is the HTTP body
    (Content-Type application/sip) — no JSON envelope. A dead next hop raises OSError, exactly like
    adapters/sbi_http.request, so proxies degrade to a SIP 5xx."""
    data = msg.encode()
    hdrs = {"Content-Type": SIP_CONTENT_TYPE}
    corr = obs.get_corr()
    if corr:
        hdrs[obs.HEADER] = corr
    req = urllib.request.Request(base_url.rstrip("/") + "/sip", data=data, method="POST",
                                 headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_messages(resp.read())


def relay_forward(msg, base_url, via_host, record_route=None, transport="UDP"):
    """RFC 3261 stateful-proxy forward: prepend our Via (16.6), Max-Forwards--, optionally
    Record-Route (16.6/12.1) to stay in the dialog, send, then strip our top Via off every returned
    response (16.7). Returns the list of response messages ready to relay further upstream."""
    fwd = msg.copy()
    fwd.decrement_max_forwards()
    if record_route:
        fwd.headers.insert(0, ["Record-Route", f"<{record_route}>"])
    fwd.prepend_via(via_host, transport)
    responses = send(base_url, fwd)
    for r in responses:
        r.pop_top_via()
    return responses


# ============================================================================ combined server
def serve(app, port, on_sip):
    """Serve one IMS CSCF: real SIP text on POST /sip (handed to `on_sip(SipMessage)-> list[
    SipMessage]`), and the NF's ordinary JSON routes (NRF registration, /metrics, inspection) via
    the SAME SbiApp route table. Non-/sip requests keep the exact JSON-over-HTTP behaviour of
    adapters/sbi_http.serve (root index, /metrics, correlation id); only /sip carries real SIP.

    Kept in this owned adapter so the SIP wire never has to touch the shared JSON serve()."""
    import json

    last_capture = {"raw": None}      # last raw SIP request bytes this NF received (spec/viewer proof)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _sip(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            last_capture["raw"] = raw.decode("utf-8", "replace")
            try:
                req = parse_message(raw)
                responses = on_sip(req)
            except Exception as exc:                       # never leak a stack to the peer
                obs.log("sip_handler_error", level="error", error=str(exc))
                responses = [make_response(SipMessage(), 500, "Server Internal Error")]
            body = "".join(m.serialize() for m in responses).encode("utf-8")
            self.send_response(200)                        # HTTP is transport-only; SIP status is in-body
            self.send_header("Content-Type", SIP_CONTENT_TYPE)
            self._finish(body)

        def _json(self, method, parsed):
            if method == "GET" and parsed.path == "/metrics":
                self._reply_text(200, obs.render_metrics())
                return
            if method == "GET" and parsed.path == "/sip/last":
                # The raw SIP text of the last message this NF received — a spec re-parses it with a
                # generic RFC 3261 parser to PROVE the wire is real SIP, not a JSON envelope.
                self._reply(200, {"raw": last_capture["raw"]})
                return
            if method == "GET" and parsed.path == "/":
                self._reply(200, {"nf": app.name, "metrics": "/metrics", "sip": "POST /sip",
                                  "routes": sorted({f"{m} /" + "/".join(seg)
                                                    for m, seg, _ in app.routes})})
                return
            handler, params = app.match(method, parsed.path)
            if handler is None:
                self._reply(*problem(404, "Not Found", detail=parsed.path))
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            status, reply = handler(params, query, body)
            self._reply(status, reply)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            obs.set_corr(self.headers.get(obs.HEADER))
            span = obs.begin_server_span(self.headers.get(obs.TRACEPARENT),
                                         f"{method} {parsed.path}")
            try:
                if method == "POST" and parsed.path == "/sip":
                    self._sip()
                else:
                    self._json(method, parsed)
            finally:
                if span is not None:
                    span.end()
                    obs.clear_trace_context()

        def _reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            ct = "application/problem+json" if status >= 400 else "application/json"
            self.send_header("Content-Type", ct)
            self._finish(data)

        def _reply_text(self, status, text):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self._finish(text.encode())

        def _finish(self, data):
            corr = obs.get_corr()
            if corr:
                self.send_header(obs.HEADER, corr)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    if not obs.initialized():
        obs.init(app.name)
    bind = netconfig.bind_host()
    server = ThreadingHTTPServer((bind, port), Handler)
    obs.log("listening", addr=f"{bind}:{port}", sip="POST /sip (real RFC 3261)")
    server.serve_forever()

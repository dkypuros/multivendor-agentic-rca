"""
obs: shared observability for every owned NF (issue #27). Stdlib only, like everything else.

Three pieces, all plumbed through adapters/sbi_http.py so every HTTP service gets them
with near-zero per-service code (docs/operations/observability.txt is the full format spec):

STRUCTURED LOGS   obs.log(event, **fields) emits exactly one JSON line to stdout:
                    {"ts": ..., "nf": ..., "level": ..., "event": ..., "corr": ..., **fields}
                  JSON IS the log format — no human-readable fallback by default; the
                  consumers are tools/stackctl.py's captured .stack/logs/*.log files and CI
                  spec-log artifacts, and `grep <corr-id>` over those reconstructs a flow.
                  The NF name is set once at startup with obs.init("amf");
                  adapters/sbi_http.serve() falls back to the SbiApp name if init was never
                  called, so even untouched services log with a sensible nf field.

METRICS           obs.counter(name, **labels).inc() and obs.gauge(name, **labels).set(v):
                  a thread-safe in-process registry. Every sbi_http-served NF answers
                  GET /metrics with Prometheus text exposition format; every sample carries
                  an nf label. obs.on_scrape(fn) registers a hook run at scrape time for
                  gauges that mirror live state (e.g. the UPF session table).

CORRELATION       A correlation id is minted at the entry edge (obs.new_corr(): the BSS
                  mints one per TMF622 product order, ue_sim per registration attempt) and
                  rides EVERY inter-NF HTTP call as an X-Correlation-Id header:
                  sbi_http request() attaches the current id, sbi_http's server side adopts
                  the inbound header for the handling thread and echoes it on the response.
                  Every obs.log() line carries it, and order records / audit events embed it
                  as a "corr" field. The id is thread-local: ThreadingHTTPServer handles each
                  request on its own thread, and synchronous southbound calls happen on that
                  same thread, so propagation is automatic across any call depth.
                  EXCLUSION: UDP legs (GTP-U, PFCP, E2, fronthaul, Uu) do NOT carry the id —
                  the corr id rides the control plane, never the user plane. Pure-UDP
                  processes (services/ran/oru) have no HTTP surface and no /metrics; the
                  O-RU's liveness is observed via its paired O-DU (ru state, O1/E2).

DISTRIBUTED       W3C Trace Context (traceparent) rides every inter-NF HTTP call on TOP of the
TRACING           correlation id (procedures/distributed_tracing.txt is the full spec). sbi_http's
                  server side opens a SERVER span per handled request (adopting an inbound
                  `traceparent`, or STARTING a trace whose 16-byte trace-id is DERIVED from the
                  bound corr id so the two cross-reference); every southbound request() opens a
                  child CLIENT span and stamps its own `traceparent`, so a multi-NF call chain
                  shares ONE trace-id with correct parent/child span links. Spans are recorded
                  through a stdlib exporter that batches and POSTs OTLP/HTTP-JSON to
                  TELCO_OTLP_ENDPOINT. DEFAULT-INERT: with no endpoint set (and TELCO_TRACE
                  unset) tracing is OFF — no spans, no traceparent header, no logs, byte-identical
                  behavior and latency; every pre-tracing spec runs this path UNEDITED. Set
                  TELCO_TRACE=log to emit spans as structured JSON logs (event="span") instead of
                  exporting — the same Loki-via-Alloy path the JSON logs already travel.
"""

import atexit
import hashlib
import json
import os
import threading
import time
import urllib.request
import uuid

HEADER = "X-Correlation-Id"
TRACEPARENT = "traceparent"

_nf = {"name": None}
_tls = threading.local()
_lock = threading.Lock()
_counters = {}   # (name, labels tuple) -> float
_gauges = {}     # (name, labels tuple) -> float
_scrape_hooks = []


# ------------------------------------------------------------------ identity
def init(name):
    """Set this process's NF name once at startup (before serving)."""
    _nf["name"] = name


def initialized():
    return _nf["name"] is not None


def nf_name():
    return _nf["name"] or "unnamed"


# ------------------------------------------------------------------ correlation
def new_corr():
    """Mint a correlation id at an entry edge (BSS product order, UE registration)."""
    return "c-" + uuid.uuid4().hex[:12]


def set_corr(corr):
    """Bind a correlation id (or None) to the current thread."""
    _tls.corr = corr


def get_corr():
    return getattr(_tls, "corr", None)


# ------------------------------------------------------------------ structured logs
def log(event, level="info", **fields):
    """Emit one JSON log line to stdout. Never raises: instrumentation must not kill an NF."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "nf": nf_name(), "level": level, "event": event}
    corr = get_corr()
    if corr is not None:
        line["corr"] = corr
    line.update(fields)
    try:
        print(json.dumps(line, default=str), flush=True)
    except Exception:
        pass


# ------------------------------------------------------------------ metrics
class _Metric:
    def __init__(self, table, key):
        self._table = table
        self._key = key


class Counter(_Metric):
    def inc(self, n=1):
        with _lock:
            self._table[self._key] = self._table.get(self._key, 0) + n


class Gauge(_Metric):
    def set(self, value):
        with _lock:
            self._table[self._key] = value

    def inc(self, n=1):
        with _lock:
            self._table[self._key] = self._table.get(self._key, 0) + n


def _key(name, labels):
    return (name, tuple(sorted(labels.items())))


def counter(name, **labels):
    key = _key(name, labels)
    with _lock:
        _counters.setdefault(key, 0)
    return Counter(_counters, key)


def gauge(name, **labels):
    key = _key(name, labels)
    with _lock:
        _gauges.setdefault(key, 0)
    return Gauge(_gauges, key)


def on_scrape(fn):
    """Register a hook run before each /metrics render (refresh gauges from live state)."""
    _scrape_hooks.append(fn)


def render_metrics():
    """Prometheus text exposition format; every sample carries an nf label."""
    for fn in _scrape_hooks:
        try:
            fn()
        except Exception:
            pass  # a broken hook must not break the scrape
    out = []
    with _lock:
        for kind, table in (("counter", _counters), ("gauge", _gauges)):
            seen = set()
            for (name, labels) in sorted(table):
                if name not in seen:
                    out.append(f"# TYPE {name} {kind}")
                    seen.add(name)
                pairs = [("nf", nf_name())] + list(labels)
                rendered = ",".join(f'{k}="{v}"' for k, v in pairs)
                value = table[(name, labels)]
                out.append(f"{name}{{{rendered}}} {value}")
    return "\n".join(out) + "\n"


# ================================================================= distributed tracing (issue #27)
# W3C Trace Context (https://www.w3.org/TR/trace-context/) propagation + an OTLP/HTTP-JSON export
# path, all plumbed through adapters/sbi_http.py so every owned NF is traced with no per-NF code.
# STDLIB ONLY (charter PURITY): no OpenTelemetry SDK — the wire shapes are authored here from the
# W3C `traceparent` grammar and the OTLP/HTTP JSON encoding, and emitted with urllib.
#
# DEFAULT-INERT CONTRACT (make-or-break): tracing is OFF unless configured. When off, begin_*_span
# return None/(None, None) immediately — no Span object, no time_ns(), no header, no log — so the
# response bytes AND the latency are byte-identical to the pre-tracing code and every existing spec
# passes unedited. Export/log is best-effort on a daemon thread and can NEVER block or fail a request.

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16
_KIND_INTERNAL, _KIND_SERVER, _KIND_CLIENT = 1, 2, 3   # OTLP SpanKind
_KIND_NAME = {_KIND_INTERNAL: "internal", _KIND_SERVER: "server", _KIND_CLIENT: "client"}

_BATCH_MAX = 128       # flush eagerly once this many spans are buffered
_FLUSH_SECS = 2.0      # otherwise flush on this cadence
_EXPORT_TIMEOUT = 3.0  # per-POST timeout; a slow collector never stalls the NF (daemon thread)

_trace_cfg = {"endpoint": None, "log": False, "on": False}
_exporter_box = {"e": None}


def reload_tracing_config():
    """(Re)read tracing config from the environment. Called once at import; specs call it after
    mutating the env in-process. Returns whether tracing is on. Config sources:
        TELCO_OTLP_ENDPOINT   full-or-base OTLP/HTTP URL -> spans EXPORT as OTLP/JSON.
        TELCO_TRACE=log       spans emit as structured JSON logs (event="span"); no export.
        neither (THE DEFAULT) tracing OFF / fully inert."""
    endpoint = (os.environ.get("TELCO_OTLP_ENDPOINT") or "").strip() or None
    log_mode = (os.environ.get("TELCO_TRACE") or "").strip().lower() in ("log", "logs")
    _trace_cfg.update(endpoint=endpoint, log=log_mode, on=bool(endpoint) or log_mode)
    return _trace_cfg["on"]


def tracing_on():
    """True iff tracing is configured (endpoint or TELCO_TRACE=log). Default False (inert)."""
    return _trace_cfg["on"]


# --------------------------------------------------------------- W3C traceparent id helpers
def new_span_id():
    """A random 8-byte span-id as 16 lowercase hex chars (W3C parent-id)."""
    return uuid.uuid4().hex[:16]


def new_trace_id(corr=None):
    """A 16-byte trace-id as 32 lowercase hex chars. DERIVED deterministically from the correlation
    id (md5 -> 16 bytes) so a trace-id and its corr id cross-reference — grep one, pivot to the
    other; random when no corr id is bound."""
    if corr:
        return hashlib.md5(corr.encode()).hexdigest()   # 32 hex == 16 bytes, stable per corr id
    return uuid.uuid4().hex


def format_traceparent(trace_id, span_id, sampled=True):
    """version-00 W3C traceparent: 00-<32hex trace-id>-<16hex span-id>-<2hex flags>."""
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(value):
    """Parse a W3C `traceparent`. Returns (trace_id, parent_span_id, sampled) or None when malformed
    (unknown version, wrong shape, non-hex, or the all-zero ids the spec forbids)."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if version != "00" or len(trace_id) != 32 or len(span_id) != 16:
        return None
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    try:
        int(trace_id, 16), int(span_id, 16)
        sampled = bool(int(flags, 16) & 0x01)
    except ValueError:
        return None
    return trace_id, span_id, sampled


# --------------------------------------------------------------- thread-local active span context
def set_trace_context(trace_id, span_id, sampled=True):
    _tls.trace_id, _tls.span_id, _tls.sampled = trace_id, span_id, sampled


def get_trace_context():
    """(trace_id, active_span_id, sampled) for this thread, or None if no span is active."""
    tid = getattr(_tls, "trace_id", None)
    if tid is None:
        return None
    return tid, getattr(_tls, "span_id", None), getattr(_tls, "sampled", True)


def clear_trace_context():
    _tls.trace_id = None
    _tls.span_id = None


# --------------------------------------------------------------- span object
class Span:
    __slots__ = ("trace_id", "span_id", "parent_span_id", "name", "kind",
                 "start_ns", "end_ns", "attributes", "status_code")

    def __init__(self, trace_id, span_id, parent_span_id, name, kind):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id or None
        self.name = name
        self.kind = kind
        self.start_ns = time.time_ns()
        self.end_ns = None
        self.attributes = {}
        self.status_code = 0   # OTLP status UNSET

    def set(self, **attrs):
        self.attributes.update(attrs)
        return self

    def end(self, ok=True, **attrs):
        """Close the span (idempotent) and hand it to the exporter/log sink. Never raises."""
        if self.end_ns is not None:
            return self
        self.attributes.update(attrs)
        self.end_ns = time.time_ns()
        self.status_code = 1 if ok else 2   # OK / ERROR
        try:
            _record_span(self)
        except Exception:
            pass   # recording a span must never break the NF
        return self


# --------------------------------------------------------------- entry points used by sbi_http
def begin_server_span(traceparent_value, name):
    """Open a SERVER span for the current handler thread and bind its context so synchronous
    southbound request() calls nest beneath it. Adopts an inbound W3C `traceparent` (continuing the
    upstream trace) or STARTS a fresh trace whose trace-id is derived from the bound corr id when
    none is present. Returns the Span (call .end() when the handler finishes) or None when tracing
    is off (the inert default). Never raises."""
    if not _trace_cfg["on"]:
        return None
    try:
        parsed = parse_traceparent(traceparent_value)
        if parsed:
            trace_id, parent_span_id, sampled = parsed
        else:
            trace_id, parent_span_id, sampled = new_trace_id(get_corr()), None, True
        span_id = new_span_id()
        set_trace_context(trace_id, span_id, sampled)
        return Span(trace_id, span_id, parent_span_id, name, _KIND_SERVER)
    except Exception:
        return None


def begin_client_span(name):
    """Open a CLIENT span for an outbound call. Returns (span, traceparent_header) or (None, None)
    when tracing is off. The span is a child of the thread's active SERVER span; when none is bound
    this call is the trace ROOT (trace-id derived from the corr id) — this is how a trace STARTS at
    an entry edge (ue_sim / BSS) that received no inbound traceparent. A client span is a leaf: it
    does NOT rebind the thread context, so sibling outbound calls all nest under the same parent.
    Never raises."""
    if not _trace_cfg["on"]:
        return None, None
    try:
        ctx = get_trace_context()
        if ctx:
            trace_id, parent_span_id, sampled = ctx
        else:
            trace_id, parent_span_id, sampled = new_trace_id(get_corr()), None, True
        span_id = new_span_id()
        span = Span(trace_id, span_id, parent_span_id, name, _KIND_CLIENT)
        return span, format_traceparent(trace_id, span_id, sampled)
    except Exception:
        return None, None


# --------------------------------------------------------------- record: export (OTLP) or log
def _record_span(span):
    if _trace_cfg["endpoint"]:
        _exporter().enqueue(span)
    elif _trace_cfg["log"]:
        log("span", kind=_KIND_NAME.get(span.kind, "internal"), span_name=span.name,
            trace_id=span.trace_id, span_id=span.span_id, parent_span_id=span.parent_span_id,
            duration_us=(span.end_ns - span.start_ns) // 1000, **span.attributes)


def flush_spans():
    """Force a synchronous export flush. Specs call this to make OTLP export deterministic."""
    e = _exporter_box["e"]
    if e is not None:
        e.flush()


# --------------------------------------------------------------- OTLP/HTTP-JSON exporter (stdlib)
def _traces_url(endpoint):
    ep = endpoint.rstrip("/")
    return ep if ep.endswith("/v1/traces") else ep + "/v1/traces"


def _exporter():
    e = _exporter_box["e"]
    if e is None:
        e = _OtlpExporter(_traces_url(_trace_cfg["endpoint"]))
        _exporter_box["e"] = e
    return e


def _otlp_attrs(d):
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": str(v)}
        out.append({"key": k, "value": val})
    return out


def _otlp_payload(spans, service_name):
    """Encode a batch of spans as an OTLP/HTTP-JSON ExportTraceServiceRequest (one resource, one
    scope). trace/span ids are base16 strings and times are string nanos, per the OTLP JSON mapping."""
    out_spans = []
    for s in spans:
        sp = {"traceId": s.trace_id, "spanId": s.span_id, "name": s.name, "kind": s.kind,
              "startTimeUnixNano": str(s.start_ns),
              "endTimeUnixNano": str(s.end_ns if s.end_ns is not None else s.start_ns),
              "attributes": _otlp_attrs(s.attributes), "status": {"code": s.status_code}}
        if s.parent_span_id:
            sp["parentSpanId"] = s.parent_span_id
        out_spans.append(sp)
    return {"resourceSpans": [{
        "resource": {"attributes": _otlp_attrs({"service.name": service_name})},
        "scopeSpans": [{"scope": {"name": "telco-lab.obs"}, "spans": out_spans}],
    }]}


class _OtlpExporter:
    """Batches finished spans and POSTs them OTLP/HTTP-JSON on a daemon thread. Best-effort: a
    down/slow collector is swallowed (spans dropped), never surfaced to the NF."""

    def __init__(self, url):
        self.url = url
        self._buf = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._service = nf_name()
        t = threading.Thread(target=self._run, name="obs-otlp", daemon=True)
        t.start()

    def enqueue(self, span):
        with self._lock:
            self._buf.append(span)
            n = len(self._buf)
        if n >= _BATCH_MAX:
            self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(timeout=_FLUSH_SECS)
            self._wake.clear()
            self.flush()

    def flush(self):
        with self._lock:
            batch, self._buf = self._buf, []
        if not batch:
            return
        try:
            data = json.dumps(_otlp_payload(batch, self._service)).encode()
            req = urllib.request.Request(self.url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=_EXPORT_TIMEOUT).close()
        except Exception:
            pass   # export is best-effort and must never affect the NF


# Flush any buffered spans on normal interpreter exit — the export thread is a daemon and would
# otherwise be killed mid-batch, so a short-lived entry edge (ue_sim, a spec) still delivers its
# root span. No-op when tracing is off (the exporter box is empty).
atexit.register(flush_spans)

reload_tracing_config()

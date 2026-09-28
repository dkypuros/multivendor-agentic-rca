"""
NWDAF: Network Data Analytics Function — the owned core's OWN analytics, the sensor the
P9 agents graduate to (issue #18).

The NWDAF is a standalone, READ-ONLY analytics consumer. It DISCOVERS the live core through the
NRF and READS the other NFs (their root index, /metrics, and inspect endpoints) to compute
DESCRIPTIVE analytics from live counts. It never mutates another NF — no policy, no session, no
provisioning is written from here. This is the network turning its own instrumentation into
first-class analytics the closed-loop agents can consume as a sensor instead of scraping raw
metrics themselves.

Spec anchors:
  Nnwdaf_AnalyticsInfo         TS 29.520 section 5.2 (AnalyticsInfo API): a consumer requests an
                               analytics report for an Analytics ID. Here:
    GET /nnwdaf-analyticsinfo/v1/analytics?analytics-id=<id>
      NF_LOAD              TS 23.288 6.5 — per-NF load/health, from the NRF registrations plus each
                          NF's reachability (root index) and /metrics scrape.
      NETWORK_SLICE_LOAD  TS 23.288 6.3 — per-S-NSSAI session counts, read from SMF /smf/sessions
                          and UPF /upf/sessions, grouped by sNssai.
      UE_MOBILITY         TS 23.288 6.7 — registered-UE view, read from the AMF UE contexts
                          (labeled read-scope simplification below).
  Nnwdaf_EventsSubscription    TS 29.520 section 5.3 (EventsSubscription API): a consumer subscribes
                               to an analytics event. Here (POST/GET/DELETE
                               /nnwdaf-eventssubscription/v1/subscriptions): the subscription is
                               RECORDED and answered with the CURRENT analytic (poll model). Real
                               NWDAF pushes Nnwdaf_EventsSubscription_Notify callbacks — labeled.
  Nnrf_NFManagement / NFDiscovery  TS 29.510 5.2 / 5.3 (register as NWDAF, discover the core NFs).
  NWDAF architecture           TS 23.288 sections 5 / 6 (analytics function, consumers, data
                               collection from NFs over the SBI).

Labeled simplifications (ledgered in procedures/nwdaf_analytics.txt):
  - DESCRIPTIVE analytics only: every number is a live count/read. There is NO ML — no
    inference, no prediction, no confidence. Real NWDAF (TS 23.288 6.x "statistics" vs
    "predictions") also produces predictions from a trained model; this NWDAF produces the
    statistics half honestly and leaves prediction as a follow-up (labeled per report).
  - POLL, not NOTIFY: an events subscription is recorded and returns the current analytic on
    create and on GET. AMF/SMF-driven Nnwdaf notifications are the follow-up.
  - UE_MOBILITY read-scope: the AMF exposes GET /amf/ue-contexts/{supi} but no bulk list (and the
    NWDAF edits no NF). So the registered-UE view is resolved over the set of SUPIs the NWDAF can
    SEE — the SUPIs carried on the SMF's live sessions — each probed against the AMF UE context.
    A UE that is registered but has no PDU session is outside this read scope; honestly labeled.
  - NF_LOAD "load" is a reachability + light-metric read, not a vendor KPI: up/down from the root
    index, plus any obs counters the NF exposes on /metrics. No fabricated CPU/latency.

A background sampler thread refreshes a cached snapshot of every analytic on an interval (like the
federation viewers' refresher) so reads are instant; a read falls back to computing live if the
cache is cold.

Run: python3 nwdaf.py   (SBI on 127.0.0.1:7016, registers with the NRF as NWDAF)
"""

import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import port, url
from domain.snssai import key as snssai_key
from domain.statestore import open_store

PORT = port("nwdaf")
NRF = url("nrf")
app = SbiApp("nwdaf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). Events subscriptions are keyed by subscriptionId.
store = open_store("nwdaf")
event_subs = store.collection("analytics_event_subscriptions")

# The Analytics IDs this NWDAF computes today (TS 23.288 6.x). Others are rejected honestly rather
# than faked (Nnwdaf uses UNSUPPORTED_ANALYTICS for an unknown id).
SUPPORTED_ANALYTICS = ("NF_LOAD", "NETWORK_SLICE_LOAD", "UE_MOBILITY")

# The SBI-registered NFs the NWDAF observes for NF_LOAD by NRF discovery. The probe is the NF's own
# root index "/" (every sbi_http NF answers it) — a reachability read, never a mutation.
DISCOVERED_NFS = ("UDM", "AUSF", "AMF", "SMF", "NEF", "PCF", "NSSF")

# NFs that are NOT SBI-registered in the NRF, so the NWDAF locates them by config (netconfig) — the
# lab instrumentation endpoints. Honest: the NRF is the discovery anchor (it does not register
# itself), and the UPF is selected by the SMF / programmed over PFCP, exposing only a lab inspect
# endpoint rather than a registered Nupf service. Each entry: (label, base URL).
CONFIG_NFS = (("NRF", url("nrf")), ("UPF", url("upf")))

# Sampler cadence — the cache refresh interval (seconds). Kept modest so a read is near-instant.
SAMPLE_INTERVAL = 2.0

_cache = {}          # analyticsId -> report dict
_cache_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def discover_all(nf_type):
    """NRF discovery returning EVERY registered base URL for an NF type (NF_LOAD wants them all,
    not just the first). Same profile shape the other NFs register with. Missing NF -> []."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type=NWDAF")
    except OSError:
        return []
    if status != 200:
        return []
    bases = []
    for inst in body.get("nfInstances", []):
        for svc in inst.get("nfServices", []):
            for ep in svc.get("ipEndPoints", []):
                base = f"http://{ep['ipv4Address']}:{ep['port']}"
                if base not in bases:
                    bases.append(base)
    return bases


def discover(nf_type):
    """First registered base URL for an NF type, or None."""
    bases = discover_all(nf_type)
    return bases[0] if bases else None


def locate_upf():
    """The UPF is not SBI-registered (the SMF selects it and programs it over PFCP); the NWDAF
    reads its lab inspect endpoint located by config (netconfig url('upf'))."""
    return url("upf")


def _parse_metrics(text):
    """Parse the Prometheus text exposition (TS-agnostic, obs.render_metrics) into {name: value}
    for the simple, unlabeled counter/gauge lines. Labeled series are summed under their base name
    so NF_LOAD can surface a coarse activity number without pretending to understand every label."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            metric, value = line.rsplit(" ", 1)
            value = float(value)
        except ValueError:
            continue
        base = metric.split("{", 1)[0]
        out[base] = out.get(base, 0.0) + value
    return out


def _probe(base):
    """Reachability + light metrics read of one NF base URL. Returns (up, metrics_dict)."""
    up = False
    metrics = {}
    try:
        status, _ = request("GET", base + "/")
        up = status == 200
    except OSError:
        return False, {}
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/metrics", timeout=3) as resp:
            if resp.status == 200:
                metrics = _parse_metrics(resp.read().decode("utf-8", "replace"))
    except OSError:
        pass
    return up, metrics


def _load_entry(nf_type, bases, located_by):
    """Reachability + light-metrics roll-up over one NF type's base URLs -> an NF_LOAD entry."""
    instances = []
    for base in bases:
        up, metrics = _probe(base)
        # A coarse activity signal: the NF's own request/session counters, if it exposes any.
        activity = sum(v for k, v in metrics.items()
                       if k.endswith("_total") or k.endswith("_active"))
        instances.append({"base": base, "up": up, "activity": activity,
                          "metricNames": sorted(metrics.keys())})
    reachable = sum(1 for i in instances if i["up"])
    return {"nfType": nf_type, "locatedBy": located_by, "up": reachable > 0,
            "instances": len(instances), "reachable": reachable,
            # Descriptive "load": reachable fraction (0..1). No prediction.
            "load": round(reachable / len(instances), 3) if instances else None,
            "detail": instances}


def analytics_nf_load():
    """NF_LOAD (TS 23.288 6.5): per-NF load/health from the NRF registrations + each NF's
    reachability and /metrics. 'load' here is reachability + a light activity read, honestly
    scoped (no fabricated CPU/latency). SBI-registered NFs are located by NRF discovery; the NRF
    itself and the UPF are located by config (they are not registered in the NRF)."""
    nfs = []
    for label, base in CONFIG_NFS:
        nfs.append(_load_entry(label, [base], "config"))
    for nf_type in DISCOVERED_NFS:
        bases = discover_all(nf_type)
        if not bases:
            nfs.append({"nfType": nf_type, "locatedBy": "nrf-discovery",
                        "registered": False, "up": False, "instances": 0, "load": None,
                        "note": "not registered in the NRF"})
        else:
            entry = _load_entry(nf_type, bases, "nrf-discovery")
            entry["registered"] = True
            nfs.append(entry)
    up = [n for n in nfs if n["up"]]
    return {"analyticsId": "NF_LOAD", "eventTime": now_iso(),
            "analyticsType": "STATISTICS",
            "nfCount": len(nfs), "nfUp": len(up),
            "nfLoad": nfs,
            "note": "descriptive: reachability + live counters, no ML prediction; the NRF and "
                    "UPF are located by config (not SBI-registered)"}


def analytics_slice_load():
    """NETWORK_SLICE_LOAD (TS 23.288 6.3): per-S-NSSAI session counts, read from the SMF (control
    view) and the UPF (user-plane view), grouped by sNssai key."""
    smf = discover("SMF")
    upf = locate_upf()
    by_slice = {}   # snssaiKey -> {sNssai, smfSessions, upfSessions, ulBytes, dlBytes}

    def slot(snssai):
        k = snssai_key(snssai)
        return by_slice.setdefault(k, {"sNssai": snssai, "snssaiKey": k,
                                       "smfSessions": 0, "upfSessions": 0,
                                       "ulBytes": 0, "dlBytes": 0})

    if smf is not None:
        try:
            status, body = request("GET", smf + "/smf/sessions")
            if status == 200:
                for s in body.get("sessions", []):
                    slot(s.get("sNssai") or {"sst": 1})["smfSessions"] += 1
        except OSError:
            pass
    upf_ok = False
    try:
        status, body = request("GET", upf + "/upf/sessions")
        if status == 200:
            upf_ok = True
            for s in body.get("sessions", []):
                entry = slot(s.get("sNssai") or {"sst": 1})
                entry["upfSessions"] += 1
                counters = s.get("counters") or {}
                entry["ulBytes"] += counters.get("ulBytes", 0)
                entry["dlBytes"] += counters.get("dlBytes", 0)
    except OSError:
        pass

    slices = sorted(by_slice.values(), key=lambda e: e["snssaiKey"])
    return {"analyticsId": "NETWORK_SLICE_LOAD", "eventTime": now_iso(),
            "analyticsType": "STATISTICS",
            "smfReachable": smf is not None, "upfReachable": upf_ok,
            "sliceCount": len(slices),
            "totalSessions": sum(e["smfSessions"] for e in slices),
            "sliceLoad": slices,
            "note": "descriptive: live session counts grouped by S-NSSAI, no ML prediction"}


def analytics_ue_mobility():
    """UE_MOBILITY (TS 23.288 6.7): registered-UE view read from the AMF UE contexts. Read scope
    is the SUPIs the NWDAF can SEE — those carried on the SMF's live sessions — each probed against
    the AMF (the AMF exposes no bulk list and the NWDAF edits no NF). Labeled simplification."""
    smf = discover("SMF")
    amf = discover("AMF")
    supis = []
    if smf is not None:
        try:
            status, body = request("GET", smf + "/smf/sessions")
            if status == 200:
                for s in body.get("sessions", []):
                    supi = s.get("supi")
                    if supi and supi not in supis:
                        supis.append(supi)
        except OSError:
            pass
    ues = []
    registered = 0
    for supi in supis:
        state = "UNKNOWN"
        guami = None
        if amf is not None:
            try:
                status, ctx = request("GET", amf + f"/amf/ue-contexts/{supi}")
                if status == 200:
                    state = ctx.get("state", "UNKNOWN")
                    guami = ctx.get("guami")
                elif status == 404:
                    state = "DEREGISTERED"
            except OSError:
                pass
        if state == "REGISTERED":
            registered += 1
        ues.append({"supi": supi, "rmState": state, "guami": guami})
    return {"analyticsId": "UE_MOBILITY", "eventTime": now_iso(),
            "analyticsType": "STATISTICS",
            "amfReachable": amf is not None,
            "observedUeCount": len(ues), "registeredUeCount": registered,
            "ues": ues,
            "readScope": "SUPIs seen on SMF sessions, probed against AMF UE contexts",
            "note": "descriptive: live AMF UE-context reads, no ML prediction; registered-but-"
                    "session-less UEs are outside the read scope (AMF has no bulk list)"}


ANALYTICS = {
    "NF_LOAD": analytics_nf_load,
    "NETWORK_SLICE_LOAD": analytics_slice_load,
    "UE_MOBILITY": analytics_ue_mobility,
}


def compute(analytics_id):
    """Compute one analytic live (used by the sampler and as a cache-miss fallback)."""
    fn = ANALYTICS.get(analytics_id)
    return fn() if fn else None


def read_analytic(analytics_id):
    """Serve an analytic from the cache when warm, else compute it live and warm the cache."""
    with _cache_lock:
        cached = _cache.get(analytics_id)
    if cached is not None:
        return cached
    report = compute(analytics_id)
    if report is not None:
        with _cache_lock:
            _cache[analytics_id] = report
    return report


# --------------------------------------------------- Nnwdaf_AnalyticsInfo (TS 29.520 5.2)

@app.route("GET", "/nnwdaf-analyticsinfo/v1/analytics")
def get_analytics(params, query, body):
    analytics_id = query.get("analytics-id") or query.get("analyticsId")
    if not analytics_id:
        return problem(400, "Bad Request", detail="analytics-id query parameter is mandatory",
                       cause="MANDATORY_QUERY_PARAM_MISSING")
    if analytics_id not in ANALYTICS:
        return problem(404, "Not Found",
                       detail=f"analytics-id {analytics_id} is not one of "
                              f"{', '.join(SUPPORTED_ANALYTICS)}",
                       cause="UNSUPPORTED_ANALYTICS")
    report = read_analytic(analytics_id)
    obs.log("analytics_served", analyticsId=analytics_id)
    obs.counter("nwdaf_analytics_reads_total", analyticsId=analytics_id).inc()
    return 200, report


# ------------------------------------------------ Nnwdaf_EventsSubscription (TS 29.520 5.3)
# A consumer subscribes to an analytics event; the NWDAF RECORDS the subscription and answers with
# the CURRENT analytic (poll model). Real NWDAF pushes Notify callbacks — labeled follow-up.

@app.route("POST", "/nnwdaf-eventssubscription/v1/subscriptions")
def create_event_subscription(params, query, body):
    # An NnwdafEventsSubscription carries eventSubscriptions[].event (the Analytics ID here).
    events = body.get("eventSubscriptions") or []
    analytics_id = body.get("analyticsId")
    if not analytics_id and events:
        analytics_id = events[0].get("event") or events[0].get("analyticsId")
    if not analytics_id:
        return problem(400, "Bad Request",
                       detail="an analyticsId (or eventSubscriptions[].event) is mandatory",
                       cause="MANDATORY_IE_MISSING")
    if analytics_id not in ANALYTICS:
        return problem(404, "Not Found",
                       detail=f"analytics-id {analytics_id} is not supported",
                       cause="UNSUPPORTED_ANALYTICS")
    sub_id = uuid.uuid4().hex
    report = read_analytic(analytics_id)
    record = {"subscriptionId": sub_id, "analyticsId": analytics_id,
              "notificationURI": body.get("notificationURI"),
              "self": f"/nnwdaf-eventssubscription/v1/subscriptions/{sub_id}",
              "createdAt": now_iso(),
              # Honesty marker on the resource: recorded, poll-served, not push-notified.
              "deliveryModel": "POLL_NOT_NOTIFY",
              "analyticsReport": report}
    event_subs.put(sub_id, record)
    obs.log("analytics_subscription_created", subscriptionId=sub_id, analyticsId=analytics_id)
    obs.counter("nwdaf_analytics_subscriptions_total").inc()
    return 201, record


@app.route("GET", "/nnwdaf-eventssubscription/v1/subscriptions/{subscriptionId}")
def get_event_subscription(params, query, body):
    record = event_subs.get(params["subscriptionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such analytics subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    # Poll: refresh the analytic against the live core so a GET reflects the current network.
    report = read_analytic(record["analyticsId"])
    if report is not None:
        record["analyticsReport"] = report
        event_subs.put(record["subscriptionId"], record)
    return 200, record


@app.route("GET", "/nnwdaf-eventssubscription/v1/subscriptions")
def list_event_subscriptions(params, query, body):
    return 200, {"subscriptions": list(event_subs.values())}


@app.route("DELETE", "/nnwdaf-eventssubscription/v1/subscriptions/{subscriptionId}")
def delete_event_subscription(params, query, body):
    record = event_subs.get(params["subscriptionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such analytics subscription",
                       cause="SUBSCRIPTION_NOT_FOUND")
    event_subs.delete(params["subscriptionId"])
    obs.log("analytics_subscription_deleted", subscriptionId=params["subscriptionId"])
    obs.counter("nwdaf_analytics_subscriptions_deleted_total").inc()
    return 204, {}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. Both Nnwdaf service names are advertised so a consumer
    # (a P9 agent, the NEF, an operator console) discovering the NWDAF finds its analytics surfaces.
    profile = {"nfType": "NWDAF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nnwdaf-analyticsinfo",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "nnwdaf-eventssubscription",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _sampler():
    """Background refresher (like the federation viewers'): recompute every analytic on an interval
    and warm the cache so reads are instant. Read-only — it only observes the core."""
    while True:
        for analytics_id in ANALYTICS:
            try:
                report = compute(analytics_id)
                if report is not None:
                    with _cache_lock:
                        _cache[analytics_id] = report
            except Exception as exc:  # noqa: BLE001 — a flaky NF must not kill the sampler
                obs.log("sampler_error", analyticsId=analytics_id, error=str(exc))
        time.sleep(SAMPLE_INTERVAL)


def _scrape():
    obs.gauge("nwdaf_analytics_subscriptions_active").set(len(list(event_subs.values())))
    with _cache_lock:
        nf_load = _cache.get("NF_LOAD")
        slice_load = _cache.get("NETWORK_SLICE_LOAD")
    if nf_load:
        obs.gauge("nwdaf_observed_nf_up").set(nf_load.get("nfUp", 0))
    if slice_load:
        obs.gauge("nwdaf_slice_sessions_total").set(slice_load.get("totalSessions", 0))


if __name__ == "__main__":
    obs.init("nwdaf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    threading.Thread(target=_sampler, daemon=True).start()
    serve(app, PORT)

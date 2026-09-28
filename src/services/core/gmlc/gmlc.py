"""
GMLC: Gateway Mobile Location Centre — the external entry point for Location Services (LCS).

The GMLC is the front door an EXTERNAL LCS client (an emergency service, a lawful-intercept
gateway, a location-based application) uses to ask "where is this UE?". It sits at the Le
reference point facing the external LCS client, applies the subscriber's LCS privacy /
authorization, and then drives the positioning: in a full network it hands the request to the
serving AMF (Namf_Location ProvideLocation), which invokes the LMF (Nlmf_Location
DetermineLocation) to compute the estimate. Here the GMLC DISCOVERS the AMF and the LMF through
the NRF and READS them — it is a standalone northbound reader; no other NF is edited.

LCS POSITIONING PATH (this wave — the standards-correct MT-LR routing, TS 23.273; ledgered in
procedures/gmlc_amf_location.txt). After the privacy check the GMLC PREFERS the standards LCS
path: it discovers the serving AMF and requests the location THROUGH the AMF (Namf_Location
ProvideLocationInfo), and the AMF invokes the LMF (Nlmf_Location DetermineLocation). This is
GATED + FALLBACK, additive over the earlier direct-LMF behavior:
  1. VIA THE AMF (preferred): a discoverable AMF that can SERVE the location (its
     /amf/ue-contexts/{supi}/location returns 200) yields the estimate — GMLC -> serving AMF
     -> LMF, the TS 23.273 flow. Metric gmlc_location_via_amf_total{path="via_amf"}.
  2. DIRECT LMF (fallback): when the AMF path is UNAVAILABLE (no AMF discoverable, no AMF
     location endpoint, no UE context, or no LMF behind the AMF) the GMLC falls back to calling
     the LMF's Nlmf_Location DetermineLocation directly — the EXACT earlier behavior, byte-
     identical, so a stack with no AMF is unchanged. Metric {path="direct_lmf"}.
  3. UNAVAILABLE (honest): when NEITHER path can serve the location the report is marked
     LMF_UNAVAILABLE with a null estimate — never a fabricated position. Metric {path="unavailable"}.

Spec anchors:
  Ngmlc_Location / Le (LCS)          TS 23.273 (5GC LCS stage 2) — the GMLC is the LCS entry
                                     point; it performs the LCS privacy check for the target UE
                                     and routes the location request to the serving AMF.
  Namf_Location ProvideLocationInfo  TS 23.273 section 6 / TS 29.518 — the serving AMF fields the
                                     GMLC's location request and invokes the LMF (the MT-LR leg
                                     the GMLC now prefers over a direct LMF call).
  Nlmf_Location DetermineLocation    TS 29.572 section 5.2 — the LMF computes the location
                                     estimate (positioning); the GMLC obtains it via the AMF, or
                                     directly on the fallback path.
  GMLC / LCS architecture            TS 23.271 (LCS functional description, carried into 5GC by
                                     TS 23.273); TS 29.515 is the Ngmlc/Nlmf-adjacent LCS API set.
  Nnrf_NFManagement / NFDiscovery    TS 29.510 sections 5.2 / 5.3 (register as GMLC, discover
                                     AMF and LMF).

Labeled simplifications (ledgered in procedures/gmlc_amf_location.txt):
  - LCS PRIVACY / AUTHORIZATION is MODELED: lcs_privacy() decides ALLOW/DENY from a modeled
    per-subscriber table (TELCO_GMLC_LCS_DENY deny-list + TELCO_GMLC_LCS_DEFAULT default),
    labeled {"modeled": true} on every report. A real GMLC evaluates the subscriber's LCS
    privacy profile from the UDM (Nudm_SDM lcs-privacy-data, TS 29.503) against the LCS client
    type. The decision seam is real; the data source is modeled.
  - AMF-MEDIATED LMF INVOCATION is now the PREFERRED path (GMLC -> serving AMF -> LMF) with an
    exact DIRECT-LMF fallback: when no AMF can serve the location the GMLC still calls the LMF's
    Nlmf DetermineLocation directly, the byte-identical earlier behavior. When neither path can
    reach an LMF the report is honestly marked LMF_UNAVAILABLE with a null estimate.
  - External LCS client authentication is not enforced (no Le-side credential check yet).

Run: python3 gmlc.py   (SBI on 127.0.0.1:7029, registers with the NRF as GMLC)
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import port, url, value
from domain.statestore import open_store

PORT = port("gmlc")
NRF = url("nrf")
app = SbiApp("gmlc")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). Every served location report is retained under a reportId so the Le
# client can fetch it back.
store = open_store("gmlc")
reports = store.collection("location_reports")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _deny_set():
    """Modeled LCS deny-list: SUPIs whose location privacy denies external LCS requests.
    Sourced from TELCO_GMLC_LCS_DENY (comma-separated), the modeled stand-in for a real
    Nudm_SDM lcs-privacy-data read."""
    raw = value("TELCO_GMLC_LCS_DENY", "") or ""
    return {s.strip() for s in raw.split(",") if s.strip()}


def lcs_privacy(supi, lcs_client_type):
    """LCS privacy / authorization decision (MODELED — labeled simplification).

    Returns (allowed: bool, decision: dict). A real GMLC evaluates the subscriber's LCS privacy
    class (TS 23.273 / TS 23.271 privacy) from the UDM against the LCS client type; here the
    decision comes from a modeled per-subscriber deny-list with an operator default. The decision
    is carried on the report labeled modeled=true so the honesty is visible to the caller."""
    default = (value("TELCO_GMLC_LCS_DEFAULT", "ALLOW") or "ALLOW").upper()
    if supi in _deny_set():
        result = "DENIED"
    else:
        result = "ALLOWED" if default == "ALLOW" else "DENIED"
    return result == "ALLOWED", {
        "result": result,
        "lcsClientType": lcs_client_type,
        "modeled": True,
        "basis": "modeled per-subscriber LCS privacy (TELCO_GMLC_LCS_DENY / _DEFAULT)",
    }


def discover(nf_type, requester="GMLC"):
    """NRF discovery, same shape as services/core/nef/nef.py — returns a base URL or None so a
    missing peer degrades to an honest marker in the report rather than crashing the GMLC."""
    try:
        status, body = request("GET", f"{NRF}/nnrf-disc/v1/nf-instances"
                                      f"?target-nf-type={nf_type}&requester-nf-type={requester}")
    except OSError:
        return None
    instances = body.get("nfInstances", []) if status == 200 else []
    if not instances:
        return None
    endpoint = instances[0]["nfServices"][0]["ipEndPoints"][0]
    return f"http://{endpoint['ipv4Address']}:{endpoint['port']}"


def serving_amf(supi):
    """Identify the serving AMF for enrichment (TS 23.273: the GMLC routes the location request
    to the serving AMF). Reads the AMF UE context if an AMF is discoverable. Returns a dict or
    None — the AMF is optional here because the LMF is called directly (labeled simplification)."""
    amf = discover("AMF")
    if amf is None:
        return None
    try:
        status, ctx = request("GET", f"{amf}/amf/ue-contexts/{supi}")
    except OSError:
        return None
    if status != 200:
        return {"amf": amf, "registered": False, "rmState": "DEREGISTERED"}
    return {"amf": amf, "registered": ctx.get("state") == "REGISTERED",
            "rmState": ctx.get("state", "UNKNOWN"), "guami": ctx.get("guami")}


def determine_location(supi, amf_info, lcs_qos):
    """Obtain the location estimate. In the full flow this is GMLC -> AMF ProvideLocation -> LMF
    DetermineLocation; here the GMLC calls the LMF's Nlmf_Location DetermineLocation directly
    (labeled). Returns (estimate_dict_or_None, lmf_status)."""
    lmf = discover("LMF")
    if lmf is None:
        return None, "LMF_UNAVAILABLE"
    payload = {"supi": supi, "lcsQos": lcs_qos,
               "servingGuami": (amf_info or {}).get("guami")}
    try:
        status, est = request("POST", f"{lmf}/nlmf-loc/v1/determine-location", payload)
    except OSError:
        return None, "LMF_UNAVAILABLE"
    if status != 200:
        return None, "LMF_ERROR"
    # Surface the LMF's LocationData: the estimate itself plus the positioning metadata the Le
    # client cares about (TS 29.572 LocationData).
    estimate = {
        "locationEstimate": est.get("locationEstimate"),
        "accuracyFulfilled": est.get("accuracyFulfilled"),
        "ageOfLocationEstimate": est.get("ageOfLocationEstimate"),
        "positioningMethod": est.get("positioningMethod"),
    }
    return estimate, "SUCCESS"


def location_via_amf(supi, lcs_qos):
    """Standards LCS positioning path (TS 23.273): request the location THROUGH the serving AMF
    (Namf_Location ProvideLocationInfo), which invokes the LMF (Nlmf_Location DetermineLocation).
    The GMLC discovers the AMF and GETs its /amf/ue-contexts/{supi}/location; the AMF is the node
    that reaches the LMF. Returns (estimate_dict_or_None, status): "SUCCESS" when the AMF served
    the estimate, else "AMF_UNAVAILABLE" so the caller FALLS BACK to the direct-LMF call. Every
    failure mode — no AMF discoverable, no location endpoint, no UE context, no LMF behind the
    AMF, a transport error — degrades to AMF_UNAVAILABLE, never to a fabricated position."""
    amf = discover("AMF")
    if amf is None:
        return None, "AMF_UNAVAILABLE"                     # no serving AMF -> fall back
    path = f"{amf}/amf/ue-contexts/{supi}/location"
    ha = (lcs_qos or {}).get("horizontalAccuracy")
    if ha is not None:
        path += f"?horizontalAccuracy={ha}"                # forward the LocationQoS to steer method
    try:
        status, loc = request("GET", path)
    except OSError:
        return None, "AMF_UNAVAILABLE"                     # AMF unreachable -> fall back
    if status != 200:
        # 404 LMF_NOT_AVAILABLE / UE_CONTEXT_NOT_FOUND, 502 LMF_UNREACHABLE, or an AMF with no
        # location endpoint at all -> the AMF path cannot serve this location -> fall back.
        return None, "AMF_UNAVAILABLE"
    # Surface the AMF's Namf location result as the Le report's LocationData. The AMF returns the
    # GAD estimate (latitude/longitude/altitude) plus the positioning metadata; carry it whole.
    estimate = {
        "locationEstimate": loc.get("locationEstimate"),
        "accuracyFulfilled": None,                         # AMF surfaces achieved accuracy, not a bool
        "accuracy": loc.get("accuracy"),
        "ageOfLocationEstimate": loc.get("ageOfLocationEstimate"),
        "positioningMethod": loc.get("positioningMethod"),
        "servingCell": loc.get("servingCell"),
        "locationSource": loc.get("locationSource", "LMF"),
    }
    return estimate, "SUCCESS"


# ------------------------------------------------- Location Services (Ngmlc_Location / Le)
# TS 23.273: an external LCS client asks the GMLC for a target UE's location. The GMLC applies
# the subscriber's LCS privacy, then drives positioning — PREFERRING the standards MT-LR route
# through the serving AMF (which invokes the LMF), with an exact direct-LMF fallback — and
# returns the location report.

@app.route("POST", "/ngmlc-loc/v1/{supi}/location-report-request")
def location_report_request(params, query, body):
    supi = params["supi"]
    lcs_client_id = body.get("lcsClientId", "unknown-lcs-client")
    lcs_client_type = body.get("lcsClientType", "EMERGENCY_SERVICES")
    location_type = body.get("locationType", "CURRENT_LOCATION")
    lcs_qos = body.get("lcsQos", {"horizontalAccuracy": 100})

    # 1) LCS privacy / authorization (MODELED) — the GMLC's defining act.
    allowed, decision = lcs_privacy(supi, lcs_client_type)
    obs.log("lcs_location_request", supi=supi, lcsClientId=lcs_client_id,
            lcsClientType=lcs_client_type, privacy=decision["result"])
    obs.counter("gmlc_location_requests_total").inc()
    if not allowed:
        obs.counter("gmlc_location_privacy_denied_total").inc()
        return problem(403, "Forbidden",
                       detail=f"LCS privacy denies location disclosure for {supi}",
                       cause="LCS_PRIVACY_DENIED")

    # 2) serving AMF (enrichment — identifies the serving node either way).
    amf_info = serving_amf(supi)

    # 3) positioning. PREFER the standards LCS path (TS 23.273): request the location THROUGH the
    #    serving AMF (Namf_Location ProvideLocationInfo), which invokes the LMF. FALL BACK to the
    #    direct LMF call (the exact earlier behavior) when the AMF path is unavailable, and to an
    #    honest LMF_UNAVAILABLE when neither can serve it. The estimate is never fabricated.
    estimate, amf_status = location_via_amf(supi, lcs_qos)
    if amf_status == "SUCCESS":
        lmf_status = "SUCCESS"
        location_path = "SERVING_AMF"              # GMLC -> serving AMF -> LMF (TS 23.273)
        obs.counter("gmlc_location_via_amf_total", path="via_amf").inc()
    else:
        # FALLBACK: byte-identical to the pre-integration direct-LMF path.
        estimate, lmf_status = determine_location(supi, amf_info, lcs_qos)
        if lmf_status == "SUCCESS":
            location_path = "DIRECT_LMF"           # fallback: GMLC -> LMF (labeled simplification)
            obs.counter("gmlc_location_via_amf_total", path="direct_lmf").inc()
        else:
            location_path = "UNAVAILABLE"          # neither path reached an LMF (honest)
            obs.counter("gmlc_location_via_amf_total", path="unavailable").inc()

    report_id = uuid.uuid4().hex
    report = {
        "reportId": report_id,
        "supi": supi,
        "lcsClientId": lcs_client_id,
        "locationType": location_type,
        "eventTime": now_iso(),
        "lcsPrivacyCheck": decision,               # ALLOWED (modeled)
        "servingAmf": amf_info,                     # None when no AMF discoverable (direct-LMF)
        "locationPath": location_path,              # SERVING_AMF | DIRECT_LMF | UNAVAILABLE
        "lmfStatus": lmf_status,                    # SUCCESS | LMF_UNAVAILABLE | LMF_ERROR
        "locationEstimate": estimate,               # None when no LMF is reachable (honest)
        "reportStatus": "SUCCESS" if lmf_status == "SUCCESS" else lmf_status,
        "self": f"/ngmlc-loc/v1/{supi}/location-reports/{report_id}",
    }
    if lmf_status != "SUCCESS":
        report["detail"] = ("GMLC could not discover an LMF in the NRF; the location cannot be "
                            "determined. The privacy check succeeded but no estimate is returned "
                            "(honest — no fabricated position).")
        obs.counter("gmlc_location_lmf_unavailable_total").inc()
    else:
        obs.counter("gmlc_location_reports_total").inc()
    reports.put(report_id, report)
    return 200, report


@app.route("GET", "/ngmlc-loc/v1/{supi}/location-reports/{reportId}")
def get_location_report(params, query, body):
    report = reports.get(params["reportId"])
    if report is None or report.get("supi") != params["supi"]:
        return problem(404, "Not Found", detail="no such location report",
                       cause="REPORT_NOT_FOUND")
    return 200, report


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. The Ngmlc location service is advertised so an external
    # LCS gateway (or the NEF LCS surface) discovering the GMLC finds its Le entry point.
    profile = {"nfType": "GMLC", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "ngmlc-loc",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("gmlc_location_reports_stored").set(len(list(reports.values())))


if __name__ == "__main__":
    obs.init("gmlc")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

"""
LMF: Location Management Function — the owned core's positioning engine.

The LMF is the NF that answers "where is this UE?". In 5G positioning (TS 23.273) it is the
sink of a location request: the GMLC/AMF hands it a target UE and a requested QoS (how accurate,
how fast), the LMF gathers measurements / assistance data from the RAN and UE, computes a
position estimate, and returns it. Its service API is Nlmf_Location (TS 29.572): the
DetermineLocation operation takes an InputData (target UE + LocationQoS) and returns a
LocationData (a geographic estimate + accuracy + the positioning method that produced it).

This is a STANDALONE positioning engine: no other NF is edited. The AMF is the LMF's natural
client (Namf -> Nlmf on an external MT-LR / NI-LR location request); wiring the AMF to invoke
Nlmf is the ledgered integration follow-up (procedures/lmf_location.txt).

Spec anchors:
  Nlmf_Location DetermineLocation   TS 29.572 section 5.2 (Nlmf_Location service, DetermineLocation
                                    operation): POST .../determine-location with InputData
                                    {externalClientType?, supi/gpsi, locationQoS, ...} -> LocationData
                                    {locationEstimate (GAD shape), accuracy, ageOfLocationEstimate,
                                    positioningMethod/positioningDataList}.
  5G positioning architecture       TS 23.273 sections 4/5 — LMF role, positioning methods
                                    (Cell ID, E-CID, DL-TDOA/OTDOA, Multi-RTT), LCS QoS.
  GAD shapes                        TS 23.032 (universal geographical area description) — the
                                    locationEstimate is modeled here as {latitude, longitude,
                                    altitude} (ellipsoid point with altitude), the common GAD shape.
  Nnrf_NFManagement / NFDiscovery   TS 29.510 sections 5.2 / 5.3 (register as LMF, be discoverable).

Modeled position source (labeled simplification, ledgered in procedures/lmf_location.txt):
  A real LMF derives position from REAL measurements: RAN E-CID reports, UE/gNB RSTD for DL-TDOA,
  round-trip time for Multi-RTT, plus assistance data. This clean-room LMF has NONE of that RAN
  telemetry, so it models the position source as a small deterministic CELL GRID: each served UE
  is mapped (by a stable hash of its SUPI — standing in for "the RAN told the LMF the serving
  cell") to one modeled cell (NCGI + known latitude/longitude), and the estimate is that cell's
  position with a modeled sub-cell offset. The positioning METHOD and ACCURACY are chosen to honor
  the requested LocationQoS: a tighter horizontalAccuracy asks for a better method (CELL_ID ->
  E_CID -> OTDOA), each with a modeled typical accuracy. This is a labeled drawing of the position
  source, not real measurements — the swap path is: feed real RAN/UE measurements or federate to
  Duranta's radio, behind this same Nlmf contract.

  A target that the LMF does not serve (not a SUPI of the served PLMN, or no target at all) is
  rejected with an honest error rather than a fabricated fix.

Run: python3 lmf.py   (SBI on 127.0.0.1:7028, registers with the NRF as LMF)
"""

import hashlib
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import plmn, port, url
from domain.statestore import open_store

PORT = port("lmf")
NRF = url("nrf")
_MCC, _MNC = plmn()[:3], plmn()[3:]
SERVED_SUPI_PREFIX = "imsi-" + plmn()          # UEs of the served PLMN, e.g. imsi-00101...
app = SbiApp("lmf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). Each determine-location result is retained under a locationId so the
# GAD estimate is retrievable (TS 29.572 the LocationData is the response resource).
store = open_store("lmf")
locations = store.collection("determined_locations")

# --- Modeled cell grid (labeled position source). A handful of gNB cells with KNOWN positions,
# arranged around a reference point (a lab campus). A real LMF would learn the serving cell and
# per-cell geometry from the RAN; here the grid is the modeled stand-in. NCGI = NR Cell Global
# Identity (TS 23.003), radius is the modeled Cell-ID coverage radius in metres.
REF_LAT, REF_LON = 37.7749, -122.4194
MODELED_CELLS = [
    {"ncgi": f"{plmn()}-0x1A2C001", "latitude": REF_LAT + 0.0000, "longitude": REF_LON + 0.0000,
     "altitude": 32.0, "radiusM": 520.0},
    {"ncgi": f"{plmn()}-0x1A2C002", "latitude": REF_LAT + 0.0090, "longitude": REF_LON + 0.0035,
     "altitude": 41.0, "radiusM": 480.0},
    {"ncgi": f"{plmn()}-0x1A2C003", "latitude": REF_LAT - 0.0072, "longitude": REF_LON + 0.0110,
     "altitude": 27.0, "radiusM": 560.0},
    {"ncgi": f"{plmn()}-0x1A2C004", "latitude": REF_LAT - 0.0041, "longitude": REF_LON - 0.0086,
     "altitude": 35.0, "radiusM": 500.0},
]

# --- Positioning methods the modeled LMF can offer, coarsest first, each with a modeled typical
# horizontal accuracy in metres (TS 23.273 relative fidelity: Cell-ID coarse, E-CID better,
# DL-TDOA/OTDOA best). LocationQoS selection picks the coarsest method that still meets the
# requested horizontalAccuracy, so a tighter QoS request yields a better method (and lower age).
POS_METHODS = [
    {"method": "CELL_ID", "accuracyM": 520.0, "ageS": 30},   # serving-cell centroid
    {"method": "E_CID",   "accuracyM": 150.0, "ageS": 8},    # enhanced cell-id (TA/AoA modeled)
    {"method": "OTDOA",   "accuracyM": 32.0,  "ageS": 2},    # DL-TDOA / downlink RSTD
]
DEFAULT_METHOD = POS_METHODS[1]   # E_CID when the client states no QoS


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hash_int(text):
    """Stable, unsalted hash so a SUPI maps to the SAME modeled cell/offset across processes and
    runs (Python's built-in hash() is per-process salted, unusable for a modeled source)."""
    return int(hashlib.sha1(text.encode()).hexdigest(), 16)


def target_supi(body):
    """Resolve the InputData target UE to a SUPI. 'supi' is used as-is; 'gpsi' is an identity
    stub for a Nudm_SDM id-translation (labeled, same convention as the NEF). Returns SUPI or None."""
    if body.get("supi"):
        return str(body["supi"])
    if body.get("gpsi"):
        return str(body["gpsi"])
    return None


def requested_accuracy(body):
    """Requested horizontal accuracy (metres) from the LocationQoS, if any. TS 29.572 LocationQoS
    carries horizontalAccuracy; we also accept a flat 'horizontalAccuracy'. None -> no QoS stated."""
    qos = body.get("locationQoS") or body.get("requestedQos") or {}
    val = qos.get("horizontalAccuracy", body.get("horizontalAccuracy"))
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def select_method(req_accuracy):
    """Choose a positioning method honoring the requested QoS (TS 23.273 LCS QoS). Pick the
    COARSEST method that still meets the requested horizontalAccuracy; if none is good enough,
    fall back to the BEST modeled method (best-effort) and flag it; no QoS -> the default."""
    if req_accuracy is None:
        return DEFAULT_METHOD, False
    for m in POS_METHODS:                       # coarsest-first: cheapest method that qualifies
        if m["accuracyM"] <= req_accuracy:
            return m, True
    return POS_METHODS[-1], False               # requested tighter than we can model: best-effort


def serving_cell(supi):
    """Modeled serving-cell resolution: a stable hash of the SUPI selects one modeled cell. This
    stands in for the RAN reporting the UE's serving cell — LABELED, not a real measurement."""
    return MODELED_CELLS[_hash_int(supi) % len(MODELED_CELLS)]


def estimate_location(supi, method):
    """Compute the modeled GAD estimate: the serving cell's position plus a small deterministic
    sub-cell offset (bounded by the method's accuracy), so the estimate looks like a real fix
    inside the cell rather than the exact tower coordinate. Returns (estimate_dict, cell)."""
    cell = serving_cell(supi)
    # Deterministic offset in [-1, 1] on each axis from independent hashes of the SUPI.
    ox = ((_hash_int("lat:" + supi) % 2000) / 1000.0) - 1.0
    oy = ((_hash_int("lon:" + supi) % 2000) / 1000.0) - 1.0
    # Convert the method accuracy (metres) to rough degrees; ~111_320 m per degree of latitude.
    span_deg = method["accuracyM"] / 111_320.0
    estimate = {
        "latitude": round(cell["latitude"] + ox * span_deg, 6),
        "longitude": round(cell["longitude"] + oy * span_deg, 6),
        "altitude": cell["altitude"],
    }
    return estimate, cell


# --------------------------------------------------- Nlmf_Location — DetermineLocation (TS 29.572)
# The consumer (GMLC via AMF, or the AMF directly on an MT-LR/NI-LR) POSTs an InputData with the
# target UE and a LocationQoS; the LMF returns a LocationData with the geographic estimate, the
# achieved accuracy, the positioning method that produced it, and the age of the estimate.

@app.route("POST", "/nlmf-loc/v1/determine-location")
def determine_location(params, query, body):
    supi = target_supi(body)
    if supi is None:
        return problem(400, "Bad Request",
                       detail="a target UE (supi or gpsi) is mandatory in the InputData",
                       cause="MANDATORY_IE_MISSING")
    # Honest error: the modeled LMF only serves UEs of its own PLMN. A target it does not serve is
    # rejected — a real LMF returns a LocationData failure / ProblemDetails rather than a fake fix.
    if not supi.startswith(SERVED_SUPI_PREFIX):
        obs.log("determine_location_target_unknown", supi=supi)
        obs.counter("lmf_location_unknown_total").inc()
        return problem(404, "Not Found",
                       detail=f"target UE {supi} is not served by this LMF (no positioning source)",
                       cause="UNKNOWN_TARGET_UE")

    req_accuracy = requested_accuracy(body)
    method, qos_met = select_method(req_accuracy)
    estimate, cell = estimate_location(supi, method)

    location_id = uuid.uuid4().hex
    # LocationData (TS 29.572 5.4): the estimate is a GAD ellipsoid-point-with-altitude shape,
    # flattened to latitude/longitude/altitude here. positioningMethod names the technique that
    # produced it; ageOfLocationEstimate is seconds since the (modeled) fix.
    location_data = {
        "locationId": location_id,
        "supi": supi,
        "latitude": estimate["latitude"],
        "longitude": estimate["longitude"],
        "altitude": estimate["altitude"],
        "accuracy": method["accuracyM"],
        "positioningMethod": method["method"],
        "ageOfLocationEstimate": method["ageS"],
        # Honest provenance / QoS outcome (not fabricated as a real measurement):
        "servingCell": cell["ncgi"],
        "requestedAccuracy": req_accuracy,
        "qosFulfilled": qos_met,
        "positionSource": "MODELED_CELL_GRID",   # labeled: not a real measurement
        "timestamp": now_iso(),
        "self": f"/nlmf-loc/v1/locations/{location_id}",
    }
    locations.put(location_id, location_data)
    obs.log("determine_location", supi=supi, positioningMethod=method["method"],
            accuracy=method["accuracyM"], servingCell=cell["ncgi"],
            requestedAccuracy=req_accuracy, qosFulfilled=qos_met)
    obs.counter("lmf_location_requests_total").inc()
    obs.counter(f"lmf_location_method_{method['method'].lower()}_total").inc()
    return 200, location_data


@app.route("GET", "/nlmf-loc/v1/locations/{locationId}")
def get_location(params, query, body):
    record = locations.get(params["locationId"])
    if record is None:
        return problem(404, "Not Found", detail="no such determined location",
                       cause="LOCATION_NOT_FOUND")
    return 200, record


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. The nlmf-loc service is advertised so the AMF/GMLC can
    # discover the LMF for the DetermineLocation operation.
    profile = {"nfType": "LMF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nlmf-loc",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("lmf_modeled_cells").set(len(MODELED_CELLS))
    obs.gauge("lmf_determined_locations").set(len(list(locations.values())))


if __name__ == "__main__":
    obs.init("lmf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

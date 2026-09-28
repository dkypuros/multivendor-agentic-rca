"""
NRF: NF registration and discovery for the owned 5G core.

Spec anchors (TS 29.510):
  Nnrf_NFManagement  section 5.2: PUT /nnrf-nfm/v1/nf-instances/{nfInstanceId} registers an NFProfile
  Nnrf_NFDiscovery   section 5.3: GET /nnrf-disc/v1/nf-instances?target-nf-type=X returns matches
NFProfile is simplified to {nfType, ipv4, port, nfInstanceId}; resource paths keep the real shapes.

Run: python3 nrf.py   (listens on 127.0.0.1:7100)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, serve
from domain import obs
from domain.netconfig import port

PORT = port("nrf")
app = SbiApp("nrf")
nf_instances = {}


@app.route("PUT", "/nnrf-nfm/v1/nf-instances/{nfInstanceId}")
def nf_register(params, query, body):
    if "nfStatus" not in body:
        return problem(400, "Bad Request", detail="NFProfile requires nfStatus",
                       cause="MANDATORY_IE_MISSING")
    body["nfInstanceId"] = params["nfInstanceId"]
    nf_instances[params["nfInstanceId"]] = body
    obs.log("nf_registered", nfType=body.get("nfType"), nfInstanceId=params["nfInstanceId"])
    obs.counter("nf_registrations_total").inc()
    return 201, body


@app.route("GET", "/nnrf-nfm/v1/nf-instances")
def nf_list(params, query, body):
    return 200, {"nfInstances": list(nf_instances.values())}


@app.route("GET", "/nnrf-disc/v1/nf-instances")
def nf_discover(params, query, body):
    # Both query parameters are mandatory per TS 29.510 section 6.2.3.2.3.1
    if "requester-nf-type" not in query:
        return problem(400, "Bad Request", detail="requester-nf-type is mandatory",
                       cause="MANDATORY_QUERY_PARAM_MISSING")
    target = query.get("target-nf-type")
    matches = [p for p in nf_instances.values() if p.get("nfType") == target]
    return 200, {"nfInstances": matches}


if __name__ == "__main__":
    obs.init("nrf")
    obs.on_scrape(lambda: obs.gauge("nf_instances").set(len(nf_instances)))
    serve(app, PORT)

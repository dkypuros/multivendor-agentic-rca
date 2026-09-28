"""
BSF: Binding Support Function — the owned core's PCF-binding registry (Nbsf_Management).

The BSF answers one question the rest of the core keeps asking: "which PCF serves THIS session?"
When a PDU session is set up the SMF picks a PCF and registers a binding here — {supi, dnn,
snssai, ipv4Addr -> pcfId/pcfFqdn}. Later an NEF/AF (or any NF that only knows the UE's IP)
DISCOVERS the serving PCF by that binding so it can steer the same session's policy to the SAME
PCF. Without the BSF a stack with more than one PCF cannot keep a UE's policy coherent. It sits
at the N-Nbsf reference point and is a standalone registry: no other NF is edited here.

Clean-room (charter HARVEST verb): the open-digital-platform-2_0 bsf.py was read for the shape of
the Nbsf surface only; every line here is authored for this stack, stdlib-only like the rest of
the owned core.

Spec anchors:
  Nbsf_Management                   TS 29.521 section 5.2 (PCF binding management service)
  RegisterBinding                   TS 29.521 5.2.2.2 — POST /nbsf-management/v1/pcfBindings
  DiscoverBinding                   TS 29.521 5.2.2.3 — GET  /nbsf-management/v1/pcfBindings?<keys>
  Get/DeregisterBinding             TS 29.521 5.2.2.3.2 / 5.2.2.5 — GET/DELETE .../{bindingId}
  PcfBinding resource               TS 29.521 6.1.6.2.2 (supi, gpsi, ipv4Addr, dnn, snssai,
                                    pcfId, pcfFqdn, pcfIpEndPoints, bindLevel)
  BindingResp (discovery result)    TS 29.521 6.1.6.2.4 — {pcfBindings: [...]}
  BSF functional description        TS 23.501 section 6.2.18
  Nnrf_NFManagement registration    TS 29.510 section 5.2 (BSF registers as nfType BSF)

Labeled simplifications (ledgered in procedures/bsf_binding.txt):
  - The SMF does NOT yet auto-register its PCF binding on PDU session create; the registration
    endpoint is driven directly (by the spec / by an operator). Wiring the SMF's session-create
    path to POST a binding here is the integration follow-up — the endpoint is the seam.
  - Discovery matches on the keys this lab uses (ipv4Addr, ipv6Prefix, supi, dnn, snssai). MAC,
    gpsi and ipDomain keys are accepted and matched when present but not otherwise modeled.
  - bindLevel defaults to NF_INSTANCE (the lab runs single PCF instances, not NF sets).

Run: python3 bsf.py   (SBI on 127.0.0.1:7018, registers with the NRF as nfType BSF)
"""

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

PORT = port("bsf")
NRF = url("nrf")
app = SbiApp("bsf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). Bindings are keyed by bindingId; discovery scans values and matches on
# the query keys — a small registry, so a linear scan is honest and simple.
store = open_store("bsf")
bindings = store.collection("pcf_bindings")   # bindingId -> PcfBinding record


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def snssai_matches(bound, wanted):
    """True when the bound S-NSSAI satisfies the wanted one (TS 29.521 discovery). sst must be
    equal; sd is only compared when the query carries one. Missing bound snssai never matches a
    specific query."""
    if not wanted:
        return True
    bound = bound or {}
    if str(bound.get("sst")) != str(wanted.get("sst")):
        return False
    if wanted.get("sd") is not None and bound.get("sd") != wanted.get("sd"):
        return False
    return True


def match(record, ipv4addr, ipv6prefix, mac, supi, gpsi, dnn, ipdomain, snssai):
    """A binding matches a discovery query when every PRESENT query key equals the binding's
    (TS 29.521 5.2.2.3.1). An empty query matches everything (a caller can list). S-NSSAI is
    compared field-wise via snssai_matches."""
    if ipv4addr and record.get("ipv4Addr") != ipv4addr:
        return False
    if ipv6prefix and record.get("ipv6Prefix") != ipv6prefix:
        return False
    if mac and record.get("macAddr48") != mac:
        return False
    if supi and record.get("supi") != supi:
        return False
    if gpsi and record.get("gpsi") != gpsi:
        return False
    if dnn and record.get("dnn") != dnn:
        return False
    if ipdomain and record.get("ipDomain") != ipdomain:
        return False
    if not snssai_matches(record.get("snssai"), snssai):
        return False
    return True


# --------------------------------------------------------- Nbsf_Management (TS 29.521 5.2)

@app.route("POST", "/nbsf-management/v1/pcfBindings")
def register_binding(params, query, body):
    """RegisterBinding (TS 29.521 5.2.2.2): create a PCF binding so the serving PCF can later be
    DISCOVERED by the session's UE address or subscriber+DNN. A binding must locate the UE (at
    least one of ipv4Addr / ipv6Prefix / macAddr48) and the PCF (at least one of pcfId / pcfFqdn /
    pcfIpEndPoints), and carry a DNN — the minimum a real BSF stores (TS 29.521 6.1.6.2.2)."""
    if not (body.get("ipv4Addr") or body.get("ipv6Prefix") or body.get("macAddr48")):
        return problem(400, "Bad Request",
                       detail="a UE address (ipv4Addr, ipv6Prefix or macAddr48) is mandatory",
                       cause="MANDATORY_IE_MISSING")
    if not (body.get("pcfId") or body.get("pcfFqdn") or body.get("pcfIpEndPoints")):
        return problem(400, "Bad Request",
                       detail="a PCF locator (pcfId, pcfFqdn or pcfIpEndPoints) is mandatory",
                       cause="MANDATORY_IE_MISSING")
    if not body.get("dnn"):
        return problem(400, "Bad Request", detail="dnn is mandatory",
                       cause="MANDATORY_IE_MISSING")
    binding_id = uuid.uuid4().hex
    record = {
        "bindingId": binding_id,
        "supi": body.get("supi"),
        "gpsi": body.get("gpsi"),
        "ipv4Addr": body.get("ipv4Addr"),
        "ipv6Prefix": body.get("ipv6Prefix"),
        "macAddr48": body.get("macAddr48"),
        "ipDomain": body.get("ipDomain"),
        "dnn": body["dnn"],
        "snssai": body.get("snssai"),
        "pcfId": body.get("pcfId"),
        "pcfFqdn": body.get("pcfFqdn"),
        "pcfIpEndPoints": body.get("pcfIpEndPoints"),
        "pcfSetId": body.get("pcfSetId"),
        # bindLevel defaults to NF_INSTANCE — the lab runs single PCF instances (labeled).
        "bindLevel": body.get("bindLevel", "NF_INSTANCE"),
        "self": f"/nbsf-management/v1/pcfBindings/{binding_id}",
        "createdAt": now_iso(),
    }
    bindings.put(binding_id, record)
    obs.log("pcf_binding_registered", bindingId=binding_id, supi=record["supi"],
            dnn=record["dnn"], ipv4Addr=record["ipv4Addr"], pcfId=record["pcfId"])
    obs.counter("bsf_bindings_registered_total").inc()
    # 201 Created; the bindingId is the resource id used for GET/DELETE (real SBI returns it in
    # Location — TS 29.521 5.2.2.2.1; here it rides in the body too, lab-friendly).
    return 201, record


@app.route("GET", "/nbsf-management/v1/pcfBindings")
def discover_binding(params, query, body):
    """DiscoverBinding (TS 29.521 5.2.2.3): return the PCF binding(s) matching the query keys —
    'which PCF serves this session?'. Answers with a BindingResp {pcfBindings:[...]} (TS 29.521
    6.1.6.2.4). An empty list is the honest miss (no faked PCF)."""
    snssai = None
    if query.get("snssai"):
        # S-NSSAI carried as sst[,sd] (lab shorthand) or as a JSON object.
        raw = query["snssai"]
        try:
            import json
            snssai = json.loads(raw)
        except (ValueError, TypeError):
            parts = raw.split(",")
            snssai = {"sst": parts[0]}
            if len(parts) > 1:
                snssai["sd"] = parts[1]
    results = [r for r in bindings.values()
               if match(r, query.get("ipv4Addr"), query.get("ipv6Prefix"),
                        query.get("macAddr48"), query.get("supi"), query.get("gpsi"),
                        query.get("dnn"), query.get("ipDomain"), snssai)]
    obs.log("pcf_binding_discovered", ipv4Addr=query.get("ipv4Addr"), supi=query.get("supi"),
            dnn=query.get("dnn"), found=len(results))
    obs.counter("bsf_discoveries_total", hit=str(bool(results)).lower()).inc()
    return 200, {"pcfBindings": results}


@app.route("GET", "/nbsf-management/v1/pcfBindings/{bindingId}")
def get_binding(params, query, body):
    record = bindings.get(params["bindingId"])
    if record is None:
        return problem(404, "Not Found", detail="no such PCF binding",
                       cause="BINDING_NOT_FOUND")
    return 200, record


@app.route("DELETE", "/nbsf-management/v1/pcfBindings/{bindingId}")
def deregister_binding(params, query, body):
    """DeregisterBinding (TS 29.521 5.2.2.5): remove a binding when its session ends."""
    if bindings.get(params["bindingId"]) is None:
        return problem(404, "Not Found", detail="no such PCF binding",
                       cause="BINDING_NOT_FOUND")
    bindings.delete(params["bindingId"])
    obs.log("pcf_binding_deregistered", bindingId=params["bindingId"])
    obs.counter("bsf_bindings_deregistered_total").inc()
    return 204, {}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2, mirroring the other NFs: nfStatus mandatory, service
    # named nbsf-management so an NEF/SMF discovers this BSF with target-nf-type=BSF.
    profile = {"nfType": "BSF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nbsf-management",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("bsf_bindings_active").set(len(list(bindings.values())))


if __name__ == "__main__":
    obs.init("bsf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

"""
UDR: Unified Data Repository — the owned core's DATA LAYER (Nudr_DataRepository, TS 29.504).

The UDR is the standalone, persistent store that sits BEHIND the front-office NFs: the UDM reads
and provisions subscription data through it, the PCF reads and writes policy data, and the NEF
reads and writes application data. It is not itself an SBI front door for UEs — it is the single
source of truth for provisioned data, discovered through the NRF like any other NF.

This pass stands up the DATA LAYER STANDALONE: the store, its Nudr resource surface, its
persistence, and its observability. UDM/PCF/NEF re-pointing their reads/writes at this UDR (today
each keeps its own embedded store — see services/core/udm/udm.py, which carries an embedded UDR)
is the ledgered integration FOLLOW-UP; this NF is the layer they will read.

Spec anchors (TS 29.504 Nudr_DataRepository, v2 API — resource shapes preserved):
  Subscription data     section 5.2.2 / 5.2.3: provisioned-data under
                        /nudr-dr/v2/subscription-data/{ueId}/{servingPlmnId}/provisioned-data/
                          am-data                        (AccessAndMobilitySubscriptionData, N35 <- UDM)
                          smf-selection-subscription-data (SmfSelectionSubscriptionData)
                          sm-data                        (SessionManagementSubscriptionData)
  Policy data           section 5.3: /nudr-dr/v2/policy-data/ues/{ueId}/{am-data|sm-data|ue-policy-set}
                        (AmPolicyData / SmPolicyData / UePolicySet, N36 <- PCF)
  Application data      section 5.4: /nudr-dr/v2/application-data/{dataType}/{entryId}
                        (e.g. influenceData, pfds — N37 <- NEF)
  Data operations       section 5.2.2 (CREATE via PUT, QUERY via GET, MODIFY via PATCH, DELETE):
                        each resource is a document; PUT replaces, GET reads, PATCH merges, DELETE removes.
  Nnrf_NFManagement     TS 29.510 section 5.2: register as UDR so the UDM/PCF/NEF can discover it.

Labeled simplifications (ledgered in procedures/udr_data_repository.txt):
  - PATCH is an RFC 7386 JSON MERGE-PATCH (deep-merge of the supplied object; null deletes a key).
    Real TS 29.504 also carries RFC 6902 JSON-Patch arrays for some resources. Honest at
    procedure fidelity; the resource shapes and status codes are the standard ones.
  - No data-subscription / notification surface yet (Nudr subscribe-to-notify, 5.2.2.4). Writers
    do not yet get change callbacks — a follow-up when a reader needs them.
  - No authorization (OAuth2 SBI). Every consumer is allowed, exactly like the rest of the owned
    core today. The hook is the register/discover seam; a token gate is a later wave.

Run: python3 udr.py   (SBI on 127.0.0.1:7017, registers with the NRF as UDR)
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

PORT = port("udr")
NRF = url("nrf")
app = SbiApp("udr")

# Persistence, env-gated exactly like every other NF (domain/statestore.py): sqlite3 when
# TELCO_STATE_DIR is set (provisioned data survives a UDR restart — the point of a data layer),
# in-memory otherwise (specs run this clean-slate path). Three Nudr data realms, kept apart:
store = open_store("udr")
subscription_data = store.collection("subscription_data")   # keyed ueId|servingPlmnId|dataType
policy_data = store.collection("policy_data")               # keyed ueId|dataType
application_data = store.collection("application_data")     # keyed dataType|entryId

# The provisioned-data resource kinds this UDR serves (TS 29.504 5.2.3). Unknown kinds are
# rejected 404 rather than silently stored, so the resource surface stays honest.
PROVISIONED_KINDS = {"am-data", "smf-selection-subscription-data", "sm-data"}
POLICY_KINDS = {"am-data", "sm-data", "ue-policy-set"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def merge_patch(target, patch):
    """RFC 7386 JSON Merge Patch: deep-merge `patch` into `target`; a null value deletes the key.
    Labeled simplification (module docstring) — the owned Nudr MODIFY at procedure fidelity."""
    if not isinstance(patch, dict) or not isinstance(target, dict):
        return patch
    out = dict(target)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_patch(out[key], value)
        else:
            out[key] = value
    return out


def _record(document, **meta):
    """Wrap a stored document with UDR bookkeeping (created/modified timestamps + a resource self)."""
    envelope = dict(document)
    envelope.setdefault("_udr", {})
    envelope["_udr"].update(meta)
    return envelope


def _crud(collection, key, resource_self, realm):
    """The four Nudr data operations over one document collection (TS 29.504 5.2.2), returned as
    handler-shaped closures. One implementation serves subscription/policy/application data — the
    resource paths differ, the CREATE/READ/MODIFY/DELETE semantics do not."""

    def read():
        doc = collection.get(key)
        if doc is None:
            return problem(404, "Not Found", detail=f"no {realm} record at {resource_self}",
                           cause="DATA_NOT_FOUND")
        obs.counter("udr_reads_total", realm=realm).inc()
        return 200, doc

    def create(body):
        existed = collection.get(key) is not None
        record = _record(body, self=resource_self, realm=realm,
                         createdAt=(collection.get(key) or {}).get("_udr", {}).get("createdAt")
                         or now_iso(), modifiedAt=now_iso())
        collection.put(key, record)
        obs.log("udr_record_written", realm=realm, resource=resource_self, created=not existed)
        obs.counter("udr_writes_total", realm=realm).inc()
        # TS 29.504: PUT creates (201) or replaces (200/204). We return the stored document.
        return (200 if existed else 201), record

    def modify(body):
        doc = collection.get(key)
        if doc is None:
            return problem(404, "Not Found", detail=f"no {realm} record to modify at {resource_self}",
                           cause="DATA_NOT_FOUND")
        merged = merge_patch(doc, body)
        merged["_udr"] = dict(doc.get("_udr", {}))
        merged["_udr"]["modifiedAt"] = now_iso()
        collection.put(key, merged)
        obs.log("udr_record_modified", realm=realm, resource=resource_self)
        obs.counter("udr_patches_total", realm=realm).inc()
        return 200, merged

    def remove():
        if collection.get(key) is None:
            return problem(404, "Not Found", detail=f"no {realm} record to delete at {resource_self}",
                           cause="DATA_NOT_FOUND")
        collection.delete(key)
        obs.log("udr_record_deleted", realm=realm, resource=resource_self)
        obs.counter("udr_deletes_total", realm=realm).inc()
        return 204, {}

    return read, create, modify, remove


# ------------------------------------------------------- Subscription data (TS 29.504 5.2.3)
# Provisioned subscription data under a (ueId, servingPlmnId) — the UDM's N35 store.

def _sub_ctx(params):
    kind = params["dataType"]
    if kind not in PROVISIONED_KINDS:
        return None, problem(404, "Not Found",
                             detail=f"unknown provisioned-data kind {kind!r}; "
                                    f"expected one of {sorted(PROVISIONED_KINDS)}",
                             cause="DATA_NOT_FOUND")
    ue, plmn = params["ueId"], params["servingPlmnId"]
    key = f"{ue}|{plmn}|{kind}"
    resource = (f"/nudr-dr/v2/subscription-data/{ue}/{plmn}/provisioned-data/{kind}")
    return _crud(subscription_data, key, resource, "subscription"), None


SUB_PATH = "/nudr-dr/v2/subscription-data/{ueId}/{servingPlmnId}/provisioned-data/{dataType}"


@app.route("GET", SUB_PATH)
def get_subscription_data(params, query, body):
    crud, err = _sub_ctx(params)
    return err if err else crud[0]()


@app.route("PUT", SUB_PATH)
def put_subscription_data(params, query, body):
    crud, err = _sub_ctx(params)
    return err if err else crud[1](body)


@app.route("PATCH", SUB_PATH)
def patch_subscription_data(params, query, body):
    crud, err = _sub_ctx(params)
    return err if err else crud[2](body)


@app.route("DELETE", SUB_PATH)
def delete_subscription_data(params, query, body):
    crud, err = _sub_ctx(params)
    return err if err else crud[3]()


# ------------------------------------------------------------ Policy data (TS 29.504 5.3)
# Per-UE policy data — the PCF's N36 store (AmPolicyData / SmPolicyData / UePolicySet).

def _pol_ctx(params):
    kind = params["dataType"]
    if kind not in POLICY_KINDS:
        return None, problem(404, "Not Found",
                             detail=f"unknown policy-data kind {kind!r}; "
                                    f"expected one of {sorted(POLICY_KINDS)}",
                             cause="DATA_NOT_FOUND")
    ue = params["ueId"]
    key = f"{ue}|{kind}"
    resource = f"/nudr-dr/v2/policy-data/ues/{ue}/{kind}"
    return _crud(policy_data, key, resource, "policy"), None


POL_PATH = "/nudr-dr/v2/policy-data/ues/{ueId}/{dataType}"


@app.route("GET", POL_PATH)
def get_policy_data(params, query, body):
    crud, err = _pol_ctx(params)
    return err if err else crud[0]()


@app.route("PUT", POL_PATH)
def put_policy_data(params, query, body):
    crud, err = _pol_ctx(params)
    return err if err else crud[1](body)


@app.route("PATCH", POL_PATH)
def patch_policy_data(params, query, body):
    crud, err = _pol_ctx(params)
    return err if err else crud[2](body)


@app.route("DELETE", POL_PATH)
def delete_policy_data(params, query, body):
    crud, err = _pol_ctx(params)
    return err if err else crud[3]()


# ----------------------------------------------------- Application data (TS 29.504 5.4)
# NEF-provisioned application data (e.g. influenceData, pfds) as a keyed collection — N37.

def _app_ctx(params):
    kind, entry = params["dataType"], params["entryId"]
    key = f"{kind}|{entry}"
    resource = f"/nudr-dr/v2/application-data/{kind}/{entry}"
    return _crud(application_data, key, resource, "application")


APP_PATH = "/nudr-dr/v2/application-data/{dataType}/{entryId}"


@app.route("GET", "/nudr-dr/v2/application-data/{dataType}")
def list_application_data(params, query, body):
    """Collection read: every application-data entry of a given kind (TS 29.504 5.4 query)."""
    prefix = params["dataType"] + "|"
    entries = [v for k, v in application_data.items() if k.startswith(prefix)]
    obs.counter("udr_reads_total", realm="application").inc()
    return 200, {"dataType": params["dataType"], "entries": entries}


@app.route("GET", APP_PATH)
def get_application_data(params, query, body):
    return _app_ctx(params)[0]()


@app.route("PUT", APP_PATH)
def put_application_data(params, query, body):
    return _app_ctx(params)[1](body)


@app.route("PATCH", APP_PATH)
def patch_application_data(params, query, body):
    return _app_ctx(params)[2](body)


@app.route("DELETE", APP_PATH)
def delete_application_data(params, query, body):
    return _app_ctx(params)[3]()


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. The single Nudr_DataRepository service is advertised so a
    # UDM/PCF/NEF discovering target-nf-type=UDR finds this data layer.
    profile = {"nfType": "UDR", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nudr-dr",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    total = len(subscription_data) + len(policy_data) + len(application_data)
    obs.gauge("udr_records_active", realm="subscription").set(len(subscription_data))
    obs.gauge("udr_records_active", realm="policy").set(len(policy_data))
    obs.gauge("udr_records_active", realm="application").set(len(application_data))
    obs.gauge("udr_records_active_total").set(total)


if __name__ == "__main__":
    obs.init("udr")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

"""
UDSF: Unstructured Data Storage Function — the owned core's SHARED STATE STORE.

The UDSF is the network's generic, per-NF key/blob store: any NF can OFFLOAD its unstructured
state (session context, subscription snapshots, agent scratch) as opaque records under its own
storage namespace, then GET it back, PATCH it, DELETE it, or QUERY records by meta-tag. It is the
5G-core way to make an NF stateless — the state lives in the shared UDSF, not in the NF's memory.
This is a standalone store: no other NF is edited. Which NFs offload which state to the UDSF is a
follow-up ledger item (procedures/udsf_storage.txt); this pass STANDS UP the shared store itself.

Spec anchors (TS 29.598 Nudsf, Rel-19):
  Nudsf_UnstructuredDataManagement   TS 29.598 section 5 (Data Repository, nudsf-dr): a Record lives
                                     at /records/{realmId}/{storageId}/{recordId} and carries opaque
                                     data blocks plus meta-tags. PUT creates/replaces, GET reads,
                                     PATCH updates, DELETE removes.
  Record resource + meta-tags        TS 29.598 5.2.2 (Record data type), 5.4 (meta-tags): a record is
                                     addressed by (realmId, storageId, recordId); meta-tags are
                                     name/value pairs an NF attaches so records can be found without
                                     the recordId.
  Records query (by meta-tag)        TS 29.598 5.2.2.3 (GET records with a filter): return the record
                                     ids (or records) whose meta-tags match a filter.
  Nnrf_NFManagement (register UDSF)  TS 29.510 5.2 — the UDSF advertises nudsf-dr in the NRF so any NF
                                     can discover the shared store.

Data model (procedure-level fidelity, JSON not the real multipart/binary blocks):
  A Record = {"blocks": <arbitrary JSON blob>, "metaTags": {name: value, ...}}. The real Nudsf
  record carries one or more named binary blocks (multipart/related) with per-block content-ids;
  here a record holds ONE arbitrary-JSON "blocks" payload. Interface-faithful at the record/tag
  level; binary block framing is the fidelity follow-up (ledgered in procedures/udsf_storage.txt).

Labeled simplifications (ledgered in procedures/udsf_storage.txt):
  - PATCH is RFC 7396 JSON merge-patch over blocks + metaTags (deep-merge, null deletes a key), not
    the block-scoped JSON Patch of TS 29.598 5.2.2.2. The merge seam is here; block-level patch is
    the follow-up.
  - Query filter is a simple meta-tag equality (tag-name / tag-value query params). The real Nudsf
    filter is a comparison/logical expression; equality is the common case and the seam for the
    richer grammar.
  - No NF authorization / OAuth2 on the store yet (TS 33.501 SBI security). Every requester is
    allowed; the hook is a follow-up, a body change not a new seam.

Run: python3 udsf.py   (SBI on 127.0.0.1:7023, registers with the NRF as UDSF)
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

PORT = port("udsf")
NRF = url("nrf")
app = SbiApp("udsf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). One collection holds every record; the key is the full
# realmId/storageId/recordId path so realms and per-NF storages stay isolated, and the record
# carries its own realmId/storageId/recordId so a tag query can scope + report them.
store = open_store("udsf")
records = store.collection("records")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def rec_key(realm_id, storage_id, record_id):
    """Composite key: a record is addressed by (realmId, storageId, recordId), TS 29.598 5.2.2."""
    return f"{realm_id}/{storage_id}/{record_id}"


def merge_patch(target, patch):
    """RFC 7396 JSON merge-patch (labeled simplification vs TS 29.598 block-scoped JSON Patch):
    deep-merge dict-into-dict; a null value deletes the key; a non-dict patch replaces outright."""
    if not isinstance(patch, dict) or not isinstance(target, dict):
        return patch
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def tags_match(record, tag_name, tag_value):
    """A record matches when it carries the meta-tag tag_name; if tag_value is given, the tag's
    value must equal it (string-compared so numbers/strings compare uniformly). TS 29.598 5.4."""
    meta = record.get("metaTags") or {}
    if tag_name not in meta:
        return False
    if tag_value is None:
        return True
    return str(meta[tag_name]) == str(tag_value)


# ----------------------------------------------- Nudsf_UnstructuredDataManagement (nudsf-dr, records)
# TS 29.598 section 5: a per-NF record store. PUT/GET/PATCH/DELETE a single record; GET the
# collection with a meta-tag filter to find records without knowing their recordId.

@app.route("PUT", "/nudsf-dr/v1/records/{realmId}/{storageId}/{recordId}")
def put_record(params, query, body):
    """Create or replace a record (TS 29.598 5.2.2.2 PUT). Body: {"blocks": <json>, "metaTags": {}}.
    201 when newly created, 200 when an existing record is replaced."""
    key = rec_key(params["realmId"], params["storageId"], params["recordId"])
    existed = records.get(key) is not None
    record = {
        "realmId": params["realmId"],
        "storageId": params["storageId"],
        "recordId": params["recordId"],
        "blocks": body.get("blocks", {}),
        "metaTags": body.get("metaTags") or {},
        "self": f"/nudsf-dr/v1/records/{key}",
        "createdAt": records.get(key, {}).get("createdAt", now_iso()) if existed else now_iso(),
        "updatedAt": now_iso(),
    }
    records.put(key, record)
    obs.log("record_put", realmId=params["realmId"], storageId=params["storageId"],
            recordId=params["recordId"], replaced=existed,
            tags=sorted((record["metaTags"] or {}).keys()))
    obs.counter("udsf_record_puts_total").inc()
    return (200 if existed else 201), record


@app.route("GET", "/nudsf-dr/v1/records/{realmId}/{storageId}/{recordId}")
def get_record(params, query, body):
    """Read one record back (TS 29.598 5.2.2.2 GET). Unknown record -> 404."""
    key = rec_key(params["realmId"], params["storageId"], params["recordId"])
    record = records.get(key)
    if record is None:
        return problem(404, "Not Found", detail=f"no record {key}", cause="RECORD_NOT_FOUND")
    obs.counter("udsf_record_gets_total").inc()
    return 200, record


@app.route("PATCH", "/nudsf-dr/v1/records/{realmId}/{storageId}/{recordId}")
def patch_record(params, query, body):
    """Update a record in place (TS 29.598 5.2.2.2 PATCH). RFC 7396 merge-patch over blocks +
    metaTags (labeled simplification). Unknown record -> 404."""
    key = rec_key(params["realmId"], params["storageId"], params["recordId"])
    record = records.get(key)
    if record is None:
        return problem(404, "Not Found", detail=f"no record {key}", cause="RECORD_NOT_FOUND")
    if "blocks" in body:
        record["blocks"] = merge_patch(record.get("blocks", {}), body["blocks"])
    if "metaTags" in body:
        record["metaTags"] = merge_patch(record.get("metaTags", {}) or {}, body["metaTags"])
    record["updatedAt"] = now_iso()
    records.put(key, record)
    obs.log("record_patched", realmId=params["realmId"], storageId=params["storageId"],
            recordId=params["recordId"])
    obs.counter("udsf_record_patches_total").inc()
    return 200, record


@app.route("DELETE", "/nudsf-dr/v1/records/{realmId}/{storageId}/{recordId}")
def delete_record(params, query, body):
    """Remove a record (TS 29.598 5.2.2.2 DELETE). 204 on success, 404 if it never existed."""
    key = rec_key(params["realmId"], params["storageId"], params["recordId"])
    if records.get(key) is None:
        return problem(404, "Not Found", detail=f"no record {key}", cause="RECORD_NOT_FOUND")
    records.delete(key)
    obs.log("record_deleted", realmId=params["realmId"], storageId=params["storageId"],
            recordId=params["recordId"])
    obs.counter("udsf_record_deletes_total").inc()
    return 204, {}


@app.route("GET", "/nudsf-dr/v1/records/{realmId}/{storageId}")
def query_records(params, query, body):
    """Query records in a storage by meta-tag (TS 29.598 5.2.2.3). Query params:
      tag-name  (required to filter; absent -> all records in the storage)
      tag-value (optional; when given, the tag must equal it)
      count-ind (optional 'true' -> return only the match count, not the records)
    Scoped to (realmId, storageId). Returns {"records": [...], "count": n}."""
    realm_id, storage_id = params["realmId"], params["storageId"]
    tag_name = query.get("tag-name")
    tag_value = query.get("tag-value")
    scope = [r for r in records.values()
             if r.get("realmId") == realm_id and r.get("storageId") == storage_id]
    matched = [r for r in scope if tag_name is None or tags_match(r, tag_name, tag_value)]
    obs.log("records_queried", realmId=realm_id, storageId=storage_id,
            tagName=tag_name, tagValue=tag_value, matched=len(matched))
    obs.counter("udsf_record_queries_total").inc()
    if query.get("count-ind") == "true":
        return 200, {"count": len(matched)}
    return 200, {"records": matched, "count": len(matched)}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. The nudsf-dr service is advertised so any NF can discover
    # the shared store and offload its state.
    profile = {"nfType": "UDSF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nudsf-dr",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    obs.gauge("udsf_records_stored").set(len(list(records.values())))


if __name__ == "__main__":
    obs.init("udsf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

"""
CHF: Charging Function — Nchf_ConvergedCharging service for the owned 5G core.

Clean-room (charter HARVEST verb): the open-digital-platform-2_0 CHF was read for the shape
of the Nchf surface only; every line here is authored for this stack, stdlib-only like the
rest of the owned core. Nothing is copied — the reference is FastAPI/pydantic and not mine.

Spec anchors:
  Nchf_ConvergedCharging            TS 32.291 section 5.2.2 (Create / Update / Release charging
                                    data), TS 32.290 section 6 (converged charging architecture,
                                    the SMF-CHF N40 reference point)
  Quota / Granted Service Unit      TS 32.291 6.1.6.2 (RequestedUnit / GrantedUnit /
                                    MultipleUnitInformation): the CHF grants a GSU (a quota of
                                    volume/time) per rating group and a result code
  Data-connectivity charging model  TS 32.255 (5G data connectivity charging: per PDU session,
                                    per rating group, online/offline)
  Nnrf_NFManagement registration    TS 29.510 section 5.2 (CHF registers as nfType CHF)

WHAT IT DOES
  The SMF, during PDU session establishment (TS 23.502 4.3.2.2), calls
  Nchf_ConvergedCharging_Create with the session context (supi, dnn, S-NSSAI, 5QI). The CHF
  opens a charging-data resource, decides a tariff + an initial GSU (Granted Service Unit —
  a volume/time quota) from a small labeled policy, and answers with the granted quota + a
  result code. As traffic flows the SMF reports usage and asks for more quota over
  .../{ref}/update (the CHF records the usage, rates it, and re-grants). At session teardown
  the SMF closes the resource over .../{ref}/release (the CHF records the final usage and
  produces a CDR-shaped summary).

  Tariff/quota is DATA-DRIVEN: TARIFF_TABLE below is a small labeled, first-match-wins table
  keyed by DNN and/or 5QI (same match semantics as the PCF's policy table and the SMF's
  UPF-selection table). It is the single place a tariff is authored today; a TMF/CHF
  tariff-authoring UI writes this table in a later pass (labeled here, ledgered in
  procedures/chf_converged_charging.txt).

  This is the CONVERGED-CHARGING alternative to today's rating path. Today the BSS billing
  plane (services/bss_billing/billing.py) polls the UPF byte counters and rates them offline
  — that stays the DEFAULT rating path and is untouched. The CHF adds the online/quota-shaped
  surface the SMF talks to at establishment; unifying the two (CHF as the one rating engine)
  is a follow-up (issue #16), ledgered in the procedure.

MONEY-LOOP CONVERGENCE (issue #16, procedures/money_loop_convergence.txt) — ADDITIVE:
  The two money paths now CLOSE into one loop. Two additions, both gated so a stack with only
  one side present is byte-identical to before:
    (1) ACCOUNT ROLLUP. The CHF stamps every charging-data resource (and its CDR) with the
        billingAccount + customer the subscriber belongs to, resolved from the SAME BSS
        supi<->customer<->account registry the billing plane reads (resolve_account below,
        TTL-cached + walk-in bootstrap, mirroring billing.py). So the online charge and the
        offline invoice line name the SAME account. No BSS reachable -> walk-in, never blocks.
    (2) RECONCILIATION. The billing plane reports the very bytes it rates offline into THIS
        CHF's charging-data resource (Nchf update) and then reconciles its rated charge
        against what the CHF metered online — same bytes, same tariff, one charge per session,
        drift flagged. The CHF is the real-time metering AUTHORITY; billing reconciles against
        it. The /chf/charging-data list (optionally ?subscriberIdentifier=) is the read surface
        billing reconciles from.

Run: python3 chf.py   (SBI on 127.0.0.1:7009, registers with the NRF as nfType CHF)
"""

import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import obs
from domain.netconfig import port, url, value
from domain.statestore import open_store

PORT = port("chf")
NRF = url("nrf")

# ------------------------------------------------------------------- tariff / quota table
# Labeled, declarative, first-match-wins. Match keys are optional (an omitted key is a
# wildcard); "tariff" is the decision applied on a match. Same match shape as the PCF's
# POLICY_TABLE and the SMF's UPF-selection table so all three read alike. This is the ONE
# tariff-authoring surface today — a TMF/CHF tariff UI (later pass) will write this table.
#
# Each tariff carries:
#   ratingGroup   the rating group the usage is metered under (TS 32.291 MultipleUnitUsage;
#                 the rating-group model TS 32.255 uses to separate service flows)
#   grantVolume   initial Granted Service Unit total volume in BYTES (TS 32.291 GrantedUnit
#                 totalVolume) — how much the CHF grants the SMF up front before it must report
#   grantTime     GSU time quota in seconds (TS 32.291 GrantedUnit time)
#   validityTime  seconds the grant is valid (TS 32.291 GrantedUnit validityTime)
#   pricePerMb    the tariff used to rate reported usage into a charge (lab currency below)
#   currency      ISO currency label for the rated amount
#   label         human string for the UI / logs (which tariff fired)
#
# The DEFAULT table is authored for this lab's DNNs (internet/ims/edge) plus one 5QI-driven
# rule: 5QI 83 (URLLC-flavored, the robot slice the PCF anchors) gets a small low-latency
# grant regardless of DNN. No pre-CHF flow is affected — the SMF only consults the CHF when
# one is registered and falls back to legacy (no charging) otherwise.
MB = 1_000_000
DEFAULT_TARIFF_TABLE = [
    {"fiveqi": 83, "tariff": {
        "label": "urllc-metered", "ratingGroup": 200, "grantVolume": 10 * MB,
        "grantTime": 300, "validityTime": 300, "pricePerMb": 0.40, "currency": "USD"}},
    {"dnn": "ims", "tariff": {
        "label": "ims-signalling", "ratingGroup": 5, "grantVolume": 2 * MB,
        "grantTime": 3600, "validityTime": 3600, "pricePerMb": 0.00, "currency": "USD"}},
    {"dnn": "edge", "tariff": {
        "label": "edge-breakout", "ratingGroup": 80, "grantVolume": 100 * MB,
        "grantTime": 3600, "validityTime": 3600, "pricePerMb": 0.25, "currency": "USD"}},
    {"dnn": "internet", "tariff": {
        "label": "internet-data", "ratingGroup": 10, "grantVolume": 50 * MB,
        "grantTime": 3600, "validityTime": 3600, "pricePerMb": 0.10, "currency": "USD"}},
    {"tariff": {   # catch-all: any DNN the table does not name gets a best-effort grant
        "label": "best-effort-data", "ratingGroup": 9, "grantVolume": 20 * MB,
        "grantTime": 3600, "validityTime": 3600, "pricePerMb": 0.10, "currency": "USD"}},
]


def load_tariff_table():
    """DEFAULT_TARIFF_TABLE unless TELCO_CHF_TARIFF overrides it (JSON array, same schema,
    netconfig file<-env precedence) — the house pattern for a declarative table."""
    raw = value("TELCO_CHF_TARIFF")
    table = json.loads(raw) if raw else DEFAULT_TARIFF_TABLE
    for rule in table:
        if "tariff" not in rule or "grantVolume" not in rule["tariff"]:
            raise RuntimeError(f"TELCO_CHF_TARIFF rule missing tariff.grantVolume: {rule}")
    return table


TARIFF_TABLE = load_tariff_table()
app = SbiApp("chf")
# Charging-data resources survive a restart when TELCO_STATE_DIR is set (house persistence
# pattern); in-memory dict otherwise — same as every other NF.
store = open_store("chf")
charging = store.collection("charging_data")   # chargingDataRef -> resource

# ------------------------------------------------------------------- account rollup (issue #16)
# The CHF's online charge ties to the SAME supi<->customer<->billingAccount registry the billing
# plane rates against (TMF632/629/666, owned by the BSS). resolve_account() below MIRRORS
# billing.py's resolver exactly — TTL cache, last-known-good, walk-in bootstrap — so the two
# money paths roll up to the SAME account by construction. ADDITIVE + gated: a CHF deployed
# without a BSS (e.g. the hermetic run_chf_spec) resolves the walk-in account and never blocks.
BSS_SUPI_MAP = url("bss") + "/bss/supi-map"
ACCOUNT_TTL = 2.0  # seconds; a SUPI missing from the cached map busts the cache immediately
BOOTSTRAP_WALKIN = {"customerId": "cust-walk-in", "accountId": "ba-walk-in"}
account_lock = threading.Lock()
_accounts = {"map": None, "walkIn": None, "fetchedAt": 0.0, "source": "bootstrap"}


def resolve_account(supi):
    """The subscriber's owner at charging time: the BSS supi registry within TTL (a SUPI missing
    from the cached map busts the cache immediately), else last-known-good, else the walk-in
    bootstrap mirror. Unknown SUPIs land on walk-in — attribution never blocks charging. Mirrors
    billing.py.resolve_account so the online charge and the offline invoice name one account.
    Returns ({customerId, accountId}, source)."""
    with account_lock:
        now = time.monotonic()
        cached = _accounts["map"]
        expired = (_accounts["fetchedAt"] == 0.0 or now - _accounts["fetchedAt"] >= ACCOUNT_TTL)
        unknown = cached is not None and supi not in cached
        if expired or unknown:
            detail = None
            try:
                status, body = request("GET", BSS_SUPI_MAP)
                if status == 200 and isinstance(body.get("supis"), dict):
                    if _accounts["source"] != "bss":
                        obs.log("account_map_resolved", source="bss", supis=len(body["supis"]))
                    _accounts.update(map=body["supis"], walkIn=body.get("walkIn"), source="bss")
                else:
                    detail = f"bss returned status={status} without a supi map"
            except OSError as exc:
                detail = f"bss unreachable: {exc}"
            if detail is not None:
                fallback = "last-known-good" if _accounts["map"] is not None else "bootstrap"
                if _accounts["source"] != fallback:
                    obs.log("account_map_fetch_failed", level="warning",
                            fallback=fallback, detail=detail)
                _accounts["source"] = fallback
            _accounts["fetchedAt"] = now   # advances on EVERY outcome: down BSS retried once/TTL
        cached, source = _accounts["map"], _accounts["source"]
        entry = (cached or {}).get(supi) or _accounts["walkIn"] or BOOTSTRAP_WALKIN
        obs.counter("chf_account_resolutions_total", source=source).inc()
        return entry, source


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def match_tariff(dnn, fiveqi):
    """(dnn, 5qi) -> tariff dict, first-match-wins. A listed fiveqi must equal the session's;
    a listed dnn must equal the session's; omitted keys are wildcards."""
    for rule in TARIFF_TABLE:
        if "fiveqi" in rule and rule["fiveqi"] != fiveqi:
            continue
        if "dnn" in rule and rule["dnn"] != dnn:
            continue
        return rule["tariff"]
    return TARIFF_TABLE[-1]["tariff"]   # the catch-all always matches; belt-and-braces


def granted_unit(tariff):
    """A Granted Service Unit (TS 32.291 6.1.6.2 GrantedUnit): the volume/time quota the CHF
    hands the SMF before it must report usage, plus its validity window."""
    return {"totalVolume": tariff["grantVolume"], "time": tariff["grantTime"],
            "validityTime": tariff["validityTime"]}


def unit_information(tariff, result="SUCCESS"):
    """MultipleUnitInformation (TS 32.291 6.1.6.2.4): the granted quota for a rating group plus
    the result code — the shape the SMF reads out of a Create/Update response."""
    return {"resultCode": result, "ratingGroup": tariff["ratingGroup"],
            "grantedUnit": granted_unit(tariff),
            "validityTime": tariff["validityTime"],
            "triggers": ["QUOTA_THRESHOLD", "VALIDITY_TIME"]}


def rate(tariff, total_bytes):
    """Rate reported usage into a charge at the tariff's pricePerMb (1 MB = 1e6 bytes)."""
    return round(total_bytes / MB * tariff["pricePerMb"], 8)


@app.route("POST", "/nchf-convergedcharging/v1/chargingdata")
def create_charging_data(params, query, body):
    # ChargingDataRequest (TS 32.291 6.1.6.2.1): subscriberIdentifier is the minimum the tariff
    # keys on; dnn/5QI select the tariff. Accept both SBI and lab-friendly field names.
    supi = body.get("subscriberIdentifier") or body.get("supi")
    if not supi:
        return problem(400, "Bad Request", detail="subscriberIdentifier is mandatory",
                       cause="MANDATORY_IE_MISSING")
    dnn = body.get("dnn", "internet")
    fiveqi = body.get("fiveqi")
    tariff = match_tariff(dnn, fiveqi)
    ref = "cdr-" + uuid.uuid4().hex[:12]
    # Account rollup (issue #16): tie the online charge to the subscriber's billing account, the
    # SAME registry the offline invoice line resolves against — so both name one account.
    owner, owner_source = resolve_account(supi)
    resource = {
        "chargingDataRef": ref, "subscriberIdentifier": supi, "dnn": dnn, "fiveqi": fiveqi,
        "tariffLabel": tariff["label"], "ratingGroup": tariff["ratingGroup"],
        "pricePerMb": tariff["pricePerMb"], "currency": tariff["currency"],
        "status": "ACTIVE", "startTime": now_iso(), "lastUpdateTime": now_iso(),
        "usedVolume": 0, "grantedVolume": tariff["grantVolume"], "charge": 0.0,
        "sequenceNumber": 1,
        # TMF666/629 account stamp (money-loop convergence): the account this online charge rolls
        # up to, and the customer that owns it — mirrors the billing charge's billingAccount.
        "billingAccount": {"id": owner["accountId"], "@referredType": "BillingAccount"},
        "customerId": owner["customerId"], "accountSource": owner_source,
    }
    charging.put(ref, resource)
    obs.log("charging_data_created", chargingDataRef=ref, supi=supi, dnn=dnn, fiveqi=fiveqi,
            tariff=tariff["label"], ratingGroup=tariff["ratingGroup"],
            grantVolume=tariff["grantVolume"], billingAccountId=owner["accountId"],
            customerId=owner["customerId"], accountSource=owner_source)
    obs.counter("charging_data_created_total", tariff=tariff["label"]).inc()
    # 201 Created; the chargingDataRef is the resource id the SMF uses for update/release (real
    # SBI returns it in Location — TS 32.291 5.2.2.2; here it rides in the body too, lab-friendly).
    return 201, {"chargingDataRef": ref, "tariffLabel": tariff["label"],
                 "invocationTimeStamp": resource["startTime"],
                 "invocationSequenceNumber": 1,
                 "multipleUnitInformation": [unit_information(tariff)]}


def _apply_usage(resource, body):
    """Fold reported UsedUnitContainer volume (TS 32.291 6.1.6.2.20) into the resource and rate
    it. Accepts multipleUnitUsage[].usedUnitContainer[].totalVolume or a lab-flat usedVolume."""
    tariff = {"pricePerMb": resource["pricePerMb"]}
    reported = 0
    for mu in body.get("multipleUnitUsage") or []:
        for uc in mu.get("usedUnitContainer") or []:
            reported += uc.get("totalVolume") or 0
    if not reported and isinstance(body.get("usedVolume"), (int, float)):
        reported = int(body["usedVolume"])
    if reported:
        resource["usedVolume"] += reported
        resource["charge"] = round(resource["charge"] + rate(tariff, reported), 8)
    return reported


@app.route("POST", "/nchf-convergedcharging/v1/chargingdata/{ref}/update")
def update_charging_data(params, query, body):
    resource = charging.get(params["ref"])
    if resource is None or resource["status"] != "ACTIVE":
        return problem(404, "Not Found", detail=f"no active charging data {params['ref']}",
                       cause="CHARGING_NOT_FOUND")
    reported = _apply_usage(resource, body or {})
    resource["sequenceNumber"] += 1
    resource["lastUpdateTime"] = now_iso()
    # Re-grant: hand the SMF a fresh GSU so traffic keeps flowing (online-charging re-authorize,
    # TS 32.291 5.2.2.3). The grant total is cumulative in this lab's simplified model.
    tariff = match_tariff(resource["dnn"], resource["fiveqi"])
    resource["grantedVolume"] += tariff["grantVolume"]
    charging.put(params["ref"], resource)
    obs.log("charging_data_updated", chargingDataRef=params["ref"], reportedVolume=reported,
            usedVolume=resource["usedVolume"], charge=resource["charge"])
    obs.counter("charging_data_updated_total", tariff=resource["tariffLabel"]).inc()
    return 200, {"chargingDataRef": params["ref"],
                 "invocationTimeStamp": resource["lastUpdateTime"],
                 "invocationSequenceNumber": resource["sequenceNumber"],
                 "multipleUnitInformation": [unit_information(tariff)]}


@app.route("POST", "/nchf-convergedcharging/v1/chargingdata/{ref}/release")
def release_charging_data(params, query, body):
    resource = charging.get(params["ref"])
    if resource is None:
        return problem(404, "Not Found", detail=f"no charging data {params['ref']}",
                       cause="CHARGING_NOT_FOUND")
    reported = _apply_usage(resource, body or {})
    resource["status"] = "CLOSED"
    resource["endTime"] = now_iso()
    resource["sequenceNumber"] += 1
    charging.put(params["ref"], resource)
    # A CDR-shaped final summary (TS 32.291 5.2.2.4 / TS 32.298 CDR fields, simplified).
    cdr = {"chargingDataRef": params["ref"], "subscriberIdentifier": resource["subscriberIdentifier"],
           "dnn": resource["dnn"], "ratingGroup": resource["ratingGroup"],
           "totalVolume": resource["usedVolume"], "charge": resource["charge"],
           "currency": resource["currency"], "startTime": resource["startTime"],
           "endTime": resource["endTime"],
           # account rollup carried onto the CDR so the settled online charge names its account
           "billingAccount": resource.get("billingAccount"),
           "customerId": resource.get("customerId")}
    obs.log("charging_data_released", chargingDataRef=params["ref"], reportedVolume=reported,
            totalVolume=resource["usedVolume"], charge=resource["charge"],
            currency=resource["currency"])
    obs.counter("charging_data_released_total", tariff=resource["tariffLabel"]).inc()
    obs.counter("charged_amount_total", tariff=resource["tariffLabel"]).inc(resource["charge"])
    return 200, {"chargingDataRef": params["ref"], "cdr": cdr}


@app.route("GET", "/nchf-convergedcharging/v1/chargingdata/{ref}")
def get_charging_data(params, query, body):
    resource = charging.get(params["ref"])
    if resource is None:
        return problem(404, "Not Found", detail=f"no charging data {params['ref']}",
                       cause="CHARGING_NOT_FOUND")
    return 200, resource


@app.route("GET", "/chf/charging-data")
def list_charging_data(params, query, body):
    # Lab introspection + the reconciliation read surface (money loop): every charging-data
    # resource the CHF holds, optionally filtered to one subscriber so the billing plane can
    # reconcile its offline rating against what the CHF metered online for that SUPI.
    records = charging.values()
    supi = query.get("subscriberIdentifier")
    if supi:
        records = [r for r in records if r.get("subscriberIdentifier") == supi]
    return 200, {"chargingData": records}


@app.route("GET", "/chf/tariff-table")
def get_tariff_table(params, query, body):
    # Lab introspection: the authored tariff table (the future CHF-UI reads/writes this).
    return 200, {"tariffTable": TARIFF_TABLE}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2, mirroring pcf/smf: nfStatus mandatory, service named
    # nchf-convergedcharging so an SMF discovers this CHF with target-nf-type=CHF.
    profile = {"nfType": "CHF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [{"serviceName": "nchf-convergedcharging",
                               "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    active = [r for r in charging.values() if r["status"] == "ACTIVE"]
    obs.gauge("charging_data_active").set(len(active))
    obs.gauge("charging_data_total").set(len(charging.values()))


if __name__ == "__main__":
    obs.init("chf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)

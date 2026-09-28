"""
CAPIF: Common API Framework — the standards-native API gateway for the owned telco-AI grid.

CAPIF is 3GPP's answer to "how does an external application discover, onboard to, and get
authorized for the network's exposed APIs" — the telco-shaped equivalent of an API gateway /
developer portal (the "Kong for 5G"). Where the NEF (services/core/nef/nef.py) is the API
PROVIDER that publishes network-exposure APIs (monitoring, traffic influence), CAPIF is the
FRAMEWORK that sits in front of every such provider: it holds the catalog an external AF browses,
onboards that AF as an "API invoker", and issues the access token the AF then presents to the
provider. This is the exposure story's front gate — external apps discover + get authorized for
network APIs HERE (anchor: the AI-grid exposure epic, issues #17/#8).

This is a STANDALONE CAPIF core function (registers with the NRF as an AF/CAPIF core). No existing
NF is edited: CAPIF fronts the NEF by holding its published API descriptions, it does not modify
the NEF. Charter axis: BREADTH. Verb: HARVEST (TS 29.222 read for shape; authored clean-room, stdlib).

Spec anchors (TS 29.222 CAPIF — Common API Framework):
  CAPIF_API_Provider_Management   section 8.4 — an API provider domain (e.g. the NEF's operator)
                                  registers its API-provider functions (APF publishing, AEF exposing).
                                  POST /api-provider-management/v1/registrations.
  CAPIF_Publish_Service           section 8.3 — an APF publishes a serviceAPIDescription.
                                  POST /published-apis/v1/{apfId}/service-apis.
  CAPIF_Discover_Service          section 8.1 — an API invoker discovers published APIs (the catalog).
                                  GET /service-apis/v1/allServiceAPIs?api-name=...
  CAPIF_API_Invoker_Management    section 8.2 — an external AF onboards as an API invoker, receiving
                                  an apiInvokerId + onboarding credentials (OAuth2 client material).
                                  POST /api-invoker-management/v1/onboardedInvokers.
  CAPIF_Security                  section 8.5 / TS 33.122 — the invoker obtains a security context and
                                  an OAuth2 access token scoped to a published API.
                                  PUT  /capif-security/v1/trustedInvokers/{apiInvokerId}
                                  POST /capif-security/v1/trustedInvokers/{apiInvokerId}/token
  Nnrf_NFManagement / discovery   TS 29.510 5.2 / 5.3 (register as an AF/CAPIF core; discover the NEF).

Labeled simplifications (ledgered in procedures/capif_api_framework.txt):
  - OAuth2 / token crypto is SHAPED, not ENFORCED. onboarding credentials and the access token are
    real in SHAPE (a JWT-structured token: base64url header.payload.signature, an AccessTokenRsp per
    RFC 6749) but the signature is a stub digest, not an RS256/HS256 signature, and no provider
    verifies it yet. Real CAPIF uses PKI-bound tokens (TS 33.122). Hooks exist; a body change enforces.
  - The NEF is NOT yet gated behind CAPIF. This pass STANDS UP the framework (catalog + onboarding +
    token issuance); wiring the NEF to reject un-tokened AFs (its af_authorized() stub) is the
    integration follow-up. CAPIF publishes the NEF's APIs; it does not yet proxy calls to them.
  - Invoker/provider identity is self-asserted (no CAPIF onboarding-secret pre-provisioning, no CSR
    signing). Real CAPIF onboards via a one-time onboarding token + CSR. Labeled.

Run: python3 capif.py   (SBI on 127.0.0.1:7027, registers with the NRF as an AF/CAPIF core)
"""

import base64
import hashlib
import json
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

PORT = port("capif")
NRF = url("nrf")
app = SbiApp("capif")

# Persistence mirrors the other NFs (domain/statestore.py): sqlite3 when TELCO_STATE_DIR is set,
# in-memory otherwise. The four CAPIF registries, keyed by their CAPIF-assigned ids.
store = open_store("capif")
providers = store.collection("api_providers")       # apiProvDomId -> provider registration
published = store.collection("published_apis")       # apiId -> serviceAPIDescription
invokers = store.collection("api_invokers")          # apiInvokerId -> onboarded invoker
security = store.collection("security_contexts")      # apiInvokerId -> {serviceSecurity, tokens}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _b64url(obj):
    """base64url of a JSON object with no padding (JWT segment encoding)."""
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def shaped_jwt(claims):
    """A JWT-STRUCTURED access token (header.payload.signature). SHAPED, not signed: the third
    segment is a sha256 digest of the first two, NOT an RS256/HS256 signature. Real CAPIF binds
    the token to the invoker's key (TS 33.122). Labeled simplification — the shape lets a provider
    parse it today; enforcement is a follow-up."""
    header = _b64url({"alg": "none", "typ": "JWT", "note": "shaped-not-signed"})
    payload = _b64url(claims)
    sig = base64.urlsafe_b64encode(
        hashlib.sha256(f"{header}.{payload}".encode()).digest()).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def discover(nf_type, requester="AF"):
    """NRF discovery (same shape as the NEF's). Returns a base URL or None so a missing provider
    NF degrades honestly rather than crashing CAPIF."""
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


# ------------------------------------------------- CAPIF_API_Provider_Management (TS 29.222 8.4)
# An API provider domain (e.g. the operator that runs the NEF) registers its provider functions:
# an APF (API Publishing Function) that publishes APIs, and an AEF (API Exposing Function) that
# actually exposes them. CAPIF assigns ids the provider then uses to publish.

def _register_provider(domain_info, func_infos):
    dom_id = "domain-" + uuid.uuid4().hex[:12]
    reg_funcs = []
    for f in func_infos:
        role = f.get("apiProvFuncRole", "AEF")
        prefix = {"APF": "apf", "AEF": "aef", "AMF": "amf"}.get(role, "func")
        reg_funcs.append({
            "apiProvFuncId": f"{prefix}-" + uuid.uuid4().hex[:12],
            "apiProvFuncRole": role,
            "apiProvFuncInfo": f.get("apiProvFuncInfo"),
        })
    record = {
        "apiProvDomId": dom_id,
        "apiProvDomInfo": domain_info,
        "apiProvFuncs": reg_funcs,
        "createdAt": now_iso(),
    }
    providers.put(dom_id, record)
    return record


@app.route("POST", "/api-provider-management/v1/registrations")
def register_provider(params, query, body):
    funcs = body.get("apiProvFuncs")
    if not funcs:
        return problem(400, "Bad Request", detail="apiProvFuncs is mandatory",
                       cause="MANDATORY_IE_MISSING")
    record = _register_provider(body.get("apiProvDomInfo"), funcs)
    obs.log("api_provider_registered", apiProvDomId=record["apiProvDomId"],
            funcs=[f["apiProvFuncRole"] for f in record["apiProvFuncs"]])
    obs.counter("capif_api_providers_total").inc()
    return 201, record


@app.route("GET", "/api-provider-management/v1/registrations/{registrationId}")
def get_provider(params, query, body):
    record = providers.get(params["registrationId"])
    if record is None:
        return problem(404, "Not Found", detail="no such provider registration",
                       cause="PROVIDER_NOT_FOUND")
    return 200, record


# ------------------------------------------------------- CAPIF_Publish_Service (TS 29.222 8.3)
# An APF publishes a serviceAPIDescription. CAPIF assigns an apiId and holds it in the catalog
# that invokers discover. The apfId path segment scopes the publish to a provider's APF.

def _publish(apf_id, desc):
    api_id = "api-" + uuid.uuid4().hex[:12]
    record = dict(desc)
    record["apiId"] = api_id
    record["apfId"] = apf_id
    record["publishedAt"] = now_iso()
    published.put(api_id, record)
    return record


@app.route("POST", "/published-apis/v1/{apfId}/service-apis")
def publish_api(params, query, body):
    if not body.get("apiName"):
        return problem(400, "Bad Request", detail="apiName is mandatory",
                       cause="MANDATORY_IE_MISSING")
    record = _publish(params["apfId"], body)
    obs.log("service_api_published", apiId=record["apiId"], apiName=record["apiName"],
            apfId=params["apfId"])
    obs.counter("capif_published_apis_total").inc()
    return 201, record


@app.route("GET", "/published-apis/v1/{apfId}/service-apis/{serviceApiId}")
def get_published_api(params, query, body):
    record = published.get(params["serviceApiId"])
    if record is None or record.get("apfId") != params["apfId"]:
        return problem(404, "Not Found", detail="no such published API",
                       cause="API_NOT_FOUND")
    return 200, record


# ------------------------------------------------------ CAPIF_Discover_Service (TS 29.222 8.1)
# The gateway's catalog: an API invoker browses all published APIs, optionally filtered by name.
# An unknown api-name yields an EMPTY list (resolved, not faked).

@app.route("GET", "/service-apis/v1/allServiceAPIs")
def discover_service_apis(params, query, body):
    api_name = query.get("api-name")
    apis = list(published.values())
    if api_name:
        apis = [a for a in apis if a.get("apiName") == api_name]
    obs.log("service_apis_discovered", apiName=api_name, matches=len(apis))
    obs.counter("capif_discoveries_total").inc()
    return 200, {"serviceAPIDescriptions": apis}


# ------------------------------------------------ CAPIF_API_Invoker_Management (TS 29.222 8.2)
# An external AF onboards as an API invoker. CAPIF assigns an apiInvokerId and returns onboarding
# information carrying OAuth2 client material (SHAPED — see module ledger).

@app.route("POST", "/api-invoker-management/v1/onboardedInvokers")
def onboard_invoker(params, query, body):
    invoker_id = "invoker-" + uuid.uuid4().hex[:12]
    client_id = "client-" + uuid.uuid4().hex[:16]
    # SHAPED OAuth2 client secret — a random opaque token, not a provisioned/pinned credential.
    client_secret = base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(b"=").decode()
    record = {
        "apiInvokerId": invoker_id,
        "notificationDestination": body.get("notificationDestination"),
        "apiInvokerInformation": body.get("apiInvokerInformation"),
        "onboardingInformation": {
            # In real CAPIF this carries the signed apiInvokerCertificate; here it is a shaped
            # OAuth2 client_id/secret pair (labeled). The AF uses it to obtain access tokens.
            "apiInvokerPublicKey": body.get("onboardingInformation", {}).get("apiInvokerPublicKey"),
            "oauth2ClientId": client_id,
            "oauth2ClientSecret": client_secret,
        },
        "onboardingStatus": "ONBOARDED",
        "createdAt": now_iso(),
    }
    invokers.put(invoker_id, record)
    obs.log("api_invoker_onboarded", apiInvokerId=invoker_id)
    obs.counter("capif_invokers_total").inc()
    return 201, record


@app.route("GET", "/api-invoker-management/v1/onboardedInvokers/{onboardingId}")
def get_invoker(params, query, body):
    record = invokers.get(params["onboardingId"])
    if record is None:
        return problem(404, "Not Found", detail="no such onboarded invoker",
                       cause="INVOKER_NOT_FOUND")
    return 200, record


@app.route("DELETE", "/api-invoker-management/v1/onboardedInvokers/{onboardingId}")
def offboard_invoker(params, query, body):
    if invokers.get(params["onboardingId"]) is None:
        return problem(404, "Not Found", detail="no such onboarded invoker",
                       cause="INVOKER_NOT_FOUND")
    invokers.delete(params["onboardingId"])
    security.delete(params["onboardingId"])
    obs.log("api_invoker_offboarded", apiInvokerId=params["onboardingId"])
    return 204, {}


# ---------------------------------------------------------- CAPIF_Security (TS 29.222 8.5 / 33.122)
# An onboarded invoker registers a security context (which published APIs it wants) and then
# obtains an OAuth2 access token scoped to a published API. The token is SHAPED (see ledger).

@app.route("PUT", "/capif-security/v1/trustedInvokers/{invokerId}")
def register_security_context(params, query, body):
    invoker_id = params["invokerId"]
    if invokers.get(invoker_id) is None:
        return problem(404, "Not Found", detail="unknown invoker — onboard first",
                       cause="INVOKER_NOT_FOUND")
    record = {
        "apiInvokerId": invoker_id,
        # securityInfo: list of {aefId, apiId, prefSecurityMethods:[OAUTH]} the invoker requests.
        "securityInfo": body.get("securityInfo", []),
        "notificationDestination": body.get("notificationDestination"),
        "tokens": (security.get(invoker_id) or {}).get("tokens", []),
        "updatedAt": now_iso(),
    }
    security.put(invoker_id, record)
    obs.log("security_context_registered", apiInvokerId=invoker_id,
            apis=[s.get("apiId") for s in record["securityInfo"]])
    obs.counter("capif_security_contexts_total").inc()
    return 201, record


@app.route("POST", "/capif-security/v1/trustedInvokers/{invokerId}/token")
def obtain_access_token(params, query, body):
    """OAuth2 client-credentials token endpoint (RFC 6749 4.4), CAPIF-scoped. The invoker presents
    its client_id/secret and a scope naming the published API; CAPIF returns an AccessTokenRsp with
    a shaped JWT bound to the invoker + api (signature is a stub digest — see module ledger)."""
    invoker_id = params["invokerId"]
    invoker = invokers.get(invoker_id)
    if invoker is None:
        return problem(404, "Not Found", detail="unknown invoker — onboard first",
                       cause="INVOKER_NOT_FOUND")
    if body.get("grant_type") != "client_credentials":
        return problem(400, "Bad Request", detail="grant_type must be client_credentials",
                       cause="UNSUPPORTED_GRANT_TYPE")
    client_id = body.get("client_id")
    if client_id != invoker["onboardingInformation"]["oauth2ClientId"]:
        # Credential is CHECKED for match (shape of auth), though not cryptographically (labeled).
        return problem(401, "Unauthorized", detail="client_id does not match onboarded invoker",
                       cause="INVOKER_NOT_AUTHORIZED")
    scope = body.get("scope", "")
    # scope form (TS 29.222 / 33.122): "3gpp#<aefId>:<apiName>"; we accept a bare api name too.
    api_name = scope.split(":")[-1] if scope else None
    matches = [a for a in published.values()
               if api_name is None or a.get("apiName") == api_name]
    if api_name and not matches:
        return problem(400, "Bad Request", detail=f"no published API named {api_name!r} to scope",
                       cause="API_NOT_FOUND")
    expires_in = 3600
    claims = {
        "iss": "capif-core",
        "sub": invoker_id,
        "aud": [a["apiId"] for a in matches],
        "scope": scope,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
    }
    token = shaped_jwt(claims)
    # Record the issued token on the invoker's security context (create a light one if absent).
    ctx = security.get(invoker_id) or {"apiInvokerId": invoker_id, "securityInfo": [], "tokens": []}
    ctx.setdefault("tokens", []).append({"scope": scope, "issuedAt": now_iso(), "apiIds": claims["aud"]})
    security.put(invoker_id, ctx)
    obs.log("access_token_issued", apiInvokerId=invoker_id, scope=scope, apiIds=claims["aud"])
    obs.counter("capif_tokens_issued_total").inc()
    # AccessTokenRsp per RFC 6749 5.1 (the shape CAPIF returns).
    return 200, {"access_token": token, "token_type": "Bearer", "expires_in": expires_in,
                 "scope": scope}


# --------------------------------------------------------------------------- NRF registration
def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. CAPIF is modeled as an AF-class core function; nfType
    # "CAPIF" lets a provider (NEF) or an invoker discover the framework in the NRF.
    profile = {"nfType": "CAPIF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "capif-api-provider-management",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "capif-discover-service",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "capif-api-invoker-management",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]},
                   {"serviceName": "capif-security",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def seed_nef_provider():
    """Seed the catalog: register the NEF's operator as an API provider and publish the NEF's two
    Nnef exposure APIs (monitoring events, traffic influence) as service APIs. This is what an
    external AF discovers — the network's exposure surface, fronted by CAPIF. Idempotent: only
    seeds when the catalog is empty (a fresh in-memory boot), so a persisted stack keeps its state.

    The AEF endpoint is the NEF's own base URL (discovered via the NRF when it is up, else the
    netconfig default) — so the published description points a discovering AF at the REAL provider."""
    if any(a.get("apiName", "").startswith("nnef-") for a in published.values()):
        return
    nef_base = discover("NEF", requester="AF") or url("nef")
    provider = _register_provider(
        {"description": "NEF operator domain — the owned core's northbound exposure provider"},
        [{"apiProvFuncRole": "APF", "apiProvFuncInfo": "NEF publishing function"},
         {"apiProvFuncRole": "AEF", "apiProvFuncInfo": "NEF exposing function"}])
    apf_id = next(f["apiProvFuncId"] for f in provider["apiProvFuncs"]
                  if f["apiProvFuncRole"] == "APF")
    aef_id = next(f["apiProvFuncId"] for f in provider["apiProvFuncs"]
                  if f["apiProvFuncRole"] == "AEF")
    seeds = [
        ("nnef-eventexposure", "TS 29.522 Nnef_EventExposure — monitor a UE (reachability, location)",
         "/3gpp-monitoring-event/v1"),
        ("nnef-trafficinfluence", "TS 29.522 Nnef_TrafficInfluence — steer a UE's traffic to an edge DNAI",
         "/3gpp-traffic-influence/v1"),
    ]
    for name, desc, base_path in seeds:
        _publish(apf_id, {
            "apiName": name,
            "description": desc,
            "aefProfiles": [{
                "aefId": aef_id,
                "versions": [{"apiVersion": "v1", "resources": [{"resourceName": name,
                                                                 "commType": "REQUEST_RESPONSE",
                                                                 "uri": base_path}]}],
                "securityMethods": ["OAUTH"],
                "interfaceDescriptions": [{"ipv4Addr": nef_base}],
            }],
        })
    obs.log("nef_apis_seeded", apiProvDomId=provider["apiProvDomId"], nefBase=nef_base,
            apis=[s[0] for s in seeds])


def _scrape():
    obs.gauge("capif_published_apis_active").set(len(list(published.values())))
    obs.gauge("capif_invokers_active").set(len(list(invokers.values())))
    obs.gauge("capif_api_providers_active").set(len(list(providers.values())))


if __name__ == "__main__":
    obs.init("capif")
    obs.on_scrape(_scrape)
    register_with_nrf()
    seed_nef_provider()
    serve(app, PORT)

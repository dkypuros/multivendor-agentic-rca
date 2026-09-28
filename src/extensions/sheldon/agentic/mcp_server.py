"""MCP server: the O-Cloud's testimony, offered to somebody else's agent.

In a multi-vendor loop the orchestrator belongs to the NEP or the OSS/BSS — it asks the
questions. The platform's job is to answer for its own layer, through a typed boundary,
without handing anyone cluster credentials. That boundary is the Model Context Protocol
(TM Forum profiles it for telecom as MCP-T, IG1454), and this file is our side of it.

What crosses the boundary is deliberately asymmetric:

  READ   every plane's testimony — hardware (Metal3), platform (Redfish/BMC), cluster
         (OCM), inventory (O2-IMS). Schema-shaped, timestamped by the tool, with the
         deterministic signals already raised.
  WRITE  four verbs, every one dry-run by default, every one still subject to the
         guardrail and the class's licence before it can be applied for real. An agent
         calling this server cannot talk itself past either.

Deliberately NOT exposed: raw kubectl, arbitrary CR writes, credentials, or anything not
named in harness/sheldon/guardrails.yaml. The tool list IS the blast radius.

Transport is JSON-RPC 2.0 over stdio (MCP 2024-11-05), stdlib only, so it runs anywhere
python does — no SDK, no server to keep alive.

  Run:    python3 -m extensions.sheldon.agentic.mcp_server
  Probe:  echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m extensions.sheldon.agentic.mcp_server
"""

import json
import sys

from extensions.sheldon.agentic import guardrail, ocloud_tools as T, trust

PROTOCOL = "2024-11-05"
SERVER = {"name": "sheldon-ocloud", "version": "1.0.0"}

# The tool surface. Each entry is (description, JSON Schema, handler). This list is the
# contract an outside agent programs against — and the ceiling on what it can do.
TOOLS = {
    "ocloud_survey": (
        "Every plane's testimony in one sweep: hardware, cluster, inventory, provisioning. "
        "Start an investigation here.",
        {"type": "object", "properties": {}},
        lambda a: T.survey(),
    ),
    "ocloud_node_health": (
        "Hardware-plane testimony from the real Metal3 BareMetalHosts: provisioning state, "
        "power, operational status, BMC errors.",
        {"type": "object", "properties": {"node": {"type": "string",
         "description": "BareMetalHost name, e.g. bm-1. Omit for all."}}},
        lambda a: T.node_health(a.get("node")),
    ),
    "ocloud_cluster_health": (
        "Cluster-plane testimony from OCM: Available, Joined, and ClockSynced — the "
        "cluster-level echo of the timing chain.",
        {"type": "object", "properties": {"cluster": {"type": "string",
         "description": "ManagedCluster name, e.g. spoke-1. Omit for all."}}},
        lambda a: T.cluster_health(a.get("cluster")),
    ),
    "ocloud_bmc_power_state": (
        "Platform-plane testimony straight from the BMC over Redfish, independent of "
        "Kubernetes. If the cluster's view and the BMC's disagree, that is evidence.",
        {"type": "object", "required": ["node"],
         "properties": {"node": {"type": "string"}}},
        lambda a: T.bmc_power_state(a["node"]),
    ),
    "ocloud_inventory": (
        "Inventory-plane testimony: the O2-IMS AllocatedNodes and ResourcePool, and "
        "whether they still agree with live Metal3 truth.",
        {"type": "object", "properties": {}},
        lambda a: T.inventory(),
    ),
    "ocloud_service_health": (
        "Service-plane testimony from the OSS over TM Forum APIs: TMF638 service inventory "
        "state and TMF642 raised alarms. The only plane that knows whose service is affected.",
        {"type": "object", "properties": {}},
        lambda a: T.service_health(),
    ),
    "ocloud_service_impact": (
        "Which services ride a given resource, and how many of them are active. The business "
        "half of an RCA — feeds the guardrail's business-authorization check.",
        {"type": "object", "required": ["resource"],
         "properties": {"resource": {"type": "string",
          "description": "kind/name, e.g. BareMetalHost/bm-1"}}},
        lambda a: T.service_impact(a["resource"]),
    ),
    "ocloud_collect_diagnostics": (
        "Read-only remediation: gather every plane's view of one target. Mutates nothing.",
        {"type": "object", "required": ["target"],
         "properties": {"target": {"type": "string",
          "description": "kind/name, e.g. BareMetalHost/bm-1 or ManagedCluster/spoke-1"}}},
        lambda a: T.collect_diagnostics(a["target"]),
    ),
    "ocloud_trust_registry": (
        "The licence ledger: what each fault class is currently permitted to do, its "
        "ceiling, and the evidence behind it. An agent should read this before proposing.",
        {"type": "object", "properties": {}},
        lambda a: trust.summary(),
    ),
    "ocloud_check_proposal": (
        "Submit a proposal to the LLM-free guardrail and get the verdict WITHOUT acting. "
        "Returns allowed/mode/findings exactly as the loop would record them.",
        {"type": "object", "required": ["fault_class", "action", "target"],
         "properties": {"fault_class": {"type": "string"}, "action": {"type": "string"},
                        "target": {"type": "string"},
                        "sandbox_verdict": {"type": ["object", "null"]}}},
        lambda a: guardrail.evaluate(
            {"fault_class": a["fault_class"], "action": a["action"], "target": a["target"],
             "parameters": a.get("parameters", {}),
             "sandbox_verdict": a.get("sandbox_verdict")},
            trust.state_for(a["fault_class"])),
    ),
    "ocloud_power_on": (
        "ACTION. Bring a node up through its BMC. dry_run defaults to true and performs a "
        "real read-back; a live apply still requires the guardrail and the class licence.",
        {"type": "object", "required": ["node"],
         "properties": {"node": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True}}},
        lambda a: _guarded("OC-NodeDown", "redfish_power_on",
                           f"BareMetalHost/{a['node']}",
                           lambda dry: T.power_on(a["node"], dry_run=dry),
                           a.get("dry_run", True)),
    ),
    "ocloud_power_off": (
        "ACTION. Take a node down through its BMC — the tested inverse of power_on, and "
        "the lab's fault injection. dry_run defaults to true.",
        {"type": "object", "required": ["node"],
         "properties": {"node": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True}}},
        lambda a: _guarded("OC-NodeDown", "redfish_power_off",
                           f"BareMetalHost/{a['node']}",
                           lambda dry: T.power_off(a["node"], dry_run=dry),
                           a.get("dry_run", True)),
    ),
    "ocloud_reprovision_cluster": (
        "ACTION. Ask the O-Cloud for a cluster with one O2-IMS ProvisioningRequest — the "
        "same CR that built spoke-1 end to end. No inverse exists, so this class is "
        "capped at 'rehearsed' forever: a human signs, always.",
        {"type": "object", "required": ["cluster", "node_mac"],
         "properties": {"cluster": {"type": "string"}, "node_mac": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True}}},
        lambda a: _guarded("OC-ClusterUnavailable", "reprovision_cluster",
                           f"ProvisioningRequest/{a['cluster']}",
                           lambda dry: T.reprovision_cluster(a["cluster"], a["node_mac"], dry_run=dry),
                           a.get("dry_run", True)),
    ),
    "ocloud_trigger_inventory_reconcile": (
        "ACTION. Nudge the o2ims reconciler to re-read Metal3/OCM truth. Idempotent.",
        {"type": "object", "properties": {"dry_run": {"type": "boolean", "default": True}}},
        lambda a: _guarded("OC-InventoryDrift", "trigger_inventory_reconcile",
                           "ResourcePool/baremetal-pool-1",
                           lambda dry: T.trigger_inventory_reconcile(dry_run=dry),
                           a.get("dry_run", True)),
    ),
}


def _guarded(fault_class, action, target, run, dry_run):
    """Every ACTION tool goes through here. A dry run is always allowed (it is how the
    sandbox verdict is produced). A LIVE apply must first earn a verdict, then pass the
    guardrail, and the class licence must permit unattended application — otherwise the
    call returns refused with the findings, and nothing happens."""
    if dry_run:
        return run(True)
    verdict = run(True)                       # rehearse first, always
    decision = guardrail.evaluate(
        {"fault_class": fault_class, "action": action, "target": target,
         "parameters": {"nodes_touched": 1},
         "sandbox_verdict": {"apply_allowed": verdict.get("apply_allowed", True),
                             "rehearsed_at": verdict.get("rehearsed_at", ""),
                             "twin_divergence": 0.0}},
        trust.state_for(fault_class))
    if not decision["allowed"] or decision["mode"] != "policy-auto":
        return {"applied": False, "refused": True, "guardrail": decision,
                "reason": "live apply requires guardrail pass and an auto-capable licence; "
                          "use the loop with --approve for a human-signed run",
                "rehearsal": verdict}
    out = run(False)
    out["guardrail"] = decision
    return out


# ------------------------------------------------------------------ JSON-RPC plumbing

def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(req):
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if method == "initialize":
        return _result(rid, {"protocolVersion": PROTOCOL, "serverInfo": SERVER,
                             "capabilities": {"tools": {}}})
    if method in ("notifications/initialized", "initialized"):
        return None                                   # notification: no response
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, (d, s, _) in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return _error(rid, -32602, f"unknown tool: {name}")
        try:
            payload = TOOLS[name][2](args)
        except T.OCloudUnavailable as exc:
            return _result(rid, {"content": [{"type": "text", "text": json.dumps(
                {"error": "ocloud_unavailable", "detail": str(exc)[:240]})}],
                "isError": True})
        except Exception as exc:                       # never crash the caller's loop
            return _result(rid, {"content": [{"type": "text", "text": json.dumps(
                {"error": type(exc).__name__, "detail": str(exc)[:240]})}],
                "isError": True})
        return _result(rid, {"content": [
            {"type": "text", "text": json.dumps(payload, indent=2)}]})
    return _error(rid, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            print(json.dumps(_error(None, -32700, "parse error")), flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()

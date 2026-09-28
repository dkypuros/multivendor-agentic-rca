"""The typed tool surface over the real Sheldon O-Cloud.

This is the ONLY module in the agentic package that touches live infrastructure. Every
other module reasons over what this one returns. That separation is deliberate: an agent
never holds cluster credentials, never shapes its own evidence, and can only act through
verbs that are named here and allowlisted in harness/sheldon/guardrails.yaml.

Two kinds of verb, and the difference is the whole design:

  TESTIMONY (read)  node_health, cluster_health, bmc_power_state, inventory,
                    provisioning_requests, service_health, service_impact,
                    collect_diagnostics
                    -> Testimony objects (harness/sheldon/schemas/testimony.schema.json):
                       source, time of read, the live fields, and any deterministic
                       signals raised. Facts only; conclusions belong upstream.

  ACTION (write)    power_on, power_off, reprovision_cluster, trigger_inventory_reconcile
                    -> every one takes dry_run and DEFAULTS TO TRUE. A dry run performs a
                       real read-back or a server-side dry-run apply and returns the
                       verdict the guardrail requires before any live application.

Transport is kubectl against Sheldon's hub plus the sushy Redfish endpoint, matching
extensions/sheldon/metal3.py. Nothing here decides policy: refusal lives in guardrail.py,
licensing in trust.py. If the O-Cloud is unreachable these raise OCloudUnavailable, which
callers treat as 'target down' rather than a crash.

Config (env-overridable):
  SHELDON_KUBECONFIG   kubeconfig for the Sheldon hub (default ~/.kube/sheldon.yaml)
  SHELDON_REDFISH_URL  sushy Redfish base (no default)
  SHELDON_REDFISH_USER / SHELDON_REDFISH_PASS   optional basic auth
  SHELDON_TIMEOUT      per-call timeout seconds (default 20)
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

KUBECTL = os.environ.get("SHELDON_KUBECTL", "kubectl")
KUBECONFIG = os.environ.get("SHELDON_KUBECONFIG", os.path.expanduser("~/.kube/sheldon.yaml"))
REDFISH_URL = os.environ.get("SHELDON_REDFISH_URL", "").rstrip("/")
REDFISH_USER = os.environ.get("SHELDON_REDFISH_USER", "")
REDFISH_PASS = os.environ.get("SHELDON_REDFISH_PASS", "")
TIMEOUT = int(os.environ.get("SHELDON_TIMEOUT", "20"))

BMH_NS = os.environ.get("SHELDON_BMH_NAMESPACE", "baremetal-operator-system")
O2IMS_NS = os.environ.get("SHELDON_O2IMS_NAMESPACE", "oran-o2ims")

# The OSS/BSS plane. The owned stack's own TM Forum surfaces (services/oss/oss.py): TMF638 service
# inventory and TMF642 alarms. This is the ONLY plane that can answer "which services, whose
# SLA, and are we permitted to act right now" -- no infrastructure plane knows any of that.
OSS_URL = os.environ.get("TELCO_OSS_URL", "http://127.0.0.1:7040").rstrip("/")
PTP_URL = os.environ.get("TELCO_PTP_URL", "http://ptp-bridge:7091").rstrip("/")
# A DU needs +/-1.5us to hold 3GPP TS 38.133 time-alignment. This lab timestamps in software,
# so its noise floor is far above that -- the threshold below is set to catch a GROSS fault
# (a firmware-class offset), not to claim telecom accuracy. See PTP_FIDELITY in the docs.
PTP_OFFSET_LIMIT_NS = int(os.environ.get("TELCO_PTP_OFFSET_LIMIT_NS", "100000"))   # 100us
TMF638 = "/tmf-api/serviceInventoryManagement/v4/service"
TMF642 = "/tmf-api/alarmManagement/v4/alarm"

# Condition types OCM sets on a ManagedCluster. ClockSynced matters more than it looks:
# it is the cluster-level echo of the timing chain the RAN depends on.
COND_AVAILABLE = "ManagedClusterConditionAvailable"
COND_JOINED = "ManagedClusterJoined"
COND_CLOCK = "ManagedClusterConditionClockSynced"


class OCloudUnavailable(RuntimeError):
    """Sheldon's hub or its Redfish endpoint could not be reached."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kubectl(args, timeout=TIMEOUT):
    cmd = [KUBECTL, "--kubeconfig", KUBECONFIG, *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OCloudUnavailable(f"kubectl failed: {exc}") from exc
    if out.returncode != 0:
        raise OCloudUnavailable(f"kubectl {' '.join(args)} -> rc {out.returncode}: "
                                f"{out.stderr.strip()[:240]}")
    return out.stdout


def _kget(kind, name=None, namespace=None):
    """Read one object or a list, as JSON."""
    args = ["get", kind]
    if name:
        args.append(name)
    if namespace:
        args += ["-n", namespace]
    args += ["-o", "json"]
    doc = json.loads(_kubectl(args))
    return doc if name else doc.get("items", [])


def _testimony(plane, source, facts, healthy=None, signals=None):
    """Build one schema-shaped Testimony. The tool stamps the time, not the caller."""
    return {
        "plane": plane,
        "source": source,
        "observed_at": _now(),
        "facts": facts,
        "healthy": healthy,
        "signals": signals or [],
    }


def _conditions(obj):
    return {c.get("type"): c.get("status") for c in obj.get("status", {}).get("conditions", [])}


# --------------------------------------------------------------------- TESTIMONY (read)

def node_health(name=None):
    """Hardware-plane testimony: the real Metal3 BareMetalHosts.

    Signals raised: bmh_powered_off, bmh_operational_error.
    """
    hosts = _kget("bmh", name, BMH_NS)
    hosts = [hosts] if name else hosts
    out = []
    for h in hosts:
        st = h.get("status", {})
        prov = st.get("provisioning", {}) or {}
        powered = st.get("poweredOn")
        opstat = st.get("operationalStatus")
        err_type = st.get("errorType") or ""
        err_msg = (st.get("errorMessage") or "").strip()
        facts = {
            "provisioning_state": prov.get("state"),
            "powered_on": powered,
            "operational_status": opstat,
            "error_type": err_type or None,
            "error_message": err_msg or None,
            "bmc_address": (h.get("spec", {}).get("bmc", {}) or {}).get("address"),
        }
        signals = []
        if powered is False:
            signals.append("bmh_powered_off")
        if (opstat and opstat != "OK") or err_type or err_msg:
            signals.append("bmh_operational_error")
        out.append(_testimony(
            "hardware", f"BareMetalHost/{h['metadata']['name']}", facts,
            healthy=(not signals), signals=signals))
    return out


def cluster_health(name=None):
    """Cluster-plane testimony: OCM ManagedClusters.

    Signals raised: managedcluster_unavailable, managedcluster_clock_unsynced.
    """
    clusters = _kget("managedclusters", name)
    clusters = [clusters] if name else clusters
    out = []
    for c in clusters:
        cond = _conditions(c)
        facts = {
            "available": cond.get(COND_AVAILABLE),
            "joined": cond.get(COND_JOINED),
            "clock_synced": cond.get(COND_CLOCK),
            "hub_accepted": c.get("spec", {}).get("hubAcceptsClient"),
        }
        signals = []
        if cond.get(COND_AVAILABLE) != "True":
            signals.append("managedcluster_unavailable")
        if cond.get(COND_CLOCK) not in ("True", None):
            signals.append("managedcluster_clock_unsynced")
        out.append(_testimony(
            "cluster", f"ManagedCluster/{c['metadata']['name']}", facts,
            healthy=(not signals), signals=signals))
    return out


def inventory():
    """Inventory-plane testimony: the O2-IMS view (AllocatedNodes + ResourcePool), and
    whether it still agrees with live Metal3 truth.

    Signals raised: inventory_mismatch.
    """
    nodes = _kget("allocatednodes.clcm.openshift.io", namespace=O2IMS_NS)
    pools = _kget("resourcepools.ocloud.openshift.io", namespace=O2IMS_NS)
    allocated = sorted(n["metadata"]["name"] for n in nodes)
    live = sorted(h["metadata"]["name"] for h in _kget("bmh", namespace=BMH_NS)
                  if (h.get("status", {}).get("provisioning", {}) or {}).get("state") == "provisioned")
    facts = {
        "allocated_nodes": allocated,
        "provisioned_hosts": live,
        "resource_pools": [p["metadata"]["name"] for p in pools],
        "node_count": len(allocated),
    }
    signals = ["inventory_mismatch"] if allocated != live else []
    return [_testimony("inventory", "O2-IMS/baremetal-pool-1", facts,
                       healthy=(not signals), signals=signals)]


def bmc_power_state(node):
    """Platform-plane testimony straight from the BMC, over Redfish.

    Independent of Kubernetes: if the cluster's view and the BMC's view disagree, that
    disagreement is itself evidence. Signals raised: bmh_powered_off.
    """
    host = _kget("bmh", node, BMH_NS)
    addr = (host.get("spec", {}).get("bmc", {}) or {}).get("address", "")
    system = addr.rsplit("/", 1)[-1] if addr else ""
    if not system:
        raise OCloudUnavailable(f"no BMC address on BareMetalHost/{node}")
    doc = _redfish_get(f"/redfish/v1/Systems/{system}")
    power = doc.get("PowerState")
    signals = ["bmh_powered_off"] if power and power.lower() != "on" else []
    return [_testimony("platform", f"Redfish/{node}",
                       {"power_state": power, "system_id": system,
                        "redfish_endpoint": REDFISH_URL},
                       healthy=(not signals), signals=signals)]


def provisioning_requests():
    """Inventory-plane testimony: the O2-IMS ProvisioningRequests and their phase."""
    prs = _kget("provisioningrequests.clcm.openshift.io")
    out = []
    for p in prs:
        ann = p.get("metadata", {}).get("annotations", {}) or {}
        out.append(_testimony(
            "inventory", f"ProvisioningRequest/{p['metadata']['name']}",
            {"cluster": p.get("spec", {}).get("name"),
             "template": p.get("spec", {}).get("templateName"),
             "phase": ann.get("o2ims.lab/phase"),
             "node": ann.get("o2ims.lab/node")},
            healthy=(ann.get("o2ims.lab/phase") in (None, "fulfilled")),
            signals=[]))
    return out


def collect_diagnostics(target):
    """Read-only remediation: gather every plane's view of one target. Mutates nothing,
    which is why classes whose contract is collect_diagnostics need no rehearsal."""
    kind, _, name = target.partition("/")
    bundle = []
    if kind in ("BareMetalHost", "bmh"):
        bundle += node_health(name)
        try:
            bundle += bmc_power_state(name)
        except OCloudUnavailable as exc:
            bundle.append(_testimony("platform", f"Redfish/{name}",
                                     {"error": str(exc)[:160]}, healthy=None))
    elif kind in ("ManagedCluster", "managedcluster"):
        bundle += cluster_health(name)
    bundle += inventory()
    bundle += service_health()
    return bundle


def survey():
    """Every plane, one sweep. What the loop calls to open an investigation.

    Five planes: hardware, cluster, inventory, service, platform. The service plane comes
    from the owned stack's own TM Forum APIs and is the only one that knows whose SLA is at
    stake; the platform plane comes from the BMC.

    The BMC is polled per node ON PURPOSE, and it matters more than it looks. Measured on
    this lab: seconds after a real power-off, Redfish reports PowerState=Off while the
    BareMetalHost still reports poweredOn=true and the ManagedCluster still reports
    Available=True — Kubernetes has not noticed yet. The platform plane sees the truth
    first, which is precisely why it has to testify separately instead of being inferred
    from the cluster's view of itself.
    """
    out = []
    for fn in (node_health, cluster_health, inventory, provisioning_requests, service_health,
                 ptp_health, nic_firmware):
        try:
            out += fn()
        except OCloudUnavailable as exc:
            out.append(_testimony("platform", f"tool/{fn.__name__}",
                                  {"error": str(exc)[:160]}, healthy=None))
    for host in [t["source"].split("/", 1)[1] for t in out if t["plane"] == "hardware"]:
        try:
            out += bmc_power_state(host)
        except OCloudUnavailable as exc:
            out.append(_testimony("platform", f"Redfish/{host}",
                                  {"error": str(exc)[:160]}, healthy=None))
    return out


def service_health():
    """Service-plane testimony: the OSS/BSS view, over TM Forum APIs.

    This plane is the SYMPTOM plane. A customer feels a service breach; the cause almost
    always lives deeper (a node, a clock, a cluster). So its signals route LAST -- an
    infrastructure cause always wins over a service symptom. What this plane uniquely
    contributes is the half no infrastructure object knows: which services are affected,
    and therefore whose SLA is in play.

    Signals raised: service_inactive (TMF638 state), service_alarm_raised (TMF642).
    Unreachable OSS is reported as an explicit gap, never as silence -- an RCA missing a
    plane should say so rather than quietly conclude from three quarters of the evidence.
    """
    out = []
    try:
        services = _oss_get(TMF638)
    except OCloudUnavailable as exc:
        return [_testimony("service", "OSS/serviceInventory",
                           {"error": str(exc)[:160],
                            "note": "service plane silent -- RCA is incomplete, not clean"},
                           healthy=None, signals=[])]
    for s in services:
        state = s.get("state") or ("active" if s.get("isServiceEnabled") else "inactive")
        chars = {c.get("name"): c.get("value") for c in s.get("serviceCharacteristic", [])}
        facts = {"service_state": state,
                 "enabled": s.get("isServiceEnabled"),
                 "supporting_resources": s.get("supportingResource", []),
                 "characteristics": chars}
        signals = ["service_inactive"] if state != "active" else []
        out.append(_testimony("service", f"Service/{s.get('id')}", facts,
                              healthy=(not signals), signals=signals))
    try:
        for a in _oss_get(TMF642):
            if str(a.get("state", "")).lower() != "raised":
                continue
            out.append(_testimony(
                "service", f"Alarm/{a.get('id')}",
                {"alarm_type": a.get("alarmType"),
                 "perceived_severity": a.get("perceivedSeverity"),
                 "probable_cause": a.get("probableCause"),
                 "alarmed_object": a.get("alarmedObject")},
                healthy=False, signals=["service_alarm_raised"]))
    except OCloudUnavailable:
        pass                       # inventory answered; alarms are a bonus, not a requirement
    return out


def ptp_health():
    """PLATFORM-plane testimony: is the node's clock actually disciplined?

    This reads the timing plane on the O-Cloud hub -- a real two-node PTP domain (two ptp4l
    instances exchanging real Sync/Follow_Up/Delay_Req over veth, running real BMCA) plus a
    ptp_mock PHC standing in for a NIC hardware clock the Realtek silicon does not have.

    Honest about fidelity, because a timing claim that overstates itself is worse than no
    claim: the PROTOCOL and the state machines are real, the ACCURACY is not. Every testimony
    carries `timestamping` and `clock_class` so nothing downstream can mistake a software-
    timestamped mock for an E810 disciplined by GNSS.
    """
    try:
        doc = _ptp_get("/ptp")
    except Exception as exc:                       # the plane being dark is itself a fact
        return [_testimony("platform", "PTP/hub",
                           {"reachable": False, "reason": str(exc)[:120]},
                           healthy=None, signals=[])]
    off = doc.get("phc_offset_ns")
    signals = []
    if doc.get("phc") != "present" or doc.get("grandmaster_daemon") != "running":
        signals.append("ptp_daemon_down")
    if doc.get("du_port_state") not in ("SLAVE", "UNCALIBRATED"):
        signals.append("ptp_not_locked")
    if isinstance(off, int) and abs(off) > PTP_OFFSET_LIMIT_NS:
        signals.append("ptp_offset_exceeded")
    facts = {k: doc.get(k) for k in
             ("phc", "gm_port_state", "du_port_state", "gm_identity", "phc_offset_ns",
              "timestamping", "clock_class", "phc2sys")}
    facts["offset_limit_ns"] = PTP_OFFSET_LIMIT_NS
    facts["fidelity"] = doc.get("fidelity_note")
    return [_testimony("platform", "PTP/hub", facts,
                       healthy=(not signals), signals=signals)]


def nic_firmware(node=None):
    """HARDWARE-plane testimony: the NIC firmware each BareMetalHost is running.

    The second of the three witnesses. Metal3's inspection data carries NIC details, so a
    firmware level that correlates with a timing fault is readable from the same object the
    hardware plane already testifies about -- no new access path, no new credential.
    """
    out = []
    for h in _kget("bmh", namespace=BMH_NS):
        name = h["metadata"]["name"]
        if node and name != node:
            continue
        hw = (h.get("status", {}).get("hardwareDetails") or {})
        nics = hw.get("nics") or []
        fw = (h.get("status", {}).get("hardwareDetails", {}).get("firmware") or {})
        suspect = h["metadata"].get("annotations", {}).get("telco-lab/nic-firmware-suspect")
        signals = ["nic_firmware_suspect"] if suspect else []
        out.append(_testimony("hardware", f"NICFirmware/{name}",
                              {"nic_count": len(nics),
                               "nic_models": sorted({n.get("model") for n in nics if n.get("model")}),
                               "bios": fw.get("bios"),
                               "suspect_firmware": suspect},
                              healthy=(not signals), signals=signals))
    return out


def service_impact(resource):
    """Which services does this resource carry, and therefore whose SLA is exposed?

    The business half of an RCA. Returns an assessment (not a testimony) that the loop
    attaches to the audit event and the guardrail consults, so 'is this worth doing now'
    is answered with evidence instead of instinct. A resource that supports nothing is a
    safer thing to touch than one carrying a live service, and that difference is only
    visible from the OSS.
    """
    _, _, name = resource.partition("/")
    try:
        services = _oss_get(TMF638)
    except OCloudUnavailable as exc:
        return {"known": False, "reason": str(exc)[:160], "impacted": [], "count": 0}
    impacted = []
    for s in services:
        refs = [str(r.get("id", r)) for r in s.get("supportingResource", [])]
        chars = {c.get("name"): str(c.get("value")) for c in s.get("serviceCharacteristic", [])}
        if any(name and name in r for r in refs) or any(name and name in v for v in chars.values()):
            impacted.append({"service": s.get("id"),
                             "state": s.get("state"),
                             "enabled": s.get("isServiceEnabled")})
    return {"known": True, "resource": resource, "impacted": impacted,
            "count": len(impacted),
            "active_impacted": sum(1 for i in impacted if i.get("state") == "active")}


def _ptp_get(path):
    req = urllib.request.Request(f"{PTP_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or "{}")


def _oss_get(path):
    req = urllib.request.Request(f"{OSS_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "[]")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise OCloudUnavailable(f"OSS {path} unreachable at {OSS_URL}: {exc}") from exc


# --------------------------------------------------------------------- ACTION (write)

def power_on(node, dry_run=True):
    """Bring a bare-metal node back up through its BMC. Reversible; inverse is power_off."""
    return _power(node, "On", dry_run)


def power_off(node, dry_run=True):
    """Take a node down through its BMC. Used as the tested inverse, and as the demo's
    fault injection (see loop.py --inject-fault)."""
    return _power(node, "ForceOff", dry_run)


def _power(node, reset_type, dry_run):
    host = _kget("bmh", node, BMH_NS)
    addr = (host.get("spec", {}).get("bmc", {}) or {}).get("address", "")
    system = addr.rsplit("/", 1)[-1] if addr else ""
    if not system:
        raise OCloudUnavailable(f"no BMC address on BareMetalHost/{node}")
    before = _redfish_get(f"/redfish/v1/Systems/{system}").get("PowerState")
    want = "On" if reset_type == "On" else "Off"
    if dry_run:
        # A real rehearsal, not a mock: read the BMC back and report whether the action
        # would change anything and whether the endpoint is actually reachable.
        return {
            "action": f"redfish_power_{want.lower()}", "target": f"BareMetalHost/{node}",
            "dry_run": True, "apply_allowed": True,
            "method": "redfish read-back (GET Systems/<id>) — no write issued",
            "power_state_before": before, "would_change": (before != want),
            "rehearsed_at": _now(),
        }
    _redfish_post(f"/redfish/v1/Systems/{system}/Actions/ComputerSystem.Reset",
                  {"ResetType": reset_type})
    return {
        "action": f"redfish_power_{want.lower()}", "target": f"BareMetalHost/{node}",
        "dry_run": False, "applied_at": _now(),
        "power_state_before": before,
        "power_state_after": _redfish_get(f"/redfish/v1/Systems/{system}").get("PowerState"),
    }


def reprovision_cluster(cluster, node_mac, dry_run=True):
    """Ask the O-Cloud for a cluster with one O2-IMS ProvisioningRequest.

    This is the same CR that built spoke-1 end to end. It has NO inverse: a rebuild
    destroys node state, which is exactly why taxonomy.yaml caps OC-ClusterUnavailable
    at `rehearsed` forever. The dry run is a server-side apply, so the API server itself
    validates the object without persisting it.
    """
    manifest = {
        "apiVersion": "clcm.openshift.io/v1alpha1",
        "kind": "ProvisioningRequest",
        "metadata": {"name": cluster},
        "spec": {
            "name": cluster,
            "description": "agentic remediation: rebuild an unavailable spoke via O2-IMS",
            "templateName": "kubeadm-single-node",
            "templateVersion": "v1",
            "templateParameters": {"nodeClusterName": cluster, "bootInterfaceMAC": node_mac},
        },
    }
    args = ["apply", "-f", "-", "-o", "json"]
    if dry_run:
        args.insert(1, "--dry-run=server")
    try:
        proc = subprocess.run(
            [KUBECTL, "--kubeconfig", KUBECONFIG, *args],
            input=json.dumps(manifest), capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OCloudUnavailable(f"kubectl apply failed: {exc}") from exc
    ok = proc.returncode == 0
    result = {
        "action": "reprovision_cluster", "target": f"ProvisioningRequest/{cluster}",
        "dry_run": dry_run, "apply_allowed": ok,
        "method": "kubectl apply --dry-run=server" if dry_run else "kubectl apply",
        "detail": (proc.stderr or proc.stdout).strip()[:240],
    }
    result["rehearsed_at" if dry_run else "applied_at"] = _now()
    return result


def trigger_inventory_reconcile(dry_run=True):
    """Nudge the o2ims reconciler to re-read Metal3/OCM truth. Idempotent by design:
    re-running converges, so the 'inverse' is simply running it again."""
    if dry_run:
        return {"action": "trigger_inventory_reconcile", "target": "ResourcePool/baremetal-pool-1",
                "dry_run": True, "apply_allowed": True,
                "method": "annotation write withheld; reconciler polls on its own interval",
                "rehearsed_at": _now()}
    _kubectl(["-n", O2IMS_NS, "annotate", "resourcepools.ocloud.openshift.io",
              "baremetal-pool-1", f"o2ims.lab/reconcile-requested={_now()}", "--overwrite"])
    return {"action": "trigger_inventory_reconcile", "target": "ResourcePool/baremetal-pool-1",
            "dry_run": False, "applied_at": _now()}


# --------------------------------------------------------------------- Redfish transport

def _redfish_req(path, data=None):
    req = urllib.request.Request(f"{REDFISH_URL}{path}", method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if REDFISH_USER:
        token = base64.b64encode(f"{REDFISH_USER}:{REDFISH_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=TIMEOUT) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw) if raw.strip().startswith("{") else {"status": resp.status}
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 202, 204):
            return {"status": exc.code}
        raise OCloudUnavailable(f"redfish {path} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OCloudUnavailable(f"redfish {path} unreachable: {exc}") from exc


def _redfish_get(path):
    return _redfish_req(path)


def _redfish_post(path, data):
    return _redfish_req(path, data)


if __name__ == "__main__":
    # Smoke test: print one sweep of every plane's testimony.
    print(json.dumps(survey(), indent=2))

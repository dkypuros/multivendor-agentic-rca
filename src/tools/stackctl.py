"""
stackctl: one command runs the world (issue #24).

Start / stop / supervise the whole owned telco stack — the same process inventory, dependency
order, and readiness checks the acceptance spec runners use (run_golden_scenario_spec.py,
run_agent_heal_spec.py, run_grid_agent_spec.py), promoted into an operator tool so a human can
bring the stack up once and drive it interactively.

Usage:
  python3 tools/stackctl.py up <profile> [--stubs] [--supervise]
  python3 tools/stackctl.py down
  python3 tools/stackctl.py status
  python3 tools/stackctl.py logs <service> [-n LINES]

Profiles (cumulative):
  core    5G core only: nrf, udm, amf, smf, upf
  ran     core + split RAN: ocucp, ocuup, odu, oru
  edge    ran + edge plane: ocloud (O2 IMS/DMS), edge_upf, edge_app (EAS)
  full    everything the golden scenario runs, plus both RICs: edge + nearrt_ric, smo,
          gridctl (8700, the owned five-verb grid control plane), oss, bss, bss_billing
  agents  full + ran_heal_agent (7070) + grid_capacity_agent (7071)

...and one profile that is NOT cumulative, because what it leaves out is its whole point:
  buyside the AI-grid buy side plus the core it orders against, and nothing else — no
          split RAN, no RIC/SMO, no IMS/EPC, no agents (see BUYSIDE below).

The OWNED gridctl (services/grid/gridctl.py, issue #17) is the default provider of :8700 in
the full/agents profiles: it supersedes the bmaas_stub spec fixture with a contract superset
(allocate/isolate/image/meter/wipe), so BSS ai-workbench / bundle orders complete without
--stubs. --stubs still adds the tests/helpers spec fixtures — aigateway_stub 8710 (without it
the grid capacity agent has nothing to sense) and, on profiles that do NOT include gridctl,
bmaas_stub 8700; when gridctl is in the profile the bmaas_stub is skipped (same port, and the
owned service is the deployment-grade provider). For a live agent demo: `up agents --stubs`.

ran_heal_agent mounts the REAL oran-agent-harness (HARNESS_PATH env var, default
../oran-agent-harness next to this repo) and needs PyYAML + jsonschema; `up agents` preflights
all three and fails loudly BEFORE starting anything, mirroring the agent's own startup check.

SUPERVISION IS OFF BY DEFAULT, deliberately: killing oru.py is the standing fault-injection
hook (CLAUDE.md: kill oru.py -> SMO O1 reads ru DISCONNECTED -> TMF642 critical alarm -> the
P9 heal agent proposes). A supervisor that auto-restarts the O-RU would race the alarm demos
and fight run_golden_scenario_spec G7 / run_agent_heal_spec S2. `up <profile> --supervise`
opts in: it stays in the foreground, restarts any service that dies (exponential backoff,
1s doubling to 30s), and logs every restart. Ctrl-C leaves the stack running; use `down`.

State lives under .stack/ at the repo root (gitignored): .stack/state.json is the registry,
.stack/logs/<service>.log the captured output. Stdlib only, like the rest of the owned stack.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters.sbi_http import request  # noqa: E402  (the same client the specs use)
from domain.netconfig import port, url  # noqa: E402  (single source of truth, issue #26)

PY = sys.executable
STACK_DIR = ROOT / ".stack"
STATE_FILE = STACK_DIR / "state.json"
LOG_DIR = STACK_DIR / "logs"

O2IMS = url("ocloud") + "/o2ims-infrastructureInventory/v1"


def _odu_reports_ru_connected():
    """O-RU readiness: it has no HTTP port; ready == the O-DU fronthaul shows CONNECTED
    (the same wait the golden scenario uses)."""
    try:
        status, body = request("GET", url("odu") + "/odu/status")
        return status == 200 and body.get("ru") == "CONNECTED"
    except OSError:
        return False


# The full inventory, in dependency order (mined from the spec runners: O-Cloud first — edge
# NFs deploy onto it — then core NRF-first, edge plane, split RAN CU->DU, RICs/SMO, stubs
# before their northbound consumers, OSS before BSS (TMF641 target), O-RU last among network
# elements, agents last of all so they never flag a cell that is born dead).
# Ports and base URLs come from domain/netconfig (the same source the services read), so
# TELCO_PORT_* / TELCO_PORT_OFFSET move the tool and the stack together.
# Fields: name, script (repo-relative), port, ready_url (any HTTP answer = up),
#         profiles (which profiles start it), health (one-liner for `status`).
SERVICES = [
    dict(name="ocloud", script="services/edge/ocloud.py", port=port("ocloud"),
         ready=f"{O2IMS}/resourcePools", profiles={"edge", "full", "agents"}),
    dict(name="nrf", script="services/core/nrf/nrf.py", port=port("nrf"),
         ready=url("nrf") + "/nnrf-nfm/v1/nf-instances",
         profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="udm", script="services/core/udm/udm.py", port=port("udm"),
         ready=url("udm") + "/nudm-sdm/v2/imsi-001010000000001/am-data",
         profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="amf", script="services/core/amf/amf.py", port=port("amf"),
         ready=url("amf") + "/amf/ue-contexts/none",
         profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="smf", script="services/core/smf/smf.py", port=port("smf"),
         ready=url("smf") + "/smf/sessions",
         profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="upf", script="services/core/upf/upf.py", port=port("upf"),
         ready=url("upf") + "/upf/sessions",
         profiles={"core", "ran", "edge", "full", "agents"}),
    # SBA network functions harvested clean-room into the owned stack (epic #9): each
    # registers with the NRF and is additive (its core integration falls back to
    # legacy behavior when it is absent).
    dict(name="pcf", script="services/core/pcf/pcf.py", port=port("pcf"),
         ready=url("pcf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="nssf", script="services/core/nssf/nssf.py", port=port("nssf"),
         ready=url("nssf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="nef", script="services/core/nef/nef.py", port=port("nef"),
         ready=url("nef") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="chf", script="services/core/chf/chf.py", port=port("chf"),
         ready=url("chf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="ausf", script="services/core/ausf/ausf.py", port=port("ausf"),
         ready=url("ausf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="nwdaf", script="services/core/nwdaf/nwdaf.py", port=port("nwdaf"),
         ready=url("nwdaf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="udr", script="services/core/udr/udr.py", port=port("udr"),
         ready=url("udr") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="bsf", script="services/core/bsf/bsf.py", port=port("bsf"),
         ready=url("bsf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="scp", script="services/core/scp/scp.py", port=port("scp"),
         ready=url("scp") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="sepp", script="services/core/sepp/sepp.py", port=port("sepp"),
         ready=url("sepp") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="udsf", script="services/core/udsf/udsf.py", port=port("udsf"),
         ready=url("udsf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="n3iwf", script="services/core/n3iwf/n3iwf.py", port=port("n3iwf"),
         ready=url("n3iwf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="nssaaf", script="services/core/nssaaf/nssaaf.py", port=port("nssaaf"),
         ready=url("nssaaf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="eir", script="services/core/eir/eir.py", port=port("eir"),
         ready=url("eir") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="capif", script="services/core/capif/capif.py", port=port("capif"),
         ready=url("capif") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="lmf", script="services/core/lmf/lmf.py", port=port("lmf"),
         ready=url("lmf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="gmlc", script="services/core/gmlc/gmlc.py", port=port("gmlc"),
         ready=url("gmlc") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="tsctsf", script="services/core/tsctsf/tsctsf.py", port=port("tsctsf"),
         ready=url("tsctsf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    # IMS voice subsystem (epic #21)
    dict(name="ims_hss", script="services/core/ims_hss/ims_hss.py", port=port("ims_hss"),
         ready=url("ims_hss") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="pcscf", script="services/core/pcscf/pcscf.py", port=port("pcscf"),
         ready=url("pcscf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="icscf", script="services/core/icscf/icscf.py", port=port("icscf"),
         ready=url("icscf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="scscf", script="services/core/scscf/scscf.py", port=port("scscf"),
         ready=url("scscf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    dict(name="mrf", script="services/core/mrf/mrf.py", port=port("mrf"),
         ready=url("mrf") + "/", profiles={"core", "ran", "edge", "full", "agents"}),
    # 4G EPC / EPS subsystem (config-resolved peers, no NRF)
    dict(name="hss4g", script="services/core/hss4g/hss4g.py", port=port("hss4g"),
         ready=url("hss4g") + "/", profiles={"core", "full", "agents"}),
    dict(name="pgw", script="services/core/pgw/pgw.py", port=port("pgw"),
         ready=url("pgw") + "/", profiles={"core", "full", "agents"}),
    dict(name="sgw", script="services/core/sgw/sgw.py", port=port("sgw"),
         ready=url("sgw") + "/", profiles={"core", "full", "agents"}),
    dict(name="mme", script="services/core/mme/mme.py", port=port("mme"),
         ready=url("mme") + "/", profiles={"core", "full", "agents"}),
    dict(name="edge_upf", script="services/edge/edge_upf.py", port=port("edge_upf"),
         ready=url("edge_upf") + "/upf/sessions", profiles={"edge", "full", "agents"}),
    dict(name="edge_app", script="services/edge/edge_app.py", port=port("edge_app"),
         ready=url("edge_app") + "/eas/status", profiles={"edge", "full", "agents"}),
    dict(name="ocucp", script="services/ran/ocucp/ocucp.py", port=port("ocucp"),
         ready=url("ocucp") + "/ocucp/ue-contexts/none",
         profiles={"ran", "edge", "full", "agents"}),
    dict(name="ocuup", script="services/ran/ocuup/ocuup.py", port=port("ocuup"),
         ready=url("ocuup") + "/ocuup/bearers",
         profiles={"ran", "edge", "full", "agents"}),
    dict(name="odu", script="services/ran/odu/odu.py", port=port("odu"),
         ready=url("odu") + "/odu/status",
         profiles={"ran", "edge", "full", "agents"}),
    dict(name="nearrt_ric", script="services/ric/nearrt_ric.py", port=port("nearrt_ric"),
         ready=url("nearrt_ric") + "/ric/kpm", profiles={"full", "agents"}),
    dict(name="smo", script="services/smo/smo.py", port=port("smo"),
         ready=url("smo") + "/smo/o1/du/status", profiles={"full", "agents"}),
    # gridctl OWNS :8700 in the deployed stack (issue #17): the Rafay-style five-verb
    # control plane, a contract superset of the bmaas_stub fixture on the same port.
    dict(name="gridctl", script="services/grid/gridctl.py", port=port("gridctl"),
         ready=url("gridctl") + "/v1/nodes", profiles={"full", "agents"}),
    # NSMF slice management (epic #11 / final.tex Ch.4): resolves slice templates into
    # S-NSSAIs + live SMF UPF-selection rules; management-plane NF, starts after the core.
    dict(name="nsmf", script="services/core/nsmf/nsmf.py", port=port("nsmf"),
         ready=url("nsmf") + "/nsmf/v1/slice-templates", profiles={"full", "agents"}),
    dict(name="bmaas_stub", script="tests/helpers/bmaas_stub.py", port=port("bmaas_stub"),
         ready=url("bmaas_stub") + "/v1/nodes", profiles=set(), stub=True),
    dict(name="aigateway_stub", script="tests/helpers/aigateway_stub.py",
         port=port("aigateway_stub"),
         ready=url("aigateway_stub") + "/v1/usage", profiles=set(), stub=True),
    dict(name="oss", script="services/oss/oss.py", port=port("oss"),
         ready=url("oss") + "/tmf-api/serviceOrdering/v4/serviceOrder",
         profiles={"full", "agents"}),
    dict(name="bss", script="services/bss/bss.py", port=port("bss"),
         ready=url("bss") + "/tmf-api/productCatalogManagement/v4/productOffering",
         profiles={"full", "agents"}),
    # AI-grid closure bridge: idle (health "disabled") unless OSAC_ENABLED=1 — safe in
    # every profile, same convention as the duranta federation weld.
    dict(name="osac_closure", script="services/oss/osac_closure.py",
         port=port("osac_closure"), ready=url("osac_closure") + "/closure/health",
         profiles={"full", "agents"}),
    dict(name="bss_billing", script="services/bss_billing/billing.py",
         port=port("bss_billing"),
         ready=url("bss_billing") + "/billing/rate-plan", profiles={"full", "agents"}),
    # O-RU last among network elements; readiness is the O-DU seeing it CONNECTED.
    dict(name="oru", script="services/ran/oru/oru.py", port=None,
         ready=_odu_reports_ru_connected, profiles={"ran", "edge", "full", "agents"}),
    dict(name="ran_heal_agent", script="services/agent/ran_heal_agent.py",
         port=port("ran_heal_agent"),
         ready=url("ran_heal_agent") + "/agent/status", profiles={"agents"},
         needs_harness=True),
    dict(name="grid_capacity_agent", script="services/agent/grid_capacity_agent.py",
         port=port("grid_capacity_agent"),
         ready=url("grid_capacity_agent") + "/agent/capacity/config",
         profiles={"agents"}),
]

# One-line health probes for `status` (falls back to the readiness URL).
HEALTH = {
    "odu": (url("odu") + "/odu/status", lambda b: f"ru={b.get('ru')}"),
    "smo": (url("smo") + "/smo/o1/du/status", lambda b: f"o1 ru={b.get('ru')}"),
    "nearrt_ric": (url("nearrt_ric") + "/ric/kpm",
                   lambda b: f"kpm reports={b.get('reports')} "
                             f"ruConnected={(b.get('latest') or {}).get('ruConnected')}"),
    "bss": (url("bss") + "/tmf-api/productCatalogManagement/v4/productOffering",
            lambda b: f"catalog offerings={len(b)}"),
    "bss_billing": (url("bss_billing") + "/billing/rate-plan",
                    lambda b: "rate-plan " + ",".join(
                        f"{k}={v.get('pricePerMb')}/MB"
                        for k, v in (b.get("ratePlan") or {}).items())),
    "oss": (url("oss") + "/tmf-api/alarmManagement/v4/alarm?state=raised",
            lambda b: f"raised alarms={len(b)}"),
    "ocloud": (f"{O2IMS}/resourcePools",
               lambda b: f"pools={len(b.get('resourcePools', b) or [])}"),
    "gridctl": (url("gridctl") + "/v1/nodes",
                lambda b: f"nodes={len(b)} "
                          f"free={sum(1 for n in b if not n.get('allocated_to'))}"),
    "amf": (url("amf") + "/amf/ue-contexts/none", lambda b: "sbi answering"),
    "ran_heal_agent": (url("ran_heal_agent") + "/agent/status",
                       lambda b: f"harness={'mounted' if b.get('harness') else '?'}"),
    "grid_capacity_agent": (url("grid_capacity_agent") + "/agent/capacity/config",
                            lambda b: f"policy={b.get('policy')}"),
}

PROFILES = ("core", "ran", "edge", "full", "agents", "buyside")

# The Phase-0 buy-side profile (docs/plans/connect-the-dots.md Phase 0). Membership is an
# EXPLICIT list rather than another cumulative tier because this profile is defined by its
# exclusions: `full` would drag in services/ran + the O-RU (a separate workstream owns the
# radio, and the standing `kill oru.py` fault-injection hook must not be disturbed), the two
# RICs, the SMO, IMS, the 4G EPC and the agents — none of which the "one order buys a GPU
# cluster" story touches. Ordering below is irrelevant to startup (the tiered dependency
# gate decides that); it follows the SERVICES table, which is where the order lives.
BUYSIDE = frozenset({
    # Core, NRF-first. The AI-grid zone anchor writes its UPF-selection rule into the SMF, so
    # the SMF is load-bearing here; its policy/charging/slice-selection peers (PCF, CHF, NSSF)
    # and the AMF's auth/data peers (AUSF, UDM, UDR) are present so the core is COHERENT for
    # the session phases that follow, not just answering on a port.
    "nrf", "udm", "udr", "ausf", "amf", "smf", "upf", "pcf", "nssf", "chf",
    # Edge substrate + the PSA the metro-east zone actually names (domain/aigrid_zones:
    # upf="edge"). The O-Cloud also holds the O2-IMS inventory that the o2ims-provisioning
    # connector stages into and that osac_closure's TMF639 weld writes.
    "ocloud", "edge_upf",
    # The owned five-verb grid control plane the gpu/bmaas connectors call (:8700).
    "gridctl",
    # The buy side itself: TMF641 orchestrator, TMF622 storefront, and the closure bridge
    # (idle unless OSAC_ENABLED=1).
    "oss", "bss", "osac_closure",
})
# Deliberately ABSENT from BUYSIDE though each looks like it belongs:
#   nsmf           no AI-grid recipe step names the nsmf-slice connector, and on the O-Cloud
#                  host its :7091 is already held by the root-owned PTP lab exporter —
#                  claiming it would be a fight this profile has no reason to pick.
#   the AI gateway there is no services/core/aigateway to start. The real one is FastAPI
#                  (extensions/aigrid-sim/lab/aigateway.py) and will not run on a stdlib-only
#                  host; tests/helpers/aigateway_stub.py re-implements its contract but is a
#                  spec fixture, still available via --stubs. More to the point, the gateway
#                  is not a host process on this box at all: it belongs to the data-plane
#                  phase and runs ON THE ORDERED SPOKE CLUSTER. The metro-east zone's
#                  aiGateway field names an endpoint inside the LADN, not a local service.

# ------------------------------------------------------- dependency model
# THE RACE (issue: ~45 NFs, some lose the race and exit / come up unregistered):
# every registrant NF runs register_with_nrf() SYNCHRONOUSLY *before* it binds its SBI
# port (see services/core/*/__main__: `register_with_nrf(); serve(...)`), and that
# registration has a BOUNDED retry budget (e.g. NEF = 25 x 0.2s = 5s). If the NRF is not
# yet answering when a registrant starts, the registrant burns that budget against a dead
# NRF; under the load of dozens of interpreters starting at once it can exhaust the budget
# and come up UNREGISTERED, or bind its port late enough to miss the readiness gate. The
# old bring-up relied only on hand-sorted list order (NRF merely second) with no gate that
# the NRF is actually READY before the registrants launch, and it tore the WHOLE stack down
# on the first miss. The fix: launch in dependency tiers, and GATE each tier ready before
# the next starts, so a registrant never starts until the thing it registers with is up.
#
# Each service optionally names what it must see READY first ("waits"); the default is the
# NRF plus the subscriber-data stores. Roots (the NRF itself, the O-Cloud edge substrate,
# and the test stubs) wait for nothing. Ports come from domain/netconfig, so this moves with
# TELCO_PORT_OFFSET like everything else.
ROOTS = frozenset({"nrf", "ocloud"})          # infra other NFs stand on; no upstream wait
CORE_DATA = frozenset({"udm", "udr", "udsf"})  # subscriber/data stores: after NRF, before the rest
# Cross-tier waits beyond the "NRF + core data" default (mined from the spec runners):
#   - the O-RU has no port; it is ready only when the O-DU fronthaul shows CONNECTED, so it
#     must not start until the O-DU is up.
#   - the edge data plane (UPF/EAS) is deployed onto the O-Cloud.
#   - the P9 heal agent must not sense a cell that is born dead: it waits for the O-RU (and
#     therefore the whole RAN chain) so it never flags a not-yet-connected radio.
EXTRA_WAITS = {
    "oru": frozenset({"odu"}),
    "edge_upf": frozenset({"ocloud"}),
    "edge_app": frozenset({"ocloud"}),
    "ran_heal_agent": frozenset({"oru", "smo"}),
}

# Bounded concurrency + per-service ready budget (env-overridable; stdlib only).
CONCURRENCY = max(1, int(os.environ.get("TELCO_STACK_CONCURRENCY", "6")))
READY_TIMEOUT = float(os.environ.get("TELCO_STACK_READY_TIMEOUT", "20"))
READY_RETRIES = int(os.environ.get("TELCO_STACK_READY_RETRIES", "1"))


def deps_for(svc, present):
    """The set of service names (restricted to those actually in this profile) that must be
    READY before `svc` may start. Default = NRF + the subscriber-data stores; roots/stubs
    wait for nothing; EXTRA_WAITS adds the cross-tier edges above."""
    name = svc["name"]
    if name == "nrf" or svc.get("stub") or name in ROOTS:
        deps = set()
    elif name in CORE_DATA:
        deps = {"nrf"}
    else:
        deps = {"nrf"} | set(CORE_DATA)
    deps |= set(EXTRA_WAITS.get(name, ()))
    return deps & present


def services_for(profile, stubs):
    picked = []
    for svc in SERVICES:
        # `buyside` selects by explicit membership; every other profile is a cumulative tier
        # carried on the service's own `profiles` set. --stubs still adds the fixtures.
        wanted = svc["name"] in BUYSIDE if profile == "buyside" else profile in svc["profiles"]
        if wanted or (stubs and svc.get("stub")):
            picked.append(svc)
    # gridctl (owned, issue #17) and the bmaas_stub fixture share :8700 by design; when the
    # profile brings the owned service, --stubs must not race it onto the same port.
    if any(s["name"] == "gridctl" for s in picked):
        picked = [s for s in picked if s["name"] != "bmaas_stub"]
    return picked


def by_name(name):
    for svc in SERVICES:
        if svc["name"] == name:
            return svc
    return None


# ---------------------------------------------------------------- state


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"profile": None, "services": {}}


def save_state(state):
    STACK_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True


# ------------------------------------------------------- port forensics


def port_listener_pid(port):
    """Find the pid listening on 127.0.0.1:<port> via /proc (stdlib, Linux). None if free."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            local, st, inode = f[1], f[3], f[9]
            if st == "0A" and int(local.rsplit(":", 1)[1], 16) == port:  # 0A = LISTEN
                inodes.add(inode)
    if not inodes:
        return None
    targets = {f"socket:[{i}]" for i in inodes}
    for piddir in Path("/proc").iterdir():
        if not piddir.name.isdigit():
            continue
        try:
            for fd in (piddir / "fd").iterdir():
                if os.readlink(fd) in targets:
                    return int(piddir.name)
        except OSError:
            continue
    return None


# ------------------------------------------------------------ readiness


def wait_ready(svc, proc, tries=150, delay=0.1):
    """Poll the service's readiness check; mirrors the spec runners' wait_up/wait_ru_connected
    (generous tries: late starters share the CPU with early pollers)."""
    ready = svc["ready"]
    for _ in range(tries):
        if proc is not None and proc.poll() is not None:
            return False  # died during startup
        if callable(ready):
            if ready():
                return True
        else:
            try:
                request("GET", ready)
                return True
            except OSError:
                pass
        time.sleep(delay)
    return False


def ready_within(svc, proc, timeout):
    """Poll the service's readiness check until it answers or `timeout` seconds elapse.
    Returns False immediately if the process dies during startup (so the caller can retry)."""
    ready = svc["ready"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # died during startup
        if callable(ready):
            if ready():
                return True
        else:
            try:
                request("GET", ready)
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


def _kill_proc(proc):
    """Stop a started service (process group, since we start_new_session)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _wait_port_free(svc, timeout=3.0):
    """After killing a service, wait for its port to actually release before we rebind it."""
    if not svc.get("port"):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_listener_pid(svc["port"]) is None:
            return
        time.sleep(0.1)


def gate_one(svc, proc, harness_path, timeout=READY_TIMEOUT, retries=READY_RETRIES):
    """Gate an already-started service on its readiness, with a single restart on failure to
    bind/ready (the bounded retry the charter asks for). The caller starts the process (so a
    whole batch can be fired concurrently first); we only re-start it on a miss. Returns
    (proc, ok, attempts)."""
    attempt = 1
    while True:
        if ready_within(svc, proc, timeout):
            return proc, True, attempt
        if attempt > retries:
            return proc, False, attempt
        # failed to bind/ready in time: clean the slot and try once more
        _kill_proc(proc)
        _wait_port_free(svc)
        proc = start_service(svc, harness_path)
        attempt += 1


def probe(svc):
    """One-line health string, or None if unreachable."""
    url, fmt = HEALTH.get(svc["name"], (None, None))
    if url is None:
        ready = svc["ready"]
        if callable(ready):
            return "ru CONNECTED (via odu)" if ready() else None
        url, fmt = ready, lambda b: "sbi answering"
    try:
        status, body = request("GET", url)
        return fmt(body) if status < 500 else f"HTTP {status}"
    except OSError:
        return None
    except Exception as exc:  # a probe must never crash status
        return f"probe error: {type(exc).__name__}"


# ---------------------------------------------------------- harness preflight


def preflight_harness():
    """Mirror ran_heal_agent's own startup check so `up agents` fails loudly and early,
    before a single process is started."""
    harness_path = Path(os.environ.get("HARNESS_PATH",
                                       str(ROOT.parent / "oran-agent-harness"))).resolve()
    if not (harness_path / "harness" / "runtime" / "router.py").is_file():
        sys.exit("stackctl: CANNOT start the agents profile — the oran-agent-harness clone "
                 f"was not found at {harness_path}.\n"
                 "Clone https://github.com/dkypuros/oran-agent-harness next to this repo, "
                 "or point HARNESS_PATH at an existing clone.")
    sys.path.insert(0, str(harness_path))
    try:
        from harness.runtime import router  # noqa: F401  (also proves PyYAML is present)
    except Exception as exc:
        sys.exit(f"stackctl: CANNOT start the agents profile — harness.runtime import failed "
                 f"from {harness_path}: {exc!r}\n"
                 "The harness requires PyYAML (pip install pyyaml).")
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        sys.exit("stackctl: CANNOT start the agents profile — jsonschema is not installed "
                 "(pip install jsonschema); ran_heal_agent refuses to run without it.")
    return harness_path


# -------------------------------------------------------------- up / start


def start_service(svc, harness_path=None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{svc['name']}.log", "ab", buffering=0)
    env = dict(os.environ)
    if svc.get("needs_harness") and harness_path:
        env["HARNESS_PATH"] = str(harness_path)
    proc = subprocess.Popen([PY, str(ROOT / svc["script"])],
                            stdout=log, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    log.close()
    return proc


def cmd_up(args):
    state = load_state()
    running = {n: r for n, r in state["services"].items() if pid_alive(r["pid"])}
    if running:
        sys.exit(f"stackctl: stack already (partially) up: {sorted(running)}.\n"
                 "Run `stackctl.py down` first.")

    wanted = services_for(args.profile, args.stubs)
    # refuse to start on top of orphans squatting on our ports
    for svc in wanted:
        if svc["port"] and port_listener_pid(svc["port"]):
            sys.exit(f"stackctl: port {svc['port']} ({svc['name']}) is already in use "
                     f"by pid {port_listener_pid(svc['port'])} — run `stackctl.py down` "
                     "(orphan sweep) or free it manually.")

    harness_path = None
    if any(s.get("needs_harness") for s in wanted):
        harness_path = preflight_harness()
        print(f"harness preflight OK: {harness_path}")

    stubs_note = " +stubs" if args.stubs else ""
    print(f"stackctl up {args.profile}{stubs_note}: {len(wanted)} services "
          f"(dependency-ordered, gated, up to {CONCURRENCY} at a time)")
    state = {"profile": args.profile, "stubs": args.stubs, "services": {}}
    procs = {}

    # TIERED, READINESS-GATED, BOUNDED-CONCURRENCY startup.
    # Repeatedly launch every service whose dependencies are already READY, in batches of at
    # most CONCURRENCY, and gate each on its own ready-probe (with a per-service timeout and a
    # single restart-on-failure) before it counts as ready. Nothing that registers with the
    # NRF starts until the NRF is READY; nothing in the general tier starts until the
    # subscriber-data stores are up; the O-RU starts only after the O-DU; the heal agent only
    # after the radio. A service whose dependency FAILED can never come up, so it is marked
    # BLOCKED instead of hung against a 20s gate. We do NOT tear the whole stack down on a
    # single miss — we bring up everything we can, then REPORT the stragglers and exit 1.
    present = {s["name"] for s in wanted}
    by_svc_name = {s["name"]: s for s in wanted}
    ready, failed, blocked = set(), set(), set()
    results = {}  # name -> (status, ms)
    pending = set(present)

    while pending:
        # Anything still pending whose deps include a failed/blocked service can never start.
        newly_blocked = {n for n in pending
                         if deps_for(by_svc_name[n], present) & (failed | blocked)}
        if newly_blocked:
            blocked |= newly_blocked
            pending -= newly_blocked
            continue
        runnable = sorted(n for n in pending
                          if deps_for(by_svc_name[n], present) <= ready)
        if not runnable:
            break  # no forward progress possible (dependency cycle / all blocked)
        batch = runnable[:CONCURRENCY]
        # Fire the whole batch concurrently, THEN gate each (start_new_session isolates them).
        t0 = time.monotonic()
        launched = {n: start_service(by_svc_name[n], harness_path) for n in batch}
        for name in batch:
            svc = by_svc_name[name]
            proc, ok, attempts = gate_one(svc, launched[name], harness_path)
            # gate_one may have restarted the proc; adopt whatever is live now.
            launched[name] = proc
            ms = int((time.monotonic() - t0) * 1000)
            port = svc["port"] or "-"
            retry_note = f" (retry x{attempts - 1})" if attempts > 1 else ""
            print(f"  {'READY' if ok else 'FAIL '}  {name:<20} pid={proc.pid:<7} "
                  f"port={port:<5} {ms}ms{retry_note}", flush=True)
            procs[name] = proc
            state["services"][name] = {"pid": proc.pid, "port": svc["port"],
                                       "script": svc["script"], "started": time.time()}
            save_state(state)
            (ready if ok else failed).add(name)
            results[name] = ("READY" if ok else "FAIL", ms)
            pending.discard(name)

    # ------------------------------------------------ POST-START HEALTH VERIFICATION
    # Re-probe every service that reported READY (a registrant can answer "/" and then exit),
    # and fold in anything that FAILED to start or was BLOCKED by a failed dependency.
    unhealthy = []
    for name in sorted(present):
        svc = by_svc_name[name]
        if name in blocked:
            unhealthy.append((name, "BLOCKED (dependency failed)"))
            continue
        if name in failed:
            unhealthy.append((name, "never became ready"))
            continue
        rec = procs.get(name)
        if rec is None or rec.poll() is not None:
            unhealthy.append((name, "exited after startup"))
            continue
        if probe(svc) is None:
            unhealthy.append((name, "process alive but health probe unreachable"))

    if unhealthy:
        print(f"\nstackctl: {len(unhealthy)} of {len(present)} service(s) NOT healthy after "
              f"`up {args.profile}{stubs_note}`:", file=sys.stderr)
        for name, why in unhealthy:
            print(f"  DOWN  {name:<20} {why}  (see .stack/logs/{name}.log)", file=sys.stderr)
        print("stack left up for inspection; `stackctl.py status` and `logs <svc>` to debug, "
              "`stackctl.py down` to clear.", file=sys.stderr)
        sys.exit(1)

    print(f"stack is up ({args.profile}{stubs_note}): {len(ready)}/{len(present)} READY. "
          "Logs: .stack/logs/  Stop: python3 tools/stackctl.py down")
    if args.supervise:
        supervise(state, procs, harness_path)


# ------------------------------------------------------------- supervise


def supervise(state, procs, harness_path):
    """Foreground supervisor: restart any dead service with exponential backoff.
    NOTE: this fights the standing O-RU fault-injection hook — never run it under the
    alarm demos or the fault-injection specs (see module docstring)."""
    print("supervising (Ctrl-C detaches, stack stays up; O-RU fault-injection demos "
          "will be auto-healed while this runs)", flush=True)
    backoff = {}  # name -> current backoff seconds
    try:
        while True:
            time.sleep(0.5)
            for name, proc in list(procs.items()):
                if proc.poll() is None:
                    continue
                delay = backoff.get(name, 1)
                print(f"[supervisor] {name} died (exit {proc.returncode}); "
                      f"restarting in {delay}s", flush=True)
                time.sleep(delay)
                backoff[name] = min(delay * 2, 30)
                svc = by_name(name)
                newp = start_service(svc, harness_path)
                ok = wait_ready(svc, newp)
                print(f"[supervisor] {name} restarted pid={newp.pid} "
                      f"{'READY' if ok else 'NOT READY'}", flush=True)
                if ok:
                    backoff[name] = 1  # healthy again: reset
                procs[name] = newp
                state["services"][name]["pid"] = newp.pid
                state["services"][name]["started"] = time.time()
                save_state(state)
    except KeyboardInterrupt:
        print("\n[supervisor] detached; stack still running (stackctl.py down to stop)")


# ----------------------------------------------------------------- down


def do_down(state):
    entries = state.get("services", {})
    # TERM everything first, then wait, then KILL stragglers (spec-runner teardown, promoted)
    for name, rec in entries.items():
        if pid_alive(rec["pid"]):
            os.kill(rec["pid"], signal.SIGTERM)
            print(f"  TERM   {name:<20} pid={rec['pid']}")
    deadline = time.monotonic() + 5
    for name, rec in entries.items():
        while pid_alive(rec["pid"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        if pid_alive(rec["pid"]):
            os.kill(rec["pid"], signal.SIGKILL)
            print(f"  KILL   {name:<20} pid={rec['pid']}")
    # orphan sweep: anything still listening on a known stack port gets the same treatment
    for svc in SERVICES:
        if not svc["port"]:
            continue
        pid = port_listener_pid(svc["port"])
        if pid:
            print(f"  ORPHAN {svc['name']:<20} pid={pid} still on port {svc['port']}")
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                if pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # The sweep scans EVERY known stack port, including ports no profile we ran
                # ever claimed — so it can meet a process that was never ours (on the O-Cloud
                # host :7091 is the root-owned PTP lab exporter). Not being allowed to signal
                # it is the correct outcome; say so and keep tearing down, rather than
                # aborting the rest of the sweep with a traceback.
                print(f"  FOREIGN {svc['name']:<19} pid={pid} on port {svc['port']} is not "
                      "ours to signal — left running")
    save_state({"profile": None, "services": {}})
    print("stack is down")


def cmd_down(args):
    do_down(load_state())


# --------------------------------------------------------------- status


def cmd_status(args):
    state = load_state()
    entries = state.get("services", {})
    if not entries:
        print("stack is down (no registry). Orphan check:")
        found = False
        for svc in SERVICES:
            if svc["port"]:
                pid = port_listener_pid(svc["port"])
                if pid:
                    print(f"  ORPHAN {svc['name']:<20} pid={pid} port={svc['port']}")
                    found = True
        if not found:
            print("  all stack ports free")
        return
    print(f"profile: {state.get('profile')}"
          + (" +stubs" if state.get("stubs") else ""))
    down = 0
    for name, rec in entries.items():
        svc = by_name(name)
        alive = pid_alive(rec["pid"])
        health = probe(svc) if alive else None
        if alive and health is not None:
            line = f"  UP    {name:<20} pid={rec['pid']:<7} port={rec['port'] or '-':<5} {health}"
        elif alive:
            line = (f"  ??    {name:<20} pid={rec['pid']:<7} port={rec['port'] or '-':<5} "
                    "process alive but probe unreachable")
        else:
            line = f"  DOWN  {name:<20} pid={rec['pid']:<7} port={rec['port'] or '-':<5} exited"
            down += 1
        print(line, flush=True)
    if down:
        print(f"{down} service(s) down — logs under .stack/logs/")
        sys.exit(1)


# ----------------------------------------------------------------- logs


def cmd_logs(args):
    path = LOG_DIR / f"{args.service}.log"
    if not path.is_file():
        known = sorted(p.stem for p in LOG_DIR.glob("*.log")) if LOG_DIR.is_dir() else []
        sys.exit(f"stackctl: no log for '{args.service}'. Captured logs: {known}")
    lines = path.read_text(errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)


# ----------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(prog="stackctl.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start a profile and wait for readiness")
    up.add_argument("profile", choices=PROFILES)
    up.add_argument("--stubs", action="store_true",
                    help="also start the tests/helpers spec fixtures: aigateway_stub (8710) "
                         "and — only on profiles without the owned gridctl (8700) — "
                         "bmaas_stub (8700)")
    up.add_argument("--supervise", action="store_true",
                    help="stay in the foreground and restart dead services (OFF by default: "
                         "it defeats the O-RU fault-injection hook)")
    up.set_defaults(fn=cmd_up)

    down = sub.add_parser("down", help="stop everything (TERM then KILL, plus orphan sweep)")
    down.set_defaults(fn=cmd_down)

    st = sub.add_parser("status", help="per-service up/down + health probe")
    st.set_defaults(fn=cmd_status)

    lg = sub.add_parser("logs", help="tail a service's captured log")
    lg.add_argument("service")
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(fn=cmd_logs)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

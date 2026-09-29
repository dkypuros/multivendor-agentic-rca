# Glossary

Every term here names something that **actually runs** in this demo. Each entry says what it does *here*, not in general, and links to the code. Terms with a deeper write-up link to a page in [concepts/](concepts/).

Things people often ask about that are *not* running in this demo are listed at the end, under [Not in this demo](#not-in-this-demo).

**Jump to:** [Platform](#platform) · [Console and flow](#console-and-flow) · [RAN](#ran-o-ran-split) · [UE and attach](#ue-and-attach) · [5G core and IMS](#5g-core-and-ims-in-the-slice) · [Timing](#timing) · [Agent](#agent) · [Authorization](#authorization) · [Tools](#tools) · [Decision](#decision) · [Output](#output)

---

## Platform

| Term | In this demo | Code |
|---|---|---|
| **OpenShift** | Runs everything. Tested on 4.22.1. | [deploy/openshift](../deploy/openshift) |
| **Project (namespace)** | `multivendor-rca`. All 10 Deployments live here. | [kustomization.yaml](../deploy/openshift/kustomization.yaml) |
| **Deployment scale 0↔1** | How Start and Stop work: the console patches `ran-slice` to 1 replica, or back to 0 to free CPU and memory. | [sandbox_controller.py#L102](../src/services/ran/sandbox_controller.py#L102) |
| **Route** | The only way into the lab from outside: it exposes the console. It has no login. | [ran-sandbox.yaml](../deploy/openshift/ran-sandbox.yaml) |
| **NetworkPolicy** | Pods accept traffic only from pods in the same project, plus the router to the console. This, not the CAPIF token, is the lab's real security boundary. | [networkpolicy.yaml](../deploy/openshift/networkpolicy.yaml) |
| **BuildConfig / `mvrca` image** | One image, built from the repo with `oc start-build`, runs every component. | [build.yaml](../deploy/openshift/build.yaml), [Containerfile](../Containerfile) |

## Console and flow

| Term | In this demo | Code |
|---|---|---|
| **ran-sandbox-controller** (the console) | The web page and the only thing a presenter clicks. It scales the slice, calls the PTP bridge and O-DU, triggers the RCA, and merges all logs into one stream. | [sandbox_controller.py](../src/services/ran/sandbox_controller.py) |
| **Start / Inject / Run RCA / Heal / Stop** | The five buttons. See the [demo walkthrough](demo-walkthrough.md). | [sandbox_controller.py#L163](../src/services/ran/sandbox_controller.py#L163) |
| **Log stream** | The console's own actions (`K8S:`, `O1:`, `PTP-BRIDGE:`, `NEP:`) interleaved with the RAN pod's real stdout. | [sandbox_controller.py#L140](../src/services/ran/sandbox_controller.py#L140) |

## RAN (O-RAN split)

Concept page: [O-RAN split and O1](concepts/oran-split-and-o1.md)

| Term | In this demo | Code |
|---|---|---|
| **ran-slice** | One pod running all 32 network functions, started by `launch_slice.py` in dependency order. They share a pod because each one calls its peers on localhost. | [launch_slice.py](../src/deploy/docker/launch_slice.py) |
| **O-CU-CP** | Central unit, control plane. Relays the UE's RRC and NAS messages toward the AMF. | [ocucp/](../src/services/ran/ocucp) |
| **O-CU-UP** | Central unit, user plane. Carries user traffic between the O-DU and the UPF. | [ocuup/](../src/services/ran/ocuup) |
| **O-DU** | Distributed unit. Owns the cell: its state, its O1 interface and its alarms. The component the fault takes down. | [odu.py](../src/services/ran/odu/odu.py) |
| **O-RU** | Radio unit. Sends a fronthaul heartbeat to the O-DU; the cell is ACTIVE only while it's connected. No real radio. | [oru/](../src/services/ran/oru) |
| **Cell state** | `ACTIVE` or `UNAVAILABLE`. ACTIVE needs the O-RU connected **and** the cell unlocked. | [odu.py#L131](../src/services/ran/odu/odu.py#L131) |
| **Administrative state** | `LOCKED` or `UNLOCKED` (3GPP TS 28.541). Inject locks the cell; Heal unlocks it. A locked cell rejects new UEs. | [odu.py#L114](../src/services/ran/odu/odu.py#L114) |
| **O1** | The O-DU's management interface: `/o1/status`, `/o1/config`, `/o1/alarms`. JSON over HTTP here, not NETCONF. | [odu.py#L114](../src/services/ran/odu/odu.py#L114) |
| **TS 28.532 alarm** | The O-DU's fault report: `CellUnavailable`, probable cause `lossOfRealTimeSynchronization`. See [concept page](concepts/o1-alarms.md). | [odu.py#L142](../src/services/ran/odu/odu.py#L142) |

## UE and attach

Concept page: [The UE attach](concepts/ue-attach.md)

| Term | In this demo | Code |
|---|---|---|
| **UE (`ue_sim`)** | A simulated phone. After Start it attaches, opens a session and sends 3 echoes. | [ue_sim.py](../src/services/ue_sim/ue_sim.py) |
| **IMSI / SUPI** | The subscriber identity: `imsi-001010000000001`. | [subscribers.json](../src/services/core/udm/subscribers.json) |
| **RRC** | Radio connection setup between UE and RAN (`RRCSetupRequest` → `RRCSetup`). | [odu.py#L70](../src/services/ran/odu/odu.py#L70) |
| **NAS** | UE-to-AMF signaling carried through the RAN: registration, authentication, security mode. | [ue_sim.py#L75](../src/services/ue_sim/ue_sim.py#L75) |
| **5G-AKA** | Mutual authentication. The UE and the network each compute the answer and must match. | [ue_sim.py#L48](../src/services/ue_sim/ue_sim.py#L48) |
| **MILENAGE** | The 3GPP algorithm set (AES-based) behind 5G-AKA. Implemented from the spec and checked against the published test vectors. | [milenage.py](../src/adapters/milenage.py) |
| **Registration** | The UE is accepted onto the network: `REGISTERED`, with an allowed slice (SST 1). | [ue_sim.py#L75](../src/services/ue_sim/ue_sim.py#L75) |
| **PDU session** | The UE's data session. It gets an IP such as `10.45.0.3` on DNN `internet`. | [ue_sim.py#L115](../src/services/ue_sim/ue_sim.py#L115) |
| **GTP-U echo** | Test traffic through the user-plane tunnel (RAN → UPF → data network). `3/3` means all replies came back. | [ue_sim.py#L115](../src/services/ue_sim/ue_sim.py#L115), [gtpu.py](../src/adapters/gtpu.py) |

## 5G core and IMS in the slice

All of these boot in the slice and register with the NRF. **"Attach"** marks the ones you can see working in the log during the UE attach; the rest boot but play no part in the RCA story, so there's no need to explain them in a demo.

| Function | Role | Here |
|---|---|---|
| **NRF** | Registry where every function announces itself (`nf_registered`). | Boot |
| **AMF** | Access and mobility: handles registration. | Attach |
| **AUSF** | Runs the network side of 5G-AKA. | Attach |
| **UDM** | Subscriber data and authentication vectors. | Attach |
| **UDR** | Database behind the UDM. | Attach (via UDM) |
| **EIR** | Checks the device IMEI is allowed (`WHITELISTED`). | Attach |
| **NSSF** | Picks the allowed network slice. | Attach |
| **SMF** | Sets up the PDU session. | Attach |
| **UPF** | Forwards user traffic; ends the GTP-U tunnel. | Attach |
| **PCF** | Session policy (`internet-default`, 5QI 9). | Attach |
| **CHF** | Opens a charging record for the session. | Attach |
| **UDSF** | Stores a copy of the session context. | Attach |
| **SCP** | Service communication proxy. | Boot |
| **NEF** | Network exposure to external apps. | Boot |
| **NWDAF** | Network analytics. | Boot |
| **SEPP** | Security edge for roaming. | Boot |
| **BSF** | Binding support for policy. | Boot |
| **NSSAAF** | Slice-specific authentication. | Boot |
| **N3IWF** | Non-3GPP (Wi-Fi) access. | Boot |
| **LMF**, **GMLC** | Location services. | Boot |
| **TSCTSF** | Time-sensitive communication. | Boot |
| **P-CSCF**, **I-CSCF**, **S-CSCF**, **IMS-HSS**, **MRF** | IMS (voice over 5G). | Boot |

Code: [src/services/core/](../src/services/core)

## Timing

Concept page: [The timing fault](concepts/timing-fault.md)

| Term | In this demo | Code |
|---|---|---|
| **ptp-bridge** | A **software model** of a PTP clock (ptp4l-style state and logs). No PTP hardware or PTP Operator is involved. | [ptp_bridge.py](../src/services/smo/ptp_bridge.py) |
| **PTP lock state** | `LOCKED` (healthy) or `FREERUN` (lost sync). Inject sets FREERUN; Heal sets LOCKED. | [ptp_bridge.py#L97](../src/services/smo/ptp_bridge.py#L97) |
| **Offset** | How far the clock is off, in nanoseconds. Inject sets −50,000,198 ns (about −50 ms). The limit the PTP plane checks is 100,000 ns. | [ptp_tools.py](../src/extensions/sheldon/agentic/ptp_tools.py) |
| **CloudEvent** | The lock-state notification (`event.ptp.sync.state-change`), shaped like the PTP Operator's cloud-event-proxy. | [ptp_bridge.py](../src/services/smo/ptp_bridge.py) |

## Agent

| Term | In this demo | Code |
|---|---|---|
| **NEP orchestrator** | The RCA agent. One HTTP call runs the whole investigation in six steps. | [nep_orchestrator.py](../src/services/orchestrator/nep_orchestrator.py) |
| **RCA** | Root cause analysis: collect evidence, decide the fault, explain it. | [nep_orchestrator.py#L248](../src/services/orchestrator/nep_orchestrator.py#L248) |
| **Span** | One step of an RCA, with its timing and status. There are six. See [concept page](concepts/audit-trail.md). | [nep_orchestrator.py#L258](../src/services/orchestrator/nep_orchestrator.py#L258) |
| **Trace** | All six spans of one RCA, with a trace ID. Kept in memory only. | [nep_orchestrator.py#L248](../src/services/orchestrator/nep_orchestrator.py#L248) |

## Authorization

Concept page: [CAPIF](concepts/capif.md)

| Term | In this demo | Code |
|---|---|---|
| **CAPIF** | 3GPP's API exposure framework (TS 29.222). Grants the agent a token for exactly four read-only tools. | [capif.py](../src/services/core/capif/capif.py) |
| **Invoker** | The agent, once onboarded to CAPIF (`invoker-…`). | [capif.py#L215](../src/services/core/capif/capif.py#L215) |
| **Scope** | What a token allows: `3gpp#mcp-aef:mcp-tools`. | [gateway-policy.yaml](../deploy/openshift/gateway-policy.yaml) |
| **Token** | A short-lived JWT from CAPIF. **Unsigned** in this lab. Cached and reused until it nearly expires. | [capif.py#L286](../src/services/core/capif/capif.py#L286) |
| **AEF** | API Exposing Function: the thing that serves the API. Here, the MCP gateway. | [nep_orchestrator.py#L105](../src/services/orchestrator/nep_orchestrator.py#L105) |

## Tools

Concept page: [MCP and the gateway](concepts/mcp-gateway.md)

| Term | In this demo | Code |
|---|---|---|
| **MCP** | Model Context Protocol: a standard way for an agent to list and call tools (JSON-RPC). | [mcp_core.py](../src/extensions/sheldon/agentic/mcp_core.py) |
| **MCP gateway** | Checks the token, hides tools outside the scope, and routes each call to the right plane's server. | [gateway.py](../src/extensions/sheldon/agentic/gateway.py) |
| **Tool** | One callable function, e.g. `ran_gnb_status`. The agent uses `tools/list` and `tools/call`. | [gateway.py#L125](../src/extensions/sheldon/agentic/gateway.py#L125) |
| **Plane** | One vendor's layer, served by its own MCP server: O-Cloud, RAN, timing (PTP), NIC. | [mcp_http.py](../src/extensions/sheldon/agentic/mcp_http.py) |

## Decision

Concept page: [How the decision is made](concepts/decision.md)

| Term | In this demo | Code |
|---|---|---|
| **Evidence** | What a plane reports, e.g. `cell UNAVAILABLE, … lossOfRealTimeSynchronization`. Built from the real tool result. | [nep_orchestrator.py#L158](../src/services/orchestrator/nep_orchestrator.py#L158) |
| **Signal** | A named finding inside the evidence, e.g. `ptp_offset_exceeded`, `du_sync_loss_alarm`. | [ran_tools.py#L36](../src/extensions/sheldon/agentic/ran_tools.py#L36) |
| **Route table** | A fixed list that maps each signal to a fault class. No AI involved. | [nep_orchestrator.py#L37](../src/services/orchestrator/nep_orchestrator.py#L37) |
| **Fault class** | The diagnosis, e.g. `OC-TimingDegraded`, or none. | [nep_orchestrator.py#L37](../src/services/orchestrator/nep_orchestrator.py#L37) |
| **Corroboration** | How many **real** planes carry a signal for that fault class, e.g. `2/3`. | [nep_orchestrator.py#L347](../src/services/orchestrator/nep_orchestrator.py#L347) |
| **Bar** | The corroboration needed for an automatic verdict: 3. | [nep_orchestrator.py#L362](../src/services/orchestrator/nep_orchestrator.py#L362) |
| **Verdict** | `APPLY (auto-eligible)`, `HOLD — below the bar, needs human approval`, or `NO ACTION — no fault signals`. Nothing is ever executed. | [nep_orchestrator.py#L368](../src/services/orchestrator/nep_orchestrator.py#L368) |
| **Emulated plane** | The NIC plane: shown as evidence, labeled `emulated: true`, **never counted**. See [concept page](concepts/decision.md#emulated-vs-real). | [nic_tools.py](../src/extensions/sheldon/agentic/nic_tools.py) |

## Output

Concept page: [Audit trail and TMF688](concepts/audit-trail.md)

| Term | In this demo | Code |
|---|---|---|
| **TMF688 RcaConcludedEvent** | The RCA's result as a TM Forum event: fault class, corroboration, verdict, evidence per plane, narrative. | [nep_orchestrator.py#L413](../src/services/orchestrator/nep_orchestrator.py#L413) |
| **Template narrative** | The plain-text explanation used when no LLM is connected, labeled `[template - LLM unavailable]`. An LLM can replace it; either way it only explains. | [nep_orchestrator.py#L195](../src/services/orchestrator/nep_orchestrator.py#L195) |

---

## Not in this demo

People ask about these. None of them runs in this demo; here's the one-line answer.

| Term | Why it's not here |
|---|---|
| **PTP Operator / linuxptp** | Timing comes from `ptp-bridge`, a software model. The PTP Operator may be installed on the cluster, but the demo doesn't use it. |
| **Intel E810 NIC** | The lab server has no E810. The NIC plane is emulated and never counts. |
| **ACM / OCM** | The O-Cloud plane asks ACM for cluster health when it's installed; the lab has no ACM hub, so the plane reports "unavailable". [How to add it](deployment.md#optional-o-cloud-plane-with-acm). |
| **OpenShift AI / vLLM** | Only used if you connect an LLM for the narrative. Off by default. [How to add it](deployment.md#optional-connect-an-llm). |
| **Real radio (PHY/RF)** | The RAN is protocol logic only; no radio signals. |
| **E2, R1, SMO, near-RT RIC** | Named in span labels as the O-RAN interfaces this pattern maps to. The O-DU opens an E2 port, but nothing uses it. |
| **NETCONF / YANG** | O1 here is JSON over HTTP. |
| **TMF642, TMF921** | Referenced in docs; only TMF688 is emitted. |
| **Persistent storage** | Everything is in memory. Logs reach the cluster's logging stack if it has one. |

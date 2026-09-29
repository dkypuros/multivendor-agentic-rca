# Glossary

Every term here names something that **actually runs** in this demo. Each entry says what it does *here*, not in general, and links to the code. **Click a term** to open its concept page (the detailed view), where one exists. **Definition** links go to the standard or official source.

Things people often ask about that are *not* running in this demo are listed at the end, under [Not in this demo](#not-in-this-demo).

**Jump to:** [Platform](#platform) · [Console and flow](#console-and-flow) · [RAN](#ran-o-ran-split) · [UE and attach](#ue-and-attach) · [5G core and IMS](#5g-core-and-ims-in-the-slice) · [Timing](#timing) · [Agent](#agent) · [Authorization](#authorization) · [Tools](#tools) · [Decision](#decision) · [Output](#output)

---

## Platform

| Term | In this demo | Code |
|---|---|---|
| <a id="openshift"></a>**OpenShift** | Runs everything. Tested on 4.22.1. <br>Definition: [Red Hat](https://www.redhat.com/en/technologies/cloud-computing/openshift) | [deploy/openshift](../deploy/openshift) |
| <a id="project-namespace"></a>**Project (namespace)** | `multivendor-rca`. All 10 Deployments live here. <br>Definition: [Kubernetes](https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/) | [kustomization.yaml](../deploy/openshift/kustomization.yaml) |
| <a id="deployment-scale-0-1"></a>**Deployment scale 0↔1** | How Start and Stop work: the console patches `ran-slice` to 1 replica, or back to 0 to free CPU and memory. <br>Definition: [Kubernetes](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#scaling-a-deployment) | [sandbox_controller.py#L102](../src/services/ran/sandbox_controller.py#L102) |
| <a id="route"></a>**Route** | The only way into the lab from outside: it exposes the console. It has no login. <br>Definition: [OKD docs](https://docs.okd.io/latest/networking/ingress_load_balancing/routes/creating-basic-routes.html) | [ran-sandbox.yaml](../deploy/openshift/ran-sandbox.yaml) |
| <a id="networkpolicy"></a>**NetworkPolicy** | Pods accept traffic only from pods in the same project, plus the router to the console. This, not the CAPIF token, is the lab's real security boundary. <br>Definition: [Kubernetes](https://kubernetes.io/docs/concepts/services-networking/network-policies/) | [networkpolicy.yaml](../deploy/openshift/networkpolicy.yaml) |
| <a id="buildconfig-mvrca-image"></a>**BuildConfig / `mvrca` image** | One image, built from the repo with `oc start-build`, runs every component. <br>Definition: [OKD docs](https://docs.okd.io/latest/cicd/builds/understanding-buildconfigs.html) | [build.yaml](../deploy/openshift/build.yaml), [Containerfile](../Containerfile) |

## Console and flow

| Term | In this demo | Code |
|---|---|---|
| <a id="ran-sandbox-controller"></a>**ran-sandbox-controller** (the console) | The web page and the only thing a presenter clicks. It scales the slice, calls the PTP bridge and O-DU, triggers the RCA, and merges all logs into one stream. | [sandbox_controller.py](../src/services/ran/sandbox_controller.py) |
| <a id="start-inject-run-rca-heal-stop"></a>**[Start / Inject / Run RCA / Heal / Stop](../demo-walkthrough.md)** | The five buttons. See the [demo walkthrough](demo-walkthrough.md). | [sandbox_controller.py#L163](../src/services/ran/sandbox_controller.py#L163) |
| <a id="log-stream"></a>**Log stream** | The console's own actions (`K8S:`, `O1:`, `PTP-BRIDGE:`, `NEP:`) interleaved with the RAN pod's real stdout. | [sandbox_controller.py#L140](../src/services/ran/sandbox_controller.py#L140) |

## RAN (O-RAN split)

Concept page: [O-RAN split and O1](concepts/oran-split-and-o1.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="ran-slice"></a>**[ran-slice](concepts/oran-split-and-o1.md)** | One pod running all 32 network functions, started by `launch_slice.py` in dependency order. They share a pod because each one calls its peers on localhost. | [launch_slice.py](../src/deploy/docker/launch_slice.py) |
| <a id="o-cu-cp"></a>**[O-CU-CP](concepts/oran-split-and-o1.md)** | Central unit, control plane. Relays the UE's RRC and NAS messages toward the AMF. <br>Definition: [3GPP TS 38.401](https://www.3gpp.org/DynaReport/38401.htm) | [ocucp/](../src/services/ran/ocucp) |
| <a id="o-cu-up"></a>**[O-CU-UP](concepts/oran-split-and-o1.md)** | Central unit, user plane. Carries user traffic between the O-DU and the UPF. <br>Definition: [3GPP TS 38.401](https://www.3gpp.org/DynaReport/38401.htm) | [ocuup/](../src/services/ran/ocuup) |
| <a id="o-du"></a>**[O-DU](concepts/oran-split-and-o1.md)** | Distributed unit. Owns the cell: its state, its O1 interface and its alarms. The component the fault takes down. <br>Definition: [3GPP TS 38.401](https://www.3gpp.org/DynaReport/38401.htm) | [odu.py](../src/services/ran/odu/odu.py) |
| <a id="o-ru"></a>**[O-RU](concepts/oran-split-and-o1.md)** | Radio unit. Sends a fronthaul heartbeat to the O-DU; the cell is ACTIVE only while it's connected. No real radio. <br>Definition: [O-RAN specs](https://www.o-ran.org/specifications) | [oru/](../src/services/ran/oru) |
| <a id="cell-state"></a>**[Cell state](concepts/oran-split-and-o1.md)** | `ACTIVE` or `UNAVAILABLE`. ACTIVE needs the O-RU connected **and** the cell unlocked. | [odu.py#L131](../src/services/ran/odu/odu.py#L131) |
| <a id="administrative-state"></a>**[Administrative state](concepts/oran-split-and-o1.md)** | `LOCKED` or `UNLOCKED` (3GPP TS 28.541). Inject locks the cell; Heal unlocks it. A locked cell rejects new UEs. <br>Definition: [3GPP TS 28.541](https://www.3gpp.org/DynaReport/28541.htm) | [odu.py#L114](../src/services/ran/odu/odu.py#L114) |
| <a id="o1"></a>**[O1](concepts/oran-split-and-o1.md)** | The O-DU's management interface: `/o1/status`, `/o1/config`, `/o1/alarms`. JSON over HTTP here, not NETCONF. <br>Definition: [O-RAN specs](https://www.o-ran.org/specifications) | [odu.py#L114](../src/services/ran/odu/odu.py#L114) |
| <a id="ts-28-532-alarm"></a>**[TS 28.532 alarm](concepts/o1-alarms.md)** | The O-DU's fault report: `CellUnavailable`, probable cause `lossOfRealTimeSynchronization`. See [concept page](concepts/o1-alarms.md). <br>Definition: [3GPP TS 28.532](https://www.3gpp.org/DynaReport/28532.htm) | [odu.py#L142](../src/services/ran/odu/odu.py#L142) |

## UE and attach

Concept page: [The UE attach](concepts/ue-attach.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="ue-ue-sim"></a>**[UE (`ue_sim`)](concepts/ue-attach.md)** | A simulated phone. After Start it attaches, opens a session and sends 3 echoes. <br>Definition: [3GPP TS 23.501](https://www.3gpp.org/DynaReport/23501.htm) | [ue_sim.py](../src/services/ue_sim/ue_sim.py) |
| <a id="imsi-supi"></a>**[IMSI / SUPI](concepts/ue-attach.md)** | The subscriber identity: `imsi-001010000000001`. <br>Definition: [3GPP TS 23.501](https://www.3gpp.org/DynaReport/23501.htm) | [subscribers.json](../src/services/core/udm/subscribers.json) |
| <a id="rrc"></a>**[RRC](concepts/ue-attach.md)** | Radio connection setup between UE and RAN (`RRCSetupRequest` → `RRCSetup`). <br>Definition: [3GPP TS 38.331](https://www.3gpp.org/DynaReport/38331.htm) | [odu.py#L70](../src/services/ran/odu/odu.py#L70) |
| <a id="nas"></a>**[NAS](concepts/ue-attach.md)** | UE-to-AMF signaling carried through the RAN: registration, authentication, security mode. <br>Definition: [3GPP TS 24.501](https://www.3gpp.org/DynaReport/24501.htm) | [ue_sim.py#L75](../src/services/ue_sim/ue_sim.py#L75) |
| <a id="5g-aka"></a>**[5G-AKA](concepts/ue-attach.md)** | Mutual authentication. The UE and the network each compute the answer and must match. <br>Definition: [3GPP TS 33.501](https://www.3gpp.org/DynaReport/33501.htm) | [ue_sim.py#L48](../src/services/ue_sim/ue_sim.py#L48) |
| <a id="milenage"></a>**[MILENAGE](concepts/ue-attach.md)** | The 3GPP algorithm set (AES-based) behind 5G-AKA. Implemented from the spec and checked against the published test vectors. <br>Definition: [3GPP TS 35.206](https://www.3gpp.org/DynaReport/35206.htm) | [milenage.py](../src/adapters/milenage.py) |
| <a id="registration"></a>**[Registration](concepts/ue-attach.md)** | The UE is accepted onto the network: `REGISTERED`, with an allowed slice (SST 1). <br>Definition: [3GPP TS 24.501](https://www.3gpp.org/DynaReport/24501.htm) | [ue_sim.py#L75](../src/services/ue_sim/ue_sim.py#L75) |
| <a id="pdu-session"></a>**[PDU session](concepts/ue-attach.md)** | The UE's data session. It gets an IP such as `10.45.0.3` on DNN `internet`. <br>Definition: [3GPP TS 23.501](https://www.3gpp.org/DynaReport/23501.htm) | [ue_sim.py#L115](../src/services/ue_sim/ue_sim.py#L115) |
| <a id="gtp-u-echo"></a>**[GTP-U echo](concepts/ue-attach.md)** | Test traffic through the user-plane tunnel (RAN → UPF → data network). `3/3` means all replies came back. <br>Definition: [3GPP TS 29.281](https://www.3gpp.org/DynaReport/29281.htm) | [ue_sim.py#L115](../src/services/ue_sim/ue_sim.py#L115), [gtpu.py](../src/adapters/gtpu.py) |

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

Definitions: every function above is defined in [3GPP TS 23.501](https://www.3gpp.org/DynaReport/23501.htm) (5G system architecture); the IMS functions in [3GPP TS 23.228](https://www.3gpp.org/DynaReport/23228.htm). Code: [src/services/core/](../src/services/core)

## Timing

Concept page: [The timing fault](concepts/timing-fault.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="ptp-bridge"></a>**[ptp-bridge](concepts/timing-fault.md)** | A **software model** of a PTP clock (ptp4l-style state and logs). No PTP hardware or PTP Operator is involved. | [ptp_bridge.py](../src/services/smo/ptp_bridge.py) |
| <a id="ptp-lock-state"></a>**[PTP lock state](concepts/timing-fault.md)** | `LOCKED` (healthy) or `FREERUN` (lost sync). Inject sets FREERUN; Heal sets LOCKED. <br>Definition: [IEEE 1588](https://standards.ieee.org/ieee/1588/6825/) | [ptp_bridge.py#L97](../src/services/smo/ptp_bridge.py#L97) |
| <a id="offset"></a>**[Offset](concepts/timing-fault.md)** | How far the clock is off, in nanoseconds. Inject sets −50,000,198 ns (about −50 ms). The limit the PTP plane checks is 100,000 ns. <br>Definition: [linuxptp](https://linuxptp.nwtime.org/) | [ptp_tools.py](../src/extensions/sheldon/agentic/ptp_tools.py) |
| <a id="cloudevent"></a>**[CloudEvent](concepts/timing-fault.md)** | The lock-state notification (`event.ptp.sync.state-change`), shaped like the PTP Operator's cloud-event-proxy. <br>Definition: [CloudEvents](https://cloudevents.io/) | [ptp_bridge.py](../src/services/smo/ptp_bridge.py) |

## Agent

| Term | In this demo | Code |
|---|---|---|
| <a id="nep-orchestrator"></a>**[NEP orchestrator](concepts/audit-trail.md)** | The RCA agent. One HTTP call runs the whole investigation in six steps. | [nep_orchestrator.py](../src/services/orchestrator/nep_orchestrator.py) |
| <a id="rca"></a>**[RCA](concepts/decision.md)** | Root cause analysis: collect evidence, decide the fault, explain it. | [nep_orchestrator.py#L248](../src/services/orchestrator/nep_orchestrator.py#L248) |
| <a id="span"></a>**[Span](concepts/audit-trail.md)** | One step of an RCA, with its timing and status. There are six. See [concept page](concepts/audit-trail.md). <br>Definition: [OpenTelemetry](https://opentelemetry.io/docs/concepts/signals/traces/) | [nep_orchestrator.py#L258](../src/services/orchestrator/nep_orchestrator.py#L258) |
| <a id="trace"></a>**[Trace](concepts/audit-trail.md)** | All six spans of one RCA, with a trace ID. Kept in memory only. <br>Definition: [OpenTelemetry](https://opentelemetry.io/docs/concepts/signals/traces/) | [nep_orchestrator.py#L248](../src/services/orchestrator/nep_orchestrator.py#L248) |

## Authorization

Concept page: [CAPIF](concepts/capif.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="capif"></a>**[CAPIF](concepts/capif.md)** | 3GPP's API exposure framework (TS 29.222). Grants the agent a token for exactly four read-only tools. <br>Definition: [3GPP TS 29.222](https://www.3gpp.org/DynaReport/29222.htm) | [capif.py](../src/services/core/capif/capif.py) |
| <a id="invoker"></a>**[Invoker](concepts/capif.md)** | The agent, once onboarded to CAPIF (`invoker-…`). <br>Definition: [3GPP TS 29.222](https://www.3gpp.org/DynaReport/29222.htm) | [capif.py#L215](../src/services/core/capif/capif.py#L215) |
| <a id="scope"></a>**[Scope](concepts/capif.md)** | What a token allows: `3gpp#mcp-aef:mcp-tools`. <br>Definition: [3GPP TS 29.222](https://www.3gpp.org/DynaReport/29222.htm) | [gateway-policy.yaml](../deploy/openshift/gateway-policy.yaml) |
| <a id="token"></a>**[Token](concepts/capif.md)** | A short-lived JWT from CAPIF. **Unsigned** in this lab. Cached and reused until it nearly expires. <br>Definition: [JWT, RFC 7519](https://datatracker.ietf.org/doc/html/rfc7519) | [capif.py#L286](../src/services/core/capif/capif.py#L286) |
| <a id="aef"></a>**[AEF](concepts/capif.md)** | API Exposing Function: the thing that serves the API. Here, the MCP gateway. <br>Definition: [3GPP TS 29.222](https://www.3gpp.org/DynaReport/29222.htm) | [nep_orchestrator.py#L105](../src/services/orchestrator/nep_orchestrator.py#L105) |

## Tools

Concept page: [MCP and the gateway](concepts/mcp-gateway.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="mcp"></a>**[MCP](concepts/mcp-gateway.md)** | Model Context Protocol: a standard way for an agent to list and call tools (JSON-RPC). <br>Definition: [modelcontextprotocol.io](https://modelcontextprotocol.io/) | [mcp_core.py](../src/extensions/sheldon/agentic/mcp_core.py) |
| <a id="mcp-gateway"></a>**[MCP gateway](concepts/mcp-gateway.md)** | Checks the token, hides tools outside the scope, and routes each call to the right plane's server. | [gateway.py](../src/extensions/sheldon/agentic/gateway.py) |
| <a id="tool"></a>**[Tool](concepts/mcp-gateway.md)** | One callable function, e.g. `ran_gnb_status`. The agent uses `tools/list` and `tools/call`. <br>Definition: [modelcontextprotocol.io](https://modelcontextprotocol.io/) | [gateway.py#L125](../src/extensions/sheldon/agentic/gateway.py#L125) |
| <a id="plane"></a>**[Plane](concepts/mcp-gateway.md)** | One vendor's layer, served by its own MCP server: O-Cloud, RAN, timing (PTP), NIC. | [mcp_http.py](../src/extensions/sheldon/agentic/mcp_http.py) |

## Decision

Concept page: [How the decision is made](concepts/decision.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="evidence"></a>**[Evidence](concepts/decision.md)** | What a plane reports, e.g. `cell UNAVAILABLE, … lossOfRealTimeSynchronization`. Built from the real tool result. | [nep_orchestrator.py#L158](../src/services/orchestrator/nep_orchestrator.py#L158) |
| <a id="signal"></a>**[Signal](concepts/decision.md)** | A named finding inside the evidence, e.g. `ptp_offset_exceeded`, `du_sync_loss_alarm`. | [ran_tools.py#L36](../src/extensions/sheldon/agentic/ran_tools.py#L36) |
| <a id="route-table"></a>**[Route table](concepts/decision.md)** | A fixed list that maps each signal to a fault class. No AI involved. | [nep_orchestrator.py#L37](../src/services/orchestrator/nep_orchestrator.py#L37) |
| <a id="fault-class"></a>**[Fault class](concepts/decision.md)** | The diagnosis, e.g. `OC-TimingDegraded`, or none. | [nep_orchestrator.py#L37](../src/services/orchestrator/nep_orchestrator.py#L37) |
| <a id="corroboration"></a>**[Corroboration](concepts/decision.md)** | How many **real** planes carry a signal for that fault class, e.g. `2/3`. | [nep_orchestrator.py#L347](../src/services/orchestrator/nep_orchestrator.py#L347) |
| <a id="bar"></a>**[Bar](concepts/decision.md)** | The corroboration needed for an automatic verdict: 3. | [nep_orchestrator.py#L362](../src/services/orchestrator/nep_orchestrator.py#L362) |
| <a id="verdict"></a>**[Verdict](concepts/decision.md)** | `APPLY (auto-eligible)`, `HOLD — below the bar, needs human approval`, or `NO ACTION — no fault signals`. Nothing is ever executed. | [nep_orchestrator.py#L368](../src/services/orchestrator/nep_orchestrator.py#L368) |
| <a id="emulated-plane"></a>**[Emulated plane](concepts/decision.md#emulated-vs-real)** | The NIC plane: shown as evidence, labeled `emulated: true`, **never counted**. See [concept page](concepts/decision.md#emulated-vs-real). | [nic_tools.py](../src/extensions/sheldon/agentic/nic_tools.py) |

## Output

Concept page: [Audit trail and TMF688](concepts/audit-trail.md)

| Term | In this demo | Code |
|---|---|---|
| <a id="tmf688-rcaconcludedevent"></a>**[TMF688 RcaConcludedEvent](concepts/audit-trail.md)** | The RCA's result as a TM Forum event: fault class, corroboration, verdict, evidence per plane, narrative. <br>Definition: [TM Forum TMF688](https://github.com/tmforum-apis/TMF688-Event) | [nep_orchestrator.py#L413](../src/services/orchestrator/nep_orchestrator.py#L413) |
| <a id="template-narrative"></a>**[Template narrative](concepts/audit-trail.md)** | The plain-text explanation used when no LLM is connected, labeled `[template - LLM unavailable]`. An LLM can replace it; either way it only explains. | [nep_orchestrator.py#L195](../src/services/orchestrator/nep_orchestrator.py#L195) |

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

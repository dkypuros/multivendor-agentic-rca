# Reference

## Sandbox console API: `ran-sandbox-controller` :7098 (the Route)

| Method | Path | Returns |
|---|---|---|
| GET | `/` | The console page |
| GET | `/healthz` | `{"ok": true}` |
| GET | `/api/ran/status` | `deployment` (replicas/ready), `pod`, `o1` (O-DU `/o1/status`), `alarms`, `ptp` (bridge `/ptp`), `ue` (last attach result), `busy` (`STARTING`/`STOPPING`/null), `errors` |
| GET | `/api/ran/logs` | Up to 300 lines `{timestamp, source, message}`. Sources: `SANDBOX`, `K8S`, `UE-SIM`, `PTP-BRIDGE`, `O1`, `NEP`, plus the slice pod's own lines (`launch_slice`, `amf`, `smf`, `odu`, …) |
| POST | `/api/ran/start` | `202 {"accepted": true, "status": "STARTING"}`. Scales the slice to 1, waits for cell `ACTIVE`, runs the UE attach. |
| POST | `/api/ran/stop` | `202 …STOPPING`. Scales the slice to 0. |
| POST | `/api/ran/inject-ptp-fault` | `{ptp, o1}`: bridge `/ptp/inject` + O-DU `administrativeState=LOCKED` |
| POST | `/api/ran/heal-ptp` | `{ptp, o1}`: bridge `/ptp/heal` + `UNLOCKED` |
| POST | `/api/ran/trigger-rca` | This run's full NEP audit trace (`traceId`, `spans`, `tmf688Event`, `llmSynthesis`), or `{"error": …}` |

## NEP orchestrator API: `nep-orchestrator` :7095 (in-cluster)

| Method | Path | Returns |
|---|---|---|
| GET | `/nep/health` | Service info and configured endpoints |
| POST/GET | `/nep/rca/trigger` | Runs the RCA and returns the TMF688 `RcaConcludedEvent`. With `?view=full`, returns this run's whole audit trace instead. |
| GET | `/nep/audit/latest` | Latest full trace: `traceId`, `spans[6]`, `tmf688Event`, `llmSynthesis` |
| GET | `/nep/audit/traces` | The last 50 traces (in memory) |
| GET | `/nep/ptp/sync`, `/nep/status` | PTP state, the current CloudEvent, and O-DU alarms. `du_timing_diagnostics` is the O-DU's **static** reference analysis, not live. |

## Component endpoints used by the flows

| Component | Endpoints |
|---|---|
| O-DU (in `ran-slice`) | `GET /o1/status`, `GET /o1/alarms`, `PUT /o1/config {administrativeState}`, `POST /rrc/ue-messages` (UE RRC/NAS), UDP 7011 (Uu) |
| ptp-bridge | `GET /ptp`, `GET /ptp/cloud-events`, `GET /ptp/logs`, `POST /ptp/inject`, `POST /ptp/heal` |
| capif | `POST /api-provider-management/v1/registrations`, `POST /published-apis/v1/{apf}/service-apis`, `POST /api-invoker-management/v1/onboardedInvokers`, `POST /capif-security/v1/trustedInvokers/{id}/token` |
| mcp-gateway | `POST /mcp` (JSON-RPC `tools/list`, `tools/call`; `Authorization: Bearer <CAPIF JWT>`) |
| mcp-* | `POST /mcp` (called by the gateway) |

## Configuration

Every value below is an environment variable. The manifests in `deploy/openshift/` set the in-namespace values; the defaults shown are the code's own.

### ran-sandbox-controller

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `7098` | Listen port |
| `SLICE_DEPLOYMENT` | `ran-slice` | Deployment that Start/Stop scales (must match the Role's `resourceNames`) |
| `SLICE_HOST` | `ran-slice` | Host the UE sends its user plane (UDP 7011) to |
| `ODU_URL` | `http://ran-slice:7010` | O-DU base URL (O1 and RRC) |
| `SLICE_READY_TIMEOUT` | `240` | Seconds to wait for `cellState=ACTIVE` before skipping the UE attach |
| `UE_TIMEOUT` | `120` | Hard deadline for one `ue_sim` run; the process is killed after it |
| `RCA_TIMEOUT` | `150` | Console → NEP timeout for one RCA. Keep it above `LLM_TIMEOUT` and below the Route timeout (180 s). |
| `PTP_BRIDGE_URL` | `http://ptp-bridge:7091` | |
| `NEP_URL` | `http://nep-orchestrator:7095` | |
| `UE_SUPI` / `UE_K` / `UE_ECHOES` | `imsi-001010000000001` / 3GPP TS 35.208 Test Set 1 key / `3` | The UE to attach. It must exist in the UDM seed (`src/services/core/udm/subscribers.json`). The key is a public test vector. |
| `SLICE_NAMESPACE` | the ServiceAccount's namespace (`default` if none) | |
| `K8S_API_URL`, `K8S_SA_DIR`, `SLICE_PORT_OFFSET` | in-cluster values | For local testing only (`tests/e2e_local.py`) |

### ran-slice

| Variable | Value in manifest | Meaning |
|---|---|---|
| `TELCO_SLICE_PROFILE` | `ran` | stackctl profile: core + IMS + split RAN, 32 NFs |
| `TELCO_BIND` | `0.0.0.0` | Listening sockets reachable through the Service |
| `TELCO_HOST` | `127.0.0.1` | NFs reach each other on loopback, inside the pod |
| `TELCO_PORT_OFFSET` | `0` | Shift every port (used by the local test) |
| `TELCO_OTLP_ENDPOINT` | unset | Optional OTLP/HTTP collector for traces |
| `SMO_BRIDGE_URL` | unset | Optional SMO perception bridge; the O-DU and O-CU-CP post RAN events to it. Unset = off. |

### nep-orchestrator

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `7095` | Listen port |
| `ODU_URL`, `PTP_BRIDGE_URL`, `CAPIF_URL`, `MCP_GATEWAY_URL` | `http://ran-slice:7010`, `http://ptp-bridge:7091`, `http://capif:7027`, `http://mcp-gateway:8800` | Upstreams |
| `AI_GATEWAY_URL` | empty (no LLM) | OpenAI-compatible base URL, without `/v1` |
| `LLM_MODEL` / `LLM_API_KEY` | `llama31-8b-w8a8` / `none` | |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | `90` / `200` | |
| `NIC_FAULT_MODE` | `follow-ptp` | `follow-ptp`: the emulated NIC reports a fault only while PTP was read and is unlocked. `always` / `never` pin it. The NIC plane never counts toward the decision bar. |
| `MLFLOW_TRACKING_URI` | empty | Label copied into traces; nothing is logged to MLflow |
| `POD_NAMESPACE` | set from the Downward API | Shown by `/nep/health` |

### MCP servers and gateway

| Deployment | Variable | Value / default |
|---|---|---|
| `mcp-gateway` | `--policy` | `/etc/gateway/gateway-policy.yaml` (ConfigMap from `deploy/openshift/gateway-policy.yaml`) |
| `mcp-ran` | `RAN_O1_URL` | `http://ran-slice:7010`. Required; without it the RAN plane answers `unavailable`. |
| `mcp-redhat` | `TELCO_PTP_URL`, `PTP_OFFSET_LIMIT_NS` | `http://ptp-bridge:7091`, `100000` |
| `mcp-intel` | `NIC_FAULT` | Server-wide default; the orchestrator passes `fault` per call |
| `mcp-ocloud` | `SHELDON_KUBECTL`, `SHELDON_KUBECONFIG` | `kubectl`, `~/.kube/sheldon.yaml` (see [deployment.md](deployment.md#optional-o-cloud-plane-with-acm)) |
| `mcp-gateway` | policy `capifScopeMap.mcp-tools` | The four tools the RCA calls: `ocloud_cluster_health`, `ran_gnb_status`, `ptp_operator_status`, `nic_timestamp_counters` |

### ptp-bridge

| Variable | Default |
|---|---|
| `PTP_BRIDGE_PORT` | `7091` |
| `PTP_NODE_NAME` | `lab-node` (used in identities and CloudEvent sources) |

## Ports

| Port | Proto | Where |
|---|---|---|
| 7098 | TCP | ran-sandbox-controller (Route) |
| 7010 | TCP | ran-slice: O-DU RRC/F1AP/O1 |
| 7011 | UDP | ran-slice: O-DU Uu user plane |
| 7091 | TCP | ptp-bridge |
| 7027 | TCP | capif |
| 8800 | TCP | mcp-gateway |
| 8850–8853 | TCP | mcp-ocloud, mcp-ran, mcp-intel, mcp-redhat |
| 7095 | TCP | nep-orchestrator |

Inside the slice pod, the NFs use the full port plan of the upstream stack on loopback. It's defined in `src/domain/netconfig.py`, for example AMF 7002, SMF 7003, UPF 7004, NRF 7100, and GTP-U 2152/2155/2156.

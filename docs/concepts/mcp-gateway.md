# MCP and the gateway

[Glossary](../glossary.md#tools) · [All concepts](README.md)

## What it is

**MCP** (Model Context Protocol) is an open standard for how an AI agent discovers and calls tools. A server lists its tools (`tools/list`), and the agent calls one by name with arguments (`tools/call`), over JSON-RPC.

The point for a multivendor network: every vendor exposes its layer the same way, so the agent needs one integration, not one per vendor.

## In this demo

Four MCP servers, one per **plane**, each in its own Deployment:

| Plane | Server | Tool the RCA calls | Reads |
|---|---|---|---|
| O-Cloud (cluster) | `mcp-ocloud` | `ocloud_cluster_health` | ACM ManagedClusters via kubectl, if present |
| RAN | `mcp-ran` | `ran_gnb_status` | The O-DU's O1 status and alarms |
| Timing (platform) | `mcp-redhat` | `ptp_operator_status` | `ptp-bridge` |
| NIC (hardware) | `mcp-intel` | `nic_timestamp_counters` | Nothing: **emulated** |

The agent never talks to these servers directly. It sends every call, with its CAPIF token, to the **MCP gateway**, which:

1. checks the token and maps its scope to the allowed tools (see [CAPIF](capif.md));
2. answers `tools/list` by merging all servers' tools and hiding the out-of-scope ones;
3. routes each `tools/call` to the server that owns the tool, or refuses it with `-32001`;
4. logs every call.

The gateway's log during one RCA:

```text
[gateway] invoker=invoker-a694b3fc2060 method=tools/call tool=ocloud_cluster_health  backend=http://mcp-ocloud:8850 -> forwarded
[gateway] invoker=invoker-a694b3fc2060 method=tools/call tool=ran_gnb_status         backend=http://mcp-ran:8851    -> forwarded
[gateway] invoker=invoker-a694b3fc2060 method=tools/call tool=ptp_operator_status    backend=http://mcp-redhat:8853 -> forwarded
[gateway] invoker=invoker-a694b3fc2060 method=tools/call tool=nic_timestamp_counters backend=http://mcp-intel:8852  -> forwarded
```

The RCA step that makes these calls is span `O-RAN.R1.MCP_Tool_Execution`. It shows `PARTIAL` when a plane can't answer, as the O-Cloud plane can't without ACM.

## Honest limits

- The gateway is a policy and routing point, not a security boundary: tokens are unsigned (see [CAPIF](capif.md)).
- Each MCP server wraps a small Python tool module; they are not vendor-supplied servers.
- The NIC server returns emulated counters. The O-Cloud server returns "unavailable" unless ACM and a kubeconfig are added.
- The RCA calls one fixed tool per plane; the agent doesn't choose tools dynamically.

## Code

- Gateway: [gateway.py](../../src/extensions/sheldon/agentic/gateway.py), policy [gateway-policy.yaml](../../deploy/openshift/gateway-policy.yaml)
- MCP protocol handling: [mcp_core.py](../../src/extensions/sheldon/agentic/mcp_core.py), HTTP wrapper [mcp_http.py](../../src/extensions/sheldon/agentic/mcp_http.py)
- Plane servers: [mcp_server.py](../../src/extensions/sheldon/agentic/mcp_server.py) (O-Cloud), [mcp_ran.py](../../src/extensions/sheldon/agentic/mcp_ran.py), [mcp_redhat.py](../../src/extensions/sheldon/agentic/mcp_redhat.py), [mcp_intel.py](../../src/extensions/sheldon/agentic/mcp_intel.py)
- The agent's fan-out: [nep_orchestrator.py#L302](../../src/services/orchestrator/nep_orchestrator.py#L302)
- Deployments: [mcp.yaml](../../deploy/openshift/mcp.yaml)

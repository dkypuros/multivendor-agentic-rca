# CAPIF

[Glossary](../glossary.md#authorization) · [All concepts](README.md)

## What it is

CAPIF (Common API Framework, 3GPP TS 29.222) is 3GPP's standard way to expose network APIs: providers **publish** APIs, callers **onboard** as invokers, and CAPIF issues each invoker an access token **scoped** to the APIs it may call.

In this demo CAPIF is the **permission layer between the agent and the tools**. It never touches RAN data and takes no part in the diagnosis. Its only job is to decide which tools the agent may call, and to give it a token that proves it. It answers the customer question "who let the AI touch my network?"

## In this demo

Every RCA's second step (span `3GPP.CAPIF.Security_Authz`):

1. **Publish:** the agent registers an API provider and publishes one API, `mcp-tools`, exposed by the MCP gateway (in CAPIF terms, the gateway is the AEF, the API Exposing Function).
2. **Onboard:** the agent registers as an API invoker and gets an ID such as `invoker-a694b3fc2060`.
3. **Token:** it requests a token with scope `3gpp#mcp-aef:mcp-tools` and gets a JWT with issuer `capif-core`, an expiry and that scope.
4. **Reuse:** the token is cached until 60 seconds before it expires, so later RCAs show `capif.tokenSource: cached` and this step takes about 0 ms.

CAPIF's own log, from the lab:

```text
{"event": "api_provider_registered", "apiProvDomId": "domain-e58654b46111", "funcs": ["APF", "AEF"]}
{"event": "service_api_published",   "apiName": "mcp-tools"}
{"event": "api_invoker_onboarded",   "apiInvokerId": "invoker-a694b3fc2060"}
{"event": "access_token_issued",     "scope": "3gpp#mcp-aef:mcp-tools"}
```

**Enforcement** happens in the MCP gateway, on every call (see [MCP and the gateway](mcp-gateway.md)):

- it checks the token: issuer `capif-core`, not expired, a known API in the scope;
- `gateway-policy.yaml` maps `mcp-tools` to exactly four read-only tools: `ocloud_cluster_health`, `ran_gnb_status`, `ptp_operator_status`, `nic_timestamp_counters`;
- `tools/list` shows only those four;
- any other `tools/call` is refused with error `-32001` "not in scope" and logged. The O-Cloud *action* tools (power, reprovision) exist on the backend but are deliberately out of scope.

## Honest limits

- **Unsigned tokens.** CAPIF issues unsigned JWTs and the gateway decodes them without checking a signature. This shows the authorization *flow*, not a security boundary. The real boundary is the namespace [NetworkPolicy](../../deploy/openshift/networkpolicy.yaml).
- **Single-pod shortcut.** The agent publishes the API itself before onboarding. In production the vendor or platform would publish its APIs, and the operator's agent would only onboard and request tokens.
- No mutual TLS or OAuth client credentials, which production CAPIF would require.

## Code

- CAPIF: [capif.py](../../src/services/core/capif/capif.py), token endpoint [#L286](../../src/services/core/capif/capif.py#L286)
- Agent side: [nep_orchestrator.py#L90](../../src/services/orchestrator/nep_orchestrator.py#L90) (cache), [#L105](../../src/services/orchestrator/nep_orchestrator.py#L105) (onboarding)
- Enforcement: [gateway.py#L66](../../src/extensions/sheldon/agentic/gateway.py#L66) (token check), [#L125](../../src/extensions/sheldon/agentic/gateway.py#L125) (scope filter and refusal)
- Scope to tools map: [gateway-policy.yaml](../../deploy/openshift/gateway-policy.yaml)

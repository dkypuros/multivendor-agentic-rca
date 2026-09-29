# Audit trail and TMF688

[Glossary](../glossary.md#output) · [All concepts](README.md)

## What it is

Every RCA leaves a record of what it did and why: a **trace** of six timed steps (**spans**), and a result event in **TM Forum TMF688** format (the Event Management API), so other OSS/BSS tools could consume it.

## In this demo

One click on **Run 4-Plane Agentic RCA** produces one trace. The console shows it as the audit-trail panel:

| # | Span | What it does | Example (post-heal) |
|---|---|---|---|
| 1 | `O-RAN.O1.FM.Alarm_Ingest` | Reads the O-DU's alarms and the PTP state | 6.56 ms, OK |
| 2 | `3GPP.CAPIF.Security_Authz` | Gets or reuses the scoped token ([CAPIF](capif.md)) | 0.01 ms, OK (cached) |
| 3 | `O-RAN.R1.MCP_Tool_Execution` | Calls the four planes ([MCP](mcp-gateway.md)) | 41.99 ms, PARTIAL (no ACM) |
| 4 | `O-RAN.NonRT_RIC.Deterministic_Router` | Decides ([decision](decision.md)) | 0.02 ms, OK |
| 5 | `Enterprise.AI.LLM_Synthesis` | Writes the narrative: LLM if connected, else a template | 0.04 ms, FALLBACK |
| 6 | `TMForum.TMF688.Audit_Event_Emission` | Builds the TMF688 event | 0 ms, OK |

The TMF688 `RcaConcludedEvent` carries the fault class, corroboration, verdict, one evidence entry per plane, and the narrative. A trimmed example after heal:

```json
{
  "eventType": "RcaConcludedEvent",
  "event": {
    "traceId": "tr-oran-tmf-4815649e593e",
    "invoker": "invoker-2073854b9b8b",
    "capifScope": "3gpp#mcp-aef:mcp-tools",
    "faultClass": null,
    "corroboration": "0/3",
    "decision": "NO ACTION — no fault signals",
    "evidence": [ { "plane": "ran", "answered": true, "emulated": false, "signals": [],
                    "evidence": "cell ACTIVE, RU CONNECTED, admin UNLOCKED, no alarms" } ]
  }
}
```

Full examples: [heal-recheck-run.md](../examples/heal-recheck-run.md), [openshift-verification-output.md](../examples/openshift-verification-output.md).

**Where to find it:** the console's **View Raw Response JSON**; `GET /nep/audit/latest` and `/nep/audit/traces` on the orchestrator (last 50 runs); and one log line per run in the console, e.g. `NEP: RCA tr-oran-tmf-…: no fault (0/3) -> NO ACTION`.

## Honest limits

- **In memory only.** The last 50 traces are lost when the orchestrator restarts. No database, no export.
- The event is TMF688-*shaped*; it isn't sent to a TM Forum event hub. It's returned to the caller and kept in memory.
- Span names use O-RAN and TM Forum terms (R1, Non-RT RIC, ODA) to show where each step maps in a production architecture. There is no RIC or R1 interface in this demo.
- The field `mlflowRunId` is a leftover name; there's no MLflow.

## Code

- The six spans: [nep_orchestrator.py#L248](../../src/services/orchestrator/nep_orchestrator.py#L248)
- TMF688 event: [nep_orchestrator.py#L413](../../src/services/orchestrator/nep_orchestrator.py#L413)
- Narrative and template fallback: [nep_orchestrator.py#L195](../../src/services/orchestrator/nep_orchestrator.py#L195)
- Audit endpoints and history: [nep_orchestrator.py#L468](../../src/services/orchestrator/nep_orchestrator.py#L468)

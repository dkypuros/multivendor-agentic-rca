# Example run: inject, RCA, heal, re-check

A real run of the sandbox on OpenShift, 29 Sep 2026, with no LLM connected (template narrative). Use it to check that your own run looks the same. Back to the [demo walkthrough](../demo-walkthrough.md#5-heal-and-re-check-2-minutes).

| Time | Step | Result |
|---|---|---|
| 11:53:04 | Start RAN Slice | UE `REGISTERED`, PDU session `10.45.0.3`, echo `3/3` |
| 11:53:58 | Inject Fault | PTP `FREERUN`, cell locked |
| 11:54:01 | Run RCA | `OC-TimingDegraded (2/3) → HOLD`, about 3 seconds after the inject |
| 11:54:21 | Heal Timing | PTP `LOCKED`, offset 0 ns, cell unlocked |
| 11:54:42 and 11:54:50 | Run RCA | `no fault (0/3) → NO ACTION`, returning instantly |

## Log stream

```text
[11:53:58.701] PTP-BRIDGE: POST /ptp/inject -> lock_state=FREERUN offset=-50000198 ns
[11:53:58.704] odu: o1_config_applied administrativeState=LOCKED
[11:53:58.704] O1: PUT O-DU /o1/config administrativeState=LOCKED
[11:54:01.901] NEP: RCA tr-oran-tmf-e6c62ed512ff: OC-TimingDegraded (2/3) -> HOLD — below the bar, needs human approval
[11:54:21.936] PTP-BRIDGE: POST /ptp/heal -> lock_state=LOCKED offset=0 ns
[11:54:21.945] odu: o1_config_applied administrativeState=UNLOCKED
[11:54:21.946] O1: PUT O-DU /o1/config administrativeState=UNLOCKED
[11:54:42.511] NEP: RCA tr-oran-tmf-0213eda43fea: no fault (0/3) -> NO ACTION — no fault signals
[11:54:50.886] NEP: RCA tr-oran-tmf-4815649e593e: no fault (0/3) -> NO ACTION — no fault signals
```

## Panels after heal

- PTP: offset `0 ns`, servo `LOCKED`, CloudEvent `LOCKED`
- O-DU: cell `ACTIVE`, alarm `None`, RF carrier `Active (oru-1, 1 UE)`
- UE: `REGISTERED imsi-001010000000001`, `10.45.0.3`, `3/3`

## Spans of the post-heal RCA

| Span | Time | Status |
|---|---|---|
| O-RAN.O1.FM.Alarm_Ingest | 6.56 ms | OK |
| 3GPP.CAPIF.Security_Authz | 0.01 ms | OK (cached token) |
| O-RAN.R1.MCP_Tool_Execution | 41.99 ms | PARTIAL: the O-Cloud plane is unavailable without ACM, as expected |
| O-RAN.NonRT_RIC.Deterministic_Router | 0.02 ms | OK |
| Enterprise.AI.LLM_Synthesis | 0.04 ms | FALLBACK (no LLM, template narrative) |
| TMForum.TMF688.Audit_Event_Emission | 0 ms | OK |

## TMF688 event (post-heal)

```json
{
  "eventType": "RcaConcludedEvent",
  "event": {
    "traceId": "tr-oran-tmf-4815649e593e",
    "capifScope": "3gpp#mcp-aef:mcp-tools",
    "faultClass": null,
    "signal": null,
    "corroboration": "0/3",
    "decision": "NO ACTION — no fault signals",
    "planes": [],
    "evidence": [
      {"plane": "cluster",  "answered": false, "emulated": false, "signals": [], "evidence": "unavailable: ocloud_unavailable kubectl failed: [Errno 2] No such file or directory: 'kubectl'"},
      {"plane": "ran",      "answered": true,  "emulated": false, "signals": [], "evidence": "cell ACTIVE, RU CONNECTED, admin UNLOCKED, no alarms"},
      {"plane": "platform", "answered": true,  "emulated": false, "signals": [], "evidence": "ptp4l offset 0 ns (limit 100000), DU port SLAVE"},
      {"plane": "hardware", "answered": true,  "emulated": true,  "signals": [], "evidence": "EMULATED ethtool -S: tx_hwtstamp_timeouts=0"}
    ]
  }
}
```

The event is trimmed to the fields that matter. **View Raw Response JSON** in the console shows the full event.

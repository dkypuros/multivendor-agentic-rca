# O1 alarms (TS 28.532)

[Glossary](../glossary.md#ran-o-ran-split) · [All concepts](README.md)

## What it is

3GPP TS 28.532 defines how a network function reports faults to management systems: each active alarm has a name, a **probable cause**, a severity and the managed object it affects. For O-RAN units these alarms are read over **O1**. They are what a NOC normally sees first: "cell unavailable".

## In this demo

The O-DU serves its active alarms at `GET /o1/alarms`. While the cell is locked (or the O-RU is disconnected) it reports one:

```json
{
  "alarmId": "ALARM-DU-001",
  "faultName": "CellUnavailable",
  "probableCause": "lossOfRealTimeSynchronization",
  "perceivedSeverity": "CRITICAL",
  "managedObject": "ManagedElement=1,GNBDUFunction=1,NRCellDU=1"
}
```

It's used in two places:

- **The console's O-DU panel:** `Active Alarm: CellUnavailable (lossOfRealTimeSynchronization)`.
- **The RAN plane during an RCA:** the `ran_gnb_status` tool reads `/o1/status` and `/o1/alarms`, and raises the signal `du_sync_loss_alarm` when an alarm's probable cause is `lossOfRealTimeSynchronization`. Evidence from the example run, during the fault: `cell UNAVAILABLE, RU CONNECTED, admin LOCKED, CellUnavailable/lossOfRealTimeSynchronization`. After heal: `cell ACTIVE, RU CONNECTED, admin UNLOCKED, no alarms`.

## Honest limits

- The alarm follows from the lock the console applies during Inject, not from the O-DU sensing the timing fault itself (it has no servo).
- The probable cause is fixed: **any** locked or RU-disconnected state is reported as `lossOfRealTimeSynchronization`. Locking the cell for another reason would produce the same alarm.
- Alarms are computed on each read; there's no alarm history, acknowledgement or clearing notification.

## Code

- Alarm endpoint: [odu.py#L142](../../src/services/ran/odu/odu.py#L142)
- RAN plane tool and signal: [ran_tools.py#L36](../../src/extensions/sheldon/agentic/ran_tools.py#L36)
- RAN plane MCP server: [mcp_ran.py](../../src/extensions/sheldon/agentic/mcp_ran.py)

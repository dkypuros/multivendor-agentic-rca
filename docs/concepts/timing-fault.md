# The timing fault

[Glossary](../glossary.md#timing) · [All concepts](README.md)

## What it is

A 5G TDD cell transmits and receives on a shared schedule, so every radio must agree on time to within about 1.5 microseconds. They get that time from **PTP** (Precision Time Protocol): a grandmaster clock, and a servo (`ptp4l`) on each server that stays **LOCKED** to it. If a clock loses sync it goes **FREERUN**, and the cell must stop transmitting before it interferes with its neighbors.

## In this demo

`ptp-bridge` is a software model of that clock. **Inject Fault** does two things:

1. **Breaks the clock:** `POST /ptp/inject` sets the offset to −50,000,198 ns (about −50 ms, roughly 30,000 times the budget), the lock state to `FREERUN` and the DU port to `UNCALIBRATED`, and publishes a CloudEvent.
2. **Protects the cell:** the console locks the cell over O1, as the DU's protective carrier shutdown. The O-DU then raises its alarm (see [O1 alarms](o1-alarms.md)).

**Heal Timing** reverses both: offset 0 ns, `LOCKED`, DU port `SLAVE`, cell unlocked.

From the example run:

```text
[11:53:58.701] PTP-BRIDGE: POST /ptp/inject -> lock_state=FREERUN offset=-50000198 ns
[11:53:58.704] O1: PUT O-DU /o1/config administrativeState=LOCKED
[11:54:21.936] PTP-BRIDGE: POST /ptp/heal -> lock_state=LOCKED offset=0 ns
[11:54:21.946] O1: PUT O-DU /o1/config administrativeState=UNLOCKED
```

During an RCA the **timing plane** reads the bridge and raises `ptp_offset_exceeded` when the offset is beyond 100,000 ns: `ptp4l offset -50000198 ns (limit 100000), DU port UNCALIBRATED`.

## Honest limits

- No PTP hardware, grandmaster, `ptp4l` or PTP Operator is involved. The bridge produces state and log lines in their shape.
- **Only the timing plane observes the fault directly.** The O-DU has no servo, so the cell lock and its alarm are applied by the console as part of the same click. Say so if asked.
- The fault is a single fixed step (−50 ms). There's no gradual drift or holdover.

## Code

- The model: [ptp_bridge.py#L97](../../src/services/smo/ptp_bridge.py#L97) (inject and heal)
- The timing plane's check: [ptp_tools.py](../../src/extensions/sheldon/agentic/ptp_tools.py)
- The console's Inject and Heal: [sandbox_controller.py#L256](../../src/services/ran/sandbox_controller.py#L256)

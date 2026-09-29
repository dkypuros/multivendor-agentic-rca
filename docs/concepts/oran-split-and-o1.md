# O-RAN split and O1

[Glossary](../glossary.md#ran-o-ran-split) · [All concepts](README.md)

**Official definition:** [O-RAN Alliance specifications](https://www.o-ran.org/specifications), [3GPP TS 38.401](https://www.3gpp.org/DynaReport/38401.htm) (NG-RAN architecture), [3GPP TS 28.541](https://www.3gpp.org/DynaReport/28541.htm) (administrativeState)

## What it is

O-RAN splits a base station into separate units from potentially different vendors, connected by open interfaces:

- **O-RU** (radio unit): the antenna side.
- **O-DU** (distributed unit): real-time scheduling; owns the cell.
- **O-CU-CP** and **O-CU-UP** (central unit, control and user plane): signaling and user traffic toward the core.

**O1** is the management interface every unit exposes to the operator's management system: configuration, status and fault (alarm) reporting.

## In this demo

All four units run as processes inside the `ran-slice` pod, next to the 5G core. The O-RU sends a fronthaul heartbeat to the O-DU. The cell is `ACTIVE` only while that heartbeat arrives **and** the cell is administratively `UNLOCKED`.

The console uses the O-DU's O1 interface in two ways:

| Call | Used for |
|---|---|
| `GET /o1/status`, `GET /o1/alarms` | The O-DU panel on the page, and the RAN plane's evidence during an RCA |
| `PUT /o1/config {administrativeState}` | **Inject** sets `LOCKED` (protective carrier shutdown); **Heal** sets `UNLOCKED` |

From the example run:

```text
[11:53:58.704] odu: o1_config_applied administrativeState=LOCKED
[11:53:58.704] O1: PUT O-DU /o1/config administrativeState=LOCKED
[11:54:21.945] odu: o1_config_applied administrativeState=UNLOCKED
```

While locked, the O-DU rejects new UEs at RRC setup (`503 CELL_LOCKED`).

## Honest limits

- No radio: the units implement protocol logic only (no PHY or RF).
- O1 here is JSON over HTTP, not NETCONF/YANG. `administrativeState` follows the 3GPP TS 28.541 model.
- The O-DU has no timing servo, so it doesn't notice a PTP fault on its own. The **console** applies the lock as part of Inject. See [the timing fault](timing-fault.md).
- The units share one pod because each calls its peers on localhost.

## Code

- O-DU cell state and O1: [odu.py#L114](../../src/services/ran/odu/odu.py#L114) (config), [#L131](../../src/services/ran/odu/odu.py#L131) (status), [#L70](../../src/services/ran/odu/odu.py#L70) (RRC admission)
- Units: [ocucp/](../../src/services/ran/ocucp), [ocuup/](../../src/services/ran/ocuup), [oru/](../../src/services/ran/oru)
- Console lock/unlock: [sandbox_controller.py#L247](../../src/services/ran/sandbox_controller.py#L247)

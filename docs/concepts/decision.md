# How the decision is made

[Glossary](../glossary.md#decision) · [All concepts](README.md)

## What it is

The heart of the demo: turning evidence from several vendors' layers into one diagnosis and one verdict **without an LLM in the decision**. The rule is simple enough to read in a minute and to audit afterwards.

## In this demo

Span `O-RAN.NonRT_RIC.Deterministic_Router`, in four steps:

1. **Signals.** Each plane's evidence carries zero or more named signals: `du_sync_loss_alarm` (RAN), `ptp_offset_exceeded` (timing), `nic_firmware_suspect` (NIC), `managedcluster_unavailable` (O-Cloud), and so on.
2. **Route table.** A fixed, ordered list maps each signal to a **fault class**. The four timing signals all map to `OC-TimingDegraded`. The first signal found in table order wins.
3. **Corroboration.** Count the **real** planes (O-Cloud, RAN, timing: 3 in total) that carry any signal of the winning fault class.
4. **Verdict.** Compare with the **bar** of 3:

| Situation | Verdict |
|---|---|
| No signals at all | `NO ACTION — no fault signals` |
| Fault class found, 3/3 real planes agree | `APPLY (auto-eligible)` |
| Fault class found, fewer than 3 agree | `HOLD — below the bar, needs human approval` |

From the example run:

| When | Signals | Result |
|---|---|---|
| During the fault | RAN `du_sync_loss_alarm`, timing `ptp_offset_exceeded`, NIC `nic_firmware_suspect`, O-Cloud unavailable | `OC-TimingDegraded`, **2/3 → HOLD** |
| After heal | none | **0/3 → NO ACTION** |

The span records `policy.corroboratingPlanes: [platform, ran]` and `policy.emulatedPlanesNotCounted: [hardware]`.

### Emulated vs real

The NIC plane is **emulated**: the lab has no Intel E810, so its counters are generated. Its response says `emulated: true`, and it reports a fault only while the timing plane is actually unlocked (`NIC_FAULT_MODE=follow-ptp`), so it never contradicts reality. It still appears in the evidence, but **it never counts toward the bar**. That's why the demo lands on HOLD: two real planes agree, the third real plane (O-Cloud) can't answer without ACM, and synthetic evidence isn't allowed to push the verdict over the line.

## Honest limits

- **Nothing is executed.** APPLY means only "eligible for automation"; the lab has no remediation or approval workflow. Heal is a button.
- With ACM absent, APPLY is unreachable in this lab: at most 2 real planes can agree.
- The table is small and hand-written; it knows the signals these four tools emit, nothing else.
- An LLM narrative may paraphrase the verdict loosely ("remediation triggered"). The audit record is the source of truth.

## Code

- Route table: [nep_orchestrator.py#L37](../../src/services/orchestrator/nep_orchestrator.py#L37)
- Decision step: [nep_orchestrator.py#L347](../../src/services/orchestrator/nep_orchestrator.py#L347) (bar at [#L362](../../src/services/orchestrator/nep_orchestrator.py#L362))
- NIC follow-ptp rule: [nep_orchestrator.py#L306](../../src/services/orchestrator/nep_orchestrator.py#L306), emulated tool [nic_tools.py](../../src/extensions/sheldon/agentic/nic_tools.py)
- Signal sources: [ran_tools.py#L36](../../src/extensions/sheldon/agentic/ran_tools.py#L36), [ptp_tools.py](../../src/extensions/sheldon/agentic/ptp_tools.py), [ocloud_tools.py#L155](../../src/extensions/sheldon/agentic/ocloud_tools.py#L155)

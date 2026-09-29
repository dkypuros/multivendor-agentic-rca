# Demo walkthrough: 20 minutes

New to a term? See the [glossary](glossary.md); the [concept pages](concepts/README.md) go deeper on the ones customers ask about.

A presenter script for the Cloud RAN AI Sandbox. Each step says **what to click**, **what appears**, and **what to say**. Everything on screen comes from a running component, so you can back up any claim with `oc`.

**The story:** a cell goes down in a multivendor network. The RAN comes from one vendor, the cloud platform from another, the timing from a third, and the NIC silicon from a fourth. Whose fault is it? An agent gathers testimony from every plane, a deterministic policy decides, and an LLM only explains the decision.

## Before you start (5 minutes, off-screen)

1. The deployment is healthy: `python3 tests/smoke_route.py https://<route>` prints `ALL GREEN`. It leaves the RAN stopped and PTP healed.
2. Open two tabs:
   - the sandbox, `https://<route>`
   - the OpenShift console, on **Workloads → Pods** in the project, to show pods appearing and disappearing
3. Optional: a terminal with `oc get pods -w -l app=ran-slice` running.
4. Decide whether an LLM is connected (see [deployment.md](deployment.md#optional-connect-an-llm)). The demo works either way. The narrative panel is labeled honestly in both cases.

The console starts at **RAN STOPPED (IDLE)**, PTP `LOCKED`, and cell `OFFLINE (slice not running)`.

## 1. Framing (2 minutes)

**Talk track:**
> "A cell is unavailable. In a multivendor RAN that single symptom can come from the radio stack, the cloud platform, the timing network or the NIC firmware. Each vendor's tools only see their own layer. Today we'll run a live RAN on OpenShift, break its timing, and let an agent collect evidence from every layer through one governed interface, with a policy engine that decides and an LLM that only explains."

Tips: Worth mentioning on the UI:
- the **log stream** on the left, merging the console's own actions with the RAN pod's real stdout
- the **controls**
- **UE**, **PTP timing plane** and **[O-DU](glossary.md#o-du) faults ([O1](glossary.md#o1))** panels, each labeled with the endpoint it reads
- **NEP Agentic RCA**

## 2. Start the RAN (4 minutes)

**Click** **Start RAN Slice**.

**What you should see:**
- The pill turns amber: `STARTING...`.
- `K8S: PATCH deployments/ran-slice/scale replicas=1`, then pod `Pending`, then `Running`. In the OpenShift tab a `ran-slice-…` pod appears.
- `launch_slice: READY nf=nrf …`, then the other 31 functions in dependency tiers (UDM and friends, then AMF, …, the O-DU, and finally the O-RU, SMF and UPF), each gated `READY`, ending with `boot status=done ready=32`.
- `K8S: … O-DU cellState=ACTIVE ru=CONNECTED`. The O-RU's fronthaul heartbeat is reaching the O-DU.
- `UE-SIM:` lines, in order:
  `RRCSetupRequest → RRCSetup`, `RegistrationRequest → AuthenticationRequest`, `AuthenticationResponse → SecurityModeCommand`, `SecurityModeComplete → RegistrationAccept`, `PDU SESSION pduAddress=10.45.0.x`, `ECHO 3/3 replies via the RAN over the GTP-U tunnel`.
- Interleaved lines from the pod itself: `amf: registration_accepted`, `smf: session_created`, `amf: pdu_session_established`.
- Panels: UE `REGISTERED imsi-001010000000001` / `10.45.0.x` / `3/3`; cell `ACTIVE`; RF carrier `Active (oru-1, 1 UE)`; pill `RAN RUNNING & LOCKED`.

**Talk track:**
> "That button scaled a Kubernetes Deployment from zero to one. The pod runs a 5G core and an O-RAN split RAN: CU control plane, CU user plane, DU and RU. When the DU reported its cell active over O1, a UE simulator attached through it. The authentication is real [5G-AKA](glossary.md#5g-aka); the UE and the UDM compute [MILENAGE](glossary.md#milenage) independently. The echoes crossed the [GTP-U](glossary.md#gtp-u-echo) tunnel through the CU-UP and UPF. When we're done, Stop scales it back to zero, so a shared lab doesn't pay for an idle RAN."

**If asked "is this OpenAirInterface?"**: No. It's a standards-shaped software RAN and core written in Python: protocol logic without a PHY or RF. The evidence-gathering pattern is the same one you'd point at OAI or a commercial DU through their O1 interfaces.

## 3. Break the timing (4 minutes)

**Click** **Inject Fault**.

**What appears:**
- `PTP-BRIDGE: POST /ptp/inject -> lock_state=FREERUN offset=-50000198 ns`
- `O1: PUT O-DU /o1/config administrativeState=LOCKED`
- `odu: o1_config_applied administrativeState=LOCKED`, from the pod's own log
- PTP panel: offset `-50000198 ns (SPIKE)`, servo `FREERUN (UNLOCKED)`, CloudEvent `FREERUN`
- O-DU panel: cell `UNAVAILABLE`, alarm `CellUnavailable (lossOfRealTimeSynchronization)`, RF carrier `Off (…admin LOCKED)`
- Pill: `FAULT: CELL UNAVAILABLE`

**Talk track:**
> "We made the grandmaster clock jump by 50 milliseconds. A TDD cell needs to stay within about 1.5 microseconds, so this is 30,000 times over budget. The timing plane went to [FREERUN](glossary.md#ptp-lock-state), and the DU's protective action was applied: the cell is locked so it won't transmit out of sync and interfere with neighbors. The alarm you see comes from the DU's own fault management: 3GPP [TS 28.532](glossary.md#ts-28-532-alarm), probable cause loss of real-time synchronization. To a traditional NOC this is just 'cell unavailable'."

**Be precise:**
- This software O-DU has no PTP servo, so the **console** applies the protective lock over the O-DU's standard O1 interface as part of Inject Fault. The O-DU then raises the alarm itself. In other words, the DU-side symptoms follow from the injection by design; only the PTP plane observes the timing fault directly.
- The UE doesn't model radio-link failure timers (N310/T310), so there are no RLF log lines. The fault is visible at the timing and DU layers.

## 4. Agentic root cause analysis (6 minutes)

**Click** **Run 4-Plane Agentic RCA**. It takes a few seconds, or 10 to 40 s with a CPU-served LLM.

**What appears:** the audit-trail panel opens, headed by a Trace ID and Run ID (identifiers of this run; traces live in the orchestrator's memory, not in MLflow).
- **Hierarchical execution spans:**
  1. `O-RAN.O1.FM.Alarm_Ingest`: DU alarms plus PTP state
  2. `3GPP.CAPIF.Security_Authz`: the agent onboards to [CAPIF](glossary.md#capif) and gets a token scoped to `3gpp#mcp-aef:mcp-tools` (first RCA; later runs reuse the token until it expires, `capif.tokenSource: cached`)
  3. `O-RAN.R1.MCP_Tool_Execution`: four tool calls through the [MCP](glossary.md#mcp) gateway
  4. `O-RAN.NonRT_RIC.Deterministic_Router`: the decision
  5. `Enterprise.AI.LLM_Synthesis`: the narrative
  6. `TMForum.TMF688.Audit_Event_Emission`
- **View Raw Response JSON** → `event.evidence`, one entry per plane:

| Plane | Typical evidence | Signal |
|---|---|---|
| cluster (O-Cloud) | `unavailable: ocloud_unavailable …` unless ACM is configured | none |
| ran (O-DU O1) | `cell UNAVAILABLE, RU CONNECTED, admin LOCKED, CellUnavailable/lossOfRealTimeSynchronization` | `du_sync_loss_alarm` |
| platform (PTP) | `ptp4l offset -50000198 ns (limit 100000), DU port UNCALIBRATED` | `ptp_offset_exceeded` |
| hardware (NIC, **emulated**) | `EMULATED ethtool -S: tx_hwtstamp_timeouts=1, delta 74.4 ms` | `nic_firmware_suspect` |

- **Decision:** `faultClass: OC-TimingDegraded`, `corroboration: 2/3`, `decision: HOLD — below the bar, needs human approval`. The router span lists `policy.corroboratingPlanes: [platform, ran]` and `policy.emulatedPlanesNotCounted: [hardware]`.
- **Narrative:** with an LLM, the model's explanation plus its model id and latency. Without one, `[template - LLM unavailable] …` over the same evidence.

**Talk track:**
> "The agent doesn't get free rein. It onboarded through CAPIF, the 3GPP exposure framework, and received a token scoped to exactly the four read-only evidence tools; the MCP gateway checks that scope before routing each call to a plane-specific server. Every plane points at timing: the PTP offset, the DU's loss-of-sync alarm, and the NIC's hardware-timestamp counters. But the NIC here is emulated, and the policy refuses to let synthetic evidence push a decision over the bar. So we have two real corroborating planes against a bar of three: the verdict is HOLD, and a human has to approve any remediation. The routing table is deterministic. The LLM never makes that decision; it only explains evidence that's already in the audit record, and the whole thing is emitted as a TM Forum [TMF688](glossary.md#tmf688-rcaconcludedevent) event."

**Be precise:**
- The **NIC plane is emulated**: its response carries `emulated: true`, it reports the fault only while PTP is actually unlocked, and it **never counts** toward the bar.
- The DU alarm follows from the lock the console applied during Inject (see section 3). The PTP plane is the one that observes the fault directly.
- The **O-Cloud plane** is real only with ACM. Without it, it says it's unavailable, and it doesn't count toward the decision.
- **CAPIF in this lab issues unsigned tokens.** The exchange and scoping are real, but the security boundary is the namespace NetworkPolicy, not the token.
- The verdicts are `APPLY (auto-eligible)`, `HOLD — below the bar, needs human approval` and `NO ACTION — no fault signals`. **Nothing is executed automatically, and the lab has no approval workflow.** Remediation in this demo is the Heal button.
- An LLM narrative may say remediation was "triggered" or "approved". It's paraphrasing the verdict; the audit record is the source of truth.

## 5. Heal and re-check (2 minutes)

**Click** **Heal Timing**.

**What appears:** `PTP-BRIDGE: … lock_state=LOCKED offset=0 ns`, `O1: … administrativeState=UNLOCKED`. Cell `ACTIVE`, alarm `None`, pill `RAN RUNNING & LOCKED`.

**Click** **Run 4-Plane Agentic RCA** again. `faultClass` is `null`, `corroboration 0/3`, `NO ACTION — no fault signals`. The same agent, fed healthy evidence, finds nothing. This control case shows the diagnosis tracks the state of the planes rather than being fixed.

See a [real example run](examples/heal-recheck-run.md) of inject, RCA, heal and re-check, with the log lines, spans and TMF688 event you should see.

## 6. Stop (1 minute)

**Click** **Stop RAN Slice**. `K8S: PATCH … replicas=0`, then `slice pod terminated; its CPU and memory are released`. The pod disappears from the OpenShift tab, the UE and cell panels go idle, and the pill reads `RAN STOPPED (IDLE)`.

**Close:**
> "One click brought up a RAN and core, a UE proved the user plane, a timing fault took the cell down, and a governed agent gathered multivendor evidence, decided by policy (and held back, because not enough of that evidence was real), explained with an LLM, and left an audit trail. Then we gave the resources back."

## OpenShift verification and validation steps: (if someone asks "is that real?")

Run these while the RAN slice is running (after Stop there's no pod to read logs from).

```bash
oc project multivendor-rca                                    # or the project you deployed into
oc get pods -l app=ran-slice -o wide                          # the pod the Start button created
oc logs deploy/ran-slice | grep -E 'registration_accepted|session_created|o1_config_applied'
oc exec deploy/nep-orchestrator -- python3 -c "import urllib.request,json; \
  print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:7095/nep/audit/latest'))['tmf688Event'], indent=1))"
oc get deploy ran-slice -o jsonpath='{.spec.replicas}'          # 1 while running, 0 after Stop
```

See [example output](examples/openshift-verification-output.md) of these commands from a real run.

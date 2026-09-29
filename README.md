# Duranta: the full OAI environment on OpenShift

This branch is **separate from `main`** and shares no files or history with it. `main` holds the lightweight demo: a clean-room Python 5G core and O-RAN RAN in one pod. This branch is for anyone who asks, "How would I run a full open-source RAN and core on OpenShift?"

> **Status: not working yet.** This README records what's known. The deployment below does not currently run on the reference lab; see [Known blockers](#known-blockers).

## What the full environment is

| Part | Project | Images seen in the lab |
|---|---|---|
| RAN: CU-CP, CU-UP, DU | [Duranta](https://lfnetworking.org/projects/duranta/) (the OpenAirInterface RAN, under LF Networking) | `oaisoftwarealliance/oai-gnb:2025.w50`, `oai-nr-cuup:2025.w50` |
| UE | Duranta nrUE with the RF simulator (no radio hardware) | `oaisoftwarealliance/oai-nr-ue:2025.w50` |
| 5G core: AMF, AUSF, NRF, SMF, UDM, UDR, UPF | [OAI CN5G](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed) (a separate OAI project; Duranta does not ship a core) | `oaisoftwarealliance/oai-*:v2.2.0` |
| Subscriber database | MySQL | `mysql:9.0.1` |
| Traffic generator | OAI CN5G test tool | `oaisoftwarealliance/trf-gen-cn5g` |

## Upstream guidance

- Duranta SA tutorial with OAI CN5G (Docker Compose, no Kubernetes): [NR_SA_Tutorial_OAI_CN5G.md](https://github.com/duranta-project/openairinterface5g/blob/develop/doc/NR_SA_Tutorial_OAI_CN5G.md)
- Duranta RF simulator from images: [5g_rfsimulator/README.md](https://github.com/duranta-project/openairinterface5g/tree/develop/ci-scripts/yaml_files/5g_rfsimulator)
- OAI CN5G Helm charts: [charts/oai-5g-core](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed/-/tree/master/charts/oai-5g-core)
- OAI CN5G on OpenShift: [oai-cn5g-fed/openshift](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed/-/tree/master/openshift)
- OAI Kubernetes operators: [openairinterface/oai-operators](https://github.com/OPENAIRINTERFACE/oai-operators)

## What has been proven

On plain Kubernetes (not OpenShift), in the author's home lab, July 2026: UE registration, a PDU session, user-plane ping with 0% loss, and about 27 Mbit/s iperf3 to the data network, with a single-block gNB and the RF simulator.

Lessons from that run:
- **Macvlan bridges need carrier.** Multus macvlan interfaces on a standalone Linux bridge with no attached port stay down, which silently breaks N2, N3 and N6. Attach a dummy port to each bridge.
- **Bandwidth.** This OAI build rejected 24 PRB on band n78. 51 PRB (20 MHz at 30 kHz SCS) works; gNB and UE must match.

## Known blockers

On the reference OpenShift 4.22 cluster, all 12 Deployments exist but **no OAI pod can be created**:

- **Security context constraints.** The OAI containers run as root (`runAsUser: 0`) and the service account has no SCC that allows it, so `restricted-v2` rejects them. The UPF, gNB and DU also need network admin and a TUN device. They need `anyuid` at minimum, and likely `privileged` for the UPF, gNB and DU.
- **Secondary networks.** Multus and macvlan attachments for N2/N3/N6 (and F1/E1 for the split RAN) are untested on OpenShift.

## Open design questions

- **RCA integration.** The demo's RAN plane reads its own O-DU's JSON O1 (`/o1/status`, `/o1/alarms`). The OAI DU doesn't expose that, so a Duranta RAN plane needs a new adapter (OAI telnet/O1, logs, or E2 through FlexRIC).
- **Fault injection.** With the RF simulator there's no PTP and no O1 cell lock, so the timing-fault story needs a replacement (for example stop the DU, cut F1 or N3, or degrade the simulated channel).
- **Resources.** OAI needs dedicated CPU; size it before sharing a cluster.

## Plan

1. Make it run on OpenShift: SCCs, then secondary networks; prove registration, PDU session and ping.
2. Put the working manifests on this branch, with install and teardown steps.
3. Optional: an OAI-backed RAN plane so the RCA pattern from `main` runs against the real stack.

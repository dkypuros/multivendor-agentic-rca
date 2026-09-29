# Full OAI 5G on OpenShift (Duranta RAN + OAI CN5G)

A separate branch from `main`. `main` is the lightweight demo. This branch shows how to run a full open-source RAN and 5G core on OpenShift.

## What you deploy

| Part | Project |
|---|---|
| RAN: CU-CP, CU-UP, DU, UE (RF simulator, no radio hardware) | [Duranta](https://lfnetworking.org/projects/duranta/), the OpenAirInterface RAN (LF Networking) |
| 5G core: NRF, UDR, UDM, AUSF, AMF, SMF, UPF, MySQL | [OAI CN5G](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed) |

All of it comes from one official OAI Helm scenario: [`e2e_scenarios/case3`](https://github.com/openairinterface/orchestration/tree/main/e2e_scenarios/case3) (E1/F1 split RAN plus core and a traffic server).

## Requirements

- OpenShift 4.x, cluster-admin
- At least 4 CPU, 16 GiB RAM, 50 GB storage for the network functions
- Helm 3
- Optional: Multus, if you want separate interfaces for N2/N3/N6

## Install

```bash
git clone https://github.com/openairinterface/orchestration.git
cd orchestration/e2e_scenarios/case3

oc new-project oai-tutorial
helm dependency update .
helm install oai . -n oai-tutorial --set global.kubernetesDistribution=Openshift
```

`kubernetesDistribution=Openshift` makes each chart create a Role and RoleBinding that let its service account use the `privileged` SCC. The core and RAN pods run as root and need it.

## Check it

```bash
oc get pods -n oai-tutorial
oc logs -n oai-tutorial -l app.kubernetes.io/name=oai-amf | grep Connected   # gNB connected to the AMF
oc logs -n oai-tutorial -l app.kubernetes.io/name=oai-nr-ue | grep -i "ip"   # UE got an IP
```

## Remove

```bash
helm uninstall oai -n oai-tutorial
oc delete project oai-tutorial
```

## Status

Not yet validated on OpenShift in this repo. The steps follow OAI's official [Helm deployment guide](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed/-/blob/master/docs/DEPLOY_SA5G_HC.md).

## Not covered yet

- Connecting the RCA agent from `main`: the OAI DU doesn't expose the demo's O1 JSON, so it needs its own RAN-plane adapter.
- A fault to inject: the RF simulator has no PTP, so the timing-fault story needs a replacement.

## References

- [OAI orchestration (Helm charts)](https://github.com/openairinterface/orchestration)
- [OAI Helm deployment guide](https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed/-/blob/master/docs/DEPLOY_SA5G_HC.md)
- [Duranta SA tutorial (Docker Compose)](https://github.com/duranta-project/openairinterface5g/blob/develop/doc/NR_SA_Tutorial_OAI_CN5G.md)
- [OAI Kubernetes operators](https://github.com/OPENAIRINTERFACE/oai-operators)

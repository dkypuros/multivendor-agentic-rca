# Deployment guide

This guide installs the whole use case into **one OpenShift project** from a checkout of this repo. It was verified on OpenShift 4.22.1 by following these steps exactly.

- [Prerequisites](#prerequisites)
- [What gets deployed](#what-gets-deployed)
- [Install](#install)
- [Verify](#verify)
- [Optional: connect an LLM](#optional-connect-an-llm)
- [Optional: O-Cloud plane with ACM](#optional-o-cloud-plane-with-acm)
- [Optional: GitOps with Argo CD](#optional-gitops-with-argo-cd)
- [Use a different namespace](#use-a-different-namespace)
- [Update after a code change](#update-after-a-code-change)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)

## Prerequisites

These are assumed to exist before you begin:

| Requirement | Why | Check |
|---|---|---|
| OpenShift **4.x** cluster (verified on 4.22) | Uses `BuildConfig`, `ImageStream` and `Route`. Plain Kubernetes works if you build the image elsewhere and replace the Route with an Ingress. | `oc version` |
| `oc` CLI logged in | All steps use `oc` | `oc whoami` |
| Permission to **create a project** and be its admin | Everything is namespaced: Deployments, a Role and RoleBinding, BuildConfig, Route. **No cluster-admin is needed.** | `oc auth can-i create projectrequests` |
| **Internal image registry** enabled | The build pushes the image to `image-registry.openshift-image-registry.svc:5000/<ns>/mvrca` | `oc get configs.imageregistry.operator.openshift.io cluster -o jsonpath='{.spec.managementState}'` → `Managed` |
| Build pods can reach **registry.access.redhat.com** and **PyPI** | Base image `ubi9/python-312`, plus `pip install pyyaml` | A disconnected cluster needs a mirror. See [Troubleshooting](#troubleshooting). |
| Pods may use **UDP** between pods in the namespace | The UE's user-plane echoes go over UDP 7011 to the `ran-slice` Service | The default OVN-Kubernetes allows this. Check any NetworkPolicy you add. |
| Capacity | Idle: about 10 m CPU and 120 MiB for 9 small pods. **RAN running:** one more pod, about 50 m CPU and 420 MiB used (request 200 m / 512 MiB, limit 2 CPU / 2 GiB) | `oc adm top nodes` |
| Python 3.11+ on your workstation | Only to run the test scripts in `tests/` (stdlib only) | `python3 --version` |

Not required:
- **An LLM.** It's optional. The RCA decision never uses one. See [below](#optional-connect-an-llm).
- **ACM / OCM.** It's optional. See [below](#optional-o-cloud-plane-with-acm).
- **PTP hardware, SR-IOV, a GPU, real radios, OpenAirInterface, or an existing 5G core.** All of the RAN, core and timing planes ship in this repo as software.

## What gets deployed

`oc apply -k deploy/openshift` creates these in the project:

| Deployment | Replicas | Role | Port |
|---|---|---|---|
| `ran-sandbox-controller` | 1 | Web console + API, behind the Route `ran-sandbox` | 7098 |
| `ran-slice` | **0** (the console scales it) | 5G core + O-RAN split RAN in one pod (`launch_slice.py`, profile `ran`) | 7010 TCP (O-DU), 7011 UDP (Uu) |
| `ptp-bridge` | 1 | Timing plane with inject/heal | 7091 |
| `capif` | 1 | CAPIF core function; issues the RCA agent's scoped token | 7027 |
| `mcp-gateway` | 1 | MCP gateway; checks the CAPIF token and routes tools (policy in the `gateway-policy` ConfigMap) | 8800 |
| `mcp-ocloud`, `mcp-ran`, `mcp-intel`, `mcp-redhat` | 1 each | The four evidence planes | 8850–8853 |
| `nep-orchestrator` | 1 | The RCA agent | 7095 |

Supporting objects:
- the `mvrca` ImageStream and BuildConfig (binary, Docker strategy, `Containerfile`)
- a ServiceAccount, Role and RoleBinding, so the console can scale **only** `ran-slice` and read its pods and logs
- the `ran-sandbox` Route (edge TLS, 180 s timeout)

All 10 Deployments run **one image**; each one's `command` picks the component.

## Install

```bash
git clone https://github.com/<you>/multivendor-agentic-rca.git
cd multivendor-agentic-rca

# 1. Project
oc new-project multivendor-rca

# 2. All manifests (Deployments start in ImagePullBackOff until step 3 finishes; that's expected)
oc apply -k deploy/openshift

# 3. Build the image from this checkout (about 2 minutes; uploads the directory as a binary build)
oc start-build mvrca --from-dir=. --follow

# 4. Restart onto the freshly built image
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca

# 5. Wait for readiness (ran-slice stays at 0/0 by design)
oc get deploy
oc get route ran-sandbox -o jsonpath='https://{.spec.host}{"\n"}'
```

Open the URL. The header should read **RAN STOPPED (IDLE)**, and the PTP panel should show `LOCKED`.

## Verify

Automated, through the Route, exactly as the browser does it. This leaves the slice stopped and PTP healed:

```bash
python3 tests/smoke_route.py https://$(oc get route ran-sandbox -o jsonpath='{.spec.host}')
```

Expected output:

```
start:
  PASS  slice pod ready
  PASS  O-DU cell ACTIVE
  PASS  UE registered
  PASS  PDU session + 3/3 GTP-U echoes
inject fault:
  PASS  PTP FREERUN
  PASS  O-DU cell UNAVAILABLE + CellUnavailable alarm
RCA during fault:
        cluster   signals=[]  unavailable: ocloud_unavailable kubectl failed ...
        ran       signals=['du_sync_loss_alarm']  cell UNAVAILABLE, RU CONNECTED, admin LOCKED, CellUnavailable/lossOfRealTimeSynchronization
        platform  signals=['ptp_offset_exceeded']  ptp4l offset -50000198 ns (limit 100000), DU port UNCALIBRATED
        hardware  signals=['nic_firmware_suspect']  EMULATED ethtool -S: tx_hwtstamp_timeouts=1, delta 74.4 ms
  PASS  diagnosis OC-TimingDegraded  (OC-TimingDegraded 3/3 APPLY (auto))
...
ALL GREEN
```

Manual checks with `oc`, while the RAN is running:

```bash
oc get pods -l app=ran-slice                    # appears after Start, disappears after Stop
oc logs deploy/ran-slice | grep -E 'READY|registration_accepted|session_created'
oc exec deploy/ran-sandbox-controller -- python3 -c \
  "import urllib.request;print(urllib.request.urlopen('http://ran-slice:7010/o1/status').read())"
```

## Optional: connect an LLM

The NEP orchestrator can ask any **OpenAI-compatible** `/v1/chat/completions` endpoint to narrate the evidence. Examples are vLLM, LiteLLM, OpenShift AI model serving, or OVMS with an OpenAI front end. The LLM only writes the narrative. The diagnosis and decision come from a deterministic routing table.

```bash
oc create secret generic llm-endpoint \
  --from-literal=AI_GATEWAY_URL=http://llama31-8b-w8a8-predictor.my-models.svc.cluster.local:8080 \
  --from-literal=LLM_MODEL=llama31-8b-w8a8 \
  --from-literal=LLM_API_KEY=none
oc rollout restart deployment/nep-orchestrator
```

- `AI_GATEWAY_URL` is the base URL, without `/v1`. `LLM_MODEL` must match an id from `GET <url>/v1/models`.
- Optional keys in the same Secret: `LLM_TIMEOUT` (seconds, default 90) and `LLM_MAX_TOKENS` (default 200). Small CPU-served models run at about 5 tokens/s, so keep the timeout generous.
- Check the result: the RCA panel's model label shows your model id and latency. With no LLM, or a failing one, it shows `none (deterministic template)`, and the raw JSON carries `llm_error` with the reason.
- The Route timeout is 180 s. Raise `haproxy.router.openshift.io/timeout` in `deploy/openshift/ran-sandbox.yaml` if your model is slower.

## Optional: O-Cloud plane with ACM

The `mcp-ocloud` server reads OCM/ACM `ManagedCluster` health with `kubectl`. The image ships without `kubectl` and without a kubeconfig. So by default this plane answers `unavailable`, and the RCA says so rather than inventing evidence.

To enable it:
1. Add `kubectl` to the image (extend the `Containerfile`).
2. Mount a kubeconfig for an ACM hub as a Secret into `mcp-ocloud`.
3. Set `SHELDON_KUBECONFIG` to that path.

Available ManagedClusters then contribute `managedcluster_unavailable` / `managedcluster_clock_unsynced` signals.

## Optional: GitOps with Argo CD

`deploy/argocd/application.yaml` is a ready-made Application for `deploy/openshift`. Edit `repoURL` (and its namespace, if you're not on OpenShift GitOps), give Argo CD read access to your repo, then apply it.

It sets `ignoreDifferences` on `ran-slice` `/spec/replicas` with `RespectIgnoreDifferences=true`. **Keep that**: otherwise self-heal immediately undoes the console's Start/Stop.

Argo CD deploys the manifests. You still build the image with `oc start-build mvrca --from-dir=.`, or point a CI pipeline at the `Containerfile`.

## Use a different namespace

Change **both** places in `deploy/openshift/kustomization.yaml`:
- `namespace:`
- the namespace segment of `images[0].newName`, `image-registry.openshift-image-registry.svc:5000/<namespace>/mvrca`

Services address each other by short name, so nothing else changes.

## Update after a code change

```bash
oc start-build mvrca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca
```

The Deployments use the `latest` tag with `imagePullPolicy: Always`, so a restart picks up the new build. `ran-slice` pulls it the next time you press **Start**.

## Uninstall

```bash
oc delete project multivendor-rca
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pods in `ImagePullBackOff` right after `oc apply -k` | Expected until the first build finishes. Run steps 3 and 4. If it persists, check that `images.newName` in `kustomization.yaml` names **your** namespace. |
| Build fails pulling `ubi9/python-312` or on `pip install` | The build has no egress. Mirror the base image, then set `FROM` in `Containerfile`. For PyYAML, either vendor it or use an internal PyPI (`PIP_INDEX_URL` as a build env). |
| Start stays at `STARTING…`, log shows `O-DU not answering yet` | `oc describe pod -l app=ran-slice`. Usually scheduling (the pod requests 512 MiB) or an image pull. `oc logs deploy/ran-slice` shows each NF reaching `READY`. |
| Start fails with a K8S `403` in the log | The Role or RoleBinding is missing, or someone renamed `ran-slice`. The Role only allows scaling the Deployment named `ran-slice`. |
| UE registers but echo shows `0/3` | UDP to `ran-slice:7011` is blocked. Check NetworkPolicies, and check that the Service still lists the `uu` UDP port. |
| RCA shows the CAPIF span `ERROR` and every plane `no CAPIF token` | `oc logs deploy/capif`, and check that `CAPIF_URL` on `nep-orchestrator` is `http://capif:7027`. |
| RCA planes all `unavailable: … 401/403` | Gateway policy mismatch. The ConfigMap `gateway-policy` must keep `capifIssuer: capif-core` and the `mcp-tools` scope. |
| RCA request ends with a gateway timeout in the browser | LLM slower than the Route timeout. Raise the Route annotation or lower `LLM_MAX_TOKENS`. |
| The RAN plane says `O-DU O1 unreachable (is the RAN slice running?)` | The RCA ran while the slice was stopped. This is correct behavior. Press Start first. |

# Deployment guide

This guide installs the whole use case into **one OpenShift project** from a checkout of this repo. It was verified on OpenShift 4.22.1 by following these steps exactly, with `tests/smoke_route.py` passing at the end.

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
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

## Prerequisites

These are assumed to exist before you begin:

| Requirement | Why | Check |
|---|---|---|
| OpenShift **4.x** cluster (verified on 4.22) | Uses `BuildConfig`, `ImageStream` and `Route`. Plain Kubernetes works if you build the image elsewhere and replace the Route with an Ingress. | `oc version` |
| `oc` CLI logged in | All steps use `oc` | `oc whoami` |
| Permission to **create a project** and be its admin | Everything is namespaced: Deployments, a Role and RoleBinding, NetworkPolicies, BuildConfig, Route. **No cluster-admin is needed.** | `oc auth can-i create projectrequests` |
| **Internal image registry** enabled | The build pushes the image to `image-registry.openshift-image-registry.svc:5000/<ns>/mvrca` | `oc get configs.imageregistry.operator.openshift.io cluster -o jsonpath='{.spec.managementState}'` → `Managed` |
| Build pods can reach **registry.access.redhat.com** and **PyPI** | Base image `ubi9/python-312`, plus `pip install pyyaml` | A disconnected cluster needs a mirror. See [Troubleshooting](#troubleshooting). |
| Pods may use **UDP** between pods in the namespace | The UE's user-plane echoes go over UDP 7011 to the `ran-slice` Service | OVN-Kubernetes allows this; the shipped NetworkPolicy allows all traffic *within* the namespace |
| The OpenShift router's namespace carries `policy-group.network.openshift.io/ingress` | The shipped NetworkPolicy admits the Route's traffic by that label (standard on OpenShift 4.x) | `oc get ns openshift-ingress --show-labels` |
| Capacity | **Scheduling requests** while idle: 140 m CPU and 448 MiB across 9 pods. **RAN running** adds one pod requesting 200 m / 512 MiB (limit 2 CPU / 2 GiB). Measured usage on the test cluster: about 1 m / 13 MiB per idle pod, and about 50 m / 420 MiB for the running slice. | `oc adm top pods` |
| A browser with internet access | The console page loads Tailwind CSS from `cdn.tailwindcss.com`. Without it, the page works but is unstyled. | — |
| Python 3.11+ on your workstation | Only for `tests/`. `smoke_route.py` is stdlib-only; `e2e_local.py` also needs PyYAML. | `python3 --version` |

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
- a ServiceAccount, Role and RoleBinding for the console. It may get and scale **only** the `ran-slice` Deployment, list pods and read pod logs in the namespace, and nothing else. No other pod mounts a ServiceAccount token.
- two NetworkPolicies: pods may only be reached from inside the namespace, and the console also from the OpenShift router
- the `ran-sandbox` Route (edge TLS, 180 s timeout)

Every container runs as non-root with no privilege escalation, all capabilities dropped and the `RuntimeDefault` seccomp profile. That fits OpenShift's `restricted-v2` SCC.

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

Expected output (abridged; details in parentheses omitted):

```
status:
  PASS  sandbox answers; PTP bridge reachable
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
        ran       signals=['du_sync_loss_alarm']  cell UNAVAILABLE, RU CONNECTED, admin LOCKED, CellUnavailable/lossOfRealTimeSynchronizatio
        platform  signals=['ptp_offset_exceeded']  ptp4l offset -50000198 ns (limit 100000), DU port UNCALIBRATED
        hardware  signals=['nic_firmware_suspect']  EMULATED ethtool -S: tx_hwtstamp_timeouts=1, delta 74.4 ms
  PASS  diagnosis OC-TimingDegraded; emulated NIC not counted -> HOLD  (OC-TimingDegraded 2/3 HOLD — below the bar, needs human approval)
  PASS  narrative present
heal:
  PASS  PTP LOCKED, cell ACTIVE, no alarm
  PASS  RCA after heal finds no fault
stop:
  PASS  slice stopped
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
- Check the result: the RCA panel's model label shows your model id and latency. With no LLM, or a failing one, the narrative starts with `[template - LLM unavailable]`, and the span `Enterprise.AI.LLM_Synthesis` has status `FALLBACK`. The reason is in `llmSynthesis.llm_error`: `oc exec deploy/nep-orchestrator -- python3 -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:7095/nep/audit/latest'))['llmSynthesis'])"`.
- **Timeouts, innermost first:** `LLM_TIMEOUT` (NEP → LLM, default 90 s), then `RCA_TIMEOUT` on `ran-sandbox-controller` (console → NEP, default 150 s), then the Route (browser → console, 180 s). Keep them in that order if you raise any of them.

## Optional: O-Cloud plane with ACM

The `mcp-ocloud` server reads OCM/ACM `ManagedCluster` health with `kubectl`. The image ships without `kubectl` and without a kubeconfig. So by default this plane answers `unavailable`, and the RCA says so rather than inventing evidence.

To enable it:
1. Add `kubectl` to the image (extend the `Containerfile`).
2. Mount a kubeconfig for an ACM hub as a Secret into `mcp-ocloud`.
3. Set `SHELDON_KUBECONFIG` to that path.

Every visible ManagedCluster then testifies. One that is not `Available` raises `managedcluster_unavailable`, and one whose clock is not synced raises `managedcluster_clock_unsynced`. Both rank **above** timing in the route table (`OC-ClusterUnavailable`, `OC-ClockDrift`), so an unhealthy hub can change the diagnosis.

The gateway policy only allows `ocloud_cluster_health` from this plane. The O-Cloud *action* tools (power, reprovision) stay out of scope.

## Optional: GitOps with Argo CD

`deploy/argocd/application.yaml` is a ready-made Application for `deploy/openshift`. Edit `repoURL` (and its namespace, if you're not on OpenShift GitOps), give Argo CD read access to your repo, then apply it.

The default OpenShift GitOps instance only manages namespaces labelled `argocd.argoproj.io/managed-by: openshift-gitops`. The Application sets that label on the namespace it creates (`managedNamespaceMetadata`). If the namespace already exists, add the label yourself: `oc label ns multivendor-rca argocd.argoproj.io/managed-by=openshift-gitops`.

It sets `ignoreDifferences` on `ran-slice` `/spec/replicas` with `RespectIgnoreDifferences=true`. **Keep that**: otherwise self-heal immediately undoes the console's Start/Stop.

Argo CD deploys the manifests. You still build the image with `oc start-build mvrca --from-dir=.`, or point a CI pipeline at the `Containerfile`.

## Use a different namespace

Change **both** places in `deploy/openshift/kustomization.yaml`:
- `namespace:`
- the namespace segment of `images[0].newName`, `image-registry.openshift-image-registry.svc:5000/<namespace>/mvrca`

Also use the new name in `oc new-project`, `oc delete project` and, if you use it, `deploy/argocd/application.yaml` (`destination.namespace`). Services address each other by short name, so nothing else changes.

## Update after a code change

```bash
oc start-build mvrca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca
```

The Deployments use the `latest` tag with `imagePullPolicy: Always`, so a restart picks up the new build. `ran-slice` pulls it the next time you press **Start**.

After editing `deploy/openshift/gateway-policy.yaml`, re-apply and run `oc rollout restart deployment/mcp-gateway`. The gateway only reads its policy at startup.

## Uninstall

```bash
oc delete project multivendor-rca
```

## Security notes

This is a lab deployment. Know these before you share the URL:

- **The console has no login.** Anyone who can reach the Route can start or stop the RAN slice (up to 2 CPU / 2 GiB), inject or heal the timing fault, and trigger RCAs, which call your LLM if one is configured. To restrict it:
  - put an OAuth proxy in front, or
  - limit source addresses with `oc annotate route ran-sandbox haproxy.router.openshift.io/ip_whitelist="<cidr> <cidr>"`.
- **CAPIF tokens are unsigned lab JWTs** (`alg: none`), and the MCP gateway checks their issuer, expiry and scope but not a signature. The TS 29.222 exchange and scope-based routing are real, but they don't stop a forged token. **The boundary is the NetworkPolicy** (`deploy/openshift/networkpolicy.yaml`): only pods in this namespace can reach the PTP bridge, the O-DU's O1 config, CAPIF, the gateway, the MCP servers and the orchestrator. Anything that can run a pod in this namespace is trusted.
- The UE key in `src/services/core/udm/subscribers.json` (and the console's `UE_K` default) is the public **3GPP TS 35.208 Test Set 1** key, meant for exactly this kind of lab. No real subscriber data ships.
- Only the console mounts a ServiceAccount token, and its Role is limited to the calls listed in [What gets deployed](#what-gets-deployed).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pods in `ImagePullBackOff` right after `oc apply -k` | Expected until the first build finishes. Run steps 3 and 4. If it persists, check that `images.newName` in `kustomization.yaml` names **your** namespace. |
| Build fails pulling `ubi9/python-312` or on `pip install` | The build has no egress. Mirror the base image, then set `FROM` in `Containerfile`. For PyYAML, either vendor it or use an internal PyPI (`PIP_INDEX_URL` as a build env). |
| Start stays at `STARTING...`, log shows `O-DU not answering yet` | `oc describe pod -l app=ran-slice`. Usually scheduling (the pod requests 512 MiB) or an image pull. `oc logs deploy/ran-slice` shows each NF reaching `READY`. |
| The log shows `SANDBOX: STARTING failed: HTTP Error 403: Forbidden` | The Role or RoleBinding is missing, or someone renamed `ran-slice`. The Role only covers the Deployment named `ran-slice`. |
| The Route returns *Application is not available* while the console pod is Ready | The router namespace lacks the `policy-group.network.openshift.io/ingress` label that `networkpolicy.yaml` admits. Check `oc get ns openshift-ingress --show-labels`, and adjust the `router-to-console` policy to your router's namespace labels. |
| UE registers but echo shows `0/3` | UDP to `ran-slice:7011` is blocked. Check NetworkPolicies, and check that the Service still lists the `uu` UDP port. |
| RCA shows the CAPIF span `ERROR` and every plane `no CAPIF token` | `oc logs deploy/capif`, and check that `CAPIF_URL` on `nep-orchestrator` is `http://capif:7027`. |
| RCA planes all `unavailable: unauthorized: …` or `… not in invoker … scope` | Gateway policy mismatch. The ConfigMap `gateway-policy` must keep `capifIssuer: capif-core` and the `mcp-tools` scope listing the four tools. Restart `mcp-gateway` after changing it. |
| RCA panel shows `RCA FAILED … timed out` | The LLM is slower than `RCA_TIMEOUT` (150 s). Lower `LLM_MAX_TOKENS` or `LLM_TIMEOUT`, or raise `RCA_TIMEOUT` together with the Route timeout (see [LLM](#optional-connect-an-llm)). |
| The RAN plane says `O-DU O1 unreachable (is the RAN slice running?)` | The RCA ran while the slice was stopped. This is correct behavior. Press Start first. |

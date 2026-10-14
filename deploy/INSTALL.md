# Install

`oc` logged in. Work from the **repo root**. GitOps only — do not `oc patch`.

Argo CD syncs **Gitea**, not GitHub. A GitHub clone does not fill Gitea. After any later change, push `main` to Gitea again.

llm-d is **CPU or GPU**, not both. Default in `deploy/overlays/multivendor/kustomization.yaml` is `llama-31-8b-llmisvc-cpu.yaml`. For NVIDIA GPU, comment that line, uncomment `llama-31-8b-llmisvc-gpu.yaml`, commit, and push Gitea.

## 1. Install Gitea

```bash
oc apply -k deploy/gitea
```

Wait until it is up:

```bash
oc -n gitea rollout status deploy/gitea
oc -n gitea wait --for=condition=complete job/gitea-bootstrap --timeout=10m
```

## 2. Read Gitea connection details from the cluster

Hostname:

```bash
GITEA_HOST=$(oc get route gitea -n gitea -o jsonpath='{.spec.host}')
```

Username and password (Secret `gitea-admin-credentials` — do not hardcode):

```bash
GITEA_USER=$(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.username}' | base64 -d)
GITEA_PASS=$(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.password}' | base64 -d)
```

Encode the password for the git URL (`!` becomes `%21`; an unencoded `!` fails in zsh):

```bash
GITEA_PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$GITEA_PASS")
```

## 3. Push this repo into Gitea

Uses the variables from step 2.

```bash
GIT_SSL_NO_VERIFY=true git push \
  "https://${GITEA_USER}:${GITEA_PASS_ENC}@${GITEA_HOST}/${GITEA_USER}/multivendor-agentic-rca.git" \
  HEAD:main
```

## 4. Install Argo CD

```bash
oc apply -k deploy/argocd
```

That apply also binds `cluster-admin` to the GitOps application controller so it can create cluster-scoped CRs (`DataScienceCluster`, `DSCInitialization`). Re-run the same command if you installed Argo CD before this binding existed.

Wait for the bootstrap Job:

```bash
oc -n openshift-gitops wait --for=condition=complete job/argocd-bootstrap-application-v1 --timeout=30m
```

That Job only **creates** the Applications. It does **not** mean the cluster is ready. Argo still has to pull Gitea and sync. While the repo-server is coming up you may see `ComparisonError` / `connection refused` on port `8081`. That is temporary — wait and refresh.

Keep watching until `mvrca-sandbox` is **Synced** (OpenShift AI continues in the background and can take 15–20 minutes):

```bash
oc get applications.argoproj.io -n openshift-gitops
```

You should see `mvrca-platform`, `mvrca-operators`, `mvrca-connectivity-link`, and `mvrca-sandbox`. Do not go to step 5 until `mvrca-sandbox` is Synced — that is when the `mvrca` BuildConfig exists.

## 5. Build the demo image

Do this after `mvrca-sandbox` is Synced (that creates the BuildConfig).

```bash
oc start-build mvrca -n multivendor-rca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca -n multivendor-rca
```

## 6. Open the UIs (this cluster)

Demo, Gitea, and GitOps are ready after step 5:

```bash
oc get route ran-sandbox -n multivendor-rca -o jsonpath='https://{.spec.host}{"\n"}'
oc get route gitea -n gitea -o jsonpath='https://{.spec.host}{"\n"}'
oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='https://{.spec.host}{"\n"}'
```

OpenShift AI is slower. There is no `rhods-dashboard` Route on RHOAI 3.x. Wait until `mvrca-operators` has created `default-dsc` and the DSC is Ready, then use the data-science gateway:

```bash
oc get applications.argoproj.io mvrca-operators -n openshift-gitops
oc wait --for=jsonpath='{.status.phase}'=Ready dsc/default-dsc --timeout=20m
oc get route data-science-gateway -n openshift-ingress -o jsonpath='https://{.spec.host}{"\n"}'
```

If `dsc/default-dsc` is NotFound, `mvrca-operators` is still installing — wait and retry the `oc wait`. The gateway Route can exist before the dashboard UI is Ready; wait for DSC Ready before opening it.

- **Demo** — no login.
- **Gitea** — Sign In with the username and password from step 2.
- **Argo CD** — Log in with OpenShift. Apps should be Synced / Healthy.
- **RHOAI** — Log in with OpenShift. Model `llama-31-8b` becomes Ready after llm-d starts.

`llama-31-8b-kserve-*` in `Init:0/1` is downloading `RedHatAI/Llama-3.1-8B-Instruct` (several minutes). Permission-denied messages on `/mnt/tmp_*` are harmless. After Init it pulls the vLLM CPU image (~5 GiB), then loads weights. `0/1 Running` with `/health` connection refused is normal until vLLM listens. CPU uses `--enforce-eager` so compile does not overrun the 10-minute startup probe. First Ready is often 8–12 minutes:

```bash
oc get llminferenceservice llama-31-8b -n multivendor-rca
oc get pods -n multivendor-rca | grep llama-31-8b
```

## 7. Check

```bash
oc get pods -n multivendor-rca
oc get llminferenceservice -n multivendor-rca
python3 tests/smoke_route.py "https://$(oc get route ran-sandbox -n multivendor-rca -o jsonpath='{.spec.host}')"
```

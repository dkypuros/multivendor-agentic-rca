# Install

`oc` logged in. Work from the **repo root**. GitOps only — do not `oc patch`.

Argo CD syncs **Gitea**, not GitHub. A GitHub clone does not fill Gitea.

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

## 3. Push this repo into Gitea

Uses the variables from step 2. If the password has special characters (`!`), URL-encode it first (`!` → `%21`).

```bash
GIT_SSL_NO_VERIFY=true git push \
  "https://${GITEA_USER}:${GITEA_PASS}@${GITEA_HOST}/${GITEA_USER}/multivendor-agentic-rca.git" \
  HEAD:main
```

## 4. Install Argo CD

```bash
oc apply -k deploy/argocd
```

Wait for the bootstrap Job:

```bash
oc -n openshift-gitops wait --for=condition=complete job/argocd-bootstrap-application-v1 --timeout=30m
```

That Job only **creates** the Applications. It does **not** mean the cluster is ready. Argo still has to pull Gitea and sync. While the repo-server is coming up you may see `ComparisonError` / `connection refused` on port `8081`. That is temporary — wait and refresh.

Keep watching until `mvrca-sandbox` is **Synced** (OpenShift AI can take 15–20 minutes):

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

OpenShift AI is slower. There is no `rhods-dashboard` Route on RHOAI 3.x. Wait until `default-dsc` is Ready, then use the data-science gateway:

```bash
oc wait --for=jsonpath='{.status.phase}'=Ready dsc/default-dsc --timeout=20m
oc get route data-science-gateway -n openshift-ingress -o jsonpath='https://{.spec.host}{"\n"}'
```

- **Demo** — no login.
- **Gitea** — Sign In with the username and password from step 2.
- **Argo CD** — Log in with OpenShift. Apps should be Synced / Healthy.
- **RHOAI** — Log in with OpenShift. Models: `llama-31-8b` should be Ready.

## 7. Check

```bash
oc get pods -n multivendor-rca
oc get llminferenceservice -n multivendor-rca
python3 tests/smoke_route.py "https://$(oc get route ran-sandbox -n multivendor-rca -o jsonpath='{.spec.host}')"
```

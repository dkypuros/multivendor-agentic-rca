# Install

`oc` logged in. Run everything from the **repo root**. Do not `oc patch`.

On a new cluster, `oc apply -k deploy` will fail: the llm-d CRD does not exist until OpenShift AI is installed. Use GitOps below (Argo waits for CRDs).

## 1. Gitea

```bash
oc apply -k deploy/gitea
oc -n gitea rollout status deploy/gitea
oc -n gitea wait --for=condition=complete job/gitea-bootstrap --timeout=10m
```

## 2. Push this checkout into Gitea

Argo CD syncs Gitea, not GitHub. A clone of GitHub does not fill Gitea.

```bash
GITEA=$(oc get route gitea -n gitea -o jsonpath='{.spec.host}')
GIT_SSL_NO_VERIFY=true git push \
  "https://gitadmin:GiteaAdmin123%21@${GITEA}/gitadmin/multivendor-agentic-rca.git" \
  HEAD:main
```

## 3. Argo CD

```bash
oc apply -k deploy/argocd
oc -n openshift-gitops wait --for=condition=complete job/argocd-bootstrap-application-v1 --timeout=30m
```

Wait until applications exist and go Synced (OpenShift AI can take 15–20 minutes):

```bash
oc get applications.argoproj.io -n openshift-gitops
```

Expect `mvrca-platform`, `mvrca-operators`, `mvrca-connectivity-link`, `mvrca-sandbox`.

## 4. Build the sandbox image

The BuildConfig appears after `mvrca-sandbox` syncs.

```bash
oc start-build mvrca -n multivendor-rca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca -n multivendor-rca
```

LLM: overlay `deploy/overlays/multivendor` is llm-d (default). To use Ollama instead, point `mvrca-sandbox` at `deploy/overlays/multivendor-ollama` and push Gitea `main`.

## 5. Links (this cluster)

```bash
echo "Demo:  https://$(oc get route ran-sandbox -n multivendor-rca -o jsonpath='{.spec.host}')"
echo "Gitea: https://$(oc get route gitea -n gitea -o jsonpath='{.spec.host}')"
echo "Argo:  https://$(oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='{.spec.host}')"
echo "RHOAI: https://$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='{.spec.host}')"
```

**Demo** — open the Demo URL. No login.

**Gitea** — open the Gitea URL → Sign In:

```bash
echo "user: $(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.username}' | base64 -d)"
echo "pass: $(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.password}' | base64 -d)"
```

Repo `gitadmin/multivendor-agentic-rca` on `main` should show your push.

**Argo CD** — open the Argo URL → **Log in with OpenShift** (same identity as `oc whoami`). Apps should be Synced / Healthy.

## 6. Validate

```bash
oc get applications.argoproj.io -n openshift-gitops
oc get pods -n multivendor-rca
oc get llminferenceservice -n multivendor-rca   # READY True
oc get route ran-sandbox -n multivendor-rca

python3 tests/smoke_route.py "https://$(oc get route ran-sandbox -n multivendor-rca -o jsonpath='{.spec.host}')"
```

## Without GitOps

Only after OpenShift AI has created the llm-d CRD:

```bash
oc apply -k deploy/overlays/operators
oc apply -k deploy/overlays/connectivity-link
until oc get crd llminferenceservices.serving.kserve.io >/dev/null 2>&1; do sleep 20; done
oc wait --for=condition=Established crd/llminferenceservices.serving.kserve.io --timeout=30m
oc apply -k deploy/overlays/multivendor
oc start-build mvrca -n multivendor-rca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca -n multivendor-rca
```

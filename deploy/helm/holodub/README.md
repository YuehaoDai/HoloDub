# holodub Helm chart (alpha)

A minimal chart that mirrors the Docker Compose topology in Kubernetes:

- `holodub-api` (Deployment + Service) — public HTTP API + UI
- `holodub-worker` (Deployment) — pipeline worker
- `holodub-ml-service` (Deployment + Service) — GPU inference
- Optional `postgres` (StatefulSet) and `redis` (Deployment)
- One shared `PersistentVolumeClaim` for `/data`

## Status

**Alpha** — the manifests below are not committed yet because the chart
needs cluster-specific tuning (storage class, GPU runtime class, ingress
controller, etc.). Treat this as a starting point: copy the values to your
cluster, write `templates/*.yaml` for the four workloads, and iterate.

The intent of shipping `Chart.yaml` and `values.yaml` first is to lock the
public configuration surface so subsequent template PRs are mechanical.

## Bootstrap (planned commands once templates land)

```bash
helm install holodub ./deploy/helm/holodub \
    --namespace holodub --create-namespace \
    --values production.yaml \
    --set-string secrets.apiAuthToken=$(openssl rand -hex 32) \
    --set-string secrets.postgresPassword=$(openssl rand -hex 32) \
    --set-string secrets.openaiApiKey=sk-xxxxxx
```

## GPU notes

`ml-service` requires an NVIDIA GPU. The chart relies on the
[NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin) being
installed; the Pod template requests `nvidia.com/gpu` based on
`ml.gpu.count`. If `ml.gpu.enabled=false` the GPU resource request is
omitted and the deployment will fall back to CPU (functional only when
`ML_TTS_BACKEND=silence`).

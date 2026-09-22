# After Jenkins — Argo CD and Kubernetes Deployment

**After Jenkins updates Git, Argo CD updates the Deployment, and Kubernetes creates the new running application.** This guide explains the process in our local scan-quality project.

## 1. Argo CD detects a difference

Our Argo CD Application tracks:

```text
Repository: mlops_pipeline
Branch: main
Directory: k8s
```

It reads `k8s/application.yaml` and compares the desired configuration with the live Kubernetes resources.

For example, Git now specifies:

```yaml
image: scan-quality:ci-15-7a7ad5288a70
```

If the live Deployment uses an older image, Argo CD reports **OutOfSync**.

## 2. Argo CD updates the Kubernetes API

With Auto-Sync enabled, Argo CD submits the updated resource configuration to the **Kubernetes API server**. Argo CD does not build images or run predictions.

The important change is inside the Deployment's Pod template. This is an excerpt, not the complete manifest:

```yaml
spec:
  template:
    spec:
      containers:
        - name: api
          image: scan-quality:ci-15-7a7ad5288a70
        - name: drift-monitor
          image: scan-quality:ci-15-7a7ad5288a70
```

Changing the Pod template triggers a Deployment rollout.

## 3. Kubernetes creates a new Deployment revision

The Deployment controller creates a new **ReplicaSet** for the changed Pod template.

Our strategy is:

```yaml
replicas: 1
strategy:
  type: Recreate
```

For this rollout, Kubernetes stops the old Pod before starting its replacement. This can briefly interrupt requests.

Older ReplicaSets remain as revision history, normally with zero replicas. That explains the multiple ReplicaSet boxes in the Argo CD resource tree: they do not mean all previous application versions are still running.

## 4. Kubernetes starts the new Pod

| Component | Responsibility |
|---|---|
| Deployment controller | Manages the rollout and ReplicaSets |
| ReplicaSet controller | Creates the required Pod |
| Scheduler | Selects a node for the Pod |
| Kubelet on that node | Arranges volume mounts and starts containers through the container runtime |
| containerd | Runs the containers from the image |

Our Kind cluster has one node, `mlops-control-plane`.

Jenkins already loaded the tested image into Kind. With this policy:

```yaml
imagePullPolicy: IfNotPresent
```

the node can use its locally stored image. This step does not rebuild the image.

## 5. The containers start their processes

### API container

The API container starts:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

FastAPI's startup logic:

1. Reads `MODEL_RUN_ID` from the container environment.
2. Reads the matching metadata.
3. Loads `model.joblib` into memory.
4. Initializes prediction logging.

The loaded model is reused for prediction requests. It is not downloaded or loaded again for every request.

### Drift-monitor container

The monitor starts:

```bash
python monitoring/drift_monitor.py
```

It loads the reference features, exposes metrics on port `8002`, and periodically reads recent prediction records.

Both containers mount the same persistent volume at `/app/data`; the monitor's mount is read-only. The image contains the model, while the volume holds the SQLite prediction records.

Replacing the Pod preserves records on this volume. Deleting the entire local Kind cluster is a different operation and does not provide the same persistence guarantee.

## 6. Kubernetes checks the containers

| Probe | Our configuration | Purpose |
|---|---|---|
| API startup | HTTP `/health` | Allows time for model loading and startup |
| API readiness | HTTP `/health` | Determines whether the API container is ready |
| API liveness | HTTP `/health` | Restarts the API container after repeated failures |
| Monitor readiness | TCP port `8002` | Checks whether its metrics port accepts connections |

The startup probe gates the API's readiness and liveness probes until startup succeeds. A readiness failure removes readiness; it does not by itself restart a container.

The Pod becomes **Ready when both containers are ready**, producing `2/2` in the READY column.

| READY | Meaning |
|---|---|
| `0/2` | Neither container is ready |
| `1/2` | One container is ready |
| `2/2` | Both containers are ready |

Because both containers share one Pod, a failed monitor readiness check can also make the whole Pod unready for normal Service traffic.

**A ready drift monitor may still be waiting for enough prediction records to calculate drift.** Its TCP readiness check verifies reachability, not sample count or model accuracy.

## 7. The Service routes requests to the ready Pod

Our Service selects Pods using:

```yaml
selector:
  app: scan-quality
```

Its API port mapping is:

```yaml
port: 8001
targetPort: api
nodePort: 30081
```

For traffic using that NodePort, the request reaches the Kind node on port `30081`, then Service routing forwards it to a ready Pod's API port `8001`. The named target port `api` resolves to the container port in the Pod specification.

Kubernetes updates the Service's EndpointSlices as Pod readiness changes, so the Service can route to the replacement Pod even when its IP differs from the old one.

In our monitoring setup, Prometheus reaches `mlops-control-plane:30081` through the shared Docker network. This does not imply that port `30081` is published on the Mac's localhost. We use a port-forward for direct API testing from the Mac.

## 8. Verify configuration, readiness, and predictions separately

| Check | What it proves |
|---|---|
| Argo CD **Synced** | Live configuration matches Git |
| Argo CD **Healthy** | Resources meet Argo CD's health checks |
| Pod **2/2 Running** | Both containers are ready and the Pod is running |
| `/health` model ID | API reports the intended model |
| Successful `/predict` | The application can perform inference |

**Synced alone does not prove predictions work.** Check both Kubernetes status and the actual prediction endpoint.

Check the rollout and Pod:

```bash
kubectl --context kind-mlops -n scan-quality \
  rollout status deployment/scan-quality --timeout=180s

kubectl --context kind-mlops -n scan-quality get pods
```

In a separate terminal, start API access and leave it running:

```bash
kubectl --context kind-mlops -n scan-quality \
  port-forward svc/scan-quality 8003:8001
```

Use an existing working port-forward if port `8003` is already occupied. A port-forward may need restarting when its selected Pod is replaced.

Check the API:

```bash
curl -sS http://127.0.0.1:8003/health

curl -sS -X POST http://127.0.0.1:8003/predict \
  -H "Content-Type: application/json" \
  -d '{"brightness":0.5,"motion":0.1,"face_visibility":0.95,"signal_quality":0.9}'
```

Confirm that `model_version` matches the model run selected for this release. The Docker image tag and model run ID are different identifiers: a new code build may still contain the same model.

## 9. Monitoring continues after deployment

Prometheus collects the API and drift-monitor metrics. Grafana displays them and evaluates the drift alert.

Operational health, successful predictions, and model quality are separate checks. Input drift indicates a distribution change; measuring prediction accuracy requires actual labels.

**Our current Jenkins pipeline finishes after pushing Git. It does not yet automate waiting for this rollout or testing the deployed API.** A successful Jenkins build means the deployment request was pushed, not that all post-deployment checks have passed.

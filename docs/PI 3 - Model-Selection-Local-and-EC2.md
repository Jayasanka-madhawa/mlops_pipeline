# How Jenkins Selects the Model: Local and EC2 Setups

**The model run ID selects a specific release bundle. Jenkins packages that model into a Docker image, and FastAPI loads it when the container starts.**

The local flow below describes our current project. The EC2/S3 flow is a proposed extension, not something we have implemented yet.

## 1. How does Jenkins run the exact model locally?

### Step 1: Jenkins receives the model ID

The release script triggers Jenkins with a parameter such as:

```text
MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

The Jenkinsfile makes this available to its shell commands:

```groovy
env.MODEL_RUN_ID = params.MODEL_RUN_ID
```

### Step 2: Jenkins retrieves that model's bundle

```bash
python scripts/release.py hydrate "$MODEL_RUN_ID"
```

The script reads the matching release folder:

```text
/Users/jayasanka/mlops-releases/d9571388a61f45369e4f709878e8246b/
```

It verifies the bundle's file checksums and copies the model and supporting files into the Jenkins workspace. The model is placed at:

```text
artifacts/d9571388a61f45369e4f709878e8246b/model.joblib
```

The ID identifies the bundle; the checksum checks help detect changed bundle contents. Jenkins does not automatically select the latest MLflow run.

### Step 3: Docker packages that exact folder

Jenkins passes the ID to the Docker build:

```bash
docker build \
  --build-arg MODEL_RUN_ID="$MODEL_RUN_ID" \
  -t "scan-quality:$IMAGE_TAG" .
```

The Dockerfile uses it:

```dockerfile
ARG MODEL_RUN_ID
COPY artifacts/${MODEL_RUN_ID}/ artifacts/${MODEL_RUN_ID}/
ENV MODEL_RUN_ID=${MODEL_RUN_ID}
```

These instructions copy the selected model files into the image and store the model ID in the image's environment.

### Step 4: FastAPI loads the model at startup

Conceptually, startup code does this:

```python
run_id = os.environ["MODEL_RUN_ID"]
model_path = ROOT / "artifacts" / run_id / "model.joblib"
model = joblib.load(model_path)
```

The model stays in memory. Each prediction request uses that loaded model; it does not retrieve the model again for every request.

### Step 5: Verify the selection

The container smoke test checks that the API reports the expected model ID. After deployment, `/health` exposes it as `model_version`.

**Kubernetes runs the Docker image. FastAPI inside the image loads the model selected by `MODEL_RUN_ID`.**

## 2. What changes if the Jenkins agent runs on EC2?

An EC2 agent cannot directly read the folder on your Mac. We can use **S3 as shared storage for release bundles**.

| Current local setup | Proposed EC2 setup |
|---|---|
| Train and evaluate on your Mac | Continue training and evaluating on your Mac |
| Bundle in `/Users/jayasanka/mlops-releases/RUN_ID/` | Bundle in `s3://your-model-bucket/releases/RUN_ID/` |
| Mac agent copies the local bundle | EC2 agent downloads the matching bundle into its workspace |
| Verify bundle checksums | Verify downloaded bundle checksums |
| Docker packages the selected model | Same packaging process |
| Load tested image into Kind | Push tested image to a registry such as Amazon ECR |

The location of the **agent executing the build** matters. Moving only the Jenkins controller to EC2 while keeping the agent on your Mac would still allow that Mac agent to use its local files.

### Proposed release flow

1. Train and evaluate a new model on your Mac.
2. Prepare its release bundle.
3. Upload the complete bundle to S3 under its run ID.
4. After the upload succeeds, trigger Jenkins with that run ID.
5. The EC2 agent downloads the corresponding S3 bundle and verifies it.
6. Jenkins runs API tests and builds a Docker image containing the selected model.
7. Jenkins smoke-tests the image and pushes it to ECR.
8. Jenkins updates Git with the registry image reference.
9. Argo CD applies the change; Kubernetes pulls the image and starts the application.

For example, this parameter:

```text
MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

would select this model object in S3:

```text
s3://your-model-bucket/releases/d9571388a61f45369e4f709878e8246b/model.joblib
```

`your-model-bucket` is a placeholder for a bucket you would create. The remaining release files and checksum manifest belong under the same run-specific prefix.

## 3. What are S3 and ECR used for?

| Component | Stores or runs |
|---|---|
| S3 | Model release bundles used during the build |
| ECR | Tested Docker images containing the application and model |
| EC2 Jenkins agent | Executes download, test, build, and push commands |
| Kubernetes | Runs the released image |

**S3 supplies the model during the build. The deployed API loads the model from inside its Docker image.** It does not need to download the model from S3 for each prediction.

## 4. What would we need to change?

- Add S3 upload support to the release preparation process.
- Add S3 download support to `hydrate`, retaining checksum verification.
- Give the EC2 agent access to the required S3 objects and container registry, typically through an EC2 IAM role.
- Replace the Kind image-loading stage with a registry push.
- Update the deployment script to accept the full registry image reference rather than only `scan-quality:...`.
- Configure the deployment cluster to pull the released image.

The model ID continues to select the bundle in both setups. The storage location and image-distribution method change.

## 5. Model ID versus image tag

| Identifier | Purpose |
|---|---|
| `MODEL_RUN_ID` | Selects the trained model release bundle |
| Docker image tag | Selects an application build containing code, dependencies, and that model |

Two different code builds can contain the same model. A new image tag does not necessarily mean a newly trained model.

**Current limitation:** code-triggered Jenkins builds still use the fixed default model run ID. Passing a newer ID through the release script does not update that default. We still need to fix that selection behavior to prevent a later code-only build from reusing the older model.

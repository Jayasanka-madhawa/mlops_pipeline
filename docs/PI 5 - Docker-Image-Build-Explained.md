# Docker Image Build — Packaging the Release

**This step creates a Docker image containing everything needed to run your API with the selected model.**

Jenkins runs this after the API tests pass:

```bash
docker build \
  --build-arg MODEL_RUN_ID="$MODEL_RUN_ID" \
  -t "scan-quality:$IMAGE_TAG" .
```

| Part | Meaning |
|---|---|
| `docker build` | Build an image using the Dockerfile |
| `--build-arg MODEL_RUN_ID=...` | Pass the selected model ID into the Dockerfile |
| `-t "scan-quality:$IMAGE_TAG"` | Give the image a name and version tag |
| `.` | Use the current Jenkins workspace as the build context—the files Docker can copy, subject to `.dockerignore` |

## 1. Prepare Python and install dependencies

Your Dockerfile starts with:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
```

The base image supplies Python and a minimal Linux environment. `WORKDIR` sets `/app` as the working directory inside the image.

Then:

```dockerfile
COPY requirements-serving.txt .
RUN pip install --no-cache-dir -r requirements-serving.txt
```

This copies the dependency list and installs packages such as FastAPI, scikit-learn, and pandas inside the image. It does not use your Mac's Conda environment at runtime.

## 2. Copy the application code

```dockerfile
COPY app/ app/
COPY monitoring/drift_monitor.py monitoring/drift_monitor.py
```

These instructions copy the API code and drift-monitor script from the Jenkins workspace into the image.

For example, the API code is stored under `/app/app/` inside the image.

## 3. Copy the selected model

Suppose Jenkins received:

```text
MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

The earlier release-bundle stage has already copied that model into the Jenkins workspace. The Dockerfile uses:

```dockerfile
ARG MODEL_RUN_ID
COPY artifacts/${MODEL_RUN_ID}/ artifacts/${MODEL_RUN_ID}/
```

`ARG` declares the build-time variable. `COPY` uses its value to select the matching model folder.

The model file becomes:

```text
/app/artifacts/d9571388a61f45369e4f709878e8246b/model.joblib
```

Then:

```dockerfile
ENV MODEL_RUN_ID=${MODEL_RUN_ID}
```

stores the model ID as a default environment variable in the image. When the container starts, FastAPI reads this variable and loads the corresponding model.

| Instruction | Purpose |
|---|---|
| `ARG MODEL_RUN_ID` | Receives the ID during the build |
| `COPY artifacts/...` | Packages the selected model files |
| `ENV MODEL_RUN_ID=...` | Makes the ID available when the container runs |

Your Dockerfile also includes the reference features:

```dockerfile
COPY data/reference_features.csv reference_features.csv
```

The drift monitor uses this reference dataset to compare against recent inputs.

## 4. Record how to start the API

```dockerfile
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

This defines the default command when a container starts from the image.

**Building the image does not start the API.** The later `docker run` command starts it during the smoke test, and Kubernetes starts it during deployment.

The drift-monitor container uses the same image but overrides the command to run `monitoring/drift_monitor.py`.

## 5. Name and store the finished image

An example image reference is:

```text
scan-quality:ci-13-9c8ad1a7e1c6
```

| Component | Meaning |
|---|---|
| `scan-quality` | Image name |
| `ci-13-9c8ad1a7e1c6` | Image tag |
| `13` | Jenkins build number |
| `9c8ad1a7e1c6` | Short source Git commit |

The image lives in Docker Desktop's image storage on your Mac, not as a normal file in your project directory. This build command does not upload it to a registry.

Your full Jenkins build command also adds:

```bash
--label "org.opencontainers.image.revision=$(git rev-parse HEAD)"
```

This records the source Git commit as image metadata so you can trace which code produced the image.

## 6. Save image references in Jenkins

Jenkins runs:

```bash
docker image inspect "scan-quality:$IMAGE_TAG" \
  --format '{{.Id}}' > image-id.txt

printf '%s\n' "scan-quality:$IMAGE_TAG" > image-tag.txt
```

| Jenkins artifact | Contents |
|---|---|
| `image-tag.txt` | Human-readable image name and tag |
| `image-id.txt` | Docker's content-based image identifier |

Jenkins archives these text files with the build. **They are references to the image, not copies of the Docker image.** An image ID is also distinct from a registry manifest digest used for registry deployments.

## 7. What happens next?

1. Jenkins starts a temporary container from this image.
2. The smoke test checks startup, model identity, predictions, and logging.
3. If the checks pass, Jenkins loads the same image into Kind.
4. Jenkins updates the image references in Git.
5. Argo CD applies the new configuration, and Kubernetes starts the release.

**One image is built, tested, and deployed. The image packages the serving code, selected model, dependencies, and drift-monitor code together.**

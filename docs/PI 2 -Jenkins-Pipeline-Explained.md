# Jenkins Pipeline Explained

Your Jenkinsfile runs **seven stages on your Mac agent**. It turns application code and a selected model into a tested Docker image, then updates Git so Argo CD can deploy it.

## 1. Checkout — get code and select the model

```groovy
deleteDir()
checkout scm
```

Clears the Jenkins job's workspace and downloads the source from GitHub. Your original project directory remains separate.

```groovy
env.MODEL_RUN_ID = params.MODEL_RUN_ID
env.IMAGE_TAG = "ci-${env.BUILD_NUMBER}-${commit}"
```

These variables identify two different things:

| Variable | Meaning | Example |
|---|---|---|
| `MODEL_RUN_ID` | Which trained model to package | `d9571388a61f45369e4f709878e8246b` |
| `IMAGE_TAG` | Which application build this is | `ci-13-9c8ad1a7e1c6` |

The pipeline validates the run ID's format and obtains the short source commit with `git rev-parse --short=12 HEAD`.

A code-triggered build uses the configured default model ID. Your release script can supply another ID.

## 2. Prepare release bundle — get the model files

```bash
python scripts/release.py hydrate "$MODEL_RUN_ID"
```

The script reads the prepared bundle from:

```text
/Users/jayasanka/mlops-releases/MODEL_RUN_ID/
```

It verifies file checksums and copies the model, metadata, and supporting data into the Jenkins workspace.

**Jenkins doesn't train a model here. It uses the one you already trained and evaluated.**

The release manifest is archived with the build for traceability.

## 3. API tests — check application behavior

```bash
python -m pytest tests/test_api.py -q \
  --junitxml=test-results/api-tests.xml
```

Tests run in your `mlops` Conda environment. They check:

- The health endpoint.
- API predictions matching offline model predictions.
- Invalid or missing inputs.
- Prediction behavior when logging fails.

Jenkins reads the XML report and displays the test results. A test failure prevents the image-build stage from running.

## 4. Build image — package the release

```bash
docker build \
  --build-arg MODEL_RUN_ID="$MODEL_RUN_ID" \
  -t "scan-quality:$IMAGE_TAG" .
```

The Dockerfile uses `MODEL_RUN_ID` to copy the correct model into the image. Your full build command also records the Git revision as an image label.

The image contains **application code + model + dependencies + drift-monitor code**.

Jenkins saves its tag and image ID as build artifacts. The actual image stays in Docker storage on your Mac.

## 5. Container smoke test — check the packaged application

```bash
docker run -d --name "$SMOKE_CONTAINER" "scan-quality:$IMAGE_TAG"
```

Starts a temporary container using the newly built image. Jenkins copies the smoke-test script into that container, then runs:

```bash
docker exec "$SMOKE_CONTAINER" \
  python /tmp/container_smoke.py "$MODEL_RUN_ID"
```

Checks that the container:

- Starts the API successfully.
- Loads the intended model.
- Returns a prediction.
- Saves the prediction record.

Afterward, Jenkins collects logs and removes the temporary container, even if the test fails.

**API tests check behavior before packaging. The smoke test checks that the package itself works.**

## 6. Load into Kind — make the image available

```bash
kind load docker-image "scan-quality:$IMAGE_TAG" --name mlops
```

Copies the tested image into your local Kubernetes cluster.

It does not rebuild the image or replace the running application yet. Kubernetes simply has the image available for use.

## 7. Update Git — request deployment

Jenkins retrieves its stored GitHub credentials, then checks whether `main` changed during the build:

```bash
git fetch origin main
```

If the remote commit differs from the tested checkout, the stage stops.

Otherwise:

```bash
python scripts/update_deployment.py "scan-quality:$IMAGE_TAG"
```

updates the image references for both API and drift monitor in `k8s/application.yaml`.

Jenkins stages that file, then commits and pushes:

```bash
git commit -m "Deploy scan-quality:$IMAGE_TAG"
git push origin HEAD:main
```

The GitHub token is supplied through Jenkins credentials, not written in the Jenkinsfile. The push is not forced, so conflicting remote changes can still cause it to fail.

That Git commit is the **deployment instruction for Argo CD**.

## What happens afterward?

Argo CD detects the changed manifest, applies it, and Kubernetes replaces the containers. The API loads the model from the new image.

Your Jenkins job ends after the Git push—it currently **doesn't wait for the Kubernetes rollout**. Check Argo CD for **Synced / Healthy** and verify the deployed API separately.

## Important pipeline controls

| Configuration | Purpose |
|---|---|
| `agent { label 'mac-mlops' }` | Runs commands on your Mac agent |
| `CONDA_BIN` | Points to Conda; commands use `conda run -n mlops` |
| `pollSCM('H/5 * * * *')` | Checks Git approximately every five minutes |
| `disableConcurrentBuilds()` | Prevents overlapping builds of this job |
| Pipeline timeout: 20 minutes | Limits pipeline execution after agent allocation |
| Smoke-test timeout: 3 minutes | Stops a stuck container test |
| Build retention: 10 | Retains recent Jenkins build records; does not clean Docker images |
| Deployment-file polling exclusion | Prevents Jenkins's deployment-only commit from triggering another build; configured in the job UI |

A required stage failure stops later stages. Cleanup in `post { always { ... } }` still runs when that stage has been entered.

## Current model-selection issue

The default model ID is fixed in the Jenkinsfile:

```groovy
defaultValue: 'd9571388a61f45369e4f709878e8246b'
```

After releasing a new model through the release script, a later code-only build could package the old default model. We still need to update how the intended model is recorded and selected.

**Jenkins prepares and tests the release. Git records the deployment choice. Argo CD applies it. Kubernetes runs it.**

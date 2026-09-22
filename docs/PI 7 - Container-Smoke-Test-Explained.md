# Container Smoke Test — Check the Packaged Application

**The container smoke test checks whether the Docker image can actually run your application.** We start a real API server inside a temporary container.

## 1. Start the container

```bash
docker run -d \
  --name "$SMOKE_CONTAINER" \
  "scan-quality:$IMAGE_TAG"
```

| Part | Meaning |
|---|---|
| `-d` | Run in the background |
| `--name` | Give the temporary container a name |
| `scan-quality:$IMAGE_TAG` | Use the image Jenkins just built |

Docker executes the image's default startup command:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

FastAPI starts and loads the packaged model. Starting the container does not guarantee that the API is ready yet; the test waits for its health endpoint.

## 2. Copy the test script inside

```bash
docker cp tests/container_smoke.py \
  "$SMOKE_CONTAINER:/tmp/container_smoke.py"
```

This copies the test script from the Jenkins workspace into the running container. It does not modify the original image.

## 3. Execute the test inside the container

```bash
docker exec "$SMOKE_CONTAINER" \
  python /tmp/container_smoke.py "$MODEL_RUN_ID"
```

`docker exec` starts an additional process inside the container. The API server continues running while the test script sends HTTP requests to:

```text
http://127.0.0.1:8001
```

Here, `127.0.0.1` means **inside the container**. We do not need to publish a port on your Mac.

The model ID passed to the script is the expected ID. The API itself loads the model configured in the image, and the test checks that it reports the expected identity.

## 4. Check the release

| Check | What the script verifies |
|---|---|
| Startup | Waits for `/health` to respond |
| Model identity | Reported model ID matches `$MODEL_RUN_ID` |
| Prediction | `/predict` returns the expected response structure and valid values |
| Logging | The returned prediction ID has a matching record in SQLite |

The container uses its own temporary database because this test does not mount your production data directory.

This is a quick check of essential functionality. It does not replace the full API test suite, model-quality evaluation, or load testing.

## 5. Pass or fail

- If checks pass, the script exits successfully and Jenkins continues.
- If a check fails, the command returns a failure and Jenkins stops later release stages.

The stage's shell uses `set -eu`, so a failed command stops the script. The Jenkins stage also has a three-minute timeout to limit how long it can run.

## 6. Clean up

The cleanup commands are:

```bash
docker logs "$SMOKE_CONTAINER" > smoke-container.log 2>&1
docker rm -f "$SMOKE_CONTAINER"
```

Jenkins saves the logs, then removes the temporary container and its temporary data. **The tested image remains** for loading into Kind.

In the Jenkinsfile, cleanup is inside `post { always { ... } }`, so it is attempted even if the test fails. The cleanup commands use `|| true` so a missing or already-stopped container does not prevent the remaining cleanup actions.

Jenkins archives `smoke-container.log` for troubleshooting.

## 7. How is this different from the earlier API tests?

| Earlier API tests | Container smoke test |
|---|---|
| Uses `TestClient` inside Python | Uses real HTTP requests to Uvicorn |
| Runs in your Conda environment | Runs in the Docker environment |
| Mocks database writes | Checks an actual SQLite write |
| Checks application behavior broadly | Checks the packaged release's essential functions |

For example, API tests might pass locally, but the smoke test can still fail if the Docker image is missing a dependency or model file.

**The same image that passes this smoke test is loaded into Kind and selected for deployment. Jenkins does not build a second deployment image.**

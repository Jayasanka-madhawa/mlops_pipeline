# Application and Model Rollback — Our MLOps Project

**In our setup, rollback means changing Git to reference a previous tested Docker image. Argo CD then deploys that image again.** Because the model is packaged inside the image, this can restore the previous model too.

## 1. How application rollback works

### Select a previous working image

For example:

```text
Current:  scan-quality:ci-15-7a7ad5288a70
Previous: scan-quality:ci-13-9c8ad1a7e1c6
```

These tags illustrate our project's releases. Choose an image you have verified works, rather than assuming every older release is suitable.

The image must still be available inside Kind. If it exists only in Docker Desktop, load it into the cluster before requesting the rollback:

```bash
kind load docker-image scan-quality:ci-13-9c8ad1a7e1c6 --name mlops
```

Do not rebuild an old tag and assume it is the original tested release.

### Update both image references in Git

In `k8s/application.yaml`, change the API and drift-monitor image references to the previous image:

```yaml
image: scan-quality:ci-13-9c8ad1a7e1c6
```

Commit and push only the intended rollback changes. This creates a new Git commit requesting the older release; it does not erase Git history.

Our manifest-only polling exclusion should prevent that deployment-only commit from triggering a new Jenkins image build.

### Argo CD applies the change

With Auto-Sync enabled, Argo CD detects the changed manifest and updates the Kubernetes Deployment.

No model training or Docker rebuild is needed for this approach.

### Kubernetes replaces the Pod

Our Deployment uses `Recreate`. Kubernetes stops the current Pod and starts a replacement using the previous image, so requests can be briefly interrupted.

The API loads the model packaged inside that image. The drift monitor also returns to the code and reference features included in the selected image.

### Verify the result

Confirm:

- Argo CD shows **Synced / Healthy**.
- Both containers reference the intended previous image.
- `/health` reports the expected model run ID.
- `/predict` works and monitoring looks normal.

If both releases contain the same model, `/health` may show the same model ID. Also check the image reference to distinguish application releases.

## 2. What rolls back and what remains?

| Restored from the previous image | Not restored by changing the image |
|---|---|
| API code and dependencies | SQLite prediction history |
| Packaged model and metadata | Prometheus metric history |
| Drift-monitor code | Grafana dashboards |
| Packaged reference features | Other Kubernetes settings that you did not change in Git |

The persistent volume remains attached to the application. Image rollback does not restore a previous database snapshot. If a future release changes the database schema incompatibly, an older image may require additional migration handling.

## 3. How model rollback works

**The model is part of the Docker image, so choosing an older image can restore its model.**

For a hypothetical example:

| Release image | Packaged model |
|---|---|
| Previous image | Model A |
| Current image | Model B |

If Model B performs badly:

1. Select the previous tested image containing Model A.
2. Change both image references in `k8s/application.yaml`.
3. Commit and push.
4. Argo CD applies the change.
5. Kubernetes starts the previous image.
6. FastAPI loads Model A from that image.

**This restores the model, serving code, and dependencies together.** No retraining is needed.

Verify that `/health` reports Model A's run ID, then test `/predict`. The image tag alone does not tell you whether the model changed; check the model identity as well.

## 4. Keep the latest code but restore an older model

If you want the latest application code with an older model, trigger a new release using the old model's run ID:

```bash
python scripts/release.py prepare OLD_MODEL_RUN_ID --trigger
```

Replace `OLD_MODEL_RUN_ID` with the actual run ID. The old verified release bundle must still be available. If no bundle exists, preparation requires the matching artifacts, evaluation report, and supporting data.

Jenkins then:

1. Checks out the configured source revision from Git.
2. Retrieves the old model's bundle.
3. Tests the API with that model.
4. Builds a new image containing the checked-out code and old model.
5. Smoke-tests the image and loads it into Kind.
6. Updates Git so Argo CD deploys the new image.

This is a new release, so it takes longer than selecting an existing image. It also tests whether the current code remains compatible with the old model.

| Goal | Method |
|---|---|
| Restore the complete previous release quickly | Deploy its existing image through Git |
| Keep current code but restore an older model | Build a new release using the old model ID |

Changing only `MODEL_RUN_ID` on a running container is insufficient if the requested model files are not inside its image. Our release process packages the selected files during the image build.

## 5. Why rollback goes through Git

Git is the desired deployment configuration watched by Argo CD. A direct cluster-only rollback could be reversed by Argo CD's self-healing back to the newer image still recorded in Git.

Updating Git makes the rollback the desired configuration and records what changed.

## 6. What is automatic today?

**Rollback is currently your decision.** A Grafana alert does not automatically start it.

After you commit the rollback, Argo CD and Kubernetes apply it automatically. Check rollout health, model identity, predictions, and monitoring before considering recovery complete.

Our separate model-selection issue also remains: code-triggered Jenkins builds use the configured fixed default model ID. A model release or rollback does not automatically update that default. We need to fix this selection behavior so later code-only releases consistently use the intended model.

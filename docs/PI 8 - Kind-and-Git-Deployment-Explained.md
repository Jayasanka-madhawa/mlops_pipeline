# Load into Kind and Update Git — Requesting Deployment

**These two stages connect Jenkins to Argo CD: first make the image available, then tell Kubernetes to use it through Git.**

## 6. Load the tested image into Kind

```bash
kind load docker-image "scan-quality:$IMAGE_TAG" --name mlops
```

Jenkins has already built and tested the image in Docker Desktop. This command imports it into the Kind cluster nodes' container image storage.

| Command part | Meaning |
|---|---|
| `kind load docker-image` | Import a local Docker image into Kind |
| `"scan-quality:$IMAGE_TAG"` | Select the tested image |
| `--name mlops` | Select our local cluster |

| Before loading | After loading |
|---|---|
| New image exists in Docker Desktop | New image also exists inside Kind |
| Kubernetes runs the previous release | Kubernetes still runs the previous release |

**Loading makes deployment possible; it does not start deployment or rebuild the image.**

Our manifest uses `imagePullPolicy: IfNotPresent`, allowing Kubernetes to use the image already stored on the node. Without the image being loaded, Kubernetes could try downloading it from a registry and fail because our image exists only locally.

## 7. Update Git with the deployment choice

### A. Authenticate to GitHub

```groovy
withCredentials([
    gitUsernamePassword(
        credentialsId: 'github-gitops-write',
        gitToolName: 'mac-git'
    )
])
```

Jenkins temporarily supplies its saved GitHub credentials to Git commands so it can push the deployment commit. The token is not written in the Jenkinsfile.

### B. Check for newer code

```bash
git fetch origin main
```

This updates Jenkins's knowledge of remote `main`. It does not change the checked-out files.

The pipeline compares:

```bash
git rev-parse HEAD
git rev-parse origin/main
```

- **Same commit:** continue.
- **Different commits:** stop because Git changed while this image was being built.

This avoids publishing a release from an outdated checkout. Run a new build against the latest commit, selecting the intended model ID, if this check fails.

### C. Change the image references

```bash
python scripts/update_deployment.py "scan-quality:$IMAGE_TAG"
```

The script updates both containers in `k8s/application.yaml`. For example:

```yaml
# API container
image: scan-quality:ci-13-9c8ad1a7e1c6

# Drift-monitor container
image: scan-quality:ci-13-9c8ad1a7e1c6
```

These are illustrative lines from separate container definitions. Both containers use the same image, but their startup commands differ.

### D. Commit and push

```bash
git add k8s/application.yaml
git commit -m "Deploy scan-quality:$IMAGE_TAG"
git push origin HEAD:main
```

Git now records the new image as the desired release. The pipeline skips committing if there is no staged change. A conflicting remote update can still reject the normal, non-forced push.

Our Jenkins polling exclusion ignores commits that change only this deployment manifest, preventing its own deployment commit from triggering another image build.

## What happens next?

1. Argo CD detects the changed manifest in Git.
2. Auto-Sync applies it to Kubernetes.
3. Kubernetes replaces the application Pod using the image already loaded into Kind.
4. FastAPI starts and loads the model packaged in that image.

Our one-replica deployment uses `Recreate`, so replacement can briefly interrupt requests.

**Jenkins updates Git, and Argo CD handles deployment.** The Jenkins job finishes after the push; it does not wait for the rollout. Check Argo CD for **Synced / Healthy**, and verify the API separately.

Before your next local edit, receive Jenkins's deployment commit:

```bash
git pull --ff-only
```

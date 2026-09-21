# Releasing a New Model — Simple Project Guide

This guide explains our scan-quality project's release process, from a locally trained model to a running Kubernetes application.

## 1. The main idea

You train and evaluate a model on your Mac. You choose the model to release and run a release command. Jenkins tests and packages it, then Argo CD deploys it to Kubernetes.

The new model travels inside a Docker image together with the application code and dependencies. You do not manually copy it into a running container.

## 2. Train and evaluate locally

Our model is a Random Forest that predicts whether a scan is poor or acceptable. It receives four numeric features: brightness, motion, face visibility, and signal quality. This learning project uses synthetic data; it does not extract these features from a camera video.

Training saves the model and metadata locally. MLflow records the experiment, including its parameters, metrics, and model artifact. Evaluate the candidate before deciding to release it.

MLflow records experiments. In our project, it does not deploy the application or handle live prediction requests.

## 3. Find the model run ID

1. Open MLflow at http://127.0.0.1:5001.
2. Select the **scan-quality** experiment.
3. Open the run you evaluated and want to release.
4. Copy **Run ID** from its overview.

Our earlier run ID was:

```text
d9571388a61f45369e4f709878e8246b
```

A new training run gets a different ID. The training output also includes a URL ending in `/runs/YOUR_RUN_ID`.

Choose the evaluated run you intend to release, not automatically the newest run.

## 4. Run the release command

From your project directory:

```bash
conda activate mlops
python scripts/release.py prepare YOUR_RUN_ID --trigger
```

Replace `YOUR_RUN_ID` with the actual ID. When prompted, enter your Jenkins username and Jenkins API token. The token input is hidden. This is a Jenkins token, not the GitHub token used by Jenkins to push deployment commits.

## 5. What happens inside release.py?

### A. Find the saved files

The script uses the run ID to locate files under:

```text
artifacts/YOUR_RUN_ID/
```

These include the saved model, metadata, and test evaluation report. Supporting data comes from the project's data directory.

### B. Check consistency

The script checks the selected run's metadata, evaluation report, and supporting data. These checks help avoid packaging inconsistent files.

**The script does not decide whether model accuracy is good enough. You make that decision after evaluation.**

### C. Prepare the release bundle

The bundle is stored outside the Git repository:

```text
/Users/jayasanka/mlops-releases/YOUR_RUN_ID/
```

| File | Purpose |
|---|---|
| `model.joblib` | Trained model |
| `metadata.json` | Model identity, features, threshold, and other details |
| `test_evaluation.json` | Saved test evaluation |
| `validation.csv` | Data used by API consistency tests |
| `reference_features.csv` | Reference inputs for drift monitoring |
| Checksum manifest | Records file hashes for integrity verification |

A checksum is a fingerprint of a file's contents. Jenkins uses these fingerprints to check that the bundle has not changed. Checksums verify integrity, not model quality or clinical validity.

If the release bundle already exists, the script verifies and reuses it rather than overwriting it.

### D. Trigger Jenkins

Because the command includes `--trigger`, the script sends an authenticated request to:

```text
POST http://localhost:8080/job/scan-quality-ci/buildWithParameters
```

It supplies:

```text
MODEL_RUN_ID=YOUR_RUN_ID
```

This means: “Start the pipeline using this model run.” A queued build is not yet a successful release; follow its progress in Jenkins.

### E. Jenkins calls hydrate

During the pipeline, Jenkins runs:

```bash
python scripts/release.py hydrate YOUR_RUN_ID
```

Here, **hydrate means copy the verified release files into the Jenkins workspace**, where tests and Docker builds can use them.

The bundle remains on your Mac. Our setup does not upload it to S3. The Jenkins Mac agent can read it because it runs under your Mac user account.

## 6. What does Jenkins do next?

| Stage | Simple explanation |
|---|---|
| Checkout | Downloads the application code from Git |
| Prepare release bundle | Verifies and copies the selected model bundle into its workspace |
| API tests | Checks health, prediction consistency, invalid inputs, and logging-failure handling |
| Build image | Packages the application, model, and dependencies into one Docker image |
| Container smoke test | Starts that image temporarily and checks health, prediction, and saved prediction logging |
| Load image into Kind | Copies the tested image into the local Kubernetes cluster |
| Update deployment in Git | Changes both image references in the Kubernetes manifest, commits, and pushes |

An example image tag is:

```text
scan-quality:ci-13-9c8ad1a7e1c6
```

The tag identifies a build of the application image. The MLflow run ID identifies the model training run. These are different identifiers.

**Jenkins builds one image, tests it, and makes that same image available for deployment.** The API and drift monitor use the same image with different startup commands.

A required stage failure prevents later deployment-update steps from running. Loading an image into Kind alone does not change the running application.

The existing load-test script is a separate test; it is not a stage in the Jenkinsfile we configured.

## 7. How does Git connect Jenkins to Argo CD?

The file `k8s/application.yaml` describes what Kubernetes should run. Jenkins updates the API and drift-monitor image references, for example:

```yaml
image: scan-quality:ci-13-9c8ad1a7e1c6
```

It pushes a deployment commit to `main`. Argo CD watches the repository's `k8s` directory and applies the changed configuration.

Jenkins polling ignores commits that change only `k8s/application.yaml`. This prevents its own deployment commit from causing an endless sequence of new image builds. A commit that also changes application files can still trigger CI.

Before editing your local repository again, receive Jenkins's commit:

```bash
git pull --ff-only
```

If `main` changes during a build, the configured Git-update stage stops rather than force-pushing over newer work. Run a new build against the latest commit, choosing the intended model run ID.

## 8. What happens in Kubernetes?

Argo CD applies the new image references. Kubernetes replaces the application Pod, and the new API container loads its bundled model into memory.

Our Pod contains two containers:

- **API:** receives features and returns predictions.
- **Drift monitor:** compares recent logged features with the reference data.

The containers share prediction data through a persistent volume. The API writes SQLite records; the drift monitor reads them.

Our deployment uses one replica and the `Recreate` strategy. Releases can briefly interrupt requests. This project does not currently provide a zero-downtime rollout.

## 9. Verify the release

In Argo CD, confirm:

- **Sync status:** Synced.
- **Application health:** Healthy.
- The displayed Git revision is the intended deployment commit.

Check Kubernetes:

```bash
kubectl --context kind-mlops -n scan-quality \
  rollout status deployment/scan-quality --timeout=180s
```

To test from your Mac, start a port-forward in a separate terminal:

```bash
kubectl --context kind-mlops -n scan-quality \
  port-forward svc/scan-quality 8003:8001
```

Keep it open, then check:

```bash
curl -sS http://127.0.0.1:8003/health
```

The `model_version` must match the new run ID. The current `application_version` field was set in application code during an earlier exercise; it is not the authoritative Docker image tag.

Send a prediction:

```bash
curl -sS -X POST http://127.0.0.1:8003/predict \
  -H "Content-Type: application/json" \
  -d '{"brightness":0.5,"motion":0.1,"face_visibility":0.95,"signal_quality":0.9}'
```

A port-forward may stop when its selected Pod is replaced. Restart it if needed after a release.

## 10. What happens to monitoring?

The API exposes operational metrics, including request activity, latency, and errors. It also attempts to save prediction records for drift analysis.

The drift monitor compares a recent five-minute window against the reference data and requires at least 100 recent records for the selected model version.

Prometheus collects metrics from Kubernetes through:

| Component | Prometheus target |
|---|---|
| API | `mlops-control-plane:30081` |
| Drift monitor | `mlops-control-plane:30082` |

Grafana displays the metrics and evaluates the drift alert. Prometheus and Grafana currently run in Docker Compose, connected to the Kind network.

Data drift means the incoming feature distribution changed. It does not prove accuracy declined; actual labels are needed to measure prediction quality. Retraining is not automatic in this project.

Both Kubernetes scrape targets have been verified UP. The final traffic-based dashboard and drift-alert verification is still pending, and external notifications are not configured.

## 11. What if the new release is bad?

Update both image references in Git to a previous tested image that is still available in Kind. Commit and push. Argo CD applies that previous release.

This restores the model and serving code together. Prediction records on the persistent volume remain; rollback does not restore an earlier database snapshot.

The GitOps rollback exercise is still a remaining project check. We previously tested image rollback with Docker Compose.

## 12. Who does what?

| Action | Responsible component |
|---|---|
| Train, evaluate, choose a model | You, on your Mac |
| Record experiments | MLflow |
| Package and trigger the chosen release | You run `release.py` |
| Test, build, load image, update Git | Jenkins and the Mac agent |
| Record desired deployment | Git |
| Apply deployment changes | Argo CD |
| Run the containers | Kubernetes through Kind |
| Collect metrics | Prometheus |
| Display charts and evaluate alerts | Grafana |
| Decide to retrain or roll back | You |

For a release to run, Docker Desktop, the Jenkins controller, the Mac agent, and the Kind cluster must be available. Argo CD runs inside that cluster. After a Mac restart, reconnect the agent and restart any dashboard port-forwards you need.

**The practical release action is simple: evaluate the model, copy its run ID, and run `release.py prepare YOUR_RUN_ID --trigger`. Then verify the deployment and monitor its behavior.**

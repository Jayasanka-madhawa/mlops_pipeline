# Step 10 — Docker packaging, Compose deployment and rollback

## Purpose and status

The final packaging step combines the API, selected model, matching reference dataset and Python dependencies into a release image. Docker Compose runs the API, drift monitor, Prometheus and Grafana together.

We supplied the configuration and explained the commands during the project. You have not shared build logs, container health output or confirmation of the final rollback checks. This chapter documents the proposed implementation and verification procedure, not a claim that those deployment checks have passed.

Training and evaluation remain local. Jenkins, an image registry, Kubernetes and Argo CD are outside this first version.

## 1. What an image packages

| Image contents | Purpose |
|---|---|
| Python runtime | Execute the application |
| Serving dependencies | Compatible numerical, ML, web and monitoring libraries |
| `app/` | API, prediction logging and operational metrics |
| `monitoring/drift_monitor.py` | Automated input-drift process |
| One `artifacts/<run_id>/` directory | Selected model, metadata and any accompanying evaluation report |
| Reference feature CSV | Baseline associated with the release |

The same application image supports two containers: the API uses its default command, while the drift monitor overrides that command.

An image is a packaged filesystem and configuration. A container is a running instance of that image, with its own process state and writable layer.

## 2. Record serving dependencies from the training environment

We supplied this command:

```bash
conda run -n mlops python -c "from importlib.metadata import version; packages = ['fastapi', 'uvicorn', 'numpy', 'pandas', 'scikit-learn', 'scipy', 'joblib', 'prometheus-client']; print('\n'.join(f'{p}=={version(p)}' for p in packages))" > requirements-serving.txt
```

It records the installed versions of selected serving packages. Run it from the project root and inspect the result:

```bash
cat requirements-serving.txt
```

The file should contain package requirements, not terminal diagnostics. It must reflect the `mlops` environment that trained the model; our base-environment scikit-learn mismatch demonstrated why this matters.

This is a top-level version snapshot, not a complete dependency lock. Transitive dependencies can still resolve differently, and a Mac environment does not guarantee an identical Linux dependency resolution. Validate the built image rather than assuming matching top-level versions prove full compatibility.

MLflow, pytest and requests are not needed by the API or drift monitor in this image. Tests and experiment tracking continue to run outside it in the first version.

## 3. Build the application image

Our proposed root `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements-serving.txt .
RUN pip install --no-cache-dir -r requirements-serving.txt

COPY app/ app/
COPY monitoring/drift_monitor.py monitoring/drift_monitor.py

ARG MODEL_RUN_ID
COPY artifacts/${MODEL_RUN_ID}/ artifacts/${MODEL_RUN_ID}/
COPY data/reference_features.csv reference_features.csv

ENV MODEL_RUN_ID=${MODEL_RUN_ID}

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8001"]
```

### Instructions explained

| Instruction | Effect |
|---|---|
| `FROM` | Select the Linux Python base image |
| `WORKDIR` | Set `/app` as the working directory for subsequent instructions and runtime |
| `PYTHONUNBUFFERED` | Make Python logs appear promptly in container output |
| `PYTHONDONTWRITEBYTECODE` | Avoid writing Python bytecode cache files |
| Dependency COPY and RUN | Install packages before copying application source, enabling useful layer caching |
| Source COPY | Include API and drift-monitor code |
| `ARG MODEL_RUN_ID` | Accept a build-time selection of the model artifact directory |
| Artifact COPY | Package the selected model and metadata |
| Reference COPY | Package the reference independently of the runtime data mount |
| `ENV MODEL_RUN_ID` | Persist the selected run ID as a runtime default |
| `CMD` | Define the API startup command |

Build arguments and runtime environment variables are different. `ARG` supplies the value during the build; `ENV` retains it in the resulting image configuration.

The model is loaded into memory when the API starts, not during the Docker build. Building an image successfully does not prove model startup succeeds.

### Reference consistency

The Dockerfile copies the current `data/reference_features.csv`. It does not automatically discover or verify the correct reference for the selected model. For our one-dataset project the reference is known, but future releases should package a versioned reference and verify its identity against model metadata.

### Reproducibility boundary

`python:3.12-slim` is a moving tag, not a pinned digest or exact patch release. Our monitoring images also use `latest`. These choices simplify the exercise but do not provide bit-for-bit reproducible builds.

The image uses the base image's default user, and we did not implement production hardening in this step. Architecture also matters: an Apple Silicon build commonly targets Linux ARM64, while a later cloud deployment might use AMD64. Build and test for the actual deployment platform.

## 4. Make the reference path configurable

The local monitor originally read `ROOT / 'data' / 'reference_features.csv'`. In the container, `/app/data` is mounted from the host for live prediction storage.

To keep the release reference inside the image, the monitor uses:

```python
reference_path = Path(
    os.environ.get(
        "REFERENCE_PATH",
        str(ROOT / "data" / "reference_features.csv"),
    )
)
reference = pd.read_csv(reference_path)
```

The Compose setting below points to `/app/reference_features.csv`. Local execution still defaults to the existing data-directory reference.

This prevents a routine change to the host's runtime data directory from silently replacing the packaged reference at startup. It does not make the reference-to-model association self-validating.

## 5. Select a release in `.env`

We proposed:

```dotenv
MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
RELEASE_TAG=v1
```

These values have distinct roles:

| Variable | Role |
|---|---|
| `MODEL_RUN_ID` | Select model files when building a new image |
| `RELEASE_TAG` | Select the application image tag to build or run |

Compose reads `.env` for configuration interpolation. It does not automatically inject every `.env` variable into every container. In this setup, the model run ID reaches the container because the Dockerfile stores it with `ENV` during the build.

A value already exported in the launching shell can override a corresponding `.env` interpolation value. This matters because earlier exercises exported `MODEL_RUN_ID`. Inspect the resolved configuration before a build:

```bash
docker compose config
```

If an old exported value is masking the intended `.env` value, either update the export or unset that specific variable in the Compose terminal. Do not assume editing `.env` changed an existing shell variable.

## 6. Compose the four services

Proposed `compose.yaml`:

```yaml
services:
  api:
    image: scan-quality:${RELEASE_TAG}
    build:
      context: .
      args:
        MODEL_RUN_ID: ${MODEL_RUN_ID}
    ports:
      - "127.0.0.1:8001:8001"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)"
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s

  drift-monitor:
    image: scan-quality:${RELEASE_TAG}
    command: ["python", "monitoring/drift_monitor.py"]
    environment:
      REFERENCE_PATH: /app/reference_features.csv
    volumes:
      - ./data:/app/data:ro
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus

  grafana:
    image: grafana/grafana:latest
    ports:
      - "127.0.0.1:3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    depends_on:
      - prometheus

volumes:
  prometheus_data:
  grafana_data:
```

### Service behavior

- The API loads its model, initializes SQLite and serves port 8001.
- The drift monitor starts from the same image but runs a different Python program.
- Its initial startup waits for the API health check, helping ensure database initialization has completed.
- Prometheus and Grafana use their own images and retain their named volumes.

`depends_on` is not a permanent dependency supervisor. A later API failure does not automatically stop the drift monitor. A health check becoming unhealthy does not, by itself, cause Docker's restart policy to restart a still-running process; restart policies respond to container/process lifecycle conditions.

Our `/health` endpoint confirms application startup and reports a model ID. It does not execute a sample prediction or check continued database-write capability.

## 7. Understand persistence and mounts

| Data | Location | Survives application-container replacement? |
|---|---|---|
| Packaged model and source | Image filesystem | Restored from whichever image is selected |
| Prediction database | Host `./data` bind mount | Yes |
| Drift reference | Image `/app/reference_features.csv` | Uses the selected image's copy |
| Prometheus history | `prometheus_data` named volume | Yes, if volume is retained |
| Grafana configuration and dashboards | `grafana_data` named volume | Yes, if volume is retained |
| Python metric counters | Process memory | No; reset on restart |

The API can write the mounted data directory. The monitor mounts it read-only. Our broad `./data` mount also exposes training and test CSVs to the API container even though it only needs runtime storage; narrower runtime storage can be separated later.

Mounting a directory hides any image files already present at the same mount path. Packaging the reference at `/app/reference_features.csv` avoids hiding it under `/app/data`.

## 8. Update Prometheus networking

Once both Python processes run inside Compose, use service names:

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: "scan-quality-api"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["api:8001"]

  - job_name: "scan-drift-monitor"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["drift-monitor:8002"]
```

The monitor does not need a host port mapping for Prometheus to reach it on the internal network.

Grafana continues to use `http://prometheus:9090` as its data-source URL. Browsers on the Mac use the published localhost ports.

Prometheus configuration is bind-mounted, not packaged inside the application image. Rolling back the application tag does not roll back this configuration or Grafana's stored dashboards and alert rules.

## 9. Build and start the first release

Before starting the API container, stop the host-run Uvicorn process using port 8001. Also stop the host-run drift monitor so only the intended monitor is active.

From the project root with Docker Desktop running:

```bash
docker compose config

docker compose build api

docker compose up -d

docker compose restart prometheus

docker compose ps
```

`build api` creates the image tagged `scan-quality:v1` when the resolved release tag is v1. The drift service references that same image; it does not define an independent build.

Restarting Prometheus loads the updated target configuration. It is a simple workflow for this exercise, not a requirement to restart all services for every configuration change.

### Inspect startup

```bash
curl http://127.0.0.1:8001/health

docker compose logs --tail=50 api drift-monitor
```

Check the reported model ID against the intended release. Then confirm both targets are UP at `http://localhost:9090/targets` and send a known-valid prediction request.

A drift monitor reporting insufficient recent samples is not a startup failure. Generate recent traffic if you want a new drift calculation.

## 10. Connect model identity to image identity

There are two version identifiers:

| Identifier | Example | Meaning |
|---|---|---|
| Model run ID | `d9571388a61f45369e4f709878e8246b` | Training execution and artifact selection |
| Release image tag | `scan-quality:v1` | Packaged application release |

A release may change code while retaining the same model, or change both. The image bundles those choices together.

Tags are mutable names. Rebuilding v1 with different contents destroys its usefulness as a stable rollback reference. Use a new tag for a new release and retain the old image; an image digest provides a stronger immutable identity.

For a future real release, build with a new run ID and new tag. The syntax below is a template: replace the placeholder with an existing artifact directory before running it.

```bash
MODEL_RUN_ID=<new_run_id> RELEASE_TAG=v2 docker compose build api
```

After validation, deploy the already built image:

```bash
RELEASE_TAG=v2 docker compose up -d --no-build api drift-monitor
```

The new image must already exist for this workflow. The command does not upload to an image registry or approve the candidate automatically.

## 11. What rollback does technically

Suppose v1 contains Model A and v2 contains Model B. If v2 causes problems, run:

```bash
RELEASE_TAG=v1 docker compose up -d --no-build api drift-monitor
```

The intended sequence for genuinely different images is:

1. Compose resolves both services to `scan-quality:v1`.
2. It replaces containers whose image or configuration differs.
3. The API starts from v1 and reads the model ID stored in that image.
4. It loads v1's joblib file and metadata into memory.
5. The monitor starts with v1's model ID and packaged reference.
6. New requests use the previous model and application code.

`--no-build` prevents building a different image from the current source. It does not recreate a missing old release magically. Keep the known-good image available locally or in a registry.

This simple Compose replacement can interrupt service and affect in-flight requests. It is not a zero-downtime, atomic switch across both services.

## 12. Make the selected release persist

The leading assignment in:

```bash
RELEASE_TAG=v1 docker compose up -d --no-build api drift-monitor
```

applies to that command. It does not edit `.env`.

After rollback, set the saved release configuration to the intended values, for example:

```dotenv
MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
RELEASE_TAG=v1
```

The run ID is used on future builds, while an existing v1 image already contains its runtime ID. Keeping them consistent reduces the risk of an accidental later build packaging a different model under the v1 tag.

Also check for shell exports overriding `.env`. For routine restart of a retained release, prefer `--no-build`; rebuilding should be an explicit new-release action.

## 13. What rollback does not undo

- Prediction records already written by v2 remain in SQLite.
- Prometheus history and Grafana dashboards remain.
- Changes to external configuration files are not automatically reverted.
- Database schema changes are not reversed by changing an image tag.
- Any action a caller already took based on a prediction is not undone.

The drift monitor filters records by model ID. After returning to v1, it selects v1 records in the recent time window rather than all v2 records. Depending on timing, it may initially have fewer than 100 matching samples.

If multiple releases reuse the same model run ID but change feature-extraction or serving semantics, that filter alone cannot distinguish their records. A distinct application-release identifier would improve attribution.

## 14. Why our same-image drill is limited

We previously suggested:

```bash
docker tag scan-quality:v1 scan-quality:v2-drill
```

This creates a second tag for the same image. It does not build a new model or change any code.

Switching between v1 and v2-drill can demonstrate configuration selection, but cannot prove restoration of different model behavior. Compose may not need to recreate an identical-image container, depending on the resolved service configuration and image identity.

`--force-recreate` could force a restart for lifecycle practice, but even that would not turn the alias into a genuinely different release. A meaningful behavioral rollback test needs two different validated images and confirmation of the selected model/application identity before and after the switch.

## 15. Verify a real rollback

Use a previous retained image rather than rebuilding it from current source.

After the command:

```bash
docker compose ps
curl http://127.0.0.1:8001/health
```

Verify:

- The health response reports the expected old model run ID when the model changed between releases.
- A known input produces the expected old release response.
- Both Prometheus targets recover to UP.
- New records contain the selected model ID.
- Logging failures are not increasing.
- The reference and monitor configuration correspond to the old release.

If the two releases share a model ID, the health endpoint alone cannot identify a code rollback. Inspect the running image identity or add a separate release-version field in a future version.

## 16. Stop and resume without deleting data

Stop services:

```bash
docker compose down
```

Start the selected existing release:

```bash
docker compose up -d --no-build
```

Do not add `-v` when you intend to retain Prometheus and Grafana named volumes. Preserve the host data directory to retain SQLite records.

Keeping data is different from backing it up. This exercise does not implement a backup or disaster-recovery procedure.

## 17. What the first version includes

- Local synthetic-data generation and model training.
- MLflow experiment tracking and versioned local model artifacts.
- FastAPI serving with input validation and model identification.
- SQLite prediction records.
- Prometheus operational metrics and Grafana dashboards.
- Automated input-drift checks and a verified Grafana firing state.
- API tests, a small concurrent load check and held-out evaluation.
- Proposed container packaging and a release-selection/rollback procedure.

Final container deployment verification remains dependent on running the build, health and target checks on your machine. The conversation did not include their output.

Production label collection, measured online accuracy, automated retraining, cloud storage, registry publishing and CI/CD orchestration remain future work.

## Completion criteria

- The selected image builds with compatible dependencies.
- The API and monitor start from the intended release image.
- Prediction storage and monitoring volumes survive container replacement.
- Health, valid prediction and Prometheus target checks pass.
- A previous distinct release can be restored without rebuilding it.
- Saved release configuration matches the intended running release.

This concludes the technical walkthrough of the first-version pipeline.

## References used earlier in the project

- [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)
- [Prometheus Docker deployment and volumes](https://prometheus.io/docs/prometheus/latest/installation/)
- [Grafana Docker deployment and persistent storage](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/)

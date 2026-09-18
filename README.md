# Scan Quality MLOps Demo

Educational classifier trained on synthetic scan-quality data.

## Start

Docker Desktop must be running.

First build:
docker compose build api

Start:
docker compose up -d

## Services

- API docs: http://localhost:8001/docs
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

## Monitoring

Predictions are stored in data/predictions.db.
A separate monitor compares the last five minutes of inputs
with the packaged training reference every 30 seconds.
At least 100 recent predictions are required.

Drift flags indicate input changes, not measured accuracy loss.

## Release selection

.env specifies MODEL_RUN_ID and RELEASE_TAG.
Use a new image tag for each real release.
Keep the previous image available for rollback.

## Rollback

RELEASE_TAG=v1 docker compose up -d --no-build api drift-monitor

## Stop

docker compose down

Avoid docker compose down -v: it deletes monitoring volumes.

## Scope

Includes local training, MLflow tracking, API serving,
prediction logging, Prometheus/Grafana monitoring, drift alerts,
API tests, a small load test and held-out evaluation.

Delayed-label monitoring, automated retraining and cloud CI/CD
are deferred.# mlops_pipeline

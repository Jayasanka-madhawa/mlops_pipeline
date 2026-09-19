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

## Source code and model artifacts

Git tracks application code, training scripts, tests and configuration.

Generated datasets, model artifacts, runtime databases and the local
.env file are excluded.

A fresh checkout must generate the dataset and train a model, or obtain
the required artifacts separately, before building the application image.

Copy .env.example to .env and set MODEL_RUN_ID to the selected training
run. Use a new RELEASE_TAG for each new build.

The current source includes the v2 health-response change. The running
deployment may still use the retained v1 image after the rollback drill.

CI: Jenkins checks main for changes approximately every five minutes.

# Step 6 — Prometheus instrumentation and Grafana dashboards

## Purpose and scope

After serving predictions and storing their records, we added operational monitoring. This step answers whether the API is reachable, how much traffic it receives, how long processing takes, and whether requests or prediction logging fail.

The monitoring path is:

1. Python code updates counters and histograms in the API process.
2. The API exposes their current values through `/metrics`.
3. Prometheus requests that endpoint every five seconds and stores time series.
4. Grafana queries Prometheus to display charts and statistics.

This chapter documents the operational metrics we implemented. Automated input-drift metrics and alerts are covered in later chapters. Operational metrics alone do not measure model accuracy.

The snippets describe existing project code. Do not add duplicate metric declarations or middleware to your current application.

## 1. Distinguish the monitoring components

| Component | Runs where in the initial setup | Responsibility |
|---|---|---|
| `prometheus-client` | Inside the local Python API process | Maintain and expose metric values |
| FastAPI `/metrics` | Local API on port 8001 | Return the metrics in an HTTP response |
| Prometheus server | Docker container, port 9090 | Scrape metrics, store history and execute PromQL |
| Grafana | Docker container, port 3000 | Query Prometheus and display dashboards |
| SQLite | `data/predictions.db` on the Mac | Store individual prediction records |

The Python client library is not the Prometheus server. Grafana is not our metric storage database. Prometheus does not query the prediction table; it reads `/metrics`.

Later, we moved FastAPI into Docker too. The metric definitions remained the same, but the scrape target changed from `host.docker.internal:8001` to `api:8001`.

## 2. Metric types used in this step

### Counter

A counter accumulates events, such as requests or failed log writes. It normally increases during the lifetime of the process and resets when that process restarts.

Examples:

```text
scan_api_requests_total{status="200"} 120
scan_prediction_log_failures_total 0
```

The HTTP status is a label: it distinguishes successful responses, validation errors and server failures without creating a unique series for every request.

### Histogram

A histogram summarizes many measurements in cumulative buckets and also exports a sum and count. We use histograms for durations.

For `scan_api_duration_seconds`, the Python client exports series such as:

```text
scan_api_duration_seconds_bucket{le="0.1"} ...
scan_api_duration_seconds_bucket{le="0.25"} ...
scan_api_duration_seconds_sum ...
scan_api_duration_seconds_count ...
```

The `le="0.1"` bucket counts observations at or below 0.1 seconds. Buckets are cumulative: an observation of 0.08 seconds appears in both the 0.1 and 0.25 buckets. They must not be summed across boundaries as if they were disjoint bins.

The sum divided by count gives the mean of the observed durations. Bucket data supports approximate quantiles such as p95.

We used default histogram buckets. These are adequate for the initial exercise, but finer buckets around expected latency would improve percentile resolution.

## 3. Define the metrics

We created `app/metrics.py`:

```python
from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "scan_api_requests_total",
    "Number of prediction requests",
    ["status"],
)

API_DURATION = Histogram(
    "scan_api_duration_seconds",
    "Prediction API processing time including database writing",
)

INFERENCE_DURATION = Histogram(
    "scan_inference_duration_seconds",
    "Feature preparation and model prediction time",
)

DB_DURATION = Histogram(
    "scan_db_write_duration_seconds",
    "Time spent attempting to save a prediction",
)

LOG_FAILURES = Counter(
    "scan_prediction_log_failures_total",
    "Number of predictions that could not be saved",
)
```

These collectors register with the Python client's default registry when the module is imported. They live in memory within that Python process.

Do not create a new Counter or Histogram with the same metric name inside each request. Define it once at module scope, then update the existing object.

The latency histograms have no status or model-version labels in our current implementation. Therefore, they do not support filtering latency directly by those dimensions. The `job` and `instance` labels visible later are attached by Prometheus when scraping.

## 4. Measure request processing with middleware

We added imports to `app/main.py`:

```python
from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import (
    REQUESTS,
    API_DURATION,
    INFERENCE_DURATION,
    DB_DURATION,
    LOG_FAILURES,
)
```

Then added middleware after constructing the FastAPI application:

```python
@app.middleware("http")
async def monitor_prediction_requests(request: Request, call_next):
    if request.url.path != "/predict":
        return await call_next(request)

    started = perf_counter()
    status = "500"

    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        REQUESTS.labels(status=status).inc()
        API_DURATION.observe(perf_counter() - started)
```

### What happens for one request

- Requests to other paths pass through without updating these custom request metrics.
- For `/predict`, the middleware starts a timer.
- `call_next` runs the downstream request handling, including validation and the endpoint.
- A returned response supplies the status label.
- The `finally` block records a count and duration even if an exception escapes.

We initialize the status to 500 so an unhandled downstream failure is recorded as a server failure. If the handler returns HTTP 422 for invalid input, the metric records 422.

Because the filter checks the path only, an unsupported method sent to `/predict` can also be counted, for example as HTTP 405. These are HTTP request counts, not necessarily counts of executed model predictions.

### Timer boundary

This middleware measures until downstream response creation returns. For our ordinary JSON endpoint, it includes validation, endpoint execution, synchronous database writing and response preparation. It does not measure complete network delivery to the client. It is not a full streamed-response timer or a measure of every possible queue before the middleware starts.

We excluded `/metrics`, `/health` and documentation paths so periodic monitoring itself does not inflate prediction request counts.

## 5. Measure inference and storage separately

After the endpoint computes its existing `inference_ms`, we added:

```python
INFERENCE_DURATION.observe(inference_ms / 1000)
```

The metric uses seconds even though the response field uses milliseconds.

We wrapped the SQLite save operation:

```python
try:
    with DB_DURATION.time():
        save_prediction(record)
except Exception:
    LOG_FAILURES.inc()
    logger.exception(
        "Failed to store prediction %s", prediction_id
    )
```

This records the duration of storage attempts, including failed attempts. It also exposes failures that do not cause an HTTP 500 because our endpoint still returns its prediction.

| Scenario | Request counter | Inference observation | DB observation | Logging failure |
|---|---|---|---|---|
| Valid prediction, saved | 200 | Yes | Yes | No |
| Invalid input | 422 | No | No | No |
| Valid prediction, save fails | 200 | Yes | Yes | Yes |
| Inference raises an unhandled error before timing is recorded | 500 | No completed observation | No | No |

This distinction explains why HTTP success rate and storage success rate must be monitored separately.

## 6. Expose the registry through HTTP

We added:

```python
@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(
        content=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
```

`generate_latest()` serializes the current metric registry. The content-type header identifies the exposition format to the scraper.

`include_in_schema=False` hides this route from generated API documentation; it is not an access-control rule. The endpoint remains reachable over HTTP wherever the API is exposed.

The registry can include default Python/runtime metrics in addition to our custom metrics. Platform support differs; do not assume every Linux process metric will be available on a native macOS process.

Inspect it with:

```bash
curl -s http://127.0.0.1:8001/metrics
```

A labeled request series may not exist until the first request with that status has been observed.

## 7. Run Prometheus and Grafana with Docker Compose

During this stage, FastAPI ran on the Mac while both monitoring services ran in containers.

Our monitoring-only `compose.yaml` was:

```yaml
services:
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

This is the configuration at this point in the walkthrough. Do not replace a later Compose file that already contains API and drift-monitor services.

The named volumes retain metric history and Grafana settings when containers are recreated, provided the volumes are preserved and the same Compose project is used. `docker compose down -v` deletes those named volumes.

We used `latest` for the learning setup. Those tags can change; a reproducible release should pin tested image versions or digests.

## 8. Configure the scrape target

Initial `monitoring/prometheus.yml`:

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: "scan-quality-api"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["host.docker.internal:8001"]
```

On Docker Desktop, `host.docker.internal` allows a container to reach a service on the host machine. We restarted the host API with `--host 0.0.0.0` for this setup. That bind also makes the host-run API listen on other interfaces, so this was a trusted local development setup.

Start the monitoring services:

```bash
docker compose up -d
```

After changing Prometheus configuration, our simple workflow was:

```bash
docker compose restart prometheus
```

The configuration file is mounted read-only into the container. Mounting a changed file does not itself guarantee the running server has reloaded it.

### Understand the addresses

| Caller | Destination | Address |
|---|---|---|
| Browser on the Mac | Grafana | `http://localhost:3000` |
| Browser on the Mac | Prometheus | `http://localhost:9090` |
| Browser or curl on the Mac | API | `http://localhost:8001` |
| Prometheus container | Host-run API | `http://host.docker.internal:8001/metrics` |
| Grafana container | Prometheus container | `http://prometheus:9090` |
| Prometheus container, after API containerization | API container | `http://api:8001/metrics` |

Inside a container, `localhost` refers to that container's own network namespace, not automatically to the Mac or another container. Compose provides service-name discovery for services on its network.

## 9. Verify collection before building dashboards

Open `http://localhost:9090/targets` and check the `scan-quality-api` target.

**UP** means Prometheus successfully scraped the endpoint. It does not prove that prediction inference, database writes or model accuracy are healthy.

Prometheus automatically creates:

```promql
up{job="scan-quality-api"}
```

A value of 1 indicates a successful scrape and 0 a failed scrape. If Prometheus itself is unavailable, Grafana may fail to query either result.

Send a prediction request, wait for scrapes, then query:

```promql
scan_api_requests_total{job="scan-quality-api"}
```

Prometheus stores sampled values. If a process increments a counter and exits before the next scrape, those increments may never be collected. The metric endpoint is not an event-delivery queue.

## 10. Connect Grafana to Prometheus

We added a Prometheus data source in Grafana with:

```text
URL: http://prometheus:9090
Scrape interval: 5s
```

Then used **Save & test** to verify connectivity. The configured interval should reflect the actual Prometheus scrape cadence.

The browser opens Grafana through port 3000, but data-source queries originate from the Grafana service. That is why the data-source URL uses the Compose service name instead of the browser's `localhost` URL.

## 11. Build and interpret the dashboard queries

Use a recent time range such as the last 15 minutes, with five-second dashboard refresh, while sending traffic.

### Requests per second

```promql
sum(rate(scan_api_requests_total{job="scan-quality-api"}[1m]))
```

`rate` estimates the per-second rate of increase using samples from the last minute and handles counter resets. `sum` combines status-code series and instances selected by the query.

A dashboard refresh fetches another view; it does not change the underlying scrape interval. Rate calculations need enough scraped samples, and a short burst may appear smoothed over the one-minute range.

### Average API processing time in milliseconds

```promql
1000 *
sum(rate(scan_api_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_api_duration_seconds_count{job="scan-quality-api"}[1m]))
```

The numerator estimates observed processing seconds per second; the denominator estimates observations per second. Dividing gives average seconds per observation, then multiplying by 1,000 converts to milliseconds.

### Average inference time

```promql
1000 *
sum(rate(scan_inference_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_inference_duration_seconds_count{job="scan-quality-api"}[1m]))
```

### Average database-write time

```promql
1000 *
sum(rate(scan_db_write_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_db_write_duration_seconds_count{job="scan-quality-api"}[1m]))
```

We placed these three latency queries in one Time series panel with legends `API`, `Inference` and `Database`, and set the display unit to milliseconds.

No observations during the range can produce an undefined average. Displaying a gap is more accurate than inventing a zero-millisecond latency.

### Server errors per second

```promql
sum(rate(scan_api_requests_total{
  job="scan-quality-api", status=~"5.."
}[1m])) or vector(0)
```

The fallback displays zero when no matching error series exists. However, it can also hide missing metric data. Interpret this panel alongside the scrape-status query; zero alone is not evidence of a healthy API.

HTTP 422 validation errors are not included in this server-error panel. They can be queried separately if invalid client inputs need monitoring.

### Prediction logging failures

```promql
sum(scan_prediction_log_failures_total{job="scan-quality-api"})
```

This displays the current process counter, summed across selected instances. In our one-worker setup it means failures since that API process started. The value can drop after a restart; it is not an all-time failure total.

For an estimated count over a recent interval, use `increase(...[5m])` instead. Because Prometheus extrapolates from scraped samples, such estimates are not a replacement for an exact audit log.

### Optional extension: approximate API p95

This was not required for the original dashboard, but the histogram supports:

```promql
1000 * histogram_quantile(
  0.95,
  sum by (le) (
    rate(scan_api_duration_seconds_bucket{job="scan-quality-api"}[5m])
  )
)
```

This estimates p95 from bucket boundaries. It differs from the load-test script's percentile over client-observed request durations. The two have different timing boundaries and measurement methods.

## 12. What our measurements do and do not establish

API duration should generally include more work than the inference timer for the same successful request. The difference includes storage and other processing, not just SQLite.

But the displayed averages can cover different populations: invalid requests contribute API observations without contributing inference or database observations. It is therefore unsafe to treat subtraction of dashboard averages as an exact per-request overhead calculation under mixed traffic.

The later local load test reported 100 successful requests at concurrency 5, with average client latency near 51 ms and p95 near 88 ms. Those are observed results for that brief run, not an SLA or a demonstration of sustained maximum capacity.

## 13. Single-worker constraint

Our first version runs one API worker. Metric state lives in that worker's memory and resets on restart.

If we launch multiple workers without configuring the Prometheus Python client's multiprocess support or a different aggregation design, scrapes may observe different workers and produce misleading results. Increasing Uvicorn workers is therefore not a purely independent change to this monitoring setup.

The model is also loaded once per worker, increasing total memory use. Scaling decisions should include both inference behavior and metric collection behavior.

## 14. Troubleshooting

| Symptom | First checks |
|---|---|
| Target DOWN | API running, correct target address, correct bind address and reachable port |
| Grafana data-source test fails | Use `http://prometheus:9090`; verify both services are on the Compose network |
| Request counter missing | Send at least one request to `/predict`, then wait for a scrape |
| Latency chart empty | Generate recent requests and allow multiple scrapes; check time range |
| Counter dropped | Check whether the API restarted |
| All requests return 200 but rows are missing | Inspect logging-failure counter and API logs |
| Duplicate metric registration error | Check for duplicate collector definitions or repeated module loading patterns |
| Only old data appears | Check target health, process state and browser time range |

Useful commands:

```bash
curl -s http://127.0.0.1:8001/metrics

docker compose ps

docker compose logs --tail=50 prometheus grafana
```

## Completion criteria

- The API exports custom metrics at `/metrics`.
- Prometheus successfully scrapes the target.
- Grafana connects to Prometheus and displays recent request and latency data.
- HTTP server errors and logging failures are monitored separately.
- Missing traffic, process restarts and timing boundaries are understood when interpreting charts.

The next chapter is **Step 7 — Traffic simulation and manual data-drift detection with the KS test**.

## Official references used during implementation

- [Prometheus Python client](https://prometheus.github.io/client_python/)
- [Histogram behavior and methods](https://prometheus.github.io/client_python/instrumenting/histogram/)
- [FastAPI middleware](https://fastapi.tiangolo.com/tutorial/middleware/)
- [Prometheus Docker installation](https://prometheus.io/docs/prometheus/latest/installation/)
- [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)
- [Grafana Docker setup](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/)
- [Grafana Prometheus data source](https://grafana.com/docs/grafana/latest/datasources/prometheus/configure/)

I# Prometheus: Server, Kubernetes, and Production

This guide answers three questions:

1. Is Prometheus a server, how does it run, and how does it collect metrics?
2. How does Prometheus run inside Kubernetes?
3. How is Prometheus normally deployed in production?

## Question 1: Is Prometheus a server? How does it run and collect metrics?

**Yes. Prometheus is a monitoring server. In our current project, it runs inside a Docker container on your Mac, outside Kubernetes.**

### How it runs

The Prometheus service in `compose.yaml` includes the following settings. This is an excerpt; our complete configuration also connects it to the default and Kind networks.

```yaml
prometheus:
  image: prom/prometheus:latest
  ports:
    - "127.0.0.1:9090:9090"
  volumes:
    - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - prometheus_data:/prometheus
```

Start it with:

```bash
docker compose up -d prometheus
```

| Setting | Purpose |
|---|---|
| Port `9090` | Prometheus web interface and query API |
| `prometheus.yml` | Defines scrape targets and intervals |
| `prometheus_data` | Stores collected metric history |

Open the server at http://localhost:9090.

### How our application creates metrics

Our Python code uses the Prometheus client library to update measurements. Conceptually:

```python
REQUESTS.labels(status="200").inc()
INFERENCE_DURATION.observe(0.025)
```

This records one successful request and an inference duration of 0.025 seconds.

The `/metrics` endpoint exposes the measurements as text. Example output:

```text
scan_api_requests_total{status="200"} 300
scan_inference_duration_seconds_count 300
scan_inference_duration_seconds_sum 7.5
```

These values indicate 300 successful requests and 300 recorded inference observations totaling 7.5 seconds. Counters and histogram observations accumulate during the process's lifetime.

### How Prometheus collects the values

Every five seconds, our Prometheus server sends HTTP GET requests to:

```text
http://mlops-control-plane:30081/metrics
http://mlops-control-plane:30082/metrics
```

The first endpoint exposes API metrics. The second exposes the drift monitor's metrics. Kubernetes NodePorts route these requests to the application containers.

Prometheus reaches the Kind node through the shared Docker network. It reads the responses and stores the values with timestamps. This is called **scraping**, or a **pull model**.

For example:

| Time | Successful request counter |
|---|---:|
| 10:00:00 | 300 |
| 10:00:05 | 320 |
| 10:00:10 | 345 |

The timestamped values let us calculate request rates and trends. The counter itself is not requests per second; a query such as `rate(scan_api_requests_total[1m])` estimates the rate.

### How Grafana uses the data

Grafana queries Prometheus at `http://prometheus:9090`, then displays charts and evaluates our configured drift alert.

| Component | Job |
|---|---|
| Python instrumentation | Creates and updates metrics |
| `/metrics` endpoints | Expose the measurements |
| Prometheus server | Fetches and stores them |
| Grafana | Queries, displays, and evaluates our alert |

**Prometheus does not read our SQLite database or calculate drift.** The drift monitor reads SQLite, calculates KS scores and drift flags, then exposes the results for Prometheus.

## Question 2: How does Prometheus run inside Kubernetes?

**We have not moved Prometheus into Kubernetes yet.** If we do, its metric-collection behavior stays similar, but Kubernetes manages the running process and its resources.

### Run the container in a Pod

Prometheus runs as a container in a Pod. A Deployment or StatefulSet manages that Pod. With Prometheus Operator, the Operator manages the Prometheus workload, normally through StatefulSets.

### Provide configuration, storage, and a network address

For a basic manually configured installation:

| Resource | Purpose |
|---|---|
| ConfigMap | Supplies `prometheus.yml` |
| PersistentVolumeClaim | Stores metric history across Pod replacement |
| Service | Gives Grafana a stable address for Prometheus |

With Prometheus Operator, Kubernetes custom resources define much of the configuration, and the Operator generates the configuration used by Prometheus.

### Scrape applications through the internal network

Our application Service is named `scan-quality` in namespace `scan-quality`. A simple in-cluster Prometheus configuration could scrape:

```text
http://scan-quality.scan-quality.svc.cluster.local:8001/metrics
http://scan-quality.scan-quality.svc.cluster.local:8002/metrics
```

These examples assume the usual `cluster.local` cluster domain. **NodePorts are unnecessary for this internal connection.**

Using a Service address this way is suitable for explaining our one-Pod setup. With multiple application replicas, configure endpoint discovery to scrape each replica individually instead of scraping a load-balanced Service address that may return a different replica each time.

### Connect Grafana

If Grafana also runs inside Kubernetes, it can query a Prometheus Service. For example:

```text
http://prometheus.monitoring.svc.cluster.local:9090
```

This example assumes a Service named `prometheus` in namespace `monitoring`. Actual names depend on the installation.

**Kubernetes runs and manages Prometheus. Prometheus itself sends scrape requests and stores the resulting metrics.**

## Question 3: What is normally used in real production?

For production Kubernetes, two common approaches are a self-managed monitoring stack and managed monitoring services.

### Option A: Run the monitoring stack inside Kubernetes

A common approach uses **Prometheus Operator** to manage Prometheus and its configuration.

| Component | Purpose |
|---|---|
| Prometheus | Collects and stores metrics |
| Prometheus Operator | Manages Prometheus-related Kubernetes resources and configuration |
| ServiceMonitor / PodMonitor | Defines application endpoints to discover and scrape |
| Grafana | Displays dashboards |
| Alertmanager | Groups and routes Prometheus alerts to notification destinations |

A ServiceMonitor selects Services using labels. Through the generated discovery configuration, Prometheus finds their backing endpoints and scrapes individual application instances. Monitoring can therefore follow Pod replacements without hardcoding Pod IP addresses.

For our project, the API and drift monitor would keep exposing `/metrics`. We would replace the external NodePort scrape configuration with internal Kubernetes discovery.

Your team manages upgrades, persistent storage, retention, resource capacity, access controls, and availability. Production deployments should use deliberately selected image/chart versions rather than relying on a floating `latest` tag.

Grafana-managed alerting, as used in our current project, is another alert-evaluation option. Introducing Prometheus rules and Alertmanager is a separate configuration choice; alerts do not move automatically.

### Option B: Use AWS-managed monitoring for EKS

An AWS setup can use:

- **Amazon Managed Service for Prometheus:** metric ingestion, storage, and queries.
- **An AWS-managed collector or a customer-managed collector:** discovers and scrapes EKS metrics, then sends them to the managed backend.
- **Amazon Managed Grafana or self-managed Grafana:** dashboards and visualization.

The managed Prometheus backend is not a Prometheus Pod that your team operates. Collection still has to be configured so application metrics reach it.

This reduces infrastructure your team maintains while introducing managed-service charges. You still manage application instrumentation, collection scope, useful queries, alert thresholds, and access permissions.

### Compare the approaches

| Setup | Where metrics are stored | How application metrics are collected |
|---|---|---|
| Our current learning project | Prometheus in Docker Compose | Scrapes Kind NodePorts |
| Self-managed Kubernetes production | Prometheus storage managed by your team | Internal endpoint discovery, often configured with ServiceMonitors or PodMonitors |
| AWS-managed approach | Amazon Managed Service for Prometheus | Managed or self-managed collector scrapes EKS and sends metrics |

For learning, moving to Prometheus Operator inside Kubernetes is a useful extension. For an AWS production team, the decision depends on operational capacity, reliability requirements, retention needs, and cost.

**In every approach, our instrumentation and drift calculations remain necessary. Prometheus collects their results; it does not automatically calculate model drift or accuracy.**

## Official references

- [Prometheus Operator: Getting Started](https://prometheus-operator.dev/docs/developer/getting-started/)
- [Amazon EKS: Monitor your cluster metrics with Prometheus](https://docs.aws.amazon.com/eks/latest/userguide/prometheus.html)
- [Amazon Managed Service for Prometheus: Ingestion methods](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-ingest-methods.html)

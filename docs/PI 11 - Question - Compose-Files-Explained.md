# What Do compose.yaml and compose.jenkins.yaml Do?

**These files tell Docker Compose which containers to run on your Mac.** They serve different parts of our MLOps project.

## 1. compose.yaml — application and monitoring

This file defines four services:

| Service | Purpose |
|---|---|
| `api` | Runs the scan-quality API locally |
| `drift-monitor` | Runs local drift checks |
| `prometheus` | Collects metrics |
| `grafana` | Displays dashboards and evaluates alerts |

It also configures images, ports, storage volumes, networks, and startup dependencies.

For example, our API image is selected using:

```yaml
image: scan-quality:${RELEASE_TAG}
```

The value of `RELEASE_TAG` comes from the shell environment or the Compose `.env` file. It controls the local Compose API image, not the Kubernetes Deployment.

### How we use it now

Now that the API and drift monitor run in Kubernetes, we mainly use this file to run **Prometheus and Grafana**:

```bash
docker compose up -d --no-deps prometheus grafana
```

Prometheus connects to the Kind Docker network and collects Kubernetes metrics from:

```text
mlops-control-plane:30081   API metrics
mlops-control-plane:30082   Drift metrics
```

Grafana connects to Prometheus at `http://prometheus:9090` through the Compose network.

The API and drift-monitor service definitions remain available for local Compose testing. Running an unrestricted `docker compose up -d` can start those services too, so we explicitly select the monitoring services when that is all we need.

### Storage

| Storage | Purpose |
|---|---|
| `prometheus_data` named volume | Preserves Prometheus metrics history |
| `grafana_data` named volume | Preserves Grafana configuration and dashboards |
| `./data:/app/data` bind mount | Stores prediction data for the Compose application |

The Kubernetes application uses its own persistent volume claim. It does not use the Compose application's `./data` mount.

## 2. compose.jenkins.yaml — Jenkins controller

This file defines the Jenkins controller container, including:

- The Jenkins image.
- Port `8080` for the web interface.
- Persistent `jenkins_home` storage for jobs, configuration, credentials, and build records.

Start it with:

```bash
docker compose -p mlops-ci -f compose.jenkins.yaml up -d
```

| Command part | Meaning |
|---|---|
| `-p mlops-ci` | Gives this Compose project a separate name |
| `-f compose.jenkins.yaml` | Selects the Jenkins Compose file |
| `up -d` | Creates or starts its services in the background |

Keep using the same project name so Compose uses the expected project resources and volume.

Open Jenkins at http://localhost:8080.

### The Mac agent is separate

Your agent runs directly on the Mac using a command beginning with:

```text
java -jar agent.jar ...
```

The Jenkins controller schedules jobs and displays results. The Mac agent executes the tests, Docker builds, and Kind commands.

**compose.jenkins.yaml starts the controller, not the Mac agent.** After restarting the Mac, the agent may need reconnecting separately.

## 3. How the configuration files differ

| File | Who reads it? | What it controls |
|---|---|---|
| `compose.yaml` | Docker Compose | Local application and monitoring containers |
| `compose.jenkins.yaml` | Docker Compose | Jenkins controller container |
| `Jenkinsfile` | Jenkins | Test, build, and release stages |
| `Dockerfile` | Docker build | Contents and default startup command of the application image |
| `k8s/application.yaml` | Argo CD / Kubernetes | Deployed API, drift monitor, Service, and storage resources |

**Argo CD watches the repository's `k8s` directory. It does not deploy either Compose file.**

## 4. What changes when we edit a file?

| Change | How it takes effect |
|---|---|
| Compose service configuration | Run the appropriate `docker compose up -d` command to apply changes |
| Jenkins pipeline | Jenkins reads the Jenkinsfile for a subsequent build |
| Kubernetes application manifest | Push to Git; Argo CD synchronizes it |

A Git commit containing a Compose-file change can trigger Jenkins polling, but our Jenkinsfile does not automatically apply the local Compose configuration. That still requires a Compose command on your Mac.

**Compose runs our local supporting services. Jenkins prepares releases. Argo CD and Kubernetes manage the deployed application.**

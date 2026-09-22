# Jenkins on EC2 — Deploying to EKS or ECS

**Jenkins can run on EC2. AWS's managed Kubernetes service is EKS; ECS is a different container orchestration service.**

This document describes a proposed cloud version of our local project. These AWS changes have not been implemented in the project yet.

## What each AWS service does

| Service | Purpose in this setup |
|---|---|
| EC2 | Runs Jenkins and/or its build agent |
| EKS | Managed Kubernetes; fits our Argo CD deployment approach |
| ECS | Runs containers without Kubernetes |
| ECR | Stores tested Docker images |
| S3 | Stores model release bundles |

The location of the build agent matters: if the agent runs on EC2, it cannot directly read your Mac's model-release folder. We use S3 to make the bundle available to it.

## Option 1: Jenkins on EC2 with EKS

This option keeps our Kubernetes and Argo CD workflow.

1. Train and evaluate the model on your Mac.
2. Upload its release bundle to S3.
3. Trigger Jenkins with the selected model run ID.
4. The EC2 agent downloads and verifies the matching bundle.
5. Jenkins runs API tests and builds the Docker image.
6. Jenkins smoke-tests the image and pushes it to ECR.
7. Jenkins updates Git with the ECR image reference.
8. Argo CD applies the updated manifest to EKS.
9. Kubernetes pulls the image from ECR and runs the application.

### What replaces Kind loading?

Our local command:

```bash
kind load docker-image "scan-quality:$IMAGE_TAG" --name mlops
```

is replaced by tagging and pushing the tested image to ECR. EKS nodes can then retrieve it from the registry.

The Kubernetes manifest would reference an image like:

```yaml
image: ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/scan-quality:ci-13-9c8ad1a7e1c6
```

`ACCOUNT_ID` and `REGION` are placeholders. Jenkins credentials or its agent's IAM role must allow the push, and the deployment environment must be able to pull the image.

### What stays the same?

- The model run ID selects a particular release bundle.
- The Docker image includes the model, serving code, and dependencies.
- Tests run before the deployment configuration is updated.
- Git records the desired deployment.
- Argo CD applies that configuration to Kubernetes.

The exact local manifests still need review for the cloud environment, especially storage, networking, and monitoring. Our current `update_deployment.py` also needs to accept a full ECR image reference.

## Option 2: Jenkins on EC2 with ECS

Training, S3 bundle storage, testing, Docker builds, and ECR image storage remain similar. The deployment steps change:

1. Jenkins pushes the tested image to ECR.
2. Jenkins registers a new ECS task-definition revision with that image reference.
3. Jenkins updates the ECS service to use the new revision.
4. ECS starts new tasks and replaces the previous tasks according to the service's deployment settings.

### ECS terms

| Term | Simple meaning |
|---|---|
| Task definition | Configuration for the containers, image references, resources, and runtime settings |
| Task | A running instance of that configuration |
| Service | Maintains the desired number of tasks and manages their replacement |

Our Kubernetes YAML and existing Argo CD flow do not directly deploy an ECS service. This option needs an ECS-specific deployment stage.

## Comparison

| Part | EC2 Jenkins + EKS | EC2 Jenkins + ECS |
|---|---|---|
| Model bundle storage | S3 | S3 |
| Docker image storage | ECR | ECR |
| Deployment configuration | Kubernetes manifests | ECS task definition and service configuration |
| Deployment in this proposed design | Argo CD applies Git changes | Jenkins registers the task definition and updates the service |
| Runtime | Kubernetes Pods | ECS tasks |
| Fit with our current project | Preserves the Kubernetes/Argo CD approach | Requires a different deployment stage |

**Use the EKS design when continuing our current Kubernetes and Argo CD learning path. The ECS design is an alternative container deployment architecture.**

In either case, S3 supplies the model during the build. The running API loads its model from inside the released Docker image rather than downloading it for every prediction.

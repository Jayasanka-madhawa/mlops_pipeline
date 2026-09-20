pipeline {
    agent {
        label 'mac-mlops'
    }

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timeout(time: 20, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    parameters {
        string(
            name: 'MODEL_RUN_ID',
            defaultValue: 'd9571388a61f45369e4f709878e8246b',
            description: 'Local training run to package'
        )
    }

    environment {
        CONDA_BIN = '/Users/jayasanka/miniconda3/bin/conda'
    }

    triggers {
    pollSCM('H/5 * * * *')
    }       

    stages {
        stage('Checkout') {
            steps {
                // Clear only this job's Jenkins workspace.
                deleteDir()
                checkout scm

                script {
                    if (!(params.MODEL_RUN_ID ==~ /[a-f0-9]{32}/)) {
                        error('MODEL_RUN_ID must be a 32-character hex run ID')
                    }

                    env.MODEL_RUN_ID = params.MODEL_RUN_ID

                    def commit = sh(
                        script: 'git rev-parse --short=12 HEAD',
                        returnStdout: true
                    ).trim()

                    env.IMAGE_TAG = "ci-${env.BUILD_NUMBER}-${commit}"
                }
            }
        }

        stage('Prepare release bundle') {
            steps {
                sh '''
                    "$CONDA_BIN" run -n mlops python scripts/release.py \
                    hydrate "$MODEL_RUN_ID"
                '''

                archiveArtifacts(
                    artifacts: 'release-manifest.json',
                    fingerprint: true
                )
            }
        }

        stage('API tests') {
            steps {
                sh '''
                    "$CONDA_BIN" run -n mlops python -m pytest \
                      tests/test_api.py -q \
                      --junitxml=test-results/api-tests.xml
                '''
            }
            post {
                always {
                    junit 'test-results/api-tests.xml'
                }
            }
        }

        stage('Build image') {
            steps {
                sh '''
                    docker build \
                      --build-arg MODEL_RUN_ID="$MODEL_RUN_ID" \
                      --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
                      -t "scan-quality:$IMAGE_TAG" .

                    docker image inspect "scan-quality:$IMAGE_TAG" \
                      --format '{{.Id}}' > image-id.txt

                    printf '%s\\n' "scan-quality:$IMAGE_TAG" > image-tag.txt
                '''

                archiveArtifacts(
                    artifacts: 'image-id.txt,image-tag.txt',
                    fingerprint: true
                )
            }
        }
        stage('Container smoke test') {
            options {
                timeout(time: 3, unit: 'MINUTES')
            }

            steps {
                script {
                    env.SMOKE_CONTAINER = "scan-smoke-${env.IMAGE_TAG}"
                }

                sh '''
                    set -eu

                    docker run -d \
                    --name "$SMOKE_CONTAINER" \
                    "scan-quality:$IMAGE_TAG"

                    docker cp tests/container_smoke.py \
                    "$SMOKE_CONTAINER:/tmp/container_smoke.py"

                    docker exec "$SMOKE_CONTAINER" \
                    python /tmp/container_smoke.py "$MODEL_RUN_ID"
                '''
            }

            post {
                always {
                    sh '''
                        if [ -n "${SMOKE_CONTAINER:-}" ]; then
                            docker logs "$SMOKE_CONTAINER" > smoke-container.log 2>&1 || true
                            docker rm -f "$SMOKE_CONTAINER" || true
                        fi
                    '''

                    archiveArtifacts(
                        artifacts: 'smoke-container.log',
                        allowEmptyArchive: true
                    )
                }
            }
        }

        stage('Load image into Kind') {
            steps {
                sh '''
                    set -eu

                    /opt/homebrew/bin/kind load docker-image \
                    "scan-quality:$IMAGE_TAG" \
                    --name mlops
                '''
            }
        }

        stage('Update deployment in Git') {
            steps {
                withCredentials([
                    gitUsernamePassword(
                        credentialsId: 'github-gitops-write',
                        gitToolName: 'mac-git'
                    )
                ]) {
                    sh '''
                        set -eu

                        git fetch origin main

                        if [ "$(git rev-parse HEAD)" != \
                            "$(git rev-parse origin/main)" ]; then
                            echo "main changed during this build."
                            echo "Run a new build against the latest commit."
                            exit 1
                        fi

                        "$CONDA_BIN" run -n mlops \
                        python scripts/update_deployment.py \
                        "scan-quality:$IMAGE_TAG"

                        git config user.name "Jenkins CI"
                        git config user.email "jenkins@mlops.local"

                        git add k8s/application.yaml

                        if git diff --cached --quiet; then
                            echo "Deployment already references this image."
                            exit 0
                        fi

                        git commit -m "Deploy scan-quality:$IMAGE_TAG"
                        git push origin HEAD:main
                    '''
                }
            }
        }

    }
}
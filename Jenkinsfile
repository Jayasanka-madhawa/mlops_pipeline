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
        SOURCE_PROJECT = '/Users/jayasanka/Documents/mlops_pipeline'
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

        stage('Prepare model and data') {
            steps {
                sh '''
                    set -eu

                    test -f "$SOURCE_PROJECT/artifacts/$MODEL_RUN_ID/model.joblib"
                    test -f "$SOURCE_PROJECT/artifacts/$MODEL_RUN_ID/metadata.json"
                    test -f "$SOURCE_PROJECT/data/validation.csv"
                    test -f "$SOURCE_PROJECT/data/reference_features.csv"

                    mkdir -p "artifacts/$MODEL_RUN_ID" data

                    cp "$SOURCE_PROJECT/artifacts/$MODEL_RUN_ID/model.joblib" \
                       "artifacts/$MODEL_RUN_ID/"

                    cp "$SOURCE_PROJECT/artifacts/$MODEL_RUN_ID/metadata.json" \
                       "artifacts/$MODEL_RUN_ID/"

                    cp "$SOURCE_PROJECT/data/validation.csv" data/
                    cp "$SOURCE_PROJECT/data/reference_features.csv" data/
                '''
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
    }
}
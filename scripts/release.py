import argparse
import getpass
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

import joblib
import pandas as pd
import requests

PROJECT = Path(__file__).resolve().parents[1]
RELEASES = Path.home() / "mlops-releases"

FILES = [
    "model.joblib",
    "metadata.json",
    "test_evaluation.json",
    "validation.csv",
    "reference_features.csv",
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(bundle, run_id):
    manifest = json.loads((bundle / "manifest.json").read_text())

    require(manifest["run_id"] == run_id, "Bundle run ID mismatch")
    require(set(manifest["files"]) == set(FILES), "Unexpected bundle files")

    for name in FILES:
        require(
            sha256(bundle / name) == manifest["files"][name],
            f"Checksum mismatch: {name}",
        )

    return manifest


def prepare(run_id):
    RELEASES.mkdir(parents=True, exist_ok=True)
    destination = RELEASES / run_id

    # Existing bundles are reused, never silently overwritten.
    if destination.exists():
        verify(destination, run_id)
        print(f"Using existing verified bundle: {destination}")
        return destination

    artifacts = PROJECT / "artifacts" / run_id
    metadata = json.loads((artifacts / "metadata.json").read_text())
    report = json.loads((artifacts / "test_evaluation.json").read_text())

    require(metadata["run_id"] == run_id, "Metadata run ID mismatch")
    require(report["model_run_id"] == run_id, "Evaluation run ID mismatch")
    require(
        report["threshold"] == metadata["threshold"],
        "Evaluation threshold mismatch",
    )
    require(
        report["test_sha256"] == sha256(PROJECT / "data" / "test.csv"),
        "Test dataset differs from the evaluation report",
    )

    model = joblib.load(artifacts / "model.joblib")
    require(
        list(model.feature_names_in_) == metadata["features"],
        "Model feature schema mismatch",
    )

    reference = pd.read_csv(PROJECT / "data" / "reference_features.csv")
    train = pd.read_csv(PROJECT / "data" / "train.csv")

    pd.testing.assert_frame_equal(reference, train[metadata["features"]])

    temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=RELEASES))

    try:
        for name in FILES:
            source = (
                artifacts / name
                if name.endswith((".joblib", ".json"))
                else PROJECT / "data" / name
            )
            shutil.copy2(source, temporary / name)

        manifest = {
            "run_id": run_id,
            "files": {
                name: sha256(temporary / name)
                for name in FILES
            },
        }

        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2)
        )

        verify(temporary, run_id)
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    print(f"Prepared release bundle: {destination}")
    return destination


def hydrate(run_id):
    bundle = RELEASES / run_id
    manifest = verify(bundle, run_id)

    artifacts = PROJECT / "artifacts" / run_id
    data = PROJECT / "data"
    artifacts.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    for name in FILES:
        destination = (
            artifacts / name
            if name.endswith((".joblib", ".json"))
            else data / name
        )
        shutil.copy2(bundle / name, destination)
        require(
            sha256(destination) == manifest["files"][name],
            f"Copied file failed verification: {name}",
        )

    shutil.copy2(bundle / "manifest.json", PROJECT / "release-manifest.json")
    print("Release bundle verified and copied into Jenkins workspace")


def trigger(run_id):
    verify(RELEASES / run_id, run_id)

    username = input("Jenkins username: ").strip()
    token = getpass.getpass("Jenkins API token: ")

    response = requests.post(
        "http://localhost:8080/job/scan-quality-ci/buildWithParameters",
        auth=(username, token),
        data={"MODEL_RUN_ID": run_id},
        timeout=30,
        allow_redirects=False,
    )

    require(
        response.status_code == 201,
        f"Jenkins did not confirm queuing: HTTP {response.status_code}",
    )

    print("Jenkins build queued.")
    print(response.headers.get("Location", ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "hydrate", "trigger"])
    parser.add_argument("run_id")
    parser.add_argument("--trigger", action="store_true")
    args = parser.parse_args()

    require(
        re.fullmatch(r"[a-f0-9]{32}", args.run_id) is not None,
        "Invalid run ID",
    )
    require(
        not args.trigger or args.action == "prepare",
        "--trigger is supported with prepare only",
    )

    if args.action == "prepare":
        prepare(args.run_id)
        if args.trigger:
            trigger(args.run_id)
    elif args.action == "hydrate":
        hydrate(args.run_id)
    else:
        trigger(args.run_id)


if __name__ == "__main__":
    main()
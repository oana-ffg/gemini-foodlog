from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_container_build_uses_the_reviewed_production_lockfile() -> None:
    dockerfile = (BACKEND_ROOT / "Dockerfile").read_text()
    dockerignore = {
        line.strip()
        for line in (BACKEND_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    gcloudignore = (BACKEND_ROOT / ".gcloudignore").read_text().splitlines()

    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert dockerfile.count("python:3.13-slim-bookworm@sha256:") == 2
    assert "USER foodlog" in dockerfile
    assert "uv.lock" not in dockerignore
    assert "!uv.lock" in gcloudignore
    assert "!cloudbuild.yaml" in gcloudignore


def test_cloud_build_smokes_the_runtime_and_fingerprints_installed_packages() -> None:
    configuration = (BACKEND_ROOT / "cloudbuild.yaml").read_text()

    assert "E2_STANDARD_2" in configuration
    assert "foodlog_backend.notification_app" in configuration
    assert "metadata.distributions()" in configuration
    assert "locked-manifest-sha256=" in configuration

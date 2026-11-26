"""Exercise the actual ESRP preflight without credentials or network access."""

from __future__ import annotations

import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import textwrap
import zipfile

import pytest


PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is required")
PACKAGE = "app-service-functions-custom-middleware"
VERSION = "1.0.0"


def test_build_leaves_checkout_to_onebranch():
    root = Path(__file__).resolve().parents[1]
    build = (root / ".pipelines" / "build-pypi.yml").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s+- checkout:", build)
    assert re.search(r"(?m)^\s+ob_git_persistCredentials:\s+false\s*$", build)


def test_build_authenticates_dependency_feed_before_python_setup():
    root = Path(__file__).resolve().parents[1]
    build = (root / ".pipelines" / "build-pypi.yml").read_text(encoding="utf-8")
    authentication = re.search(r"(?m)^\s+- task:\s+PipAuthenticate@1\s*$", build)
    python_setup = re.search(r"(?m)^\s+- task:\s+UsePythonVersion@0\s*$", build)
    assert authentication and python_setup
    assert authentication.start() < python_setup.start()
    inputs = build[authentication.end():python_setup.start()]
    assert re.search(
        r"(?m)^\s+artifactFeeds:\s+Antares/antares-websites-ml-python\s*$", inputs
    )
    assert re.search(r"(?m)^\s+onlyAddExtraIndex:\s+false\s*$", inputs)


def test_build_uses_actual_checkout_path_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    build = (root / ".pipelines" / "build-pypi.yml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^              - pwsh: \|\r?\n(?P<script>.*?)^                displayName:",
        build,
    )
    assert match, "Package build step not found"
    assert re.search(
        r"(?m)^\s+workingDirectory: '\$\(Build\.Repository\.LocalPath\)'\s*$", build
    )
    assert re.search(
        r"(?m)^\s+SourceFolder: '\$\(Build\.Repository\.LocalPath\)\\dist'\s*$", build
    )

    checkout = tmp_path / "checkout with spaces"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "build-pypi.ps1").write_text(
        'param([string] $TestResultsFile)\n'
        'Write-Output "Invoked build script"\n'
        'Write-Output "Test results: $TestResultsFile"\n',
        encoding="utf-8",
    )
    results = str(tmp_path / "test results")
    command = (
        textwrap.dedent(match["script"])
        .replace("$(Build.Repository.LocalPath)", str(checkout))
        .replace("$(Common.TestResultsDirectory)", results)
    )
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Invoked build script" in result.stdout
    assert f"Test results: {results}\\pytest.xml" in result.stdout


def test_release_references_esrp_template_from_self():
    root = Path(__file__).resolve().parents[1]
    release = (root / ".pipelines" / "release-pypi.yml").read_text(encoding="utf-8")
    references = re.findall(r"(?m)^\s+- template:\s+(\S+)\s*$", release)
    assert "/.pipelines/templates/publish-pypi-esrp.yml@self" in references
    assert (root / ".pipelines" / "templates" / "publish-pypi-esrp.yml").is_file()


@pytest.fixture
def release_preflight(tmp_path):
    template = (
        Path(__file__).resolve().parents[1]
        / ".pipelines"
        / "templates"
        / "publish-pypi-esrp.yml"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  - pwsh: \|\r?\n(?P<script>.*?)^    displayName:", template
    )
    assert match, "ESRP validation step not found"
    script = tmp_path / "preflight.ps1"
    script.write_text(textwrap.dedent(match["script"]), encoding="utf-8")

    dist = tmp_path / "dist"
    dist.mkdir()

    def run(**overrides):
        env = {
            **os.environ,
            "ESRP_FOLDER_LOCATION": str(dist),
            "ESRP_EXPECTED_VERSION": VERSION,
            "ESRP_OWNERS": "owner@microsoft.com",
            "ESRP_APPROVERS": "approver@microsoft.com",
            **overrides,
        }
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    return dist, run


def write_distributions(
    folder,
    *,
    wheel_name=PACKAGE,
    wheel_version=VERSION,
    sdist_name=PACKAGE,
    sdist_version=VERSION,
    missing_wheel_metadata=False,
    missing_sdist_metadata=False,
):
    prefix = f"{PACKAGE.replace('-', '_')}-{VERSION}"
    wheel = folder / f"{prefix}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if not missing_wheel_metadata:
            archive.writestr(
                f"{prefix}.dist-info/METADATA",
                f"Metadata-Version: 2.4\nName: {wheel_name}\nVersion: {wheel_version}\n\n",
            )

    with tarfile.open(folder / f"{prefix}.tar.gz", "w:gz") as archive:
        if not missing_sdist_metadata:
            metadata = (
                f"Metadata-Version: 2.4\nName: {sdist_name}\nVersion: {sdist_version}\n\n"
            ).encode()
            entry = tarfile.TarInfo(f"{prefix}/PKG-INFO")
            entry.size = len(metadata)
            archive.addfile(entry, io.BytesIO(metadata))


def test_release_accepts_matching_distributions_and_normalizes_contacts(release_preflight):
    dist, run = release_preflight
    write_distributions(dist, wheel_name=PACKAGE.replace("-", "_"))
    result = run(ESRP_OWNERS="owner@microsoft.com,\n second@microsoft.com")
    assert result.returncode == 0, result.stderr
    assert f"Validated {PACKAGE} {VERSION}" in result.stdout
    assert (
        "variable=esrpReleaseOwners]owner@microsoft.com,second@microsoft.com"
        in result.stdout
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"wheel_name": "wrong-package"}, "Artifact metadata mismatch"),
        ({"wheel_version": "2.0.0"}, "Artifact metadata mismatch"),
        ({"sdist_name": "wrong-package"}, "Artifact metadata mismatch"),
        ({"sdist_version": "2.0.0"}, "Artifact metadata mismatch"),
        ({"missing_wheel_metadata": True}, "Expected one .dist-info/METADATA"),
        ({"missing_sdist_metadata": True}, "Expected one top-level PKG-INFO"),
        (
            {"wheel_name": f"{PACKAGE}\nName: duplicate"},
            "Expected one Name and Version",
        ),
        (
            {"wheel_name": f"\n\nName: {PACKAGE}\nVersion: {VERSION}"},
            "Expected one Name and Version",
        ),
    ],
)
def test_release_rejects_bad_metadata(release_preflight, changes, message):
    dist, run = release_preflight
    write_distributions(dist, **changes)
    result = run()
    assert result.returncode != 0
    assert message in result.stderr
    assert "variable=esrpReleaseOwners]" not in result.stdout


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ESRP_EXPECTED_VERSION": ""}, "expectedVersion is empty or unresolved"),
        ({"ESRP_EXPECTED_VERSION": "$(Unset)"}, "expectedVersion is empty or unresolved"),
        ({"ESRP_EXPECTED_VERSION": "2.0.0"}, "Artifact metadata mismatch"),
        ({"ESRP_OWNERS": "owner"}, "individual alias@microsoft.com"),
        ({"ESRP_APPROVERS": "OWNER@microsoft.com"}, "must be different individuals"),
        ({"ESRP_APPROVERS": ", ,"}, "At least one owner and one approver"),
    ],
)
def test_release_rejects_invalid_inputs(release_preflight, overrides, message):
    dist, run = release_preflight
    write_distributions(dist)
    result = run(**overrides)
    assert result.returncode != 0
    assert message in result.stderr


def test_release_rejects_extra_files(release_preflight):
    dist, run = release_preflight
    write_distributions(dist)
    (dist / "unexpected.txt").write_text("not a distribution", encoding="utf-8")
    result = run()
    assert result.returncode != 0
    assert "Expected only one wheel and one source distribution" in result.stderr

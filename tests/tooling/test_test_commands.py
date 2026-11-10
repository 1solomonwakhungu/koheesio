"""Exercise the local test entry points without installing the project's integrations."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest
import tomli

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def test_project(tmp_path):
    # Hatch is a contributor tool, not a runtime dependency. The surrounding
    # interpreter supplies pytest/xdist; this project never installs packages.
    hatch = shutil.which("hatch")
    if hatch is None:
        pytest.skip("Hatch is required to exercise contributor test commands")
    pytest.importorskip("xdist")
    config = tomli.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    envs = config["tool"]["hatch"]["envs"]
    scripts = envs["default"]["scripts"]
    environment_path = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(environment_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-command-fixture"\nversion = "0.0.0"\n'
        "[tool.hatch.envs.default]\nskip-install = true\n"
        f"path = {json.dumps(str(environment_path))}\n"
        "[tool.hatch.envs.default.scripts]\n"
        + "\n".join(
            f"{name} = {json.dumps(scripts[name])}"
            for name in ("test", "spark-tests", "non-spark-tests", "spark-remote-tests")
        )
        + "\n[tool.hatch.envs.dev]\n"
        + f"template = {json.dumps(envs['dev']['template'])}\n"
        + f"path = {json.dumps(str(environment_path))}\n"
        + '[tool.pytest.ini_options]\nmarkers = ["spark", "skip_on_remote_session"]\n'
    )
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "def test_passes():\n    assert True\n"
        "def test_fails():\n    assert False\n"
        "@pytest.mark.spark\ndef test_spark_passes():\n    assert True\n"
        "@pytest.mark.spark\ndef test_spark_fails():\n    assert False\n"
    )
    # A passing result also proves that file arguments reach pytest.
    (tmp_path / "test_unselected.py").write_text("def test_passes_unselected():\n    assert False\n")
    hatch_config = tmp_path / "hatch.toml"
    hatch_config.write_text("")
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC") if key in os.environ}
    env.update(
        PATH=os.pathsep.join((str(Path(sys.executable).parent), str(Path(hatch).parent), os.defpath)),
        HATCH_CONFIG=str(hatch_config),
        HATCH_DATA_DIR=str(tmp_path / "hatch-data"),
        HATCH_CACHE_DIR=str(tmp_path / "hatch-cache"),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1",
        UV_OFFLINE="1",
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        TMPDIR=str(tmp_path),
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
    )
    return tmp_path, env, hatch


@pytest.mark.parametrize("outcome", ["passes", "fails"])
@pytest.mark.parametrize(
    "entrypoint,expected_tests",
    [
        ("dev:test", 2),
        ("dev:spark-tests", 1),
        ("dev:non-spark-tests", 1),
        ("dev:spark-remote-tests", 2),
        ("dev-test", 2),
        ("dev-test-spark", 1),
        ("dev-test-non-spark", 1),
    ],
)
def test_test_command_exit_status(test_project, entrypoint, expected_tests, outcome):
    project, env, hatch = test_project
    pytest_args = ["test_sample.py", "-k", outcome]
    # Enable only the plugin the real script needs, excluding any inherited
    # user plugins. The leading space is required by pytest{env:HATCH_TEST_ARGS:}.
    env["HATCH_TEST_ARGS"] = " -p xdist.plugin -p no:cacheprovider"
    if ":" in entrypoint:
        command = [hatch, "run", entrypoint, *pytest_args]
    else:
        make = shutil.which("make")
        if make is None:
            pytest.skip("make is required to exercise contributor Make targets")
        env["HATCH_TEST_ARGS"] += " " + " ".join(pytest_args)
        command = [make, "-f", str(PROJECT_ROOT / "makefile"), entrypoint]
    result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=60, check=False)
    output = result.stdout + result.stderr
    assert f"{expected_tests} {'passed' if outcome == 'passes' else 'failed'}" in output, output
    assert (result.returncode == 0) == (outcome == "passes"), output

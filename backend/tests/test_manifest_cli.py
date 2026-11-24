"""`harness-validate`: the command-line front end of the manifest validator."""

import json
import tomllib
from pathlib import Path

import pytest

from harness.manifest.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "manifests"
SCHEMA_FILE = Path(__file__).parents[1] / "schemas" / "harness-v1.schema.json"


def test_valid_file_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    path = FIXTURES / "valid" / "sample-hotel.yaml"
    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f"{path}: OK" in out
    assert "sample-hotel" in out


def test_invalid_file_exits_one_and_prints_each_issue_with_its_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = FIXTURES / "invalid" / "duplicate-agent-ids.yaml"
    assert main([str(path)]) == 1
    out = capsys.readouterr().out
    assert f"{path}: agents[1].id: duplicate agent id 'front-desk'" in out


def test_several_files_report_each_and_fail_if_any_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = FIXTURES / "valid" / "minimal.yaml"
    bad = FIXTURES / "invalid" / "unknown-change-class.yaml"
    assert main([str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{good}: OK" in out
    assert f"{bad}: change_classes.secrets:" in out


def test_unreadable_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.yaml"
    assert main([str(missing)]) == 2
    assert str(missing) in capsys.readouterr().err


def test_no_arguments_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_schema_flag_prints_the_committed_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schema"]) == 0
    out = capsys.readouterr().out
    assert out == SCHEMA_FILE.read_text()
    assert json.loads(out)["$id"].endswith("/harness-v1.schema.json")


def test_console_script_is_registered() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["harness-validate"] == "harness.manifest.cli:main"

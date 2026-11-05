"""`Helm` hands Helm an argv list, never a shell string.

A chart value, a storage class name or a path is caller input. A shell would interpret it,
so every command is built as a list and run with `shell=False`. These tests pin the list
shape and the `extra_args` convention: a string entry is one argument, exactly as given,
and a sequence entry expands to its elements.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from sregym.service import helm as helm_module
from sregym.service.helm import Helm

HELM_SOURCE = Path(helm_module.__file__)


def install(monkeypatch, **kwargs):
    """Run `Helm.install` against a fake Helm and return the argv it was given."""
    process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"installed", b"")))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(helm_module.subprocess, "Popen", popen)
    Helm.install(**kwargs)
    return popen.call_args.args[0], popen.call_args.kwargs


def test_install_runs_popen_with_an_argv_list_and_no_shell(monkeypatch):
    argv, kwargs = install(
        monkeypatch, release_name="demo", chart_path="example/chart", namespace="demo", remote_chart=True
    )

    assert isinstance(argv, list)
    assert kwargs["shell"] is False


def test_install_argv_names_the_release_chart_and_namespace(monkeypatch):
    argv, _ = install(monkeypatch, release_name="demo", chart_path="example/chart", namespace="demo", remote_chart=True)

    assert argv[:6] == ["helm", "install", "demo", "example/chart", "-n", "demo"]
    assert "--create-namespace" in argv


def test_values_file_is_one_unquoted_argument(monkeypatch, tmp_path):
    values_file = tmp_path / "project with spaces" / "values.yaml"

    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        values_file=values_file,
    )

    assert argv[argv.index("-f") + 1] == str(values_file)
    assert not any(argument.strip("'\"") != argument for argument in argv)


def test_a_values_path_with_a_space_in_extra_args_is_one_argument(monkeypatch, tmp_path):
    values_file = tmp_path / "project with spaces" / "overlay values.yaml"

    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=["-f", str(values_file)],
    )

    assert argv[-2:] == ["-f", str(values_file)]


def test_a_string_extra_arg_with_a_space_stays_one_argument(monkeypatch, tmp_path):
    values_file = tmp_path / "project with spaces" / "overlay values.yaml"

    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=[f"-f {values_file}"],
    )

    assert argv[-1] == f"-f {values_file}"
    assert argv.count(str(values_file)) == 0


def test_sequence_extra_args_are_used_verbatim(monkeypatch):
    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=[["--set-string", "a=b"]],
    )

    assert argv[-2:] == ["--set-string", "a=b"]


def test_a_quoted_string_extra_arg_keeps_its_quotes(monkeypatch):
    # Nothing splits a string entry any more, so the quotes are part of the argument.
    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=['--set-string "x=a b"'],
    )

    assert argv[-1] == '--set-string "x=a b"'


def test_a_string_extra_arg_with_a_semicolon_stays_one_argument(monkeypatch):
    # With a shell this value would start a second command. As an argv entry it is inert.
    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=['--set-string "storageClass=gp3;curl x|sh"'],
    )

    assert argv[-1] == '--set-string "storageClass=gp3;curl x|sh"'


@pytest.mark.parametrize("entry", [["--wait"], "--wait"])
def test_every_extra_args_convention_extends_the_same_argv(monkeypatch, entry):
    argv, _ = install(
        monkeypatch,
        release_name="demo",
        chart_path="example/chart",
        namespace="demo",
        remote_chart=True,
        extra_args=[entry],
    )

    assert argv[-1] == "--wait"


def test_uninstall_uses_an_argv_list(monkeypatch):
    monkeypatch.setattr(Helm, "_release_status", staticmethod(lambda _release_name, _namespace: True))
    process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"release uninstalled", b"")))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(helm_module.subprocess, "Popen", popen)

    Helm.uninstall(release_name="demo", namespace="a namespace")

    assert popen.call_args.args[0] == ["helm", "uninstall", "demo", "-n", "a namespace"]
    assert popen.call_args.kwargs["shell"] is False


def test_release_status_uses_an_argv_list(monkeypatch):
    process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"demo\n", b"")))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(helm_module.subprocess, "Popen", popen)

    assert Helm._release_status("demo", "a namespace") is True

    assert popen.call_args.args[0] == ["helm", "list", "-n", "a namespace"]
    assert popen.call_args.kwargs["shell"] is False


def test_add_repo_and_repo_update_use_an_argv_list(monkeypatch):
    process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"", b"")))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(helm_module.subprocess, "Popen", popen)

    Helm.add_repo("demo", "https://example.test/charts with a space")
    assert popen.call_args.args[0] == ["helm", "repo", "add", "demo", "https://example.test/charts with a space"]
    assert popen.call_args.kwargs["shell"] is False

    Helm.repo_update()
    assert popen.call_args.args[0] == ["helm", "repo", "update"]
    assert popen.call_args.kwargs["shell"] is False


def test_no_shell_is_used_anywhere_in_the_helm_module():
    """A regression guard: one `shell=True` puts caller input back in a shell."""
    assert "shell=True" not in HELM_SOURCE.read_text()

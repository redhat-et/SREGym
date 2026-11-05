"""Identity contract of the Lite suite validator, exercised without a cluster.

`validate_lite.py` refuses a context it may not delete from and records who the cluster
is, so `--resume` cannot skip problems that a different cluster passed. The tests below
call the extracted functions directly and mock every kubectl call.
"""

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

_KUBECONFIG = Path(os.environ.get("KUBECONFIG") or Path.home() / ".kube" / "config")
_HAS_CLUSTER = _KUBECONFIG.exists()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_CLUSTER, reason="no kubeconfig on disk, so there is no kubectl context"),
]


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parent / "validate_lite.py"
    spec = importlib.util.spec_from_file_location("lite_suite_runner_identity_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kind_node(name="node", uid="node-uid", architecture="arm64"):
    return {
        "metadata": {"name": name, "uid": uid},
        "spec": {"providerID": f"kind://docker/kind/{name}"},
        "status": {"nodeInfo": {"architecture": architecture}},
    }


def test_kind_context_is_disposable_without_confirmation(runner, monkeypatch):
    check_output = Mock(side_effect=["kind-kind\n", json.dumps({"items": [_kind_node()]}), "node\n"])
    monkeypatch.setattr(runner.subprocess, "check_output", check_output)

    identity = runner.cluster_identity()

    assert identity == {
        "context": "kind-kind",
        "platform": runner.KIND,
        "identity": [{"name": "node", "uid": "node-uid", "architecture": "arm64"}],
    }


def test_foreign_context_is_refused_without_confirmation(runner, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "check_output", Mock(return_value="rosa-smoke\n"))

    with pytest.raises(RuntimeError, match="deletes namespaces"):
        runner.cluster_identity()


def test_foreign_context_named_on_the_command_line_is_refused_too(runner):
    refusal = runner.disposable_context_refusal("rosa-smoke", confirmed=False)

    assert refusal is not None
    assert "rosa-smoke" in refusal
    assert "--confirm-disposable-cluster" in refusal


def test_confirmed_foreign_context_is_identified_by_the_kube_system_uid(runner, monkeypatch):
    check_output = Mock(side_effect=["rosa-smoke\n", "9d5b0d2e-uid\n"])
    monkeypatch.setattr(runner.subprocess, "check_output", check_output)

    identity = runner.cluster_identity(confirm_disposable_cluster=True)

    assert identity == {"context": "rosa-smoke", "platform": runner.OPENSHIFT, "identity": "9d5b0d2e-uid"}
    assert check_output.call_args_list[-1].args[0] == runner.KUBE_SYSTEM_UID_COMMAND
    assert runner.KUBE_SYSTEM_UID_COMMAND[-1] == "jsonpath={.metadata.uid}"


def test_a_context_that_is_not_the_current_one_is_refused(runner):
    refusal = runner.context_refusal("kind-kind", "rosa-smoke")

    assert refusal is not None
    assert "validate one cluster and mutate another" in refusal
    assert runner.context_refusal("rosa-smoke", "rosa-smoke") is None
    assert runner.context_refusal(None, "rosa-smoke") is None


def _report(runner, *, context="rosa-smoke", platform=None, identity="uid-1"):
    return {
        "cluster": {"context": context, "platform": platform or runner.OPENSHIFT, "identity": identity},
        "profile": "full",
        "with_loki": True,
        "attempts": [{"problem": "network_policy_block", "passed": True}],
    }


def _identity(runner, *, context="rosa-smoke", platform=None, identity="uid-1"):
    return {"context": context, "platform": platform or runner.OPENSHIFT, "identity": identity}


def test_resume_accepts_the_same_cluster(runner):
    stored = _report(runner)
    assert runner.resume_refusal(stored, _identity(runner), "full") is None


def test_resume_refuses_a_recreated_cluster(runner):
    refusal = runner.resume_refusal(_report(runner), _identity(runner, identity="uid-2"), "full")

    assert refusal is not None
    assert "kube-system namespace UID" in refusal
    assert "use a new directory" in refusal


def test_resume_refuses_another_context_and_platform(runner):
    other_context = runner.resume_refusal(_report(runner), _identity(runner, context="kind-kind"), "full")
    other_platform = runner.resume_refusal(_report(runner), _identity(runner, platform=runner.KIND), "full")

    assert other_context is not None and "this run is against 'kind-kind'" in other_context
    assert other_platform is not None and "this run is 'kind'" in other_platform


def test_a_refused_context_ends_the_run_with_one_line(runner, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner.sys, "argv", ["validate_lite.py", "--output-dir", str(tmp_path)])

    def refuse(*_args, **_kwargs):
        raise RuntimeError("Refusing context 'rosa-smoke': this run deletes namespaces")

    monkeypatch.setattr(runner, "cluster_identity", refuse)

    assert runner.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("REFUSED: Refusing context 'rosa-smoke'")
    assert not (tmp_path / "suite.json").exists()

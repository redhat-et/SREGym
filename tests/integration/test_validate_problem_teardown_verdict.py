"""The validator's teardown verdict asks for the survivor check, and only after a teardown.

``Conductor.cluster_damage_verdict`` compares two snapshots, so it sees what a run removed or
changed. An object this harness created and failed to remove is an addition, which the snapshot
comparison reports but does not judge. ``after_teardown=True`` turns on that second direction,
and the validator may ask for it only when it ran the teardown being judged: without
``--teardown-observers`` the observer stack staying up is the requested state. ``main.py``
already passes it this way for the campaign exit.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sregym.service.damage_report import CLEAN, DamageReport

VALIDATOR = Path(__file__).resolve().parent / "validate_problem.py"
REMOVED = ("clusterrole/mcp-server", "namespace/observe")


def _load_validator(monkeypatch):
    """Load the validator by path: it is a script, not an importable module."""
    spec = importlib.util.spec_from_file_location("sregym_validate_problem_teardown_test", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Oracle:
    """Fails while the fault is live and passes once the problem has recovered."""

    def __init__(self):
        self.faulted = False

    def capture_baseline(self):
        pass

    def evaluate(self):
        return {"success": not self.faulted}


def _problem(oracle):
    def inject_fault():
        oracle.faulted = True

    def recover_fault():
        oracle.faulted = False

    return SimpleNamespace(
        app=SimpleNamespace(name="demo", cleanup=Mock()),
        namespace="demo",
        mitigation_oracle=oracle,
        inject_fault=inject_fault,
        recover_fault=recover_fault,
    )


def _conductor_for(problem):
    return SimpleNamespace(
        problems=SimpleNamespace(
            PROBLEM_REGISTRY={"demo": object()},
            get_problem_instance=lambda _problem_id: problem,
        ),
        kubectl=SimpleNamespace(wait_for_namespace_deletion=Mock()),
        mcp_server=SimpleNamespace(stop_port_forward=Mock()),
        verify_platform=Mock(),
        _verify_scc_grant=Mock(),
        required_binaries=Mock(return_value=["oc"]),
        dependency_check=Mock(),
        fix_kubernetes=Mock(),
        undeploy_app=Mock(),
        deploy_app=Mock(),
        stop_k8s_proxy=Mock(),
        teardown_observers=Mock(return_value=list(REMOVED)),
        cluster_damage_verdict=Mock(return_value=DamageReport(verdict=CLEAN)),
        problem=None,
        problem_id=None,
        app=None,
    )


def _validate(monkeypatch, tmp_path, *, teardown_observers):
    module = _load_validator(monkeypatch)
    conductor = _conductor_for(_problem(_Oracle()))
    monkeypatch.setattr(module, "Conductor", lambda **_kwargs: conductor)

    passed, stages = module.validate(
        "demo",
        0,
        0,
        0,
        teardown_observers=teardown_observers,
        damage_json=tmp_path / "damage.json",
    )
    return module, conductor, passed, stages


def test_a_validator_teardown_asks_the_verdict_for_the_survivor_check(monkeypatch, tmp_path):
    module, conductor, passed, stages = _validate(monkeypatch, tmp_path, teardown_observers=True)

    _, kwargs = conductor.cluster_damage_verdict.call_args
    assert kwargs["after_teardown"] is True
    assert kwargs["deleted_by_us"] == REMOVED
    assert passed is True
    assert stages["damage"].status == module.PASS


def test_without_a_teardown_no_verdict_is_requested(monkeypatch, tmp_path):
    """The observer stack staying up is the requested state, so there is nothing to judge."""
    _, conductor, passed, stages = _validate(monkeypatch, tmp_path, teardown_observers=False)

    conductor.cluster_damage_verdict.assert_not_called()
    assert passed is True
    assert "damage" not in stages


def test_a_failed_deploy_still_removes_the_application(monkeypatch, tmp_path):
    """deploy_app() raising leaves a namespace behind: cleanup has to run, not report a hollow PASS."""
    module = _load_validator(monkeypatch)
    problem = _problem(_Oracle())
    conductor = _conductor_for(problem)
    conductor.problem = problem
    conductor.deploy_app = Mock(side_effect=RuntimeError("deploy timed out"))
    monkeypatch.setattr(module, "Conductor", lambda **_kwargs: conductor)

    passed, stages = module.validate("demo", 0, 0, 0, teardown_observers=False, damage_json=tmp_path / "damage.json")

    assert passed is False
    problem.app.cleanup.assert_called_once()
    conductor.kubectl.wait_for_namespace_deletion.assert_called_once_with("demo")
    assert stages["deploy"].status == module.FAIL

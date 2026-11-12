"""On OpenShift the observer stack is reset after each scenario, and nowhere else.

The reset removes the observer namespace (`observe`) after each component's own teardown, so the
next scenario deploys a fresh stack. It never touches the `sregym` namespace, and it runs only with
reconcile off and Loki deployed by this run.
"""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym import cluster_platform, storage
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.conductor import Conductor
from sregym.service.cluster_state import RECONCILE_OFF, RECONCILE_ON, RECONCILE_REPORT
from sregym.service.helm import Helm
from sregym.service.kubectl import WAIT_FOR_POD_READY_TIMEOUT
from sregym.service.telemetry import prometheus as prometheus_module
from sregym.service.telemetry.prometheus import Prometheus
from sregym.storage import OPENebs

DELETE_COMMAND = "kubectl delete namespace observe --ignore-not-found --wait=false"


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _conductor(monkeypatch, platform, *, reconcile=RECONCILE_OFF, deploy_loki=True, calls=None):
    """A conductor whose observers and kubectl record into `calls`, in order."""
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    calls = [] if calls is None else calls

    def record(name):
        def _call(*args, **kwargs):
            calls.append(name)

        return _call

    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.observer_reset")
    conductor.config = SimpleNamespace(enable_noise=False, deploy_loki=deploy_loki)
    conductor.reconcile = reconcile
    conductor.problem = SimpleNamespace(
        recover_fault=record("recover_fault"),
        app=SimpleNamespace(cleanup=record("app_cleanup")),
    )
    conductor.prometheus = SimpleNamespace(namespace="observe", teardown=record("prometheus_teardown"))
    conductor.loki = SimpleNamespace(namespace="observe", teardown=record("loki_teardown"))
    conductor.kubectl = MagicMock()
    conductor.kubectl.exec_command_checked.side_effect = lambda command, timeout=None: calls.append(command) or ""
    conductor.cluster_state = MagicMock()
    conductor.cluster_state.reconcile_to_baseline.side_effect = lambda: calls.append("reconcile_to_baseline") or {}
    conductor.cluster_state.plan_reconcile.side_effect = lambda: calls.append("plan_reconcile") or []
    conductor._baseline_captured = False
    conductor._baseline_usable = True
    conductor.results = {}
    conductor.submission_stage = "tearing_down"
    conductor._submission_lock = threading.RLock()
    conductor._submission_generation = 1
    conductor._aborted_submission_generations = set()
    conductor._accepting_submissions = False
    conductor._attempt_closed = True
    return conductor


def test_openshift_resets_the_observers_after_the_app_and_the_teardowns_before_the_namespace(monkeypatch):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, calls=calls)

    conductor._cleanup_sync()

    assert calls == [
        "recover_fault",
        "app_cleanup",
        "prometheus_teardown",
        "loki_teardown",
        DELETE_COMMAND,
    ]
    assert "cleanup_error" not in conductor.results
    assert conductor.submission_stage == "done"


def test_openshift_reset_runs_after_a_skipped_baseline_reconcile(monkeypatch):
    # Reconcile off is the OpenShift default. With the baseline captured and the mode off, the
    # reset still runs once and after the (skipped) reconcile block.
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, calls=calls)
    conductor._baseline_captured = True

    conductor._cleanup_sync()

    assert "reconcile_to_baseline" not in calls
    assert calls[-1] == DELETE_COMMAND


@pytest.mark.parametrize("reconcile", [RECONCILE_ON, RECONCILE_REPORT])
def test_openshift_with_reconcile_on_or_report_leaves_the_observers_alone(monkeypatch, reconcile):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, reconcile=reconcile, calls=calls)
    conductor._baseline_captured = True

    conductor._cleanup_sync()

    assert "prometheus_teardown" not in calls
    assert "loki_teardown" not in calls
    assert DELETE_COMMAND not in calls
    assert not [call for call in calls if call.startswith("kubectl delete namespace")]


def test_openshift_without_loki_deployed_by_this_run_leaves_the_observers_alone(monkeypatch):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, deploy_loki=False, calls=calls)

    conductor._cleanup_sync()

    assert "prometheus_teardown" not in calls
    assert "loki_teardown" not in calls
    assert not [call for call in calls if call.startswith("kubectl delete namespace")]


def test_kind_never_resets_the_observers(monkeypatch):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, KIND, calls=calls)

    conductor._cleanup_sync()

    assert calls == ["recover_fault", "app_cleanup"]
    assert "prometheus_teardown" not in calls
    assert "loki_teardown" not in calls
    assert not [call for call in calls if call.startswith("kubectl delete namespace")]


def test_the_reset_never_deletes_the_sregym_namespace(monkeypatch):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, calls=calls)

    conductor._cleanup_sync()

    assert not [call for call in calls if "sregym" in call.split()]
    assert not [call for call in calls if "namespace sregym" in call or "namespace/sregym" in call]


def test_a_failing_component_teardown_is_logged_and_the_namespace_is_still_deleted(monkeypatch):
    calls: list[str] = []
    conductor = _conductor(monkeypatch, OPENSHIFT, calls=calls)

    def failing_teardown():
        calls.append("loki_teardown")
        raise RuntimeError("helm uninstall timed out")

    conductor.loki.teardown = failing_teardown

    conductor._cleanup_sync()

    assert calls[-1] == DELETE_COMMAND
    assert "cleanup_error" not in conductor.results


def test_a_failing_namespace_delete_is_a_warning_and_not_a_cleanup_error(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, OPENSHIFT)
    conductor.kubectl.exec_command_checked.side_effect = RuntimeError("Command timed out after 300s")

    with caplog.at_level(logging.WARNING, logger="test.observer_reset"):
        conductor._cleanup_sync()

    assert "cleanup_failed" not in conductor.results
    assert "cleanup_error" not in conductor.results
    assert conductor.submission_stage == "done"
    assert "Observer stack reset failed" in caplog.text
    assert "Command timed out after 300s" in caplog.text


def test_the_namespace_delete_does_not_wait_for_the_namespace(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT)

    conductor._cleanup_sync()

    command = conductor.kubectl.exec_command_checked.call_args.args[0]
    assert "--wait=false" in command
    assert "--wait=true" not in command
    assert "--timeout" not in command


# ------------------------------------------------------------------ deploy waits for observe


def _prometheus_deploy(monkeypatch, platform, *, terminating, calls, wait_error=None):
    """Run `Prometheus.deploy()` with the cluster calls stubbed, recording into `calls`.

    Returns the Prometheus and the mock of the namespace wait. The wait records itself into
    `calls` and raises `wait_error` when one is given, as a timed-out wait does.
    """
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    monkeypatch.setattr(cluster_platform, "_platform", None)
    if platform == KIND:
        monkeypatch.setenv("SREGYM_STORAGE_CLASS", OPENebs)
        monkeypatch.setattr(storage, "_storage_class", None)

    prometheus = Prometheus()
    monkeypatch.setattr(prometheus, "_is_prometheus_running", lambda: False)
    monkeypatch.setattr(prometheus, "_requested_helm_configs", lambda: dict(prometheus.helm_configs))
    monkeypatch.setattr(prometheus, "_delete_retained_pvcs", lambda: calls.append("delete_pvcs"))

    kubectl_result = SimpleNamespace(stdout="Terminating" if terminating else "", returncode=0)
    monkeypatch.setattr(prometheus_module.subprocess, "run", MagicMock(return_value=kubectl_result))

    def wait_for_namespace_deletion(namespace, max_wait=None, **_):
        calls.append("wait_for_observe_deletion")
        if wait_error is not None:
            raise wait_error

    wait = MagicMock(side_effect=wait_for_namespace_deletion)
    kube = SimpleNamespace(wait_for_namespace_deletion=wait)
    monkeypatch.setattr(prometheus_module, "KubeCtl", lambda: kube)

    for name in ("uninstall", "install", "record_values_fingerprint", "assert_if_deployed"):
        monkeypatch.setattr(Helm, name, MagicMock(side_effect=lambda *a, _name=name, **kw: calls.append(_name)))
    return prometheus, wait


def test_openshift_deploy_waits_for_observe_to_terminate_before_installing_prometheus(monkeypatch):
    calls: list[str] = []
    prometheus, wait = _prometheus_deploy(monkeypatch, OPENSHIFT, terminating=True, calls=calls)

    prometheus.deploy()

    wait.assert_called_once_with("observe", max_wait=WAIT_FOR_POD_READY_TIMEOUT)
    assert calls.index("wait_for_observe_deletion") < calls.index("uninstall") < calls.index("install")


def test_openshift_deploy_does_not_wait_when_observe_is_not_terminating(monkeypatch):
    calls: list[str] = []
    prometheus, wait = _prometheus_deploy(monkeypatch, OPENSHIFT, terminating=False, calls=calls)

    prometheus.deploy()

    wait.assert_not_called()
    assert "install" in calls


def test_openshift_deploy_fails_with_a_runtime_error_when_observe_is_still_terminating(monkeypatch):
    calls: list[str] = []
    prometheus, _ = _prometheus_deploy(
        monkeypatch, OPENSHIFT, terminating=True, calls=calls, wait_error=Exception("was not deleted")
    )

    with pytest.raises(RuntimeError, match=f"'observe' is still Terminating after {WAIT_FOR_POD_READY_TIMEOUT}s"):
        prometheus.deploy()

    assert "install" not in calls


def test_kind_deploy_keeps_its_namespace_wait_and_its_300_second_bound(monkeypatch):
    # Kind's deploy already waited for a Terminating namespace on `main`. The bound stays 300 s.
    calls: list[str] = []
    prometheus, wait = _prometheus_deploy(monkeypatch, KIND, terminating=True, calls=calls)

    prometheus.deploy()

    wait.assert_called_once_with("observe", max_wait=300)
    assert calls.index("wait_for_observe_deletion") < calls.index("install")

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from sregym import cluster_platform
from sregym.cluster_platform import OPENSHIFT
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.service.cluster_state import ClusterBaseline, ClusterStateManager


class _RecordingHandler(logging.Handler):
    """Collect log records from one logger.

    caplog is unusable in this repo: `logger.init_logger()` installs handlers with
    propagate=False on the `all.*` loggers, so whether a record reaches pytest
    depends on which test in the full suite initialised logging first.
    """

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def recorded_logs(logger_name):
    """Context manager yielding the messages logged to ``logger_name``."""
    import contextlib

    @contextlib.contextmanager
    def _capture():
        handler = _RecordingHandler()
        target = logging.getLogger(logger_name)
        target.addHandler(handler)
        try:
            yield handler.messages
        finally:
            target.removeHandler(handler)

    return _capture()


@pytest.fixture
def manager():
    manager = ClusterStateManager.__new__(ClusterStateManager)
    manager.baseline = None
    for name in ("core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1"):
        setattr(manager, name, MagicMock())
    for client_name, method_name in (
        ("core_v1", "read_namespace"),
        ("core_v1", "list_namespace"),
        ("rbac_v1", "list_cluster_role"),
        ("rbac_v1", "list_cluster_role_binding"),
        ("core_v1", "list_persistent_volume"),
        ("storage_v1", "list_storage_class"),
        ("apiextensions_v1", "list_custom_resource_definition"),
        ("admission_v1", "list_validating_webhook_configuration"),
        ("admission_v1", "list_mutating_webhook_configuration"),
        ("admission_v1", "list_validating_admission_policy"),
        ("admission_v1", "list_validating_admission_policy_binding"),
        ("core_v1", "list_node"),
    ):
        getattr(getattr(manager, client_name), method_name).return_value = SimpleNamespace(items=[])
    manager.core_v1.read_namespace.return_value = SimpleNamespace(metadata=SimpleNamespace(uid="cluster-1"))
    return manager


@pytest.mark.parametrize(
    ("client", "method", "call_number"),
    [
        ("core_v1", "read_namespace", 1),
        ("core_v1", "list_namespace", 1),
        ("rbac_v1", "list_cluster_role", 1),
        ("rbac_v1", "list_cluster_role_binding", 1),
        ("core_v1", "list_persistent_volume", 1),
        ("storage_v1", "list_storage_class", 1),
        ("apiextensions_v1", "list_custom_resource_definition", 1),
        ("admission_v1", "list_validating_webhook_configuration", 1),
        ("admission_v1", "list_mutating_webhook_configuration", 1),
        ("admission_v1", "list_validating_admission_policy", 1),
        ("admission_v1", "list_validating_admission_policy_binding", 1),
        ("core_v1", "list_node", 1),
        ("core_v1", "list_node", 2),
        ("core_v1", "read_namespaced_config_map", 1),
    ],
)
def test_failed_baseline_read_is_not_saved(manager, tmp_path, client, method, call_number):
    read = getattr(getattr(manager, client), method)
    failure = ApiException(status=503)
    read.side_effect = failure if call_number == 1 else [SimpleNamespace(items=[]), failure]
    path = tmp_path / "baseline.json"

    with pytest.raises(ApiException) as exc_info:
        manager.save_baseline_state(path)

    assert exc_info.value is failure
    assert manager.baseline is None
    assert not path.exists()


def test_empty_successful_reads_remain_valid(manager, tmp_path):
    manager.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    path = tmp_path / "baseline.json"

    manager.save_baseline_state(path)

    assert json.loads(path.read_text())["cluster_uid"] == "cluster-1"
    assert json.loads(path.read_text())["cluster_roles"] == []
    assert manager.load_baseline_state(path)


def test_admission_policy_baseline_round_trip():
    baseline = ClusterBaseline(
        validating_admission_policies={"existing-policy"},
        validating_admission_policy_bindings={"existing-binding"},
    )
    assert ClusterBaseline.from_json(baseline.to_json()) == baseline
    assert baseline.to_dict()["validating_admission_policies"] == ["existing-policy"]


def test_legacy_baseline_preserves_preexisting_admission_policies(manager, tmp_path):
    manager.admission_v1.list_validating_admission_policy.return_value.items = [
        SimpleNamespace(metadata=SimpleNamespace(name="existing-policy"))
    ]
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "cluster-1"}))
    assert manager.load_baseline_state(path)
    assert manager.baseline.validating_admission_policies == {"existing-policy"}


def test_baseline_without_cluster_identity_is_rejected(manager, tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text("{}")

    assert not manager.load_baseline_state(path)
    assert manager.baseline is None


def test_baseline_from_another_cluster_is_rejected(manager, tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "old-cluster"}))

    assert not manager.load_baseline_state(path)
    assert manager.baseline is None


@pytest.mark.parametrize("resource", ["policies", "bindings"])
@pytest.mark.parametrize("status", [403, 404, 503])
def test_admission_policy_api_errors(manager, resource, status):
    method = "list_validating_admission_policy" + ("_binding" if resource == "bindings" else "")
    getattr(manager.admission_v1, method).side_effect = ApiException(status=status)
    if status == 404:
        assert manager._get_admission_policy_names(resource) == set()
    else:
        with pytest.raises(ApiException):
            manager._get_admission_policy_names(resource)


def test_reconcile_removes_new_policies_before_namespaces_and_preserves_baseline(manager):
    manager.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    manager.capture_baseline()
    manager.baseline.validating_admission_policies = {"existing-policy"}
    manager.baseline.validating_admission_policy_bindings = {"existing-binding"}
    for method, names in (
        ("list_validating_admission_policy", ["existing-policy", "new-policy"]),
        ("list_validating_admission_policy_binding", ["existing-binding", "new-binding"]),
    ):
        getattr(manager.admission_v1, method).return_value.items = [
            SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names
        ]
    manager.kubectl = MagicMock()
    manager.kubectl.gc_orphan_localpv_dirs.return_value = {}
    changes = manager.reconcile_to_baseline()
    manager.admission_v1.delete_validating_admission_policy.assert_called_once_with(name="new-policy")
    manager.admission_v1.delete_validating_admission_policy_binding.assert_called_once_with(name="new-binding")
    deletes = [call[0] for call in manager.admission_v1.method_calls if call[0].startswith("delete_")]
    assert deletes == ["delete_validating_admission_policy_binding", "delete_validating_admission_policy"]
    assert changes["validating_admission_policies_deleted"] == ["new-policy"]


# --- A baseline that describes a cluster that is gone ------------------------


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _live_namespaces(*names: str) -> SimpleNamespace:
    return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names])


def test_baseline_naming_a_namespace_that_is_gone_is_refused(manager, tmp_path):
    path = tmp_path / "baseline.json"
    # A protected namespace is never stale, even when it is not live.
    path.write_text(
        json.dumps({"cluster_uid": "cluster-1", "namespaces": ["sregym", "default", "kube-system", "gone-ns"]})
    )
    manager.core_v1.list_namespace.return_value = _live_namespaces("sregym")

    with recorded_logs("all.infra.cluster_state") as messages:
        assert not manager.load_baseline_state(path)

    assert manager.baseline_load_status == "stale"
    assert manager.stale_namespaces == ["gone-ns"]
    assert manager.baseline is None
    text = " ".join(messages)
    assert "gone-ns" in text
    assert "replaced" in text


@pytest.mark.parametrize(
    ("allow_missing", "loaded"),
    [(("gone-ns",), False), (("gone-ns", "also-gone"), True)],
)
def test_allow_missing_exempts_only_the_namespaces_it_names(manager, tmp_path, allow_missing, loaded):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "cluster-1", "namespaces": ["gone-ns", "also-gone"]}))
    manager.core_v1.list_namespace.return_value = _live_namespaces()

    assert manager.load_baseline_state(path, allow_missing=allow_missing) is loaded

    if loaded:
        assert manager.baseline_load_status == "loaded"
        assert manager.stale_namespaces == []
    else:
        assert manager.baseline_load_status == "stale"
        assert manager.stale_namespaces == ["also-gone"]


def test_a_permission_failure_while_checking_namespaces_is_not_clean(manager, tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "cluster-1"}))
    failure = ApiException(status=403)
    manager.core_v1.list_namespace.side_effect = failure

    with pytest.raises(ApiException) as exc_info:
        manager.load_baseline_state(path)

    assert exc_info.value is failure
    assert manager.baseline is None


@pytest.mark.parametrize(
    ("content", "status"),
    [
        (None, "missing"),
        (json.dumps({"cluster_uid": "old-cluster"}), "foreign_cluster"),
        ("{", "corrupt"),
    ],
)
def test_load_status_names_every_refusal(manager, tmp_path, content, status):
    path = tmp_path / "baseline.json"
    if content is not None:
        path.write_text(content)

    assert not manager.load_baseline_state(path)
    assert manager.baseline_load_status == status


# --- deploy_app: a refused snapshot is not overwritten ------------------------


class _DeployReachedNetworking(Exception):
    """Raised by the stubbed networking probe, which is the first step after the baseline block."""


def _deploy_conductor(cluster_state) -> Conductor:
    conductor = Conductor.__new__(Conductor)
    conductor.logger = MagicMock()
    conductor.cluster_state = cluster_state
    conductor.kubectl = MagicMock()
    conductor.kubectl.assert_pod_networking = MagicMock(side_effect=_DeployReachedNetworking)
    conductor.problem = SimpleNamespace(namespace="sregym-app", app=SimpleNamespace(namespaces=["sregym-app"]))
    conductor._baseline_captured = False
    conductor._baseline_usable = True
    return conductor


def _load_returns(status: str) -> MagicMock:
    cluster_state = MagicMock()
    cluster_state.load_baseline_state.return_value = status == "loaded"
    cluster_state.baseline_load_status = status
    return cluster_state


def test_a_stale_baseline_is_neither_used_nor_replaced(monkeypatch, tmp_path):
    path = tmp_path / "baseline.json"
    monkeypatch.setattr(conductor_module, "CLUSTER_BASELINE_STATE_FILE", path)
    cluster_state = _load_returns("stale")
    conductor = _deploy_conductor(cluster_state)

    with pytest.raises(_DeployReachedNetworking):
        conductor.deploy_app()

    cluster_state.load_baseline_state.assert_called_once_with(path, allow_missing=("sregym-app",))
    cluster_state.save_baseline_state.assert_not_called()
    assert conductor._baseline_usable is False


@pytest.mark.parametrize("status", ["missing", "foreign_cluster", "corrupt"])
def test_a_baseline_refused_for_any_other_reason_is_still_replaced(monkeypatch, tmp_path, status):
    path = tmp_path / "baseline.json"
    monkeypatch.setattr(conductor_module, "CLUSTER_BASELINE_STATE_FILE", path)
    cluster_state = _load_returns(status)
    conductor = _deploy_conductor(cluster_state)

    with pytest.raises(_DeployReachedNetworking):
        conductor.deploy_app()

    cluster_state.save_baseline_state.assert_called_once_with(path)
    assert conductor._baseline_usable is True


def test_openshift_says_the_baseline_is_for_reporting_only(monkeypatch, tmp_path):
    monkeypatch.setenv("SREGYM_PLATFORM", OPENSHIFT)
    monkeypatch.setattr(conductor_module, "CLUSTER_BASELINE_STATE_FILE", tmp_path / "baseline.json")
    conductor = _deploy_conductor(_load_returns("loaded"))

    with pytest.raises(_DeployReachedNetworking):
        conductor.deploy_app()

    assert (
        "[DEPLOY] The cluster baseline is kept for reporting only; it is not an ownership map: "
        "reconcile is off by default on this platform"
        in [call.args[0] for call in conductor.logger.info.call_args_list]
    )

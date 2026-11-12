"""Ordering and failure paths of FinalizerDeadlockController.inject_fault.

The finalizer blocks ConfigMap deletion until a live controller removes it.
If the controller is not ready when the finalizer is planted, nothing can
clear it and the namespace stays Terminating. These tests check that the
controller comes up first and that a failed injection leaves the namespace
deletable. They drive the orchestration only, not a cluster.
"""

from types import SimpleNamespace
from unittest import mock

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from sregym.conductor.problems.finalizer_deadlock_controller import (
    FinalizerDeadlockController,
    _correct_clusterrole_rules,
)
from sregym.service.ownership_ledger import ForeignObjectError, OwnershipLedger

_STEPS = (
    "_delete_support_resources",
    "_create_service_account",
    "_create_clusterrole",
    "_create_clusterrolebinding",
    "_create_controller_script",
    "_deploy_controller",
    "_wait_for_controller_ready",
    "_create_finalized_configmap",
    "_replace_clusterrole",
    "_wait_for_forbidden_log",
    "_force_clear_finalizer",
)


def _problem(calls, failing=None, error=None):
    """A problem whose steps only record that they ran."""
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.configmap_name = "reservation-cleanup-token"
    problem.controller_name = "cleanup-controller"
    problem.clusterrole_name = "configmap-cleanup-controller"
    problem.finalizer = "cleanup.reservations.io/pending-cleanup"
    problem.kubectl = SimpleNamespace(
        core_v1_api=SimpleNamespace(delete_namespaced_config_map=lambda *a, **k: calls.append("delete_configmap"))
    )

    for name in _STEPS:

        def step(*_args, _name=name, **_kwargs):
            calls.append(_name)
            if _name == failing:
                raise error

        setattr(problem, name, step)

    return problem


def test_controller_ready_before_finalizer():
    calls = []
    _problem(calls).inject_fault()

    assert calls.index("_wait_for_controller_ready") < calls.index("_create_finalized_configmap"), (
        "the finalizer must not be planted before a controller exists to remove it"
    )
    assert calls.index("_deploy_controller") < calls.index("_wait_for_controller_ready")


def test_readiness_timeout_plants_no_finalizer():
    """Nothing is stranded, so the namespace stays deletable and a retry works."""
    calls = []
    problem = _problem(calls, failing="_wait_for_controller_ready", error=TimeoutError("not ready"))

    with pytest.raises(TimeoutError):
        problem.inject_fault()

    assert "_create_finalized_configmap" not in calls
    assert "_force_clear_finalizer" not in calls, "there is nothing to clear"


def test_failure_restores_rbac_then_clears_finalizer():
    """The restored role lets the running controller clear the finalizer itself."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("no forbidden log"))

    with pytest.raises(RuntimeError):
        problem.inject_fault()

    tail = calls[calls.index("_wait_for_forbidden_log") :]
    assert tail.count("_replace_clusterrole") == 1
    assert tail.index("_replace_clusterrole") < tail.index("_force_clear_finalizer")


def test_cleanup_restores_correct_clusterrole_rules():
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("no forbidden log"))
    problem._replace_clusterrole = mock.Mock()

    with pytest.raises(RuntimeError):
        problem.inject_fault()

    assert problem._replace_clusterrole.call_args_list[-1] == mock.call(_correct_clusterrole_rules())


def test_cleanup_error_does_not_mask_original():
    """The cleanup runs from an except block. Its own error must not replace the real one."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("the real failure"))
    problem._force_clear_finalizer = mock.Mock(side_effect=Exception("cleanup also broke"))

    with pytest.raises(RuntimeError, match="the real failure"):
        problem.inject_fault()

    problem._force_clear_finalizer.assert_called_once()


def test_failed_clusterrole_restore_still_clears_finalizer():
    calls = []
    problem = _problem(calls)
    problem._restore_clusterrole = mock.Mock(side_effect=Exception("rbac api down"))

    problem._undo_finalizer_trap()

    assert calls == ["_force_clear_finalizer"]


def test_recovery_keeps_the_controller_the_oracle_needs():
    """The oracle runs after recover_fault and needs the controller and its ClusterRole.

    Removing the support objects in recovery made a recovered fault read as
    `controller_not_healthy`. Only the RBAC is restored; the ClusterRole and its binding are
    cluster-scoped and recorded in the ownership ledger, so the teardown collects them.
    """
    calls = []
    problem = _problem(calls)
    problem._restore_clusterrole = lambda: calls.append("_restore_clusterrole")
    problem._wait_until_configmap_deleted = lambda timeout_seconds: calls.append("_wait") or True

    problem.recover_fault()

    assert calls == ["_restore_clusterrole", "_wait"]
    assert "_delete_support_resources" not in calls


def _bare_problem():
    """A problem whose Kubernetes clients are mocks, so the real create and delete bodies can be inspected."""
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.configmap_name = "reservation-cleanup-token"
    problem.controller_name = "cleanup-controller"
    problem.sa_name = "cleanup-controller"
    problem.clusterrole_name = "configmap-cleanup-controller"
    problem.clusterrolebinding_name = "configmap-cleanup-controller"
    problem.finalizer = "cleanup.reservations.io/pending-cleanup"
    problem.rbac_v1 = mock.Mock()
    problem.kubectl = SimpleNamespace(core_v1_api=mock.Mock(), apps_v1_api=mock.Mock())
    return problem


def _obj(uid):
    return SimpleNamespace(metadata=SimpleNamespace(uid=uid))


def _sent_bodies(*mocks):
    """Every body argument sent to the API by the given mocks, serialised to plain dicts."""
    bodies = []
    for api in mocks:
        for call in api.mock_calls:
            if "body" in call.kwargs:
                bodies.append(ApiClient().sanitize_for_serialization(call.kwargs["body"]))
    return bodies


def _labels_and_annotations(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("labels", "annotations") and isinstance(value, dict):
                found.extend(f"{k}={v}" for k, v in value.items())
            _labels_and_annotations(value, found)
    elif isinstance(node, list):
        for item in node:
            _labels_and_annotations(item, found)
    return found


def test_no_built_object_carries_a_sregym_or_owned_label():
    problem = _bare_problem()
    problem.rbac_v1.create_cluster_role.return_value = _obj("uid-cr")
    problem.rbac_v1.create_cluster_role_binding.return_value = _obj("uid-crb")

    problem._create_service_account()
    problem._create_clusterrole(_correct_clusterrole_rules())
    problem._create_clusterrolebinding()
    problem._create_controller_script()
    problem._deploy_controller()
    problem._create_finalized_configmap()
    problem._replace_clusterrole(_correct_clusterrole_rules())

    bodies = _sent_bodies(problem.rbac_v1, problem.kubectl.core_v1_api, problem.kubectl.apps_v1_api)
    assert len(bodies) >= 6, "the test must inspect every object the scenario builds"

    texts = [text for body in bodies for text in _labels_and_annotations(body, [])]
    assert "app.kubernetes.io/managed-by=cleanup-controller" in texts
    leaked = [text for text in texts if "sregym" in text.lower() or "owned" in text.lower()]
    assert leaked == []


def test_cluster_scoped_creates_are_recorded_by_uid():
    problem = _bare_problem()
    problem.rbac_v1.create_cluster_role.return_value = _obj("uid-cr")
    problem.rbac_v1.create_cluster_role_binding.return_value = _obj("uid-crb")

    problem._create_clusterrole(_correct_clusterrole_rules())
    problem._create_clusterrolebinding()

    ledger = OwnershipLedger()
    assert ledger.uid_of("clusterrole", "configmap-cleanup-controller") == "uid-cr"
    assert ledger.uid_of("clusterrolebinding", "configmap-cleanup-controller") == "uid-crb"


def test_foreign_clusterrole_is_refused_and_never_deleted():
    problem = _bare_problem()
    problem.rbac_v1.create_cluster_role.side_effect = ApiException(status=409)
    problem.rbac_v1.read_cluster_role.return_value = _obj("uid-someone-else")

    with pytest.raises(ForeignObjectError, match="configmap-cleanup-controller"):
        problem._create_clusterrole(_correct_clusterrole_rules())
    problem.rbac_v1.replace_cluster_role.assert_not_called()

    problem._force_clear_finalizer = mock.Mock()
    problem._wait_until_configmap_deleted = mock.Mock()
    problem._delete_support_resources()

    problem.rbac_v1.delete_cluster_role.assert_not_called()
    problem.rbac_v1.delete_cluster_role_binding.assert_not_called()


def test_foreign_clusterrolebinding_is_refused_and_not_replaced():
    problem = _bare_problem()
    problem.rbac_v1.create_cluster_role_binding.side_effect = ApiException(status=409)
    problem.rbac_v1.read_cluster_role_binding.return_value = _obj("uid-someone-else")

    with pytest.raises(ForeignObjectError, match="configmap-cleanup-controller"):
        problem._create_clusterrolebinding()
    problem.rbac_v1.replace_cluster_role_binding.assert_not_called()


def test_recorded_cluster_scoped_objects_are_deleted_by_uid():
    problem = _bare_problem()
    ledger = OwnershipLedger()
    ledger.record("clusterrole", "configmap-cleanup-controller", "uid-cr")
    ledger.record("clusterrolebinding", "configmap-cleanup-controller", "uid-crb")

    problem._force_clear_finalizer = mock.Mock()
    problem._wait_until_configmap_deleted = mock.Mock()
    problem._delete_support_resources()

    cr_call = problem.rbac_v1.delete_cluster_role.call_args
    crb_call = problem.rbac_v1.delete_cluster_role_binding.call_args
    assert cr_call.kwargs["body"].preconditions.uid == "uid-cr"
    assert crb_call.kwargs["body"].preconditions.uid == "uid-crb"
    assert ledger.entries() == [], "a completed delete forgets the entry"

"""A reconcile pass must be describable without being performed, and the mode decides which one runs."""

import dataclasses
import importlib.util
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT, PLATFORMS, set_platform
from sregym.conductor.conductor import Conductor, resolve_reconcile_mode
from sregym.service import cluster_state
from sregym.service.cluster_state import (
    RECONCILE_MODES,
    RECONCILE_OFF,
    RECONCILE_ON,
    RECONCILE_REPORT,
    ClusterBaseline,
    ClusterStateManager,
)

# Every write `reconcile_to_baseline` can make, as (label, owning mock, method).
_WRITE_CALLS = (
    ("namespace", "kubectl", "delete_namespace"),
    ("localpv_gc", "kubectl", "gc_orphan_localpv_dirs"),
    ("coredns_restart", "kubectl", "exec_command"),
    ("cluster_role", "rbac_v1", "delete_cluster_role"),
    ("cluster_role_binding", "rbac_v1", "delete_cluster_role_binding"),
    ("persistent_volume", "core_v1", "delete_persistent_volume"),
    ("node", "core_v1", "patch_node"),
    ("coredns_configmap", "core_v1", "replace_namespaced_config_map"),
    ("storage_class", "storage_v1", "delete_storage_class"),
    ("crd", "apiextensions_v1", "delete_custom_resource_definition"),
    ("validating_webhook", "admission_v1", "delete_validating_webhook_configuration"),
    ("mutating_webhook", "admission_v1", "delete_mutating_webhook_configuration"),
    ("admission_policy", "admission_v1", "delete_validating_admission_policy"),
    ("admission_policy_binding", "admission_v1", "delete_validating_admission_policy_binding"),
    ("cr_finalizers", "custom_api", "patch_namespaced_custom_object"),
)

_WRITE_VERBS = frozenset({"create", "delete", "patch", "replace", "update", "exec", "run"})

# The plan this fixture produces, in order. It names every family in _WRITE_CALLS.
_EXPECTED_TARGETS = [
    "validatingadmissionpolicybinding/leftover-policy-binding",
    "validatingadmissionpolicy/leftover-policy",
    "namespace/chaos-app",
    "clusterrole/leftover-role",
    "clusterrolebinding/leftover-binding",
    "persistentvolume/leftover-pv",
    "node/* /var/openebs/local",
    "storageclass/leftover-sc",
    "customresource/leftover.example.com CR chaos-app/widget-1",
    "customresourcedefinition/leftover.example.com",
    "validatingwebhookconfiguration/leftover-vwc",
    "mutatingwebhookconfiguration/leftover-mwc",
    "node/worker-1 labels",
    "node/worker-1 taints",
    "configmap/kube-system/coredns",
]


def _items(*names: str) -> SimpleNamespace:
    return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names])


def _node(name: str, labels: dict[str, str], taints: list[tuple[str, str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        spec=SimpleNamespace(
            taints=[SimpleNamespace(key=key, value=value, effect=effect) for key, value, effect in taints]
        ),
    )


@pytest.fixture
def build_manager(monkeypatch):
    """Build a manager whose live cluster differs from its baseline in every reconcile family."""

    def build() -> tuple[ClusterStateManager, MagicMock]:
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "widget-detached"}},
                {
                    "metadata": {
                        "name": "widget-1",
                        "namespace": "chaos-app",
                        "finalizers": ["example.com/cleanup"],
                    }
                },
            ]
        }
        monkeypatch.setattr(cluster_state.client, "CustomObjectsApi", MagicMock(return_value=custom_api))

        manager = ClusterStateManager.__new__(ClusterStateManager)
        for name in ("core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1"):
            setattr(manager, name, MagicMock())
        manager.kubectl = MagicMock()
        manager.kubectl.gc_orphan_localpv_dirs.return_value = {"worker-1": 2}

        manager.baseline = ClusterBaseline(
            cluster_uid="cluster-1",
            namespaces={"sregym"},
            cluster_roles={"sregym-runner"},
            persistent_volumes=set(),
            storage_classes={"sregym-sc"},
            node_labels={"worker-1": {"sregym.io/role": "worker"}},
            node_taints={"worker-1": []},
            coredns_configmap_data={"Corefile": "baseline"},
        )

        manager.core_v1.list_namespace.return_value = _items("sregym", "chaos-app")
        manager.rbac_v1.list_cluster_role.return_value = _items("sregym-runner", "leftover-role", "system:leftover")
        manager.rbac_v1.list_cluster_role_binding.return_value = _items("leftover-binding")
        manager.core_v1.list_persistent_volume.return_value = _items("leftover-pv")
        manager.storage_v1.list_storage_class.return_value = _items("sregym-sc", "leftover-sc")
        manager.apiextensions_v1.list_custom_resource_definition.return_value = _items("leftover.example.com")
        manager.admission_v1.list_validating_webhook_configuration.return_value = _items("leftover-vwc")
        manager.admission_v1.list_mutating_webhook_configuration.return_value = _items("leftover-mwc")
        manager.admission_v1.list_validating_admission_policy.return_value = _items("leftover-policy")
        manager.admission_v1.list_validating_admission_policy_binding.return_value = _items("leftover-policy-binding")
        manager.apiextensions_v1.read_custom_resource_definition.return_value = SimpleNamespace(
            spec=SimpleNamespace(versions=[SimpleNamespace(name="v1")])
        )
        manager.core_v1.list_node.return_value = SimpleNamespace(
            items=[
                _node("worker-1", {"sregym.io/role": "worker", "leftover": "yes"}, [("leftover", "true", "NoSchedule")])
            ]
        )
        manager.core_v1.read_namespaced_config_map.return_value = SimpleNamespace(data={"Corefile": "modified"})
        return manager, custom_api

    return build


def _write_calls(manager: ClusterStateManager, custom_api: MagicMock) -> list[str]:
    """Every call in a reconcile pass whose method name starts with a writing verb."""
    mocks = [
        (owner, getattr(manager, owner))
        for owner in ("kubectl", "core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1")
    ]
    mocks.append(("custom_api", custom_api))
    return [
        f"{owner}.{name}"
        for owner, mock in mocks
        for name, _, _ in mock.method_calls
        if name.split("_", 1)[0] in _WRITE_VERBS
    ]


def _attach_write_recorder(manager: ClusterStateManager, custom_api: MagicMock) -> MagicMock:
    """Merge the writing mocks into one recorder, so two passes compare call by call, in order."""
    recorder = MagicMock()
    for label, owner, method in _WRITE_CALLS:
        mock = custom_api if owner == "custom_api" else getattr(manager, owner)
        recorder.attach_mock(getattr(mock, method), label)
    return recorder


def test_plan_reconcile_mutates_nothing(build_manager):
    manager, custom_api = build_manager()

    plan = manager.plan_reconcile()

    assert [mutation.target for mutation in plan] == _EXPECTED_TARGETS
    assert manager.kubectl.delete_namespace.mock_calls == []
    assert [call for call in manager.core_v1.method_calls if call[0].startswith("delete_namespaced_")] == []
    assert [call for call in manager.rbac_v1.method_calls if call[0].startswith("delete_cluster_")] == []
    assert manager.core_v1.delete_persistent_volume.mock_calls == []
    assert manager.storage_v1.delete_storage_class.mock_calls == []
    assert [call for call in manager.apiextensions_v1.method_calls if call[0].startswith("delete_")] == []
    assert manager.core_v1.patch_node.mock_calls == []
    assert manager.core_v1.replace_namespaced_config_map.mock_calls == []
    assert manager.kubectl.exec_command.mock_calls == []
    assert [call for call in manager.admission_v1.method_calls if call[0].startswith(("create_", "delete_"))] == []
    assert [call for call in custom_api.method_calls if call[0].startswith(("create_", "patch_"))] == []
    assert manager.kubectl.gc_orphan_localpv_dirs.mock_calls == []
    assert manager.kubectl.run_node_script_pod.mock_calls == []
    assert _write_calls(manager, custom_api) == []


def test_plan_names_what_reconcile_applies(build_manager, monkeypatch):
    plan = build_manager()[0].plan_reconcile()
    handed: list[tuple[str, str]] = []

    class _RecordingApplyingSink(cluster_state._ApplyingSink):
        """Records every write it is handed, then applies it."""

        def do(self, action: str, target: str, note: str, apply) -> None:
            handed.append((action, target))
            super().do(action, target, note, apply)

    # `reconcile_to_baseline` builds its own applying sink; swap in the recording one.
    monkeypatch.setattr(cluster_state, "_ApplyingSink", _RecordingApplyingSink)
    manager, _ = build_manager()
    manager.reconcile_to_baseline()

    assert handed == [(mutation.action, mutation.target) for mutation in plan]


def test_apply_reconcile_reproduces_reconcile_to_baseline(build_manager):
    applied_manager, applied_api = build_manager()
    applied_events = _attach_write_recorder(applied_manager, applied_api)
    expected_changes = applied_manager.reconcile_to_baseline()

    plan_manager, plan_api = build_manager()
    planned_events = _attach_write_recorder(plan_manager, plan_api)
    sink = cluster_state._PlanningSink()
    changes = plan_manager._reconcile(sink)
    plan_manager.apply_reconcile(sink.mutations)

    assert planned_events.mock_calls == applied_events.mock_calls
    assert changes == expected_changes
    assert expected_changes["localpv_dirs_gc"] == {"removed": 2, "by_node": {"worker-1": 2}}
    assert expected_changes["coredns_reset"] is True


def test_plan_mutations_are_frozen_and_explained(build_manager):
    plan = build_manager()[0].plan_reconcile()

    assert plan
    for mutation in plan:
        assert mutation.note
        with pytest.raises(dataclasses.FrozenInstanceError):
            mutation.action = "create"


# --- Which mode cleanup uses -------------------------------------------------


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    """Start every test with no platform chosen, so nothing leaks between them."""
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


@pytest.mark.parametrize(("platform", "expected"), [(KIND, RECONCILE_ON), (OPENSHIFT, RECONCILE_OFF)])
def test_reconcile_mode_default_follows_the_platform(platform, expected):
    set_platform(platform)

    assert resolve_reconcile_mode(None) == expected


@pytest.mark.parametrize("mode", RECONCILE_MODES)
def test_an_explicit_reconcile_mode_wins_on_every_platform(mode):
    for platform in PLATFORMS:
        set_platform(platform)

        assert resolve_reconcile_mode(mode) == mode


def test_an_unknown_reconcile_mode_names_the_valid_values():
    with pytest.raises(ValueError) as exc_info:
        resolve_reconcile_mode("destroy")

    message = str(exc_info.value)
    assert "destroy" in message
    for mode in RECONCILE_MODES:
        assert mode in message


def _cleanup_conductor(cluster_state, reconcile: str, *, baseline_usable: bool = True) -> Conductor:
    """A conductor whose cleanup has nothing left to do but reconcile."""
    conductor = Conductor.__new__(Conductor)
    conductor.logger = MagicMock()
    conductor.config = SimpleNamespace(enable_noise=False)
    conductor.problem = None
    conductor.cluster_state = cluster_state
    conductor._baseline_captured = True
    conductor._baseline_usable = baseline_usable
    conductor.reconcile = reconcile
    conductor.results = {}
    conductor.submission_stage = "tearing_down"
    conductor._submission_lock = threading.RLock()
    conductor._submission_generation = 1
    conductor._aborted_submission_generations = set()
    conductor._accepting_submissions = True
    conductor._attempt_closed = False
    return conductor


def _logged_lines(conductor: Conductor) -> list[str]:
    return [call.args[0] for call in conductor.logger.info.call_args_list]


def test_cleanup_mode_off_keeps_the_baseline_and_reads_nothing(build_manager):
    manager, _ = build_manager()
    conductor = _cleanup_conductor(manager, RECONCILE_OFF)

    conductor._cleanup_sync()

    manager.core_v1.list_namespace.assert_not_called()
    manager.kubectl.gc_orphan_localpv_dirs.assert_not_called()
    assert (
        "[CLEANUP] Reconcile skipped (off): the baseline is kept for reporting, it is not an ownership map"
        in _logged_lines(conductor)
    )
    assert not conductor.results.get("cleanup_failed")


def test_cleanup_mode_report_lists_every_write_and_makes_none(build_manager):
    expected = build_manager()[0].plan_reconcile()
    manager, custom_api = build_manager()
    conductor = _cleanup_conductor(manager, RECONCILE_REPORT)

    with patch.object(manager, "plan_reconcile", wraps=manager.plan_reconcile) as plan:
        conductor._cleanup_sync()

    plan.assert_called_once_with()
    logged = _logged_lines(conductor)
    reported = [line for line in logged if line.startswith("[CLEANUP] would ")]
    assert reported == [
        f"[CLEANUP] would {mutation.action} {mutation.target} — {mutation.note}" for mutation in expected
    ]
    assert f"[CLEANUP] Reconcile report: {len(expected)} mutations, none applied" in logged
    assert _write_calls(manager, custom_api) == []
    # The LocalPV sweep is one of the reported mutations, never run by a report.
    manager.kubectl.gc_orphan_localpv_dirs.assert_not_called()
    assert not conductor.results.get("cleanup_failed")


def test_cleanup_skips_reconcile_when_the_baseline_is_not_usable(build_manager):
    manager, _ = build_manager()
    conductor = _cleanup_conductor(manager, RECONCILE_ON, baseline_usable=False)

    conductor._cleanup_sync()

    manager.core_v1.list_namespace.assert_not_called()
    manager.kubectl.gc_orphan_localpv_dirs.assert_not_called()
    assert (
        "[CLEANUP] Reconcile skipped: the persisted baseline names namespaces that no longer exist, "
        "so it is neither usable nor replaced" in _logged_lines(conductor)
    )
    assert not conductor.results.get("cleanup_failed")


# --- The two entry points ----------------------------------------------------


class _ReachedTheRestOfTheRun(Exception):
    """Raised by a stub to prove the run was not refused."""


def _load_module(monkeypatch, path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def benchmark_main(monkeypatch):
    module = _load_module(monkeypatch, Path(__file__).resolve().parents[2] / "main.py", "reconcile_modes_main_test")
    monkeypatch.setattr(module, "init_logger", lambda: None)
    monkeypatch.setattr(module, "set_profile", lambda profile: None)
    monkeypatch.setattr(module, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(module.os, "environ", module.os.environ.copy())
    return module


@pytest.fixture
def validator(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integration" / "validate_problem.py"
    module = _load_module(monkeypatch, path, "reconcile_modes_validator_test")
    monkeypatch.setattr(module, "init_logger", lambda: None)
    monkeypatch.setattr(module, "set_profile", lambda profile: None)
    return module


def _benchmark_args(**overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        agent=None,
        internet_access="open",
        container_hardening="on",
        profile="full",
        platform=OPENSHIFT,
        noise=False,
        use_external_harness=False,
        stages=None,
        baseline=None,
        propagation=None,
        reconcile="on",
        confirm_destructive_reconcile=False,
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def test_benchmark_parser_accepts_a_reconcile_mode():
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    completed = subprocess.run(
        [sys.executable, str(main_path), "--reconcile", "report", "--help"],
        capture_output=True,
        text=True,
        cwd=main_path.parent,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--reconcile {on,report,off}" in completed.stdout


def test_benchmark_refuses_reconcile_on_openshift_without_confirmation(benchmark_main, monkeypatch):
    monkeypatch.setattr(benchmark_main, "ConductorConfig", Mock(side_effect=_ReachedTheRestOfTheRun))

    with pytest.raises(SystemExit) as exc_info:
        benchmark_main._run_benchmark(_benchmark_args())

    message = str(exc_info.value)
    assert "--confirm-destructive-reconcile" in message
    assert "--reconcile report" in message
    assert "deletes every object absent from the baseline" in message


def test_benchmark_proceeds_with_the_destructive_confirmation(benchmark_main, monkeypatch):
    monkeypatch.setattr(benchmark_main, "ConductorConfig", Mock(side_effect=_ReachedTheRestOfTheRun))

    with pytest.raises(_ReachedTheRestOfTheRun):
        benchmark_main._run_benchmark(_benchmark_args(confirm_destructive_reconcile=True))


def test_validator_parser_accepts_a_reconcile_mode(validator, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["validate_problem.py", "--problem", "p", "--reconcile", "report"])
    monkeypatch.setattr(validator, "validate", Mock(side_effect=_ReachedTheRestOfTheRun))

    with pytest.raises(_ReachedTheRestOfTheRun):
        validator.main()


def test_validator_refuses_reconcile_on_openshift_without_confirmation(validator, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["validate_problem.py", "--problem", "p", "--platform", "openshift", "--reconcile", "on"]
    )
    monkeypatch.setattr(validator, "validate", Mock(side_effect=_ReachedTheRestOfTheRun))

    with pytest.raises(SystemExit) as exc_info:
        validator.main()

    message = str(exc_info.value)
    assert "--confirm-destructive-reconcile" in message
    assert "--reconcile report" in message
    assert "deletes every object absent from the baseline" in message
    validator.validate.assert_not_called()


def test_validator_proceeds_with_the_destructive_confirmation(validator, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_problem.py",
            "--problem",
            "p",
            "--platform",
            "openshift",
            "--reconcile",
            "on",
            "--confirm-destructive-reconcile",
        ],
    )
    monkeypatch.setattr(validator, "validate", Mock(side_effect=_ReachedTheRestOfTheRun))

    with pytest.raises(_ReachedTheRestOfTheRun):
        validator.main()

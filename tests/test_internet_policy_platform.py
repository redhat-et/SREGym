"""Filtered internet access is refused on OpenShift, and open access says so.

Filtered access is a Calico feature: the boundary is a GlobalNetworkPolicy in the
`adminnetworkpolicy` tier, and OpenShift runs OVN-Kubernetes, which has no such
tier. So the run must stop at argument resolution with one line instead of
aborting later inside `cluster_egress.start()` with a Calico capability message.

Two things fall out of that, and both are asserted here:

* `claudecode_radar` sets `cluster_access: false`, which the container runner
  serves only under the filtered boundary, so that agent has no valid mode on
  this platform and the refusal names it instead of leaving a confusing failure;
* the mode an attempt actually ran under is recorded in its phase ledger, never as
  a result column, because per-attempt headers are compared between runs.
"""

import csv
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.conductor import Conductor
from sregym.phases import PhaseLedger, read_ledger, results_columns

MODULE_NAME = "sregym_benchmark_main_for_internet_policy_test"


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _args(**overrides):
    values = {
        "platform": OPENSHIFT,
        "internet_access": "filtered",
        "agent": "stratus",
        "confirm_destructive_reconcile": False,
        "model": "gpt-5",
        "judge_model": None,
        "use_external_harness": False,
        "force_build": False,
        "profile": "full",
        "noise": False,
        "container_hardening": "on",
        "stages": None,
        "problem": None,
        "suite": None,
        "n_attempts": 1,
        "agent_timeout": 60,
        "resume": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _prepare_run(monkeypatch, tmp_path):
    """Load main.py by path and stub everything a resolved run reaches next.

    The seams are deliberate: `Conductor` construction and
    `LAUNCHER.enable_container_isolation` are the first cluster and container
    steps after argument resolution, and the driver thread records the keyword
    arguments main.py handed it, so a run that proceeds proves the mode reached
    the attempt.
    """
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main, "_driver_base_dir", None)
    launcher = MagicMock()
    launcher._container_runner = None
    conductor = MagicMock()
    conductor_class = MagicMock(return_value=conductor)
    preflight = MagicMock()
    driver_calls = []
    monkeypatch.setattr(benchmark_main, "LAUNCHER", launcher)
    monkeypatch.setattr(benchmark_main, "Conductor", conductor_class)
    monkeypatch.setattr(benchmark_main, "run_preflight_check", preflight)
    monkeypatch.setattr(benchmark_main, "_run_driver_and_shutdown", lambda *a, **kw: driver_calls.append(kw))
    monkeypatch.setattr(benchmark_main, "run_api", lambda _conductor: None)
    monkeypatch.setattr(benchmark_main, "get_container_host_bind_address", lambda: "172.17.0.1")
    return benchmark_main, launcher, conductor_class, preflight, driver_calls


def test_openshift_refuses_filtered_internet_access_before_any_container_work(monkeypatch, tmp_path):
    benchmark_main, launcher, conductor_class, preflight, driver_calls = _prepare_run(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        benchmark_main._run_benchmark(_args())

    message = str(exit_info.value.code)
    # One line, not a traceback: the same shape as the reconcile refusal.
    assert "\n" not in message
    assert "--internet-access open" in message
    assert "adminnetworkpolicy" in message
    assert "Calico" in message
    # The isolation guarantee is stated as lost, so no result is read as filtered.
    assert "not comparable with a kind result on isolation grounds" in message
    launcher.enable_container_isolation.assert_not_called()
    launcher.set_internet_policy.assert_not_called()
    preflight.assert_not_called()
    conductor_class.assert_not_called()
    assert driver_calls == []


def test_openshift_runs_with_open_internet_access_and_records_the_mode(monkeypatch, tmp_path, caplog):
    benchmark_main, launcher, conductor_class, preflight, driver_calls = _prepare_run(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO):
        results = benchmark_main._run_benchmark(_args(internet_access="open", use_external_harness=False))

    assert results == []
    conductor_class.assert_called_once()
    launcher.enable_container_isolation.assert_called_once()
    assert driver_calls and driver_calls[0]["internet_access"] == "open"
    startup = [record.getMessage() for record in caplog.records if "Config —" in record.getMessage()]
    assert startup, "no startup configuration line was logged"
    assert "internet_access: open" in startup[0]
    assert f"platform: {OPENSHIFT}" in startup[0]


def test_openshift_refusal_names_the_agent_that_has_no_valid_mode(monkeypatch, tmp_path):
    benchmark_main, launcher, conductor_class, _, _ = _prepare_run(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        benchmark_main._run_benchmark(_args(agent="claudecode_radar"))

    message = str(exit_info.value.code)
    assert "\n" not in message
    assert "'claudecode_radar'" in message
    assert "cluster_access: false" in message
    assert "no valid mode here" in message
    # Telling this agent to re-run with --internet-access open would fail in the
    # container runner, so the refusal does not offer it.
    assert "--internet-access open" not in message
    launcher.enable_container_isolation.assert_not_called()
    conductor_class.assert_not_called()


def test_kind_keeps_running_filtered_internet_access(monkeypatch, tmp_path):
    benchmark_main, launcher, conductor_class, preflight, driver_calls = _prepare_run(monkeypatch, tmp_path)

    benchmark_main._run_benchmark(_args(platform=KIND))

    conductor_class.assert_called_once()
    launcher.enable_container_isolation.assert_called_once()
    preflight.assert_called_once()
    assert driver_calls and driver_calls[0]["internet_access"] == "filtered"


def test_openshift_external_harness_run_is_not_refused_for_a_boundary_it_never_builds(monkeypatch, tmp_path):
    benchmark_main, launcher, conductor_class, _, driver_calls = _prepare_run(monkeypatch, tmp_path)

    benchmark_main._run_benchmark(_args(use_external_harness=True))

    conductor_class.assert_called_once()
    # No proxy, no agent, no container: nothing would have built the boundary.
    launcher.enable_container_isolation.assert_not_called()
    assert driver_calls and driver_calls[0]["internet_access"] == "filtered"


def _conductor(monkeypatch, platform, *, filtered):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.internet_policy_platform")
    conductor.config = SimpleNamespace(internet_policy=SimpleNamespace(is_filtered=filtered))
    conductor.cluster_egress = MagicMock()
    conductor.k8s_proxy = MagicMock()
    conductor.k8s_proxy.hidden_namespaces = set()
    conductor.k8s_proxy.generate_agent_kubeconfig.return_value = "/tmp/agent-kubeconfig"
    conductor._agent_kubeconfig_path = None
    conductor.kubectl = MagicMock()
    conductor.kubectl.list_namespaces.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="openshift-monitoring"))]
    )
    return conductor


def test_start_k8s_proxy_refuses_filtered_internet_access_on_openshift(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, filtered=True)

    with pytest.raises(RuntimeError) as raised:
        conductor.start_k8s_proxy()

    message = str(raised.value)
    assert "adminnetworkpolicy" in message
    assert "--internet-access open" in message
    conductor.cluster_egress.start.assert_not_called()
    conductor.k8s_proxy.start.assert_not_called()


def test_start_k8s_proxy_on_openshift_never_removes_the_egress_boundary(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, OPENSHIFT, filtered=False)

    with caplog.at_level(logging.DEBUG, logger="test.internet_policy_platform"):
        conductor.start_k8s_proxy()

    conductor.cluster_egress.start.assert_not_called()
    # The delete returns 403 under a restricted identity, and cluster_egress.stop()
    # raises on anything but 404, so the call is skipped rather than tolerated.
    conductor.cluster_egress.stop.assert_not_called()
    conductor.k8s_proxy.start.assert_called_once_with()
    assert any("no cluster egress boundary to remove" in record.getMessage() for record in caplog.records)


def test_start_k8s_proxy_failure_on_openshift_does_not_remove_the_egress_boundary(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, filtered=False)
    conductor.k8s_proxy.start.side_effect = RuntimeError("port already bound")

    with pytest.raises(RuntimeError, match="port already bound"):
        conductor.start_k8s_proxy()

    conductor.cluster_egress.stop.assert_not_called()


def test_start_k8s_proxy_on_kind_still_clears_the_egress_boundary(monkeypatch):
    conductor = _conductor(monkeypatch, KIND, filtered=False)
    conductor.k8s_proxy.start.side_effect = RuntimeError("port already bound")

    with pytest.raises(RuntimeError, match="port already bound"):
        conductor.start_k8s_proxy()

    # Unchanged kind behaviour: an open run clears a boundary an interrupted
    # filtered run may have left, before the proxy starts and again if it fails.
    assert conductor.cluster_egress.stop.call_count == 2


def test_start_k8s_proxy_on_kind_starts_the_boundary_when_filtered(monkeypatch):
    conductor = _conductor(monkeypatch, KIND, filtered=True)

    conductor.start_k8s_proxy()

    conductor.cluster_egress.start.assert_called_once_with()
    conductor.cluster_egress.stop.assert_not_called()
    conductor.k8s_proxy.start.assert_called_once_with()


def test_the_attempt_records_the_mode_in_its_ledger_and_adds_no_result_column(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        bind_phase_ledger=Mock(),
        clear_cluster_egress_boundary=Mock(),
        start_problem=AsyncMock(side_effect=RuntimeError("deploy failed")),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
        abandon_submission_work=Mock(),
        record_incomplete_attempt=Mock(),
        problem=SimpleNamespace(cleanup_timeout_seconds=60),
    )

    results = benchmark_main.driver_loop(conductor, use_external_harness=True, internet_access="open")

    context = conductor.bind_phase_ledger.call_args.kwargs
    assert context["internet_access"] == "open"
    # Not a result column: the per-attempt header is compared between runs.
    assert "internet_access" not in results[0][None][0]
    published = list(tmp_path.rglob("*.csv"))
    assert published
    with published[0].open(newline="") as handle:
        header = next(csv.reader(handle))
    assert "deployment_profile" in header
    assert "internet_access" not in header
    # The same context keys that produced that row produce no column here either.
    ledger_path = tmp_path / "phases_attempt1.jsonl"
    ledger = PhaseLedger(ledger_path, context={"problem_id": "problem", **context})
    with ledger.phase("deploy"):
        pass
    records = read_ledger(ledger_path)
    assert records[0]["internet_access"] == "open"
    assert not [key for key in results_columns(records) if "internet_access" in key]

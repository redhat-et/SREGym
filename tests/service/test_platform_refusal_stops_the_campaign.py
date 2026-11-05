"""A platform refusal ends the campaign; it never starts teardown.

`verify_platform` refuses when the declared platform is not confirmed by the
cluster. That is a decision about the cluster, not a transient deploy failure, so
main.py's retry loop must neither retry it nor run `finish_problem_with_deadline`,
whose recovery and application cleanup would write to the very cluster the check
just declared off-limits.

It also ends the campaign. One refusal reported per problem and per attempt reads
like a broken benchmark rather than a wrong cluster, so the loop keeps the single
row it wrote and raises `BenchmarkCampaignAborted`, which the existing handler
reports and turns into a non-zero exit.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

REFUSAL = "This cluster is OpenShift, but the platform resolved to 'kind'."


def _load_main_module():
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_refusal_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _conductor(benchmark_main, error):
    return SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        bind_phase_ledger=Mock(),
        clear_cluster_egress_boundary=Mock(),
        start_problem=AsyncMock(side_effect=error),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
        abandon_submission_work=Mock(),
        record_incomplete_attempt=Mock(),
    )


def _refusal(benchmark_main):
    # Read through main.py's own binding: tests/test_cluster_platform.py reloads the
    # module, which produces a second class object, and main.py's isinstance check uses
    # the binding it imported.
    return benchmark_main.PlatformVerificationError(REFUSAL)


def test_a_platform_refusal_is_attempted_once_and_records_the_failure(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    conductor = _conductor(benchmark_main, _refusal(benchmark_main))

    # The loop stops the campaign instead of walking on to the next attempt.
    with pytest.raises(benchmark_main.BenchmarkCampaignAborted) as raised:
        benchmark_main.driver_loop(conductor, use_external_harness=True)

    conductor.start_problem.assert_awaited_once()
    assert raised.value.partial_results == [
        {None: [{"problem_id": "problem", "attempt": 1, "deployment_profile": "full", "deploy_failed": True}]}
    ]
    assert "does not confirm the declared platform" in str(raised.value)


def test_a_platform_refusal_runs_no_recovery_or_teardown(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    conductor = _conductor(benchmark_main, _refusal(benchmark_main))

    with pytest.raises(benchmark_main.BenchmarkCampaignAborted):
        benchmark_main.driver_loop(conductor, use_external_harness=True)

    conductor.finish_problem_in_background.assert_not_called()
    conductor.wait_for_submission_work.assert_not_awaited()
    conductor.abandon_submission_work.assert_not_called()
    conductor.record_incomplete_attempt.assert_not_called()


def test_a_generic_deploy_failure_still_retries_and_cleans_up(monkeypatch, tmp_path):
    # The refusal handling must not swallow the transient-failure path.
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    conductor = _conductor(benchmark_main, RuntimeError("connection reset"))
    conductor.problem = SimpleNamespace(cleanup_timeout_seconds=60)

    benchmark_main.driver_loop(conductor, use_external_harness=True)

    assert conductor.start_problem.await_count == 3
    assert conductor.finish_problem_in_background.call_count == 3

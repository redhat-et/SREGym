"""The end of a campaign: teardown only when asked, and a verdict that can fail the run.

The order is the property under test. The damage verdict is given what the teardown removed, so
a teardown the run performed itself cannot read as damage; the verdict must therefore run after
it. And a plain local campaign does neither, which is what keeps kind behaviour unchanged.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.damage_report import CLEAN, DAMAGED, OWNED, DamageReport


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_finish_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _conductor(verdict=CLEAN):
    return SimpleNamespace(
        teardown_observers=Mock(return_value=["clusterrole/mcp-server", "namespace/observe"]),
        cluster_damage_verdict=Mock(return_value=DamageReport(verdict=verdict)),
    )


def test_a_plain_local_campaign_tears_nothing_down_and_judges_nothing(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    conductor = _conductor()

    benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=False, results=[{"agent": []}])

    conductor.teardown_observers.assert_not_called()
    conductor.cluster_damage_verdict.assert_not_called()
    assert not (tmp_path / "damage.json").exists()


def test_teardown_runs_before_the_verdict_and_hands_it_what_it_removed(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    conductor = _conductor()

    benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=True, results=[{"agent": []}])

    conductor.teardown_observers.assert_called_once()
    assert conductor.cluster_damage_verdict.call_args.kwargs["deleted_by_us"] == (
        "clusterrole/mcp-server",
        "namespace/observe",
    )
    written = json.loads((tmp_path / "damage.json").read_text())
    assert written["verdict"] == CLEAN


def test_an_openshift_campaign_judges_the_cluster_without_tearing_down(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    conductor = _conductor(verdict=OWNED)

    benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=False, results=[{"agent": []}])

    conductor.teardown_observers.assert_not_called()
    assert conductor.cluster_damage_verdict.call_args.kwargs["deleted_by_us"] == ()
    assert json.loads((tmp_path / "damage.json").read_text())["verdict"] == OWNED


def test_a_damaged_cluster_fails_the_campaign_and_keeps_the_results(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    conductor = _conductor(verdict=DAMAGED)
    rows = [{"agent": [{"problem_id": "p", "run_status": "complete"}]}]

    with pytest.raises(benchmark_main.BenchmarkCampaignAborted) as raised:
        benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=False, results=rows)

    assert raised.value.partial_results == rows
    assert "DAMAGED" in str(raised.value)
    assert (tmp_path / "damage.json").exists(), "the evidence survives the failure"


def test_the_survivor_check_is_on_only_when_a_teardown_was_asked_for(monkeypatch, tmp_path):
    """Without a teardown the observer stack staying up is the requested state, not a leftover."""
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    conductor = _conductor()

    benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=True, results=[{"agent": []}])

    _, kwargs = conductor.cluster_damage_verdict.call_args
    assert kwargs["after_teardown"] is True
    assert kwargs["deleted_by_us"] == ("clusterrole/mcp-server", "namespace/observe")


def test_an_openshift_campaign_without_teardown_judges_without_the_survivor_check(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    conductor = _conductor()

    benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=False, results=[{"agent": []}])

    _, kwargs = conductor.cluster_damage_verdict.call_args
    assert kwargs["after_teardown"] is False


def test_a_leftover_after_teardown_fails_the_campaign(monkeypatch, tmp_path):
    """The survivor verdict must not be swallowed: it ends the campaign like any damage."""
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    leftover = DamageReport(verdict=DAMAGED, survivors=["clusterrole/configmap-cleanup-controller"])
    conductor = _conductor()
    conductor.cluster_damage_verdict = Mock(return_value=leftover)

    with pytest.raises(benchmark_main.BenchmarkCampaignAborted):
        benchmark_main.finish_campaign(conductor, tmp_path, teardown_observers=True, results=[])

    written = json.loads((tmp_path / "damage.json").read_text())
    assert written["verdict"] == DAMAGED
    assert written["survivors"] == ["clusterrole/configmap-cleanup-controller"]


def test_an_aborted_campaign_still_judges_the_cluster(monkeypatch, tmp_path):
    """An abort is when the cluster state is least known, so it must still be judged."""
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    monkeypatch.setattr(benchmark_main, "_driver_base_dir", tmp_path)
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(baseline=object())

    benchmark_main.judge_cluster_after_abort(conductor, teardown_observers=False, abort_reason="interrupted")

    conductor.cluster_damage_verdict.assert_called_once_with()
    conductor.teardown_observers.assert_not_called()
    assert (tmp_path / "damage.json").exists()


def test_an_aborted_run_without_a_baseline_invents_no_verdict(monkeypatch, tmp_path):
    """Comparing against the cluster's already-damaged self would be a pass, not a check."""
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    monkeypatch.setattr(benchmark_main, "_driver_base_dir", tmp_path)
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(baseline=None)

    benchmark_main.judge_cluster_after_abort(conductor, teardown_observers=False, abort_reason="interrupted")

    conductor.cluster_damage_verdict.assert_not_called()
    assert not (tmp_path / "damage.json").exists()


def test_a_plain_kind_abort_is_judged_only_when_teardown_was_asked_for(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    monkeypatch.setattr(benchmark_main, "_driver_base_dir", tmp_path)
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(baseline=object())

    benchmark_main.judge_cluster_after_abort(conductor, teardown_observers=False, abort_reason="interrupted")
    conductor.cluster_damage_verdict.assert_not_called()

    benchmark_main.judge_cluster_after_abort(conductor, teardown_observers=True, abort_reason="interrupted")
    conductor.cluster_damage_verdict.assert_called_once_with()


def test_a_run_already_judged_is_not_judged_again_after_the_interrupt(monkeypatch, tmp_path):
    """A second verdict would compare against a baseline taken before the teardown it ran."""
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    monkeypatch.setattr(benchmark_main, "_driver_base_dir", tmp_path)
    conductor = _conductor()
    conductor.cluster_state = SimpleNamespace(baseline=object())
    conductor._damage_verdict_given = True

    benchmark_main.judge_cluster_after_abort(conductor, teardown_observers=True, abort_reason="interrupted")

    conductor.cluster_damage_verdict.assert_not_called()

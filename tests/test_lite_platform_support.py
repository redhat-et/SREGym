"""The OpenShift support table, and the suite refusal it drives.

``PLATFORM_SUPPORT`` is the harness's support table: one verdict per Lite scenario, and
``main.platform_support_skips`` reads it before any cluster work. These tests pin the
table's shape and the two behaviours that read it. ``kind`` must stay silent: its output
is compared against recorded transcripts.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from sregym.conductor.problem_sets import (
    PLATFORM_SUPPORT,
    PLATFORM_VERDICTS,
    SREGYM_LITE_IA_PROBLEMS,
    SREGYM_LITE_PROBLEMS,
)

LITE_SETS = {
    "sregym-lite": SREGYM_LITE_PROBLEMS,
    "sregym-lite-ia": SREGYM_LITE_IA_PROBLEMS,
}

# One per set, and the same fault in both: it rewrites kube-system/coredns, which
# OpenShift does not serve DNS from.
# Scenarios proven end to end on a real cluster: all 16 runnable Lite ones, the Incident Arena ports none.
WORKS = {"sregym-lite": 16, "sregym-lite-ia": 0}

EXCLUDED = {
    "sregym-lite": "service_dns_resolution_failure_social_network",
    "sregym-lite-ia": "service_dns_resolution_failure_slack_spine",
}

ALL_PROBLEMS = tuple(SREGYM_LITE_PROBLEMS) + tuple(SREGYM_LITE_IA_PROBLEMS)


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_platform_support", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def benchmark_main():
    return _load_main_module()


@pytest.fixture
def on_kind(benchmark_main, monkeypatch):
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: False)
    return benchmark_main


@pytest.fixture
def on_openshift(benchmark_main, monkeypatch):
    monkeypatch.setattr(benchmark_main, "is_openshift", lambda: True)
    return benchmark_main


def test_every_problem_in_both_sets_has_exactly_one_entry():
    assert set(PLATFORM_SUPPORT) == set(ALL_PROBLEMS)


@pytest.mark.parametrize("problem", ALL_PROBLEMS)
def test_every_verdict_is_one_of_the_three(problem):
    assert PLATFORM_SUPPORT[problem]["verdict"] in PLATFORM_VERDICTS


@pytest.mark.parametrize("suite", LITE_SETS)
def test_each_set_excludes_exactly_the_dns_fault(suite):
    excluded = [problem for problem in LITE_SETS[suite] if PLATFORM_SUPPORT[problem]["verdict"] == "exclude"]
    assert excluded == [EXCLUDED[suite]]


@pytest.mark.parametrize("problem", ALL_PROBLEMS)
def test_every_non_working_problem_names_its_blocker(problem):
    entry = PLATFORM_SUPPORT[problem]
    if entry["verdict"] == "works":
        assert entry["reason"]
        return
    assert entry["reason"].strip()
    assert entry["requires"] is None or entry["requires"].strip()
    assert set(entry) == {"verdict", "reason", "requires"}


@pytest.mark.parametrize("suite", LITE_SETS)
def test_the_excluded_problem_is_skipped_and_named_on_openshift(on_openshift, capsys, suite):
    """One scenario the platform cannot express skips; the other sixteen still run."""
    skipped = on_openshift.platform_support_skips(suite)

    assert skipped == [EXCLUDED[suite]]
    output = capsys.readouterr().out
    works = WORKS[suite]
    assert f"[openshift] {suite}: {works} works, {16 - works} fixable, 1 exclude" in output
    assert f"[openshift] exclude: {EXCLUDED[suite]}:" in output
    assert output.count("[openshift] fixable: ") == 16 - works
    assert f"Skipping {EXCLUDED[suite]} in {suite}" in output
    assert "The rest of the suite runs" in output


def test_a_suite_with_nothing_excluded_runs_whole(on_openshift, monkeypatch, capsys):
    monkeypatch.setitem(
        on_openshift.PROBLEM_SETS,
        "synthetic-lite",
        ("network_policy_block_slack_spine", "env_variable_shadowing_saleor"),
    )

    assert on_openshift.platform_support_skips("synthetic-lite") == []

    output = capsys.readouterr().out
    assert "[openshift] synthetic-lite: 0 works, 2 fixable, 0 exclude" in output
    assert output.count("[openshift] fixable: ") == 2


@pytest.mark.parametrize("suite", LITE_SETS)
def test_kind_says_nothing_at_all(on_kind, capsys, suite):
    assert on_kind.platform_support_skips(suite) == []
    assert capsys.readouterr() == ("", "")


def test_an_unsupported_suite_name_says_nothing(on_openshift, capsys):
    assert on_openshift.platform_support_skips("not-a-suite") == []
    assert on_openshift.platform_support_skips(None) == []
    assert capsys.readouterr() == ("", "")


def test_the_suite_selection_drops_what_the_table_excludes(on_openshift, monkeypatch, capsys):
    """The skip is not a label: the excluded problem must not reach the driver."""
    selection = ["network_policy_block", "namespace_memory_limit"]
    monkeypatch.setitem(on_openshift.PROBLEM_SETS, "synthetic-lite", tuple(selection))
    monkeypatch.setattr(on_openshift, "is_openshift", lambda: True)

    skipped = on_openshift.platform_support_skips("synthetic-lite")
    filtered = [pid for pid in selection if pid not in skipped]

    assert "network_policy_block" in filtered
    assert "namespace_memory_limit" in filtered

    # And against the real set: the excluded DNS problem is the one that leaves.
    real = list(on_openshift.PROBLEM_SETS["sregym-lite"])
    real_skipped = on_openshift.platform_support_skips("sregym-lite")
    capsys.readouterr()
    assert len([pid for pid in real if pid not in real_skipped]) == len(real) - 1
    assert EXCLUDED["sregym-lite"] in real_skipped


def test_the_skip_reason_reaches_the_log_and_not_only_the_terminal(on_openshift, caplog):
    """operations.md calls the log file 'the whole run', so the skip belongs in it."""
    import logging

    with caplog.at_level(logging.WARNING):
        on_openshift.platform_support_skips("sregym-lite")

    assert "Skipping" in caplog.text
    assert EXCLUDED["sregym-lite"] in caplog.text


def test_a_suite_whose_every_scenario_is_excluded_is_refused(on_openshift):
    """Zero problems must not exit 0 and look like a successful suite run."""
    selection = (EXCLUDED["sregym-lite"],)
    skips = on_openshift.platform_support_skips("sregym-lite")

    with pytest.raises(ValueError, match="every scenario in it is excluded"):
        on_openshift.apply_platform_skips("sregym-lite", selection, skips)


def test_a_selection_that_keeps_something_is_returned_without_the_skips(on_openshift):
    selection = list(on_openshift.PROBLEM_SETS["sregym-lite"])
    skips = on_openshift.platform_support_skips("sregym-lite")

    kept = on_openshift.apply_platform_skips("sregym-lite", selection, skips)

    assert len(kept) == len(selection) - 1
    assert EXCLUDED["sregym-lite"] not in kept
    assert kept == [pid for pid in selection if pid != EXCLUDED["sregym-lite"]]

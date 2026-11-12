"""Provenance columns: which commit, dirty state, submodule and platform produced a run.

run_provenance() is read once per process and must never raise, so a run on a machine without
git, or without the applications submodule, still records "unknown" rather than failing.
"""

import hashlib
import subprocess

import pytest

from sregym import cluster_platform, run_artifacts
from sregym.run_artifacts import run_provenance

KEYS = {
    "run.git_sha",
    "run.git_dirty",
    "run.submodule_sha",
    "run.submodule_dirty",
    "run.submodule_diff_sha",
    "run.platform",
}


@pytest.fixture(autouse=True)
def fresh_cache():
    run_provenance.cache_clear()
    yield
    run_provenance.cache_clear()


def _fake_git(outputs, calls):
    """Stand-in for subprocess.run keyed on the git arguments, recording every call."""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("cwd")))
        stdout = outputs.get(tuple(argv[1:]), "")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return fake_run


def test_keys_are_present_and_platform_comes_from_the_platform_accessor(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", "openshift")
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git({}, []))

    provenance = run_provenance()

    assert set(provenance) == KEYS
    assert provenance["run.platform"] == "openshift"


def test_values_come_from_git_and_dirty_means_non_empty_porcelain(monkeypatch):
    outputs = {
        ("rev-parse", "HEAD"): "abc123\n",
        ("status", "--porcelain", "--untracked-files=no", "--ignore-submodules=dirty"): " M main.py\n",
    }
    calls = []
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git(outputs, calls))

    provenance = run_provenance()

    assert provenance["run.git_sha"] == "abc123"
    assert provenance["run.git_dirty"] == "true"
    assert provenance["run.submodule_dirty"] == "false"
    assert calls[0][1] == run_artifacts._REPO_ROOT
    assert calls[2][1] == run_artifacts._APPLICATIONS_DIR


def test_an_untracked_only_tree_is_not_dirty(monkeypatch):
    """Scratch files do not make a run's code dirty; only tracked changes do."""

    def untracked_only(argv, **kwargs):
        listing = "" if "--untracked-files=no" in argv else "?? scratch.txt\n"
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")

    monkeypatch.setattr(run_artifacts.subprocess, "run", untracked_only)

    assert run_provenance()["run.git_dirty"] == "false"


def test_the_submodule_diff_fingerprint_is_stable_for_the_same_diff(monkeypatch):
    diff = "diff --git a/x.yaml b/x.yaml\n-image: nginx\n+image: docker.io/library/nginx\n"
    calls = []
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git({("diff",): diff}, calls))

    first = run_provenance()["run.submodule_diff_sha"]
    run_provenance.cache_clear()
    second = run_provenance()["run.submodule_diff_sha"]

    expected = hashlib.sha256(diff.strip().encode()).hexdigest()[:12]
    assert first == second == expected
    assert len(first) == 12


def test_a_different_submodule_diff_gets_a_different_fingerprint(monkeypatch):
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git({("diff",): "+a\n"}, []))
    first = run_provenance()["run.submodule_diff_sha"]
    run_provenance.cache_clear()
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git({("diff",): "+b\n"}, []))

    assert run_provenance()["run.submodule_diff_sha"] != first


def test_no_submodule_diff_is_clean(monkeypatch):
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git({}, []))

    assert run_provenance()["run.submodule_diff_sha"] == "clean"


def test_every_git_field_is_unknown_when_git_cannot_run(monkeypatch):
    def missing_git(argv, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(run_artifacts.subprocess, "run", missing_git)

    provenance = run_provenance()

    assert set(provenance) == KEYS
    for key in (
        "run.git_sha",
        "run.git_dirty",
        "run.submodule_sha",
        "run.submodule_dirty",
        "run.submodule_diff_sha",
    ):
        assert provenance[key] == "unknown", key


def test_nonzero_exit_is_unknown_not_clean(monkeypatch):
    def failing_git(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(run_artifacts.subprocess, "run", failing_git)

    provenance = run_provenance()

    assert provenance["run.git_sha"] == "unknown"
    assert provenance["run.git_dirty"] == "unknown"


def test_git_is_queried_once_per_process(monkeypatch):
    calls = []
    outputs = {("rev-parse", "HEAD"): "abc123\n"}
    monkeypatch.setattr(run_artifacts.subprocess, "run", _fake_git(outputs, calls))

    first = run_provenance()
    queried = len(calls)
    second = run_provenance()

    assert queried == 5
    assert len(calls) == queried
    assert first == second

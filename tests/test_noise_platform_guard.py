"""`--noise` is refused on OpenShift, loudly, at every path that installs Chaos Mesh.

Without the guard the install fails quietly (the chaos-daemon is privileged and CRI-O is not
recognised), `_chaos_mesh_ready` stays False, and the run reports a result with no noise in it.
The conductor swallows noise start errors into warnings, so the argument check in main.py is the
guard that makes the flag effective; the install-path raises close the routes that do not use it.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, NOISE_UNAVAILABLE, OPENSHIFT
from sregym.generators.noise import manager

REPO = Path(__file__).resolve().parents[1]

# Every problem that reaches Chaos Mesh. The first installs it itself; the other two build a
# ChaosInjector when CPU_containment is set. None is in a Lite suite, and none needs --noise.
CHAOS_MESH_PROBLEMS = (
    "missing_stale_cache_fallback_astronomy_shop",
    "capacity_decrease_rpc_retry_storm",
    "load_spike_rpc_retry_storm",
)


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _platform(monkeypatch, name):
    monkeypatch.setattr(cluster_platform, "_platform", name)


def _main():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sregym_main_for_noise_guard", REPO / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_only_kind_supports_noise(monkeypatch):
    _platform(monkeypatch, KIND)
    assert cluster_platform.supports_noise() is True
    _platform(monkeypatch, OPENSHIFT)
    assert cluster_platform.supports_noise() is False


def test_the_refusal_names_the_reasons_and_only_applies_to_the_flag(monkeypatch):
    main = _main()
    _platform(monkeypatch, OPENSHIFT)

    text = main.noise_refusal(True)

    assert text is not None and NOISE_UNAVAILABLE in text and "Re-run without --noise" in text
    assert main.noise_refusal(False) is None
    _platform(monkeypatch, KIND)
    assert main.noise_refusal(True) is None


def test_the_flag_stops_the_run_before_any_cluster_call():
    """End to end through the real argument handling: a non-zero exit with the refusal text."""
    done = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--noise",
            "--platform",
            "openshift",
            "--problem",
            "network_policy_block",
            "--internet-access",
            "open",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode != 0
    assert "Refusing --noise on openshift" in done.stdout + done.stderr


def test_the_installer_raises_on_openshift_without_touching_the_cluster(monkeypatch):
    _platform(monkeypatch, OPENSHIFT)
    kubectl = Mock()
    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl", kubectl):
        noise = manager.NoiseManager()
        with pytest.raises(RuntimeError, match="Chaos Mesh is not supported on openshift"):
            noise._ensure_chaos_mesh_installed()

    kubectl.return_value.exec_command.assert_not_called()


def test_the_legacy_injector_raises_on_openshift_before_any_api_call(monkeypatch):
    from sregym.generators.noise.impl import stress_injector

    _platform(monkeypatch, OPENSHIFT)
    with (
        patch.object(stress_injector, "KubeCtl") as kubectl,
        patch.object(stress_injector, "Helm") as helm,
        pytest.raises(RuntimeError, match="Chaos Mesh is not supported on openshift"),
    ):
        stress_injector.ChaosInjector("application")

    kubectl.assert_not_called()
    helm.add_repo.assert_not_called()


@pytest.mark.parametrize("problem", CHAOS_MESH_PROBLEMS)
def test_each_problem_that_reaches_chaos_mesh_is_covered_by_an_install_guard(problem):
    """The three bypass the flag, so each must reach one of the two guarded constructors."""
    sources = {
        "missing_stale_cache_fallback_astronomy_shop": (
            "conductor/problems/missing_stale_cache_fallback_astronomy_shop.py",
            "_ensure_chaos_mesh_installed",
        ),
        "capacity_decrease_rpc_retry_storm": (
            "conductor/problems/capacity_decrease_rpc_retry_storm.py",
            "CPU_containment=True",
        ),
        "load_spike_rpc_retry_storm": ("conductor/problems/load_spike_rpc_retry_storm.py", "CPU_containment=True"),
    }
    path, marker = sources[problem]
    assert marker in (REPO / "sregym" / path).read_text()
    # ...and the constructor that CPU_containment reaches carries the guard.
    injector = (REPO / "sregym/generators/noise/impl/stress_injector.py").read_text()
    assert "require_noise_support()" in injector
    assert "require_noise_support()" in (REPO / "sregym/generators/noise/manager.py").read_text()


def test_kind_is_unchanged(monkeypatch):
    _platform(monkeypatch, KIND)
    cluster_platform.require_noise_support()  # does not raise

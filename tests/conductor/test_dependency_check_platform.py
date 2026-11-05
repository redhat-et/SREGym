"""docker is a local-runtime dependency, not a universal one.

The kind udev preflight, the node-level injectors and the agent container runner
shell out to docker. A validator run against a managed cluster uses none of
them, so requiring docker there fails a run that would otherwise work.
"""

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor, ConductorConfig


class DependencyCheckReached(Exception):
    """Stops start_problem at the dependency check, which is the call under test."""


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


@pytest.fixture
def conductor(monkeypatch):
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.dependency_check_platform")
    conductor.config = ConductorConfig()
    conductor.problem_id = "demo"
    conductor.problem = None
    conductor.app = None
    conductor.results = {}
    conductor.agent_name = None
    conductor.agent_launches_container = False
    conductor.agent_timeout_seconds = None
    conductor.agent_deadline_monotonic = None
    conductor._submission_lock = threading.RLock()
    conductor._submit_future = None
    conductor._pending_submission_stages = {}
    conductor._submission_generation = 1
    conductor._aborted_submission_generations = set()
    conductor._accepting_submissions = True
    conductor._attempt_closed = True
    conductor._platform_verified = False
    conductor.kubectl = MagicMock()
    conductor.problems = SimpleNamespace(
        get_problem_instance=lambda _problem_id: SimpleNamespace(app=SimpleNamespace())
    )
    monkeypatch.setattr(conductor_module, "DetectionOracle", lambda _problem: object())
    return conductor


@pytest.mark.parametrize(
    "platform,agent_name,launches_container,expected",
    [
        (KIND, None, False, ["kubectl", "helm", "docker"]),
        (KIND, "stratus", True, ["kubectl", "helm", "docker"]),
        (KIND, "stratus", False, ["kubectl", "helm", "docker"]),
        (OPENSHIFT, None, False, ["kubectl", "helm"]),
        (OPENSHIFT, "stratus", True, ["kubectl", "helm", "docker"]),
        # An agent registered with container_isolation: false runs on the host, so no
        # container is launched for it and a managed platform needs no docker.
        (OPENSHIFT, "stratus", False, ["kubectl", "helm"]),
    ],
)
def test_required_binaries_follow_platform_and_agent(
    monkeypatch, conductor, platform, agent_name, launches_container, expected
):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor.agent_name = agent_name
    conductor.agent_launches_container = launches_container

    assert conductor.required_binaries() == expected


def test_register_agent_records_whether_a_container_is_launched():
    conductor = Conductor.__new__(Conductor)

    conductor.register_agent("stratus")
    assert conductor.agent_name == "stratus"
    assert conductor.agent_launches_container is True

    conductor.register_agent("radar", container_isolation=False)
    assert conductor.agent_launches_container is False


@pytest.mark.parametrize(
    "platform,agent_name,expected",
    [
        (KIND, None, ["kubectl", "helm", "docker"]),
        (OPENSHIFT, None, ["kubectl", "helm"]),
        (OPENSHIFT, "stratus", ["kubectl", "helm", "docker"]),
    ],
)
def test_start_problem_checks_the_binaries_the_platform_needs(monkeypatch, conductor, platform, agent_name, expected):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    # An OpenShift run must name a storage class: the default one is OpenEBS, and selecting it
    # installs cluster-scoped objects nothing removes, so the run is refused before this point.
    monkeypatch.setenv("SREGYM_STORAGE_CLASS", "gp3-csi")
    if agent_name is not None:
        conductor.register_agent(agent_name)
    conductor.kubectl.openshift_api_present.return_value = platform == OPENSHIFT
    conductor.dependency_check = MagicMock(side_effect=DependencyCheckReached)

    with pytest.raises(DependencyCheckReached):
        asyncio.run(conductor.start_problem())

    conductor.dependency_check.assert_called_once_with(expected)


def test_platform_discovery_runs_once_per_conductor(conductor):
    conductor.kubectl.openshift_api_present.return_value = False

    conductor.verify_platform()
    conductor.verify_platform()

    assert conductor.kubectl.openshift_api_present.call_count == 1
    assert conductor.platform == KIND


def test_a_platform_refusal_fires_before_the_first_cluster_write(monkeypatch, conductor):
    # An OpenShift cluster declared as kind is exactly the case the check refuses, and
    # fix_kubernetes rewrites cluster-wide state, so it must never be reached.
    monkeypatch.setenv("SREGYM_PLATFORM", KIND)
    conductor.kubectl.openshift_api_present.return_value = True
    fix_kubernetes = MagicMock()
    monkeypatch.setattr(conductor, "fix_kubernetes", fix_kubernetes)

    # Read through the module: tests/test_cluster_platform.py reloads it, which makes a
    # second class object, and the refusal is raised from the reloaded module.
    with pytest.raises(cluster_platform.PlatformVerificationError, match="--platform openshift"):
        asyncio.run(conductor.start_problem())

    fix_kubernetes.assert_not_called()
    # Discovery is the only call the check makes; nothing on the cluster was written.
    assert [name for name, _args, _kwargs in conductor.kubectl.mock_calls] == ["openshift_api_present"]


def test_the_validator_uses_the_platform_aware_binary_list():
    # A validator run launches no agent container, so on a managed platform it needs
    # no Docker daemon on the runner host. The list must come from the conductor, not
    # from a literal in the validator.
    source = Path("tests/integration/validate_problem.py").read_text()

    assert 'dependency_check(["kubectl", "helm", "docker"])' not in source
    assert "dependency_check(conductor.required_binaries())" in source
    # The validator drives the stages itself and never calls start_problem, so the
    # platform has to be confirmed here as well, before the first destructive step.
    assert "conductor.verify_platform()" in source

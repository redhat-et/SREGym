"""fix_kubernetes must not repair kind-side components on a managed platform.

The kind repairs patch a Calico CRD, a kube-proxy DaemonSet, kubelet thresholds
and CoreDNS ConfigMap templates. On OpenShift those repairs either fail or hit
objects the platform owns, so the gate has to be visible in the command list and
in the log. The Failed-pod delete differs by platform: kind runs `main`'s cluster-wide
delete, and OpenShift only deletes Failed pods in the problem namespace, because a
cluster-wide delete reaches other tenants.
"""

from types import SimpleNamespace

import pytest

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig
from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM

NAMESPACE = "astronomy-shop"
FAILED_POD_DELETE = f"kubectl delete pods -n {NAMESPACE} --field-selector=status.phase=Failed --ignore-not-found=true"
# `main`'s cluster-wide delete, verbatim.
KIND_FAILED_POD_DELETE = (
    "kubectl delete pods --all-namespaces "
    f"--field-selector=status.phase=Failed,metadata.namespace!={KubeletEvictionThresholdMisconfig.NAMESPACE} "
    "--ignore-not-found=true"
)
SKIPPED_REPAIRS_LOG = "[FIX] Skipping the distribution-specific repairs"


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


class _RecordingLogger:
    """Records messages of the level the test cares about.

    caplog cannot be used here: `logger.init_logger()` installs handlers with
    propagate=False on the `all.*` loggers, so whichever test in the full suite
    runs first decides whether caplog sees anything at all.
    """

    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def debug(self, message):
        pass

    def error(self, message):
        pass


class _FakeKubeCtl:
    def __init__(self, commands):
        self.commands = commands

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        return ""


class _RecordingRemoteOSFaultInjector:
    def __init__(self, calls):
        self.calls = calls

    def recover_kubelet_crash(self):
        self.calls.append("recover_kubelet_crash")

    def recover_disk_pressure_all(self):
        self.calls.append("recover_disk_pressure_all")

    def recover_clock_drift(self):
        self.calls.append("recover_clock_drift")


class _RecordingVirtualizationFaultInjector:
    def __init__(self, calls, namespace):
        self.calls = calls
        self.namespace = namespace

    def recover_daemon_set_image_replacement(self, daemon_set_name):
        self.calls.append(f"recover_daemon_set_image_replacement:{daemon_set_name}")

    def recover_all_nxdomain_templates(self):
        self.calls.append("recover_all_nxdomain_templates")


@pytest.fixture
def ledger(monkeypatch):
    """Collect every repair fix_kubernetes performs, in call order."""
    calls: list[str] = []
    commands: list[str] = []
    monkeypatch.setattr(conductor_module, "KubeCtl", lambda: _FakeKubeCtl(commands))
    return SimpleNamespace(calls=calls, commands=commands)


def _conductor(monkeypatch, ledger, platform):
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = _RecordingLogger()
    conductor.kubectl = _FakeKubeCtl(ledger.commands)
    conductor.problem = SimpleNamespace(namespace=NAMESPACE)
    monkeypatch.setattr(
        conductor_module,
        "VirtualizationFaultInjector",
        lambda namespace: _RecordingVirtualizationFaultInjector(ledger.calls, namespace),
    )
    monkeypatch.setattr(
        conductor_module, "RemoteOSFaultInjector", lambda: _RecordingRemoteOSFaultInjector(ledger.calls)
    )
    monkeypatch.setattr(
        Conductor,
        "_fix_calico_route_reflector_label_drift",
        lambda self: ledger.calls.append("calico_route_reflector_label_drift"),
    )
    monkeypatch.setattr(
        NightlyRebalanceOOM,
        "cleanup_leftover_actor",
        classmethod(lambda cls: ledger.calls.append("nightly_rebalance_oom_actor")),
    )
    return conductor


@pytest.mark.parametrize(
    "marker",
    ["--all-namespaces", "calico-node", "ippool", "bgppeer", "ipamconfig", "coredns", "docker exec"],
)
def test_openshift_runs_no_distribution_specific_command(monkeypatch, ledger, marker):
    conductor = _conductor(monkeypatch, ledger, OPENSHIFT)

    conductor.fix_kubernetes()

    assert not [command for command in ledger.commands if marker in command.lower()]


def test_openshift_runs_no_distribution_specific_repair(monkeypatch, ledger):
    conductor = _conductor(monkeypatch, ledger, OPENSHIFT)

    conductor.fix_kubernetes()

    assert ledger.calls == ["nightly_rebalance_oom_actor", "recover_clock_drift"]


def test_openshift_deletes_failed_pods_only_in_the_problem_namespace(monkeypatch, ledger):
    conductor = _conductor(monkeypatch, ledger, OPENSHIFT)

    conductor.fix_kubernetes()

    assert ledger.commands == [FAILED_POD_DELETE]
    assert "metadata.namespace!=" not in FAILED_POD_DELETE


def test_openshift_says_in_one_line_which_repairs_it_skipped(monkeypatch, ledger):
    conductor = _conductor(monkeypatch, ledger, OPENSHIFT)

    conductor.fix_kubernetes()

    skipped = [message for message in conductor.logger.infos if message.startswith(SKIPPED_REPAIRS_LOG)]
    assert len(skipped) == 1
    for repair in ("kube-proxy", "kubelet crash", "disk-pressure", "Calico", "route-reflector", "CoreDNS"):
        assert repair in skipped[0]


def test_kind_still_runs_every_distribution_specific_repair(monkeypatch, ledger):
    conductor = _conductor(monkeypatch, ledger, KIND)

    conductor.fix_kubernetes()

    assert ledger.calls == [
        "recover_daemon_set_image_replacement:kube-proxy",
        "recover_kubelet_crash",
        "recover_disk_pressure_all",
        "calico_route_reflector_label_drift",
        "recover_all_nxdomain_templates",
        "nightly_rebalance_oom_actor",
        "recover_clock_drift",
    ]
    assert [command for command in ledger.commands if "kubectl patch ippool" in command]
    assert not [message for message in conductor.logger.infos if message.startswith(SKIPPED_REPAIRS_LOG)]


def test_kind_deletes_failed_pods_cluster_wide_as_main_does(monkeypatch, ledger):
    conductor = _conductor(monkeypatch, ledger, KIND)

    conductor.fix_kubernetes()

    assert KIND_FAILED_POD_DELETE in ledger.commands
    assert FAILED_POD_DELETE not in ledger.commands

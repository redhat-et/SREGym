"""The capacity preflight refuses before the pods exist, and does nothing on `kind`.

The footprint is the documented per-profile table, so these tests pin the arithmetic
around it: 34 pods, 3400m and 8704Mi under the `full` profile, and the storage the same
clusters' charts claim -- 26 GiB with Loki under `full`, 24 GiB under `svelte`, less
when the run skips Loki. The verdict is taken once per campaign, so the last tests
drive two problems through `start_problem` on one conductor and count the scans.
"""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym import cluster_platform, profile
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.service.capacity_preflight import (
    CapacityShortfall,
    check_capacity,
    footprint,
    untainted_allocatable,
)

NAMESPACE = "hotel-reservation"
MODULE_LOGGER = "all.sregym.capacity_preflight"


@pytest.fixture(autouse=True)
def reset_platform_and_profile(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)
    monkeypatch.setattr(profile, "_profile", None)
    monkeypatch.delenv("SREGYM_PROFILE", raising=False)


def _node(name, cpu="4000m", memory="16Gi", taints=None):
    spec = {"taints": taints} if taints else {}
    return {"metadata": {"name": name}, "spec": spec, "status": {"allocatable": {"cpu": cpu, "memory": memory}}}


def _kubectl(*nodes, quotas=None):
    kubectl = MagicMock()
    kubectl.exec_command.return_value = json.dumps({"kind": "NodeList", "items": list(nodes)})
    kubectl.get_resource_quotas.return_value = quotas or []
    return kubectl


def _quota(name, **hard):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), spec=SimpleNamespace(hard=hard))


def _conductor(monkeypatch, platform, kubectl) -> Conductor:
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.capacity_preflight")
    conductor.config = ConductorConfig()
    conductor.kubectl = kubectl
    conductor.problem = SimpleNamespace(namespace=NAMESPACE, app=SimpleNamespace(namespaces=[NAMESPACE]))
    conductor.prometheus = SimpleNamespace(namespace="observe")
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    return conductor


def _messages(caplog, level):
    return [
        record.getMessage() for record in caplog.records if record.name == MODULE_LOGGER and record.levelno >= level
    ]


class ObserversDeployed(Exception):
    """Stops `deploy_app` at the first observer deploy, right after the capacity preflight."""


def _problem():
    return SimpleNamespace(
        namespace=NAMESPACE,
        app=SimpleNamespace(namespaces=[NAMESPACE], namespace=NAMESPACE, deploy=MagicMock()),
        baseline_duration_s=0,
    )


def _campaign_conductor(monkeypatch, platform, *, kubectl=None) -> Conductor:
    """A conductor that runs `start_problem` up to the capacity preflight, without a cluster.

    Only the steps between `start_problem` and `deploy_app`'s preflight are stubbed, and
    `deploy_app` itself is the real one: the call site is what these tests are about.
    """
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.capacity_preflight")
    conductor.config = ConductorConfig()
    conductor.kubectl = kubectl if kubectl is not None else _kubectl(_node("worker-a"), _node("worker-b"))
    conductor.kubectl.assert_pod_networking = MagicMock()
    conductor.kubectl.openshift_api_present.return_value = platform == OPENSHIFT
    conductor.problems = SimpleNamespace(get_problem_instance=lambda _problem_id: _problem())
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
    conductor._capacity_verified = False
    conductor._baseline_captured = True
    conductor._baseline_usable = True
    for name in (
        "_verify_scc_grant",
        "dependency_check",
        "get_problem_stages",
        "_build_stage_sequence",
        "_preflight_active_oracles",
        "_ensure_metrics_api",
        "verify_storage_class",
        "fix_kubernetes",
        "undeploy_app",
    ):
        setattr(conductor, name, MagicMock())
    conductor.prometheus = SimpleNamespace(namespace="observe", deploy=MagicMock(side_effect=ObserversDeployed))
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    monkeypatch.setattr(conductor_module, "DetectionOracle", lambda _problem: object())
    # A cluster-provided storage class: the OpenEBS block, which installs an operator,
    # has nothing to do with the capacity preflight this run reaches next.
    monkeypatch.setattr(conductor_module, "uses_openebs", lambda: False)
    return conductor


def test_the_footprint_is_the_documented_table():
    # 19 application pods plus about 15 observer pods (OPENSHIFT-READINESS.md §13.4),
    # each allowed the request jaeger.yaml and loki-values.yaml declare.
    full = footprint("full")
    assert (full.pods, full.cpu_millicores, full.memory_mib) == (34, 3400, 8704)
    svelte = footprint("svelte")
    assert (svelte.pods, svelte.cpu_millicores, svelte.memory_mib) == (32, 3200, 8192)


@pytest.mark.parametrize(
    "profile_name,deploy_loki,storage_gib,claims,components",
    [
        # 8 GiB Prometheus + 2 GiB Alertmanager + 6 app volumes + 10 GiB Loki.
        ("full", True, 26, 9, "8Gi Prometheus + 2Gi Alertmanager + 6Gi application volumes + 10Gi Loki = 26Gi"),
        ("full", False, 16, 8, "8Gi Prometheus + 2Gi Alertmanager + 6Gi application volumes = 16Gi"),
        # svelte deploys no Alertmanager (observer/prometheus/values-svelte.yaml), which
        # is why its pod count is two lower as well.
        ("svelte", True, 24, 8, "8Gi Prometheus + 6Gi application volumes + 10Gi Loki = 24Gi"),
        ("svelte", False, 14, 7, "8Gi Prometheus + 6Gi application volumes = 14Gi"),
    ],
)
def test_the_storage_estimate_follows_the_profile_and_the_loki_flag(
    profile_name, deploy_loki, storage_gib, claims, components
):
    need = footprint(profile_name, deploy_loki=deploy_loki)

    assert need.storage_gib == storage_gib
    assert need.persistentvolumeclaims == claims
    assert need.storage_sentence == components
    assert sum(gib for _name, gib in need.storage_components) == need.storage_gib


def test_a_tainted_node_is_not_counted():
    kubectl = _kubectl(
        _node("worker-a"),
        _node("control-plane-0", cpu="8000m", memory="32Gi", taints=[{"key": "node-role.kubernetes.io/master"}]),
    )

    allocatable = untainted_allocatable(kubectl)

    assert allocatable.nodes == ("worker-a",)
    assert (allocatable.cpu_millicores, allocatable.memory_mib) == (4000, 16384)


def test_an_unreadable_node_list_is_not_a_verdict():
    kubectl = MagicMock()
    kubectl.exec_command.return_value = "Error from server (Forbidden): nodes is forbidden"

    assert untainted_allocatable(kubectl) is None


def test_a_run_that_fits_says_nothing(caplog):
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"))

    check_capacity(kubectl, [NAMESPACE])

    assert caplog.records == []


def test_a_run_that_does_not_fit_is_refused_with_the_shortfall():
    kubectl = _kubectl(_node("worker-a", cpu="2000m", memory="8Gi"))

    with pytest.raises(CapacityShortfall) as error:
        check_capacity(kubectl, [NAMESPACE])

    message = str(error.value)
    assert "34 pods" in message
    assert "CPU 3400m of 2000m available" in message
    assert "memory 8704Mi of 8192Mi available" in message
    assert "1 untainted worker node(s)" in message


def test_a_run_above_eighty_percent_warns_but_proceeds(caplog):
    # 3400m of 4000m is 85 %; 8704Mi of 16384Mi is well under the threshold.
    kubectl = _kubectl(_node("worker-a"))

    check_capacity(kubectl, [NAMESPACE])

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "above 80%" in warnings[0]
    assert "CPU 3400m of 4000m available" in warnings[0]


def test_a_storage_quota_below_the_estimate_is_refused_with_the_arithmetic():
    quotas = [_quota("run-quota", **{"requests.storage": "20Gi"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    with pytest.raises(CapacityShortfall) as error:
        check_capacity(kubectl, [NAMESPACE])

    message = str(error.value)
    # Both numbers, and the components that add up to the estimate, so the sum can be
    # checked instead of trusted.
    assert "26Gi of storage in 9 persistent volume claims" in message
    assert "storage 26Gi of 20Gi available (ResourceQuota run-quota in hotel-reservation)" in message
    assert "8Gi Prometheus + 2Gi Alertmanager + 6Gi application volumes + 10Gi Loki = 26Gi" in message


def test_a_claim_quota_below_the_estimate_is_refused():
    quotas = [_quota("run-quota", persistentvolumeclaims="3")]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    with pytest.raises(CapacityShortfall) as error:
        check_capacity(kubectl, [NAMESPACE])

    assert "persistent volume claims 9 of 3 available (ResourceQuota run-quota in hotel-reservation)" in str(
        error.value
    )


def test_a_cluster_with_enough_storage_passes(caplog):
    quotas = [_quota("run-quota", **{"requests.storage": "40Gi", "persistentvolumeclaims": "12"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    check_capacity(kubectl, [NAMESPACE])

    assert caplog.records == []


def test_a_run_without_loki_is_held_to_the_smaller_estimate():
    # 20Gi holds the 16Gi run that skips Loki and not the 26Gi run that deploys it.
    quotas = [_quota("run-quota", **{"requests.storage": "20Gi"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    with pytest.raises(CapacityShortfall, match="26Gi of storage"):
        check_capacity(kubectl, [NAMESPACE])

    check_capacity(kubectl, [NAMESPACE], deploy_loki=False)


def test_the_estimate_follows_the_active_profile(monkeypatch):
    monkeypatch.setenv("SREGYM_PROFILE", "svelte")
    quotas = [_quota("run-quota", **{"requests.storage": "20Gi"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    # svelte claims no Alertmanager volume: 24Gi still exceeds the 20Gi quota, and the
    # components no longer name a component the profile does not deploy.
    with pytest.raises(CapacityShortfall) as error:
        check_capacity(kubectl, [NAMESPACE])

    message = str(error.value)
    assert "24Gi of storage in 8 persistent volume claims" in message
    assert "Alertmanager" not in message
    assert "8Gi Prometheus + 6Gi application volumes + 10Gi Loki = 24Gi" in message


def test_a_lower_resource_quota_in_the_namespace_is_refused_and_named():
    quotas = [_quota("run-quota", **{"requests.cpu": "500m"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)

    with pytest.raises(CapacityShortfall) as error:
        check_capacity(kubectl, [NAMESPACE])

    message = str(error.value)
    assert "ResourceQuota run-quota in hotel-reservation" in message
    assert "CPU 3400m of 500m available" in message


def test_an_unreadable_quota_is_not_a_verdict(caplog):
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"))
    kubectl.get_resource_quotas.side_effect = RuntimeError("Failed to get resource quotas: 403 Forbidden")

    check_capacity(kubectl, [NAMESPACE])

    # A quota nobody could read is a debug line: the run is neither refused nor warned at.
    assert _messages(caplog, logging.WARNING) == []
    assert len(caplog.records) == 1


def test_nothing_runs_on_kind(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, KIND, _kubectl(_node("worker-a", cpu="100m", memory="1Gi")))

    conductor._verify_capacity()

    # kind is a workstation sized for this run: no read, no log line, no refusal.
    assert conductor.kubectl.mock_calls == []
    assert caplog.records == []


def test_openshift_reads_the_nodes_and_refuses_before_the_observers(monkeypatch):
    kubectl = _kubectl(_node("worker-a", cpu="100m", memory="1Gi"))
    conductor = _conductor(monkeypatch, OPENSHIFT, kubectl)

    with pytest.raises(CapacityShortfall, match="34 pods"):
        conductor._verify_capacity()

    assert [call.args[0] for call in kubectl.exec_command.call_args_list] == ["kubectl get nodes -o json"]
    assert [call.args[0] for call in kubectl.get_resource_quotas.call_args_list] == ["observe", "sregym", NAMESPACE]


def test_the_check_follows_the_runs_loki_setting(monkeypatch):
    # 20Gi holds the 16Gi run that skips Loki; `deploy_loki` is the conductor's own flag.
    quotas = [_quota("run-quota", **{"requests.storage": "20Gi"})]
    kubectl = _kubectl(_node("worker-a"), _node("worker-b"), _node("worker-c"), quotas=quotas)
    conductor = _conductor(monkeypatch, OPENSHIFT, kubectl)
    conductor.config = ConductorConfig(deploy_loki=False)

    conductor._verify_capacity()

    assert conductor._capacity_verified is True


def test_a_kind_campaign_never_scans(monkeypatch, caplog):
    scanner = MagicMock()
    monkeypatch.setattr(conductor_module, "check_capacity", scanner)
    conductor = _campaign_conductor(monkeypatch, KIND)

    for _ in range(2):
        with pytest.raises(ObserversDeployed):
            asyncio.run(conductor.start_problem())

    assert scanner.call_count == 0
    assert _messages(caplog, logging.INFO) == []


def test_one_campaign_scans_the_cluster_once(monkeypatch):
    scanner = MagicMock()
    monkeypatch.setattr(conductor_module, "check_capacity", scanner)
    conductor = _campaign_conductor(monkeypatch, OPENSHIFT)

    for _ in range(2):
        with pytest.raises(ObserversDeployed):
            asyncio.run(conductor.start_problem())

    # The second problem of the campaign reuses the first one's verdict.
    assert scanner.call_count == 1
    assert scanner.call_args.args[1] == ["observe", "sregym", NAMESPACE]


def test_a_refused_campaign_is_not_remembered_as_verified(monkeypatch):
    conductor = _campaign_conductor(
        monkeypatch, OPENSHIFT, kubectl=_kubectl(_node("worker-a", cpu="100m", memory="1Gi"))
    )

    with pytest.raises(CapacityShortfall, match="34 pods"):
        asyncio.run(conductor.start_problem())

    assert conductor._capacity_verified is False

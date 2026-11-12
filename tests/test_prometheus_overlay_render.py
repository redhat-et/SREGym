"""What Helm makes of the Prometheus overlay.

An overlay that names a component Helm does not know is dropped without a warning,
and a node selector that is not deleted leaves the pin in place; only a render shows
which. The `kind` pins are not asserted here — `tests/test_deployment_profiles.py`
covers that chain — this proves the coalescing rule was applied correctly.
"""

import shutil
import subprocess

import pytest
import yaml

from sregym.paths import BASE_DIR

CHART = BASE_DIR / "observer/prometheus/prometheus"
OVERLAY = BASE_DIR / "observer/prometheus/values-openshift.yaml"
PIN = "node-role.kubernetes.io/control-plane"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def mappings_in(node):
    """Every mapping in `node`, at any depth."""
    if isinstance(node, dict):
        return [node, *(inner for value in node.values() for inner in mappings_in(value))]
    if isinstance(node, list):
        return [inner for item in node for inner in mappings_in(item)]
    return []


def pod_specs(document):
    """The mappings that hold `containers`, which in this chart are the pod specs."""
    return [mapping for mapping in mappings_in(document) if "containers" in mapping]


@pytest.fixture(scope="module")
def rendered():
    result = subprocess.run(
        ["helm", "template", "prometheus", str(CHART), "-f", str(OVERLAY)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def test_no_rendered_pod_spec_pins_the_control_plane(rendered):
    pinned = [
        document["metadata"]["name"]
        for document in rendered
        for spec in pod_specs(document)
        if PIN in (spec.get("nodeSelector") or {})
    ]

    assert pinned == []


def test_no_rendered_node_selector_or_toleration_survives(rendered):
    """The overlay nulls the whole map and replaces the list, so nothing is left to render."""
    leftovers = [
        (document["metadata"]["name"], key)
        for document in rendered
        for spec in pod_specs(document)
        for key in ("nodeSelector", "tolerations")
        if key in spec
    ]

    assert leftovers == []


def test_the_node_exporter_daemonset_is_not_rendered(rendered):
    daemonsets = [document["metadata"]["name"] for document in rendered if document["kind"] == "DaemonSet"]

    assert daemonsets == []


def test_the_overlay_keeps_the_components_that_serve_the_harness(rendered):
    """A null node selector must not cost a workload; the chart still renders all five."""
    names = {document["metadata"]["name"] for document in rendered}

    assert {
        "prometheus-server",
        "prometheus-alertmanager",
        "prometheus-kube-state-metrics",
        "prometheus-prometheus-pushgateway",
        "prometheus-prometheus-blackbox-exporter",
    } <= names


def test_kube_state_metrics_leaves_its_uid_and_fsgroup_to_the_constraint(rendered):
    """kube-state-metrics had no pod on ROSA: `anyuid` rejects a pod that sets a seccomp
    profile, and `restricted-v2` rejects the chart's 65534 UID and fsGroup. With the three
    fields unset, the constraint supplies allowed ones and the pod passes `restricted-v2`.
    The other four workloads keep the chart's values, because they run under `anyuid`."""
    specs = {
        document["metadata"]["name"]: spec
        for document in rendered
        for spec in pod_specs(document)
        if document["kind"] in {"Deployment", "StatefulSet"}
    }
    kube_state_metrics = specs["prometheus-kube-state-metrics"].get("securityContext") or {}

    for field in ("runAsUser", "runAsGroup", "fsGroup"):
        assert field not in kube_state_metrics or kube_state_metrics[field] is None
    assert kube_state_metrics.get("runAsNonRoot") is True
    assert kube_state_metrics.get("seccompProfile") == {"type": "RuntimeDefault"}

    assert specs["prometheus-server"]["securityContext"]["runAsUser"] == 65534

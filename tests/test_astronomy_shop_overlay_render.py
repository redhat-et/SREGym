"""That the Astronomy Shop collector keeps the shape the public benchmark ships.

The app's own collector is the path every component's OTLP export takes, and the vendored chart renders it
as a DaemonSet with `hostPath: /` at `/hostfs` (the `hostMetrics` preset) and six hostPorts. On ROSA no SCC
admits that as shipped, so the fix belongs on the cluster, not in the values: `rosa_prerun.sh` grants the
`hostaccess` SCC to the `astronomy-shop/otel-collector` service account, which allows exactly the host
directory and host ports this pod asks for and no privileged container.

These tests exist so a future change cannot quietly trim the workload instead: the overlay must leave the
collector alone, and the values the grant is justified by must still be there.
"""

import shutil
import subprocess

import pytest
import yaml

from sregym.paths import BASE_DIR

CHART = BASE_DIR.parent / "SREGym-applications/astronomy-shop/charts/opentelemetry-demo"
VALUES = BASE_DIR / "service/apps/values"
FIXES = VALUES / "astronomy-shop-fixes.yaml"
OVERLAY = VALUES / "astronomy-shop-openshift.yaml"
PRERUN = BASE_DIR.parent / "scripts/rosa_prerun.sh"
COLLECTOR = "otel-collector-agent"
HOST_PORTS = {6831, 14250, 14268, 4317, 4318, 9411}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@pytest.fixture(scope="module")
def collector():
    result = subprocess.run(
        [
            "helm",
            "template",
            "astronomy-shop",
            str(CHART),
            "--set",
            "prometheus.enabled=false",
            "-f",
            str(FIXES),
            "-f",
            str(OVERLAY),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.skip(f"the astronomy-shop chart did not render here: {result.stderr.strip().splitlines()[:1]}")
    # PyYAML cannot take the whole stream (literal SQL in a ConfigMap), so split and parse per document.
    for chunk in result.stdout.split("\n---"):
        try:
            document = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(document, dict) and document.get("kind") == "DaemonSet":
            if (document.get("metadata") or {}).get("name") == COLLECTOR:
                return document
    raise AssertionError(f"{COLLECTOR} is no longer rendered: the app's telemetry path changed shape")


def pod_spec(collector):
    return collector["spec"]["template"]["spec"]


def test_the_overlay_does_not_change_the_collector(collector):
    """Fidelity: the public benchmark's collector shape is the one that runs, and the cluster is what
    changes. Adding a values key here would be trimming the workflow."""
    overlay = yaml.safe_load(OVERLAY.read_text()) or {}

    assert set(overlay) == {"grafana"}, sorted(overlay)


def test_the_collector_still_reads_the_host_and_binds_its_ports(collector):
    """These are the two things that need `hostaccess`, and the reason the grant is not `anyuid`."""
    host_paths = [volume["hostPath"]["path"] for volume in pod_spec(collector)["volumes"] if "hostPath" in volume]
    ports = {
        port["hostPort"]
        for container in pod_spec(collector)["containers"]
        for port in (container.get("ports") or [])
        if port.get("hostPort")
    }

    assert host_paths == ["/"]
    assert ports == HOST_PORTS


def test_the_collector_pins_nothing_hostaccess_would_refuse(collector):
    """`hostaccess` assigns the uid and fsGroup from the namespace's ranges and allows no seccomp field,
    so the pod must keep asking for none of them."""
    spec = pod_spec(collector)
    pinned = (spec.get("securityContext") or {}) | (spec["containers"][0].get("securityContext") or {})

    for field in ("runAsUser", "runAsGroup", "fsGroup", "seccompProfile"):
        assert field not in pinned, field
    assert spec.get("hostNetwork") is not True


def test_the_prerun_script_grants_hostaccess_to_that_service_account():
    script = PRERUN.read_text()

    assert "system:openshift:scc:hostaccess" in script
    assert "name: otel-collector" in script
    assert "namespace: astronomy-shop" in script
    assert "sregym-astronomy-collector-hostaccess" in script

"""What Helm makes of the Promtail overlay, when the chart is reachable.

Promtail is the only observer that must read and write outside its image: the chart mounts the node's
pod logs and its own positions file from the host. On OpenShift the pod carries the namespace's MCS
label, so under a narrower SCC the position write is refused and `ls /var/log/pods` answers
`Permission denied` (measured on ROSA 4.22.18). The overlay therefore backs the positions mount with an
emptyDir and asks for a privileged container, which runs as `spc_t` and can read the log tree; with that,
Promtail is 3/3 Ready and Loki returns labels.

`grafana/promtail` is a remote chart, so this skips where it cannot be fetched.
"""

import shutil
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from sregym.paths import BASE_DIR

VALUES = BASE_DIR / "observer/loki/promtail-values.yaml"
OVERLAY = BASE_DIR / "observer/loki/promtail-values-openshift.yaml"
POSITIONS_PATH = "/run/promtail"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@pytest.fixture(scope="module")
def rendered():
    result = subprocess.run(
        [
            "helm",
            "template",
            "promtail",
            "grafana/promtail",
            "-n",
            "observe",
            "-f",
            str(VALUES),
            "-f",
            str(OVERLAY),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(f"grafana/promtail is not reachable here: {result.stderr.strip().splitlines()[:1]}")
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    return SimpleNamespace(documents=documents, text=result.stdout)


@pytest.fixture(scope="module")
def promtail_pod_spec(rendered):
    daemonset = next(document for document in rendered.documents if document.get("kind") == "DaemonSet")
    return daemonset["spec"]["template"]["spec"]


def volume(pod_spec, name):
    return next(volume for volume in pod_spec["volumes"] if volume["name"] == name)


def mount(pod_spec, name):
    return next(m for m in pod_spec["containers"][0]["volumeMounts"] if m["name"] == name)


def test_the_positions_mount_no_longer_comes_from_the_host(promtail_pod_spec):
    run = volume(promtail_pod_spec, "run")

    assert "hostPath" not in run
    assert run["emptyDir"] == {}


def test_the_positions_path_still_matches_the_rendered_config(rendered, promtail_pod_spec):
    """The chart writes `positions.filename` into its config; the mount has to be at that same path."""
    assert mount(promtail_pod_spec, "run")["mountPath"] == POSITIONS_PATH
    assert f"filename: {POSITIONS_PATH}/positions.yaml" in rendered.text


def test_the_pod_and_container_log_paths_stay_read_only(promtail_pod_spec):
    """Promtail still has to read the node's pod logs; those mounts are untouched by this change."""
    for name, path in (("pods", "/var/log/pods"), ("containers", "/var/lib/docker/containers")):
        assert volume(promtail_pod_spec, name)["hostPath"]["path"] == path
        assert mount(promtail_pod_spec, name)["readOnly"] is True


def test_the_container_is_privileged_because_only_that_reads_the_logs(promtail_pod_spec):
    """Measured on ROSA 4.22.18, in this order: no grant at all and every pod is refused; with
    `hostmount-anyuid` the pod is admitted and `ls /var/log/pods` answers `Permission denied`; with
    `privileged` the container runs as `spc_t`, reads them, and Loki returns labels. The narrower grant
    cannot be given that read, so the overlay asks for the container the working state needs — and keeps
    the read-only root filesystem and the emptyDir-backed positions mount."""
    container = promtail_pod_spec["containers"][0]
    security_context = container.get("securityContext") or {}

    assert security_context.get("privileged") is True
    assert security_context.get("readOnlyRootFilesystem") is True

"""The shared helper behind `jaeger.yaml` and `otel-collector.yaml` on OpenShift.

Both manifests are applied with `kubectl apply -f` and both pin the `kind` control
plane. The helper drops the two scheduling keys and keeps everything else, so the
`kind` manifests stay the `kind` input and the OpenShift apply differs in exactly
those keys.
"""

from copy import deepcopy

import pytest
import yaml

from sregym import cluster_platform
from sregym.cluster_platform import OPENSHIFT
from sregym.paths import BASE_DIR
from sregym.service.pod_scheduling import kubectl_apply_command, without_node_pins

MANIFESTS = [
    BASE_DIR / "observer/jaeger/jaeger.yaml",
    BASE_DIR / "observer/otel_collector/otel-collector.yaml",
]
NODE_SCHEDULING_KEYS = ("nodeSelector", "tolerations")


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def documents(path):
    return [body for body in yaml.safe_load_all(path.read_text()) if body]


def mappings_in(node):
    """Every mapping in `node`, at any depth."""
    if isinstance(node, dict):
        return [node, *(inner for value in node.values() for inner in mappings_in(value))]
    if isinstance(node, list):
        return [inner for item in node for inner in mappings_in(item)]
    return []


def pod_specs(document):
    """The mappings that hold `containers`, which in these manifests are the pod specs."""
    return [mapping for mapping in mappings_in(document) if "containers" in mapping]


def without_scheduling_keys(document):
    """`document` as it should be applied: the two keys gone from every pod spec."""
    for pod_spec in pod_specs(document):
        for key in NODE_SCHEDULING_KEYS:
            del pod_spec[key]
    return document


def applied_documents(command):
    """The documents a heredoc `kubectl apply` command pipes to the server."""
    payload = command.split("<<'EOF'\n", 1)[1].rsplit("EOF", 1)[0]
    return [body for body in yaml.safe_load_all(payload) if body]


@pytest.mark.parametrize("path", MANIFESTS, ids=[path.name for path in MANIFESTS])
def test_kind_applies_the_manifest_file_itself(path):
    assert kubectl_apply_command(path, "observe") == f"kubectl apply -f {path} -n observe"


@pytest.mark.parametrize("path", MANIFESTS, ids=[path.name for path in MANIFESTS])
def test_openshift_drops_the_two_keys_and_changes_nothing_else(path):
    expected = [without_scheduling_keys(deepcopy(document)) for document in documents(path)]
    assert any(NODE_SCHEDULING_KEYS[0] in spec for document in documents(path) for spec in pod_specs(document))

    cluster_platform.set_platform(OPENSHIFT)

    assert applied_documents(kubectl_apply_command(path, "observe")) == expected


def test_the_helper_drops_the_keys_from_every_pod_spec_it_is_given():
    """The transformation itself, independent of how the command is built."""
    pinned = [
        {
            "kind": "List",
            "items": [
                {
                    "kind": "Deployment",
                    "spec": {"template": {"spec": {"nodeSelector": {"a": "b"}, "tolerations": [], "containers": []}}},
                },
                {"kind": "Pod", "spec": {"nodeSelector": {"a": "b"}, "tolerations": [], "containers": []}},
            ],
        }
    ]

    stripped = without_node_pins(pinned)

    assert [set(spec) for document in stripped for spec in pod_specs(document)] == [{"containers"}, {"containers"}]

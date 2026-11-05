"""The MCP Deployment's node pins, and the ClusterRole branch next to them.

`mcp_server/k8s/deployment.yaml` is rendered with `kubectl kustomize` and filtered
in Python, so the two keys are dropped there rather than by a patch. The ClusterRole
rewrite lives in the same loop and must not move with them.
"""

from copy import deepcopy
from unittest.mock import patch

import pytest
import yaml

from sregym import cluster_platform
from sregym.cluster_platform import OPENSHIFT
from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubernetes_access_policy import restricted_cluster_role
from sregym.service.mcp_server import MCPServer
from sregym.service.owned_objects import OWNED_LABEL, OWNED_VALUE

NODE_SCHEDULING_KEYS = ("nodeSelector", "tolerations")


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


@pytest.fixture
def rendered_manifests():
    # The real files. Kustomize adds labels and rewrites the image, which the two
    # keys under test do not depend on.
    manifest = yaml.safe_load((MCP_SERVER_K8S / "kustomization.yaml").read_text())
    return yaml.safe_dump_all([yaml.safe_load((MCP_SERVER_K8S / path).read_text()) for path in manifest["resources"]])


def resources(rendered, *, restrict_network_access=False):
    with patch("sregym.service.mcp_server.KubeCtl"):
        server = MCPServer(restrict_network_access=restrict_network_access)
    server.kubectl.exec_command_checked.return_value = rendered
    return server._deployment_resources()


def deployment(bodies):
    return next(body for body in bodies if body["kind"] == "Deployment" and body["metadata"]["name"] == "mcp-server")


def test_the_manifest_pins_the_control_plane_so_the_test_is_not_vacuous(rendered_manifests):
    pod_spec = deployment(resources(rendered_manifests))["spec"]["template"]["spec"]

    assert set(NODE_SCHEDULING_KEYS) <= set(pod_spec)


def test_openshift_drops_exactly_the_two_keys_from_the_pod_spec(rendered_manifests):
    kind_bodies = resources(rendered_manifests)
    expected = deepcopy(deployment(kind_bodies))
    for key in NODE_SCHEDULING_KEYS:
        del expected["spec"]["template"]["spec"][key]

    cluster_platform.set_platform(OPENSHIFT)

    assert deployment(resources(rendered_manifests)) == expected


def test_openshift_leaves_every_other_resource_equal(rendered_manifests):
    kind_bodies = resources(rendered_manifests)

    cluster_platform.set_platform(OPENSHIFT)
    openshift_bodies = resources(rendered_manifests)

    assert len(kind_bodies) == len(openshift_bodies)
    assert [body for body in openshift_bodies if body["kind"] != "Deployment"] == [
        body for body in kind_bodies if body["kind"] != "Deployment"
    ]


@pytest.mark.parametrize("filtered", [True, False])
def test_the_cluster_role_branch_is_unaffected(rendered_manifests, filtered):
    def cluster_roles():
        bodies = resources(rendered_manifests, restrict_network_access=filtered)
        return [body for body in bodies if body["kind"] == "ClusterRole"]

    kind_roles = cluster_roles()

    cluster_platform.set_platform(OPENSHIFT)

    assert cluster_roles() == kind_roles
    original = yaml.safe_load((MCP_SERVER_K8S / "clusterrole.yaml").read_text())
    # Every applied resource now carries the ownership label, which is what lets the teardown
    # remove it by ownership rather than by name. Compare with that label set aside, so this
    # test keeps its subject: the platform transform.
    for role in kind_roles:
        assert role["metadata"]["labels"][OWNED_LABEL] == OWNED_VALUE
        role["metadata"]["labels"].pop(OWNED_LABEL)
    assert kind_roles == [restricted_cluster_role(original) if filtered else original]

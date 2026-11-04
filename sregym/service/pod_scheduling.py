"""Drop the control-plane node pins from an applied observer manifest.

`jaeger.yaml` and `otel-collector.yaml` are `kubectl apply -f` inputs, so there is
no values overlay to remove `nodeSelector` and `tolerations` from the way the Helm
charts have one. Both pin the workload to the single `kind` node, which is also its
control plane. On OpenShift those keys are dropped and the documents are applied
from memory, so the manifest files stay the `kind` input. On `kind` the file is
applied as it is, unchanged.
"""

from pathlib import Path

import yaml

from sregym.cluster_platform import is_openshift

NODE_SCHEDULING_KEYS = ("nodeSelector", "tolerations")


def _mappings(node: dict | list):
    """Yield every mapping in `node`, at any depth."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _mappings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _mappings(item)


def _pod_specs(document: dict) -> list[dict]:
    """Every pod spec in `document`.

    A pod spec is the mapping that holds `containers`: no other mapping in these
    manifests has that key. It is found wherever it nests, so `spec.template.spec`
    (a workload) and `spec` (a Pod) need no separate case.
    """
    return [mapping for mapping in _mappings(document) if "containers" in mapping]


def without_node_pins(documents: list[dict]) -> list[dict]:
    """Return `documents` with the node scheduling keys gone from every pod spec.

    Every other field is left as it was, so the applied manifest differs from the
    `kind` one in exactly these two keys.
    """
    for document in documents:
        for pod_spec in _pod_specs(document):
            for key in NODE_SCHEDULING_KEYS:
                pod_spec.pop(key, None)
    return documents


def kubectl_apply_command(manifest_path: Path, namespace: str) -> str:
    """The `kubectl apply` command line for `manifest_path` in `namespace`.

    On `kind` this applies the file itself. On OpenShift the documents are read,
    their node pins dropped, and the result piped to `kubectl apply -f -`; the
    heredoc is quoted so the shell expands nothing inside the manifest.
    """
    if not is_openshift():
        return f"kubectl apply -f {manifest_path} -n {namespace}"
    documents = without_node_pins([body for body in yaml.safe_load_all(Path(manifest_path).read_text()) if body])
    return f"kubectl apply -f - -n {namespace} <<'EOF'\n{yaml.safe_dump_all(documents)}EOF"

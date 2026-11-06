"""The lint scanner, on fixture text."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "openshift_chart_lint", Path(__file__).resolve().parent.parent / "scripts" / "openshift_chart_lint.py"
)
lint = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lint
_spec.loader.exec_module(lint)


def test_flags_the_kube_dns_resolver():
    assert lint.scan("resolver kube-dns.kube-system.svc.cluster.local valid=10s")["kube-dns name"] == 1


def test_clean_text_has_no_findings():
    assert lint.scan("image: ghcr.io/x/y:1\nresolver dns-default.openshift-dns.svc.cluster.local") == {}


def test_flags_privileged_hostpath_and_pins():
    found = lint.scan("privileged: true\n  hostPath:\n    path: /x\nrunAsUser: 1001\n")
    assert set(found) == {"privileged", "hostPath", "runAsUser pin"}


def test_unqualified_image_is_flagged_and_qualified_is_not():
    assert lint.scan("image: mongo:4.4")["unqualified image"] == 1
    assert lint.scan("image: docker.io/library/mongo:4.4") == {}

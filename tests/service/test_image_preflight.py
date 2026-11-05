"""The image-reference preflight names the manifest and the reference, and only reports.

The rule: a reference whose first path component is not a registry host is rewritten by the
cluster's own short-name policy, which is how a measured OpenShift run turned eight pod pulls
into a 600 s readiness timeout (`OPENSHIFT-READINESS.md` §11). A registered reference is not a
short name, so the alias table does not apply.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sregym.service import image_preflight as conductor_module_image
from sregym.service.image_preflight import (
    ALLOWLIST,
    classify,
    find_unqualified,
    report,
)

MODULE_LOGGER = "all.sregym.image_preflight"

QUALIFIED_REFERENCES = [
    ("registry.k8s.io/pause:3.10", "registry.k8s.io"),
    ("ghcr.io/sregym/hotel-reservation@sha256:" + "0" * 64, "ghcr.io"),
    ("localhost:5000/foo", "localhost:5000"),
    ("localhost/foo", "localhost"),
    ("example.com:443/foo", "example.com:443"),
]

UNQUALIFIED_REFERENCES = [
    "busybox:1.36",
    "python:3.12-alpine",
    "jaegertracing/all-in-one:1.57",
    "otel/opentelemetry-collector-contrib:0.104.0",
    "foo/bar",
]


@pytest.mark.parametrize(("reference", "host"), QUALIFIED_REFERENCES)
def test_a_reference_that_names_its_registry_is_qualified(reference, host):
    assert classify(reference) == (True, host)


@pytest.mark.parametrize("reference", UNQUALIFIED_REFERENCES)
def test_a_tag_a_slash_or_a_bare_name_is_not_a_registry(reference):
    # `jaegertracing/...` and `otel/...` are Docker Hub namespaces, not registries, and the tag
    # after a colon is not a port.
    assert classify(reference) == (False, "")


def test_a_qualified_reference_yields_no_finding(tmp_path):
    (tmp_path / "pause.yaml").write_text("spec:\n  containers:\n    - image: registry.k8s.io/pause:3.10\n")
    (tmp_path / "notes.conf").write_text("image: mongo:4.4.6\n")

    assert find_unqualified([tmp_path]) == []


def test_an_unqualified_reference_yields_a_finding_naming_the_file(tmp_path):
    manifest = tmp_path / "mongodb-geo-deployment.yaml"
    manifest.write_text("spec:\n  containers:\n    - image: mongo:4.4.6\n")

    findings = find_unqualified([tmp_path])

    assert [(finding.reference, finding.manifest) for finding in findings] == [("mongo:4.4.6", manifest)]
    assert findings[0].reason


def test_an_allowlisted_reference_yields_no_finding(tmp_path):
    # It is in the allowlist because a measured run pulled it, so the preflight must not report
    # it again on every run.
    assert "memcached" in ALLOWLIST
    (tmp_path / "memcached.yaml").write_text("spec:\n  containers:\n    - image: memcached:1.6.39-alpine\n")

    assert find_unqualified([tmp_path]) == []


def test_a_file_that_does_not_parse_is_skipped_on_one_debug_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=MODULE_LOGGER)
    template = tmp_path / "deployment.yaml"
    template.write_text("spec:\n  containers:\n    - image: {{ .Values.image }}\n")
    (tmp_path / "redis.yaml").write_text("spec:\n  containers:\n    - image: redis:7\n")

    findings = find_unqualified([tmp_path])

    assert [finding.reference for finding in findings] == ["redis:7"]
    skipped = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(skipped) == 1
    assert str(template) in skipped[0]


def test_every_document_in_a_multi_document_file_is_scanned(tmp_path):
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(
        "kind: Deployment\nspec:\n  containers:\n    - image: mongo:4.4.6\n---\n"
        "kind: Deployment\nspec:\n  containers:\n    - image: ghcr.io/sregym/app:1\n"
        "---\n"
        "kind: Deployment\nspec:\n  containers:\n    - image: hashicorp/consul:1.22.3\n"
    )

    assert [finding.reference for finding in find_unqualified([manifest])] == ["mongo:4.4.6", "hashicorp/consul:1.22.3"]


def test_a_json_manifest_is_scanned(tmp_path):
    manifest = tmp_path / "deployment.json"
    manifest.write_text(json.dumps({"kind": "Deployment", "spec": {"containers": [{"image": "mongo:4.4.6"}]}}))

    assert [finding.reference for finding in find_unqualified([tmp_path])] == ["mongo:4.4.6"]


def test_the_report_has_one_line_per_finding_then_the_fix(tmp_path):
    first = tmp_path / "a.yaml"
    first.write_text("spec:\n  containers:\n    - image: mongo:4.4.6\n")
    second = tmp_path / "b.yaml"
    second.write_text("spec:\n  containers:\n    - image: hashicorp/consul:1.22.3\n")

    lines = report(find_unqualified([first, second])).splitlines()

    assert f"{first}: mongo:4.4.6" in lines[0]
    assert f"{second}: hashicorp/consul:1.22.3" in lines[1]
    assert lines[2].startswith("2 image reference(s) in 2 manifest file(s) name no registry host.")
    assert "docker.io/mongo:4.4.6" in lines[2]
    assert "pin the SREGym-applications submodule" in lines[2]
    assert len(lines) == 3


# --- wiring and the regression this milestone exists for -------------------------------


def test_nothing_is_scanned_on_kind(monkeypatch):
    from sregym.conductor.conductor import Conductor

    conductor = Conductor.__new__(Conductor)
    conductor.logger = MagicMock()
    called = []
    monkeypatch.setattr(conductor_module_image, "find_unqualified", lambda paths: called.append(paths) or [])
    monkeypatch.setattr("sregym.conductor.conductor.is_openshift", lambda: False)

    Conductor._verify_image_references(conductor, SimpleNamespace(app=SimpleNamespace(k8s_deploy_path=None)))

    assert called == []


def test_an_unqualified_reference_in_the_app_manifests_refuses_the_deploy(tmp_path, monkeypatch):
    from sregym.conductor.conductor import Conductor

    app_dir = tmp_path / "kubernetes"
    app_dir.mkdir()
    (app_dir / "deployment.yaml").write_text(
        "kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n        - name: a\n          image: mongo:4.4.6\n"
    )
    conductor = Conductor.__new__(Conductor)
    conductor.logger = MagicMock()
    monkeypatch.setattr("sregym.conductor.conductor.is_openshift", lambda: True)
    monkeypatch.setattr("sregym.conductor.conductor.find_unqualified", conductor_module_image.find_unqualified)

    with pytest.raises(RuntimeError) as raised:
        Conductor._verify_image_references(conductor, SimpleNamespace(app=SimpleNamespace(k8s_deploy_path=app_dir)))

    message = str(raised.value)
    assert "mongo:4.4.6" in message
    assert "deployment.yaml" in message


def test_the_references_this_item_qualified_stay_qualified():
    # The preflight reports these; qualifying them is what made the report empty.
    expected = {
        "sregym/observer/jaeger/jaeger.yaml": "docker.io/jaegertracing/all-in-one:1.57",
        "sregym/observer/otel_collector/otel-collector.yaml": "docker.io/otel/opentelemetry-collector-contrib:0.104.0",
        "sregym/conductor/oracles/conntrack_mitigation.py": "docker.io/busybox:1.36",
        "sregym/conductor/problems/apps/frappe/controllers.py": "docker.io/busybox:1.36",
    }
    root = Path(__file__).resolve().parents[2]

    for relative, reference in expected.items():
        assert reference in (root / relative).read_text(), relative

    findings = find_unqualified([root / "sregym" / "observer"])
    assert findings == []


def test_the_override_warns_and_continues(monkeypatch, tmp_path):
    """The preflight cannot know a cluster's short-name table, so an operator may assert it."""
    from sregym.conductor.conductor import Conductor, ConductorConfig

    app_dir = tmp_path / "kubernetes"
    app_dir.mkdir()
    (app_dir / "d.yaml").write_text(
        "kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n        - image: mongo:4.4.6\n"
    )
    monkeypatch.setattr("sregym.conductor.conductor.is_openshift", lambda: True)
    problem = SimpleNamespace(app=SimpleNamespace(k8s_deploy_path=app_dir))

    refusing = Conductor.__new__(Conductor)
    refusing.logger = MagicMock()
    refusing.config = ConductorConfig()
    with pytest.raises(RuntimeError):
        Conductor._verify_image_references(refusing, problem)

    allowing = Conductor.__new__(Conductor)
    allowing.logger = MagicMock()
    allowing.config = ConductorConfig(allow_unqualified_images=True)
    Conductor._verify_image_references(allowing, problem)

    warned = " ".join(str(call.args[0]) for call in allowing.logger.warning.call_args_list)
    assert "mongo:4.4.6" in warned
    assert "--allow-unqualified-images" in warned

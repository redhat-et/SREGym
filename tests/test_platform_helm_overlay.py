"""The platform overlay: one values file per application, applied on OpenShift only."""

from pathlib import Path
from types import SimpleNamespace

import yaml

from sregym.service.apps import base
from sregym.service.apps.base import Application
from sregym.service.apps.social_network import SocialNetwork


def _app(tmp_path, monkeypatch, *, openshift, key="demo", create_file=True, extra_args=None):
    monkeypatch.setattr(base, "PLATFORM_VALUES_DIR", tmp_path)
    monkeypatch.setattr(base, "is_openshift", lambda: openshift)
    if create_file:
        (tmp_path / f"{key}-openshift.yaml").write_text("a: b\n")
    app = Application.__new__(Application)
    app.name = "demo"
    app.logger = SimpleNamespace(info=lambda *_a, **_k: None)
    app.platform_overlay_key = key
    app.helm_configs = {"release_name": "demo"}
    if extra_args is not None:
        app.helm_configs["extra_args"] = extra_args
    return app


def test_openshift_with_an_overlay_appends_the_values_file(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, openshift=True, extra_args=["--set", "x=1"])
    configs = app.platform_helm_configs()
    assert configs["extra_args"] == ["--set", "x=1", "-f", str(tmp_path / "demo-openshift.yaml")]
    assert app.helm_configs["extra_args"] == ["--set", "x=1"]


def test_kind_adds_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, openshift=False)
    assert app.platform_helm_configs() == app.helm_configs


def test_openshift_without_an_overlay_file_adds_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, openshift=True, create_file=False)
    assert app.platform_helm_configs() == app.helm_configs


def test_an_application_without_a_key_adds_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, openshift=True)
    app.platform_overlay_key = None
    assert "extra_args" not in app.platform_helm_configs()


def test_social_network_overlay_points_nginx_at_the_openshift_dns_service():
    overlay = Path(base.PLATFORM_VALUES_DIR) / f"{SocialNetwork.platform_overlay_key}-openshift.yaml"
    values = yaml.safe_load(overlay.read_text())
    assert values["global"]["nginx"]["resolverName"] == "dns-default.openshift-dns.svc.cluster.local"


def test_astronomy_shop_overlay_unpins_the_grafana_ids_the_scc_refuses():
    from sregym.service.apps.astronomy_shop import AstronomyShop

    overlay = Path(base.PLATFORM_VALUES_DIR) / f"{AstronomyShop.platform_overlay_key}-openshift.yaml"
    values = yaml.safe_load(overlay.read_text())
    assert values == {"grafana": {"securityContext": {"runAsUser": None, "runAsGroup": None, "fsGroup": None}}}

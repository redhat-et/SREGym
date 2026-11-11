"""The OpenShift scheduling overlays: what they say, and when they are used.

The overlays carry the node pins the `kind` chain relies on, so two things can go
wrong silently: a component name Helm does not know is ignored without a warning,
and a node selector that is not deleted leaves an unschedulable workload. Both are
checked here on the files themselves; `test_prometheus_overlay_render.py` checks
what Helm actually makes of them.
"""

from urllib.parse import urlsplit

import pytest
import yaml

from sregym import cluster_platform, profile, storage
from sregym.cluster_platform import OPENSHIFT
from sregym.paths import BASE_DIR
from sregym.service.helm import Helm
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus

PROMETHEUS_OVERLAY = BASE_DIR / "observer/prometheus/values-openshift.yaml"
LOKI_OVERLAY = BASE_DIR / "observer/loki/values-openshift.yaml"
PROMTAIL_OVERLAY = BASE_DIR / "observer/loki/promtail-values-openshift.yaml"
OVERLAYS = [PROMETHEUS_OVERLAY, LOKI_OVERLAY, PROMTAIL_OVERLAY]

PIN = "node-role.kubernetes.io/control-plane"
LOKI_PUSH_URL = "http://loki.observe.svc.cluster.local:3100/loki/api/v1/push"


@pytest.fixture(autouse=True)
def reset_selection(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)
    monkeypatch.setattr(profile, "_profile", None)
    monkeypatch.delenv("SREGYM_PROFILE", raising=False)
    monkeypatch.setattr(storage, "_storage_class", None)
    monkeypatch.delenv("SREGYM_STORAGE_CLASS", raising=False)


def overlay_values(path):
    return yaml.safe_load(path.read_text())


def chart_scope_names():
    """The Prometheus chart's top-level values scopes, straight from the vendored chart."""
    values = yaml.safe_load((BASE_DIR / "observer/prometheus/prometheus/values.yaml").read_text())
    return set(values)


# ---------------------------------------------------------------------------------- shape


@pytest.mark.parametrize("path", OVERLAYS, ids=[path.name for path in OVERLAYS])
def test_overlay_parses_as_yaml(path):
    assert isinstance(overlay_values(path), dict)


@pytest.mark.parametrize("path", OVERLAYS, ids=[path.name for path in OVERLAYS])
def test_overlay_never_names_the_control_plane_pin(path):
    """A per-key null keeps the key and renders `nodeSelector: {<pin>: null}`, which the API server rejects.

    Helm coalesces maps, so a key is deleted only by nulling the whole map. An
    overlay whose *values* name the pin at all is therefore wrong, whatever it
    sets it to. The file's comments may name it; the values may not.
    """
    assert PIN not in yaml.safe_dump(overlay_values(path))


@pytest.mark.parametrize("path", [PROMETHEUS_OVERLAY, LOKI_OVERLAY, PROMTAIL_OVERLAY], ids=lambda path: path.name)
def test_every_node_selector_override_nulls_the_whole_map(path):
    for scope, settings in overlay_values(path).items():
        if isinstance(settings, dict) and "nodeSelector" in settings:
            assert settings["nodeSelector"] is None, f"{path.name}: {scope}.nodeSelector must be null"


# ---------------------------------------------------------------------------- Prometheus


def test_prometheus_overlay_names_the_chart_scopes():
    overlay = overlay_values(PROMETHEUS_OVERLAY)

    assert set(overlay) == {
        "server",
        "alertmanager",
        "kube-state-metrics",
        "prometheus-pushgateway",
        "prometheus-blackbox-exporter",
        "prometheus-node-exporter",
    }
    # A name Helm does not know is dropped without a warning, so every scope must be
    # one the chart itself declares.
    assert set(overlay) <= chart_scope_names()


def test_prometheus_overlay_clears_the_pins_and_switches_off_node_exporter():
    overlay = overlay_values(PROMETHEUS_OVERLAY)

    for scope in overlay:
        if scope == "prometheus-node-exporter":
            # One pod per node with a hostPort, on a node the platform manages.
            assert overlay[scope]["enabled"] is False
        else:
            assert overlay[scope]["nodeSelector"] is None, scope
            assert overlay[scope]["tolerations"] == [], scope


# ---------------------------------------------------------------------------------- Loki


def test_loki_overlay_names_the_chart_scopes():
    overlay = overlay_values(LOKI_OVERLAY)
    chart_scopes = set(yaml.safe_load((BASE_DIR / "observer/loki/loki-values.yaml").read_text()))

    assert set(overlay) == {"singleBinary", "gateway", "promtail"}
    assert set(overlay) <= chart_scopes


def test_loki_overlay_clears_the_pins_and_disables_the_gateway():
    overlay = overlay_values(LOKI_OVERLAY)

    assert overlay["singleBinary"]["nodeSelector"] is None
    assert overlay["singleBinary"]["tolerations"] == []
    assert overlay["gateway"]["enabled"] is False
    assert overlay["gateway"]["nodeSelector"] is None
    assert overlay["gateway"]["tolerations"] == []
    # The gateway chart resolves names through kube-dns, which OpenShift does not
    # serve, so the disabled gateway must not remain the push endpoint.
    assert overlay["promtail"]["config"]["clients"] == [{"url": LOKI_PUSH_URL}]


def test_promtail_overlay_empties_the_tolerations_and_moves_the_client():
    overlay = overlay_values(PROMTAIL_OVERLAY)

    assert set(overlay) == {"tolerations", "config"}
    assert overlay["tolerations"] == []
    assert overlay["config"]["clients"] == [{"url": LOKI_PUSH_URL}]


def test_the_overlay_push_url_keeps_the_kind_push_path():
    """Promtail posts to the URL it is given and appends nothing, so a client URL without the path
    is accepted by the chart, reports Ready, and ingests nothing. The overlay changes the host only."""
    kind = yaml.safe_load((BASE_DIR / "observer/loki/promtail-values.yaml").read_text())["config"]["clients"][0]["url"]
    kind_path = urlsplit(kind).path
    assert kind_path == "/loki/api/v1/push"

    for overlay in (
        overlay_values(PROMTAIL_OVERLAY)["config"]["clients"],
        overlay_values(LOKI_OVERLAY)["promtail"]["config"]["clients"],
    ):
        assert urlsplit(overlay[0]["url"]).path == kind_path


# --------------------------------------------------------------------------------- wiring


def test_prometheus_appends_its_overlay_only_on_openshift():
    prometheus = Prometheus()
    kind_configs = prometheus._requested_helm_configs()

    # `kind` is the untouched chain: the metadata config, with no extra argument at all.
    assert kind_configs == prometheus.helm_configs

    cluster_platform.set_platform(OPENSHIFT)

    assert prometheus._requested_helm_configs()["extra_args"] == ["-f", str(PROMETHEUS_OVERLAY)]


def test_loki_appends_its_overlay_only_on_openshift():
    loki = Loki()
    kind_configs = loki._requested_helm_configs()

    assert kind_configs == loki.helm_configs

    cluster_platform.set_platform(OPENSHIFT)

    assert loki._requested_helm_configs()["extra_args"] == [
        *loki.helm_configs["extra_args"],
        "-f",
        str(LOKI_OVERLAY),
    ]


def test_promtail_appends_its_overlay_only_on_openshift():
    loki = Loki()
    kind_configs = loki._promtail_helm_configs()

    assert kind_configs["extra_args"] == ["-f", loki.promtail_values_file]

    cluster_platform.set_platform(OPENSHIFT)

    assert loki._promtail_helm_configs()["extra_args"] == [
        "-f",
        loki.promtail_values_file,
        "-f",
        str(PROMTAIL_OVERLAY),
    ]


@pytest.mark.parametrize("requested", ["prometheus", "loki", "promtail"])
def test_a_platform_switch_changes_the_values_fingerprint(requested):
    """The overlay has to be part of the requested configs, or a reused release keeps the pins."""
    if requested == "prometheus":
        configs_for = Prometheus()._requested_helm_configs
    else:
        loki = Loki()
        configs_for = loki._requested_helm_configs if requested == "loki" else loki._promtail_helm_configs
    kind_fingerprint = Helm.requested_values_fingerprint(configs_for())

    cluster_platform.set_platform(OPENSHIFT)

    assert Helm.requested_values_fingerprint(configs_for()) != kind_fingerprint

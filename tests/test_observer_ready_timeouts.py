"""The observer readiness waits use the harness's one timeout, not a local 120 s.

`OtelCollector` and `IngressNginx` waited a hardcoded 120 s for their pods. On a
cold image cache that is not enough: the first `kind` validator run of this branch
died with `OTel Collector not ready within 120s` while the image was still pulling.
`WAIT_FOR_POD_READY_TIMEOUT` already exists for exactly this, defaults to 600 s and
reads `$WAIT_FOR_POD_READY_TIMEOUT`, so no new flag is introduced.
"""

import importlib

import pytest

from sregym.observer.ingress_nginx import IngressNginx
from sregym.observer.otel_collector.otel_collector import OtelCollector
from sregym.service import kubectl as kubectl_module

OBSERVERS = [OtelCollector, IngressNginx]


@pytest.mark.parametrize("observer_class", OBSERVERS)
def test_the_wait_defaults_to_the_shared_constant(observer_class):
    default = observer_class._wait_for_ready.__defaults__[0]

    assert default == kubectl_module.WAIT_FOR_POD_READY_TIMEOUT
    assert default != 120


@pytest.mark.parametrize("observer_class", OBSERVERS)
def test_deploy_lets_the_wait_choose_its_own_timeout(monkeypatch, observer_class):
    # A literal at the call site would defeat the constant, so deploy must pass none.
    observer = observer_class()
    calls = []
    monkeypatch.setattr(observer, "_wait_for_ready", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(observer, "run_cmd", lambda command: "")
    for helper in ("_create_jaeger_backend_service", "_remove_orphaned_cluster_objects"):
        if hasattr(observer, helper):
            monkeypatch.setattr(observer, helper, lambda *args, **kwargs: None)

    observer.deploy()

    assert calls == [((), {})]


def test_the_constant_reads_the_environment(monkeypatch):
    monkeypatch.setenv("WAIT_FOR_POD_READY_TIMEOUT", "42")

    reloaded = importlib.reload(kubectl_module)
    try:
        assert reloaded.WAIT_FOR_POD_READY_TIMEOUT == 42
    finally:
        monkeypatch.delenv("WAIT_FOR_POD_READY_TIMEOUT", raising=False)
        importlib.reload(kubectl_module)

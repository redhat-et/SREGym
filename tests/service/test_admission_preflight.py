"""The `anyuid` SCC grant is made visible, never taken.

Every path is also checked for the calls it does not make: the harness must not grant
the SCC itself, and a review it is not allowed to run must not be read as a grant.
"""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym import cluster_platform
from sregym.cluster_platform import KIND, OPENSHIFT
from sregym.conductor.conductor import Conductor
from sregym.service.admission_preflight import (
    CANNOT_VERIFY,
    GRANTED,
    MISSING,
    check_promtail_privileged,
    check_scc_grant,
    grant_commands,
    promtail_grant_command,
)

MODULE_LOGGER = "all.sregym.admission_preflight"
NAMESPACE = "hotel-reservation"
EXACT_COMMAND = "oc adm policy add-scc-to-group anyuid system:serviceaccounts:hotel-reservation"

# Every call on the APIs the harness could change the cluster through. A preflight
# that reaches any of them has stopped being read-only.
MUTATING_CALLS = (
    ("core_v1_api", "create_namespace"),
    ("core_v1_api", "patch_namespace"),
    ("core_v1_api", "replace_namespace"),
    ("core_v1_api", "delete_namespace"),
    ("core_v1_api", "create_namespaced_pod"),
    ("core_v1_api", "delete_namespaced_pod"),
    ("core_v1_api", "create_namespaced_service_account"),
    ("core_v1_api", "create_namespaced_resource_quota"),
    ("apps_v1_api", "patch_namespaced_deployment"),
    ("apps_v1_api", "replace_namespaced_deployment"),
    ("apps_v1_api", "delete_namespaced_deployment"),
)


@pytest.fixture(autouse=True)
def reset_platform(monkeypatch):
    monkeypatch.setattr(cluster_platform, "_platform", None)
    monkeypatch.delenv("SREGYM_PLATFORM", raising=False)


def _review(allowed: bool) -> client.V1SubjectAccessReview:
    return client.V1SubjectAccessReview(
        spec=client.V1SubjectAccessReviewSpec(),
        status=client.V1SubjectAccessReviewStatus(allowed=allowed),
    )


def _kubectl(*answers) -> MagicMock:
    """A KubeCtl whose review API answers `answers` in turn, granted when none are given.

    One answer answers every review, which is the common case here; several answer
    in turn; an exception raises.
    """
    kubectl = MagicMock()
    # Discovery answers as an OpenShift cluster does; the no-SCC case has its own test.
    kubectl.apis_api.get_api_versions.return_value.groups = [
        SimpleNamespace(name="security.openshift.io"),
        SimpleNamespace(name="apps"),
    ]
    review_api = kubectl.authorization_v1_api.create_subject_access_review
    first = answers[0] if answers else None
    if first is None:
        review_api.return_value = _review(True)
    elif len(answers) > 1:
        review_api.side_effect = list(answers)
    elif isinstance(first, Exception):
        review_api.side_effect = first
    else:
        review_api.return_value = first
    return kubectl


def _conductor(monkeypatch, platform, kubectl) -> Conductor:
    monkeypatch.setenv("SREGYM_PLATFORM", platform)
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.admission_preflight")
    conductor.kubectl = kubectl
    conductor.problem = SimpleNamespace(namespace=NAMESPACE, app=SimpleNamespace(namespaces=[NAMESPACE]))
    conductor.prometheus = SimpleNamespace(namespace="observe")
    conductor.mcp_server = SimpleNamespace(namespace="sregym")
    return conductor


def _module_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == MODULE_LOGGER]


def _assert_nothing_was_changed(kubectl) -> None:
    for api_name, call_name in MUTATING_CALLS:
        getattr(getattr(kubectl, api_name), call_name).assert_not_called()
    # No `oc` and no `kubectl`: the answer comes from the review API alone.
    kubectl.exec_command.assert_not_called()


def test_a_granted_scc_passes_with_one_debug_line(caplog):
    kubectl = _kubectl()

    result = check_scc_grant(kubectl, [NAMESPACE])

    assert result.state == GRANTED
    assert result.missing == ()
    assert result.unverified == ()
    assert [(record.levelno, "granted" in record.getMessage()) for record in _module_records(caplog)] == [
        (logging.DEBUG, True)
    ]


def test_a_missing_grant_returns_the_exact_command(caplog):
    result = check_scc_grant(_kubectl(_review(False)), [NAMESPACE])

    assert result.state == MISSING
    assert result.missing == (NAMESPACE,)
    assert grant_commands(result.missing) == [EXACT_COMMAND]
    # The refusal is the caller's; the module does not print a second copy of the command.
    assert not [record for record in _module_records(caplog) if record.levelno >= logging.WARNING]


def test_a_missing_grant_stops_the_run_and_names_the_namespace(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, _kubectl(_review(False)))

    with pytest.raises(RuntimeError) as error:
        conductor._verify_scc_grant()

    message = str(error.value)
    assert NAMESPACE in message
    assert EXACT_COMMAND in message
    # The prerequisite is the operator's to fix, and the harness says so.
    assert "harness never grants an SCC" in message


def test_the_run_checks_the_namespaces_it_deploys_into(monkeypatch):
    kubectl = _kubectl()
    conductor = _conductor(monkeypatch, OPENSHIFT, kubectl)

    conductor._verify_scc_grant()

    reviewed = [
        call.args[0].spec.groups[0] for call in kubectl.authorization_v1_api.create_subject_access_review.call_args_list
    ]
    assert reviewed == [
        "system:serviceaccounts:observe",
        "system:serviceaccounts:sregym",
        f"system:serviceaccounts:{NAMESPACE}",
    ]


def test_a_refused_review_cannot_verify_and_prints_the_command(caplog):
    kubectl = _kubectl(ApiException(status=403, reason="Forbidden"))

    result = check_scc_grant(kubectl, [NAMESPACE])

    assert result.state == CANNOT_VERIFY
    assert result.missing == ()
    assert result.unverified == (NAMESPACE,)
    warnings = [record.getMessage() for record in _module_records(caplog) if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Cannot verify" in warnings[0]
    assert EXACT_COMMAND in warnings[0]


def test_a_refused_review_continues_the_run(monkeypatch):
    conductor = _conductor(monkeypatch, OPENSHIFT, _kubectl(ApiException(status=403, reason="Forbidden")))

    # A project-scoped identity cannot read cluster-scoped SCC bindings, so this is a
    # warning and the run continues rather than being blocked with nothing to fix.
    conductor._verify_scc_grant()


def test_another_review_failure_is_not_read_as_a_verdict():
    kubectl = _kubectl(ApiException(status=500, reason="Internal Server Error"))

    with pytest.raises(ApiException):
        check_scc_grant(kubectl, [NAMESPACE])


def test_the_review_asks_for_the_namespace_group_not_a_user():
    kubectl = _kubectl()

    check_scc_grant(kubectl, [NAMESPACE])

    spec = kubectl.authorization_v1_api.create_subject_access_review.call_args.args[0].spec
    # A group review works before the namespace exists; a user review would need it.
    assert spec.user is None
    assert spec.groups == [f"system:serviceaccounts:{NAMESPACE}"]
    attributes = spec.resource_attributes
    assert (attributes.group, attributes.resource, attributes.name, attributes.verb) == (
        "security.openshift.io",
        "securitycontextconstraints",
        "anyuid",
        "use",
    )


@pytest.mark.parametrize("answer", [_review(True), _review(False), ApiException(status=403, reason="Forbidden")])
def test_no_mutating_api_call_is_made(answer):
    kubectl = _kubectl(answer)

    check_scc_grant(kubectl, ["observe", "sregym", NAMESPACE])

    _assert_nothing_was_changed(kubectl)


def test_the_preflight_cannot_shell_out_or_write():
    source = Path("sregym/service/admission_preflight.py").read_text()

    assert "subprocess" not in source
    assert "exec_command" not in source


def test_nothing_runs_on_kind(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, KIND, _kubectl(_review(False)))

    conductor._verify_scc_grant()

    # kind has no SCCs: no read, no log line, and no refusal.
    assert conductor.kubectl.mock_calls == []
    assert caplog.records == []


def test_a_cluster_without_the_scc_api_is_unverifiable_not_missing():
    """A plain Kubernetes cluster has no SCCs, so `allowed: false` means no such resource."""
    kubectl = MagicMock()
    kubectl.apis_api.get_api_versions.return_value.groups = [
        SimpleNamespace(name="apps"),
        SimpleNamespace(name="metrics.k8s.io"),
    ]

    result = check_scc_grant(kubectl, ["hotel-reservation"])

    assert result.state == CANNOT_VERIFY
    assert result.missing == ()
    kubectl.authorization_v1_api.create_subject_access_review.assert_not_called()


def test_discovery_failure_keeps_the_strict_path():
    """If the API list cannot be read, keep asking rather than assuming there is nothing to ask."""
    kubectl = _kubectl()
    kubectl.apis_api.get_api_versions.side_effect = RuntimeError("cannot reach the API server")

    result = check_scc_grant(kubectl, ["hotel-reservation"])

    assert result.state == GRANTED
    kubectl.authorization_v1_api.create_subject_access_review.assert_called_once()


# ------------------------------------------------------ Promtail's privileged SCC (warn-only)


def test_the_promtail_review_asks_about_the_service_account_user():
    """The documented grant is `add-scc-to-user -z promtail`, so a group-only review would always say no."""
    kubectl = _kubectl(_review(True))

    assert check_promtail_privileged(kubectl) is True

    spec = kubectl.authorization_v1_api.create_subject_access_review.call_args.args[0].spec
    assert spec.user == "system:serviceaccount:observe:promtail"
    assert spec.resource_attributes.name == "privileged"
    assert spec.resource_attributes.verb == "use"
    _assert_nothing_was_changed(kubectl)


def test_the_promtail_command_is_the_exact_documented_one():
    assert promtail_grant_command() == "oc adm policy add-scc-to-user privileged -z promtail -n observe"


def test_the_promtail_check_is_unknown_when_the_review_is_refused_or_there_is_no_scc_api():
    assert check_promtail_privileged(_kubectl(ApiException(status=403, reason="Forbidden"))) is None

    plain = MagicMock()
    plain.apis_api.get_api_versions.return_value.groups = [SimpleNamespace(name="apps")]
    assert check_promtail_privileged(plain) is None
    plain.authorization_v1_api.create_subject_access_review.assert_not_called()


def test_a_missing_promtail_grant_warns_and_never_refuses(monkeypatch, caplog):
    conductor = _conductor(
        monkeypatch, OPENSHIFT, _kubectl(_review(True), _review(True), _review(True), _review(False))
    )
    conductor.config = SimpleNamespace(deploy_loki=True)

    with caplog.at_level(logging.WARNING, logger="test.admission_preflight"):
        conductor._verify_scc_grant()  # anyuid granted for three namespaces; promtail answers no

    assert "unmeasured prerequisite" in caplog.text
    assert promtail_grant_command() in caplog.text


def test_the_promtail_warning_is_skipped_without_loki(monkeypatch, caplog):
    conductor = _conductor(monkeypatch, OPENSHIFT, _kubectl(_review(False)))
    conductor.config = SimpleNamespace(deploy_loki=False)
    conductor.kubectl.authorization_v1_api.create_subject_access_review.side_effect = [_review(True)] * 3

    with caplog.at_level(logging.WARNING, logger="test.admission_preflight"):
        conductor._verify_scc_grant()

    assert "unmeasured prerequisite" not in caplog.text

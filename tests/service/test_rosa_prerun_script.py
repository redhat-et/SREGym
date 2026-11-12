"""The operator's precondition script for a SREGym Lite run on OpenShift.

`scripts/rosa_prerun.sh` is the only place that may change a cluster on the operator's behalf, so what
it is allowed to do is worth pinning: it prints before it acts, a dry run changes nothing, it never
reaches for a namespaced or destructive verb, and every value it accepts can actually produce a working
cluster. The harness grants no permission at all; this script is the operator's, and these tests keep
the two apart.
"""

import shutil
import subprocess

import pytest

from sregym.paths import BASE_DIR

SCRIPT = BASE_DIR.parent / "scripts/rosa_prerun.sh"
TEXT = SCRIPT.read_text()

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def grants():
    """Every line that would change an SCC binding."""
    return [
        line.strip()
        for line in TEXT.splitlines()
        if "adm policy add-scc" in line or ("oc apply -f -" in line and "clusterrolebinding" in line.lower())
    ]


def executed_lines():
    """Lines that would run a destructive verb, with a shell line-continuation stripped."""
    return [
        line.strip().rstrip("\\").strip()
        for line in TEXT.splitlines()
        if line.strip().startswith(("oc delete", "helm uninstall", "kubectl delete"))
    ]


def test_the_script_parses():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_it_says_what_it_prepares_and_for_which_suite():
    for phrase in ("SREGym Lite", "--suite sregym-lite", "Promtail", "anyuid"):
        assert phrase in TEXT


def test_every_grant_is_printed_before_it_runs():
    """A grant that is not behind `run` would execute during a dry run."""
    for grant in grants():
        assert grant.startswith(("run oc adm policy", 'plan "oc adm policy', 'plan "oc apply')), grant


def test_a_dry_run_executes_no_grant():
    assert "DRY_RUN" in TEXT
    assert '[ "$DRY_RUN" = "1" ] && return 0' in TEXT


def test_it_deletes_only_its_own_stale_binding():
    """Leftovers are named with the command to run, never run. The single delete the script performs is
    its own `sregym-promtail-*` binding from an earlier PROMTAIL_SCC choice, so the service account
    cannot accumulate SCC grants; nothing namespaced and nothing of the operator's is touched."""
    executed = executed_lines()

    assert len(executed) == 1, executed
    assert executed[0].startswith("oc delete clusterrolebinding sregym-promtail-hostmount-anyuid")
    for forbidden in ("oc delete ns", "oc delete namespace", "delete pvc", "helm uninstall"):
        assert not any(line.startswith(forbidden) for line in executed), forbidden


def test_the_undo_command_is_printed_not_run():
    """The script adds and reports; removing the operator's own standing binding is the operator's act."""
    assert "oc delete clusterrolebinding sregym-promtail-privileged" in TEXT
    assert not any(
        "sregym-promtail-privileged" in line and line.startswith("oc delete") for line in executed_lines()
    )


def test_it_never_asks_for_elevated_privileges():
    assert "sudo" not in TEXT
    assert "su " not in TEXT.replace("usage", "")


def test_promtail_is_granted_by_default_with_the_scc_that_works():
    """Measured on ROSA: without a grant Promtail's pods are refused, and with `hostmount-anyuid` they
    are admitted and cannot read `/var/log/pods`. `privileged` is the only value that works, so it is the
    default and `hostmount-anyuid` is not offered as an alternative."""
    assert 'PROMTAIL_SCC="${PROMTAIL_SCC:-privileged}"' in TEXT
    for choice in ("privileged", "none", "hostmount-anyuid"):
        assert choice in TEXT


def test_hostmount_anyuid_is_refused_because_it_cannot_work():
    """The overlay asks for a privileged container and `hostmount-anyuid` does not admit one, so that
    value can never produce a working Promtail. Refused, rather than warned about while the script still
    exits 0 and calls the cluster ready with Loki guaranteed empty."""
    branch = TEXT[TEXT.index("  hostmount-anyuid)") : TEXT.index("  *)")]
    assert "failed " in branch
    assert "cannot work" in branch
    assert "ready" not in branch


def test_only_the_working_value_applies_a_binding():
    applied = [line.strip() for line in TEXT.splitlines() if line.strip().startswith("if cat <<EOF | oc apply")]

    assert applied, "the privileged binding is applied with a heredoc"
    assert "system:openshift:scc:$PROMTAIL_SCC" not in TEXT
    assert "name: system:openshift:scc:privileged" in TEXT


def test_a_previous_choice_is_pruned():
    """Switching values must not leave both bindings on the service account."""
    assert "oc delete clusterrolebinding sregym-promtail-hostmount-anyuid" in TEXT


def test_promtails_grant_survives_the_namespace_it_names():
    """`--teardown-observers` deletes `observe`, and a RoleBinding inside it would go with it, so the
    grant is a ClusterRoleBinding naming the service account identity. Measured: a pod created after the
    namespace-scoped binding was removed was still admitted with `scc=privileged`."""
    assert "kind: ClusterRoleBinding" in TEXT
    assert "name: promtail" in TEXT and "namespace: observe" in TEXT
    assert "survives" in TEXT


def test_the_checks_a_run_needs_are_reported_not_assumed():
    for check in (
        "SREGYM_ALLOW_UNVERIFIED_PLATFORM",
        "ANTHROPIC_API_KEY",
        "storageclass",
        "Worker capacity",
        "harness process",
    ):
        assert check in TEXT


def test_the_next_command_carries_the_flags_an_openshift_run_requires():
    for flag in ("--suite sregym-lite", "--platform openshift", "--internet-access open", "--teardown-observers"):
        assert flag in TEXT


def test_the_harness_infrastructure_is_scanned_by_its_neutral_label():
    assert "-l app.kubernetes.io/instance=observability-stack" in TEXT


def test_the_sregym_owned_label_survives_only_as_a_legacy_scan():
    """`sregym.io/owned` is no longer an ownership marker. Every line naming it must sit in the legacy scan,
    and that scan only reports, so it never deletes."""
    legacy_start = TEXT.index("# Legacy: an earlier harness version")
    legacy_end = TEXT.index("# Fixed names the scenarios create.")
    legacy_block = TEXT[legacy_start:legacy_end]
    assert "sregym.io/owned" in legacy_block
    assert TEXT.count("sregym.io/owned") == legacy_block.count("sregym.io/owned")
    assert "not labelled as harness infrastructure on purpose" in TEXT
    assert "not labelled sregym.io/owned on purpose" not in TEXT


def test_the_fixed_names_the_scenarios_create_are_checked_read_only():
    for name in (
        "platform-ops",
        "gatekeeper-mutating-webhook-configuration",
        "cert-manager-webhook",
        "istio-sidecar-injector",
        "kyverno-resource-mutating-webhook-cfg",
        "linkerd-proxy-injector-webhook-config",
        "configmap-cleanup-controller",
    ):
        assert name in TEXT, name
    fixed_start = TEXT.index("# Fixed names the scenarios create.")
    fixed_end = TEXT.index(
        "# ------------------------------------------------------------------------------------------------ 6. next"
    )
    fixed_block = TEXT[fixed_start:fixed_end]
    assert "oc get " in fixed_block
    for verb in ("delete", "apply", "patch", "create", "label"):
        assert f"oc {verb}" not in fixed_block, verb
    assert "refuse rather than adopt" in TEXT

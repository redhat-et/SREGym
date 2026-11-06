"""Named sets of registered SREGym problems.

``PLATFORM_SUPPORT`` is the harness's refusal list: what a Lite scenario is
allowed to attempt on OpenShift, and why not when it is refused.
``docs/openshift.md`` is the human statement of the same table.
"""

SREGYM_LITE_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "edge_request_filter_cpu_saturation",
    "network_policy_block",
    "env_variable_shadowing_astronomy_shop",
    "mutating_webhook_resource_limits_social_network",
    "finalizer_deadlock_controller_hotel_reservation",
    "kafka_poison_pill_hol_block",
    "internal_traffic_policy_local_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "namespace_memory_limit",
    "valkey_auth_disruption",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
    "duplicate_pvc_mounts_social_network",
    "rolling_update_misconfigured_social_network",
    "search_rate_retry_collapse_hotel_reservation",
)

# SREGym-Lite's faults re-targeted at the larger Incident Arena apps (Frappe,
# Saleor, Slack Spine), in Lite order; see problems/environment_ports.py.
SREGYM_LITE_IA_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_frappe",
    "edge_request_filter_cpu_saturation_frappe",
    "network_policy_block_slack_spine",
    "env_variable_shadowing_saleor",
    "mutating_webhook_resource_limits_slack_spine",
    "finalizer_deadlock_controller_frappe",
    "kafka_poison_pill_hol_block_slack_spine",
    "internal_traffic_policy_local_slack_spine",
    "service_dns_resolution_failure_slack_spine",
    "service_wrong_pod_selection_frappe",
    "namespace_memory_limit_saleor",
    "redis_auth_disruption_slack_spine",
    "secret_rotation_stale_env_credentials_saleor",
    "unschedulable_incorrect_port_assignment_frappe",
    "duplicate_pvc_mounts_slack_spine",
    "rolling_update_misconfigured_frappe",
    "search_rate_retry_collapse_slack_spine",
)

PROBLEM_SETS = {
    "sregym-lite": SREGYM_LITE_PROBLEMS,
    "sregym-lite-ia": SREGYM_LITE_IA_PROBLEMS,
}

# The three verdicts a scenario can carry. ``works`` needs no work before the
# plan's items land, ``fixable`` is blocked by something named in its reason,
# and ``exclude`` cannot be expressed on OpenShift without changing the fault.
PLATFORM_VERDICTS = ("works", "fixable", "exclude")

# Verdicts for OpenShift, read from section 4 of
# .openshift-compatibility/OPENSHIFT-PLAN.md (the code-read table), not measured
# here. ``reason`` names the blocker that keeps a scenario off ``works``;
# ``requires`` names the right or capacity an operator has to supply, or None.
#
# The shared blockers plan section 4.1 records for every scenario (the
# application-admission grant, the cluster bootstrap) are stated once in
# docs/openshift.md; a row names them only where nothing more specific blocks it.
PLATFORM_SUPPORT = {
    "cronjob_sidecar_blocks_completion_hotel_reservation": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "the injected sidecar and every other change is namespaced"
        ),
        "requires": "anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    "edge_request_filter_cpu_saturation": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "`kubectl exec` only; the chart's `runAsUser: 101` is admitted under `anyuid`"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "network_policy_block": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "a namespaced NetworkPolicy only"
        ),
        "requires": "anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    "env_variable_shadowing_astronomy_shop": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "namespaced env edits; the chart's `runAsUser: 1001` is admitted under `anyuid`"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "mutating_webhook_resource_limits_social_network": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "five cluster-scoped `MutatingWebhookConfiguration` objects and namespace `platform-ops`, all removed at teardown"
        ),
        "requires": "cluster-admin; anyuid grant for the app namespace",
    },
    "finalizer_deadlock_controller_hotel_reservation": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "writes a `ClusterRole` and a `ClusterRoleBinding`; recovery keeps the controller the oracle needs and the teardown collects the owned RBAC"
        ),
        "requires": "cluster-admin; anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    "kafka_poison_pill_hol_block": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "`kubectl exec` into the in-namespace broker"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "internal_traffic_policy_local_astronomy_shop": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "pins a pod away from its caller; the node picker skips tainted and cordoned nodes (ROSA's infra workers)"
        ),
        "requires": "two or more untainted schedulable workers; anyuid grant for the app namespace",
    },
    "service_dns_resolution_failure_social_network": {
        "verdict": "exclude",
        "reason": (
            "writes `kube-system/coredns` and restarts `deployment coredns`; OpenShift serves DNS from "
            "`openshift-dns/dns-default`"
        ),
        "requires": None,
    },
    "service_wrong_pod_selection_hotel_reservation": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "label and Service selector edits inside the app namespace only"
        ),
        "requires": "anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    "namespace_memory_limit": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "a namespaced `ResourceQuota`; it refuses when a memory quota exists, and a fresh project is clean"
        ),
        "requires": "anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    "valkey_auth_disruption": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "`kubectl exec ... CONFIG SET requirepass` inside the in-namespace Valkey pod"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "secret_rotation_stale_env_credentials_astronomy_shop": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "namespaced Secret, ConfigMap and env edits only"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "unschedulable_incorrect_port_assignment": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "a deliberate `nodeSelector` in the app namespace"
        ),
        "requires": "anyuid grant for the app namespace",
    },
    "duplicate_pvc_mounts_social_network": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "RWO PVC plus anti-affinity, with the injector naming no `storageClassName`"
        ),
        "requires": "two or more schedulable workers; anyuid grant for the app namespace",
    },
    "rolling_update_misconfigured_social_network": {
        "verdict": "works",
        "reason": "passed end to end on ROSA 4.22.18 (validate_problem, damage verdict CLEAN) once the Social Network chart's nginx resolver names OpenShift's dns-default; the platform overlay supplies that",
        "requires": "anyuid grant for the app namespace",
    },
    "search_rate_retry_collapse_hotel_reservation": {
        "verdict": "works",
        "reason": (
            "passed the oracle lifecycle on ROSA 4.22.18 with a clean or owned damage verdict (logs/rosa-oracle-all): "
            "env edits only; the load arrives through `kubectl port-forward`"
        ),
        "requires": "anyuid grant for the app namespace; qualified image references (scripts/qualify_submodule_images.py)",
    },
    # The ports carry the verdict of the fault they were adapted from. Where the
    # Lite reason is a chart pin of the Lite application, it does not transfer, so
    # the port names the shared application-admission grant instead; no read of an
    # Incident Arena application under restricted-v2 exists yet.
    "cronjob_sidecar_blocks_completion_frappe": {
        "verdict": "fixable",
        "reason": "the injected sidecar runs unqualified `busybox:1.36`; everything else is namespaced",
        "requires": "qualified image references (plan item 8)",
    },
    "edge_request_filter_cpu_saturation_frappe": {
        "verdict": "fixable",
        "reason": "the fault is `kubectl exec` only, so the shared application-admission grant is the blocker",
        "requires": "anyuid grant for the app namespace",
    },
    "network_policy_block_slack_spine": {
        "verdict": "fixable",
        "reason": "a namespaced NetworkPolicy only",
        "requires": "anyuid grant for the app namespace",
    },
    "env_variable_shadowing_saleor": {
        "verdict": "fixable",
        "reason": "namespaced env edits only, so the shared application-admission grant is the blocker",
        "requires": "anyuid grant for the app namespace",
    },
    "mutating_webhook_resource_limits_slack_spine": {
        "verdict": "fixable",
        "reason": "a cluster-scoped `MutatingWebhookConfiguration`",
        "requires": "cluster-admin",
    },
    "finalizer_deadlock_controller_frappe": {
        "verdict": "fixable",
        "reason": "a cluster-scoped `ClusterRole` and its binding",
        "requires": "cluster-admin",
    },
    "kafka_poison_pill_hol_block_slack_spine": {
        "verdict": "fixable",
        "reason": "`kubectl exec` into the in-namespace Redpanda pod; the images are already qualified",
        "requires": None,
    },
    "internal_traffic_policy_local_slack_spine": {
        "verdict": "fixable",
        "reason": "the fault pins a pod away from its caller, so it needs two or more schedulable workers",
        "requires": "two or more schedulable workers",
    },
    "service_dns_resolution_failure_slack_spine": {
        "verdict": "exclude",
        "reason": (
            "writes `kube-system/coredns` and restarts `deployment coredns`; OpenShift serves DNS from "
            "`openshift-dns/dns-default`"
        ),
        "requires": None,
    },
    "service_wrong_pod_selection_frappe": {
        "verdict": "fixable",
        "reason": "label and Service selector edits inside the app namespace only",
        "requires": "anyuid grant for the app namespace",
    },
    "namespace_memory_limit_saleor": {
        "verdict": "fixable",
        "reason": "a namespaced `ResourceQuota` on a fresh project",
        "requires": None,
    },
    "redis_auth_disruption_slack_spine": {
        "verdict": "fixable",
        "reason": "`kubectl exec … CONFIG SET requirepass` inside the in-namespace Redis pod",
        "requires": None,
    },
    "secret_rotation_stale_env_credentials_saleor": {
        "verdict": "fixable",
        "reason": "namespaced Secret, ConfigMap and env edits only",
        "requires": "anyuid grant for the app namespace",
    },
    "unschedulable_incorrect_port_assignment_frappe": {
        "verdict": "fixable",
        "reason": "a deliberate `nodeSelector` to a node that does not exist",
        "requires": None,
    },
    "duplicate_pvc_mounts_slack_spine": {
        "verdict": "fixable",
        "reason": "the injector's PVC names no `storageClassName`, and RWO plus anti-affinity needs two or more workers",
        "requires": "two or more schedulable workers",
    },
    "rolling_update_misconfigured_frappe": {
        "verdict": "fixable",
        "reason": "the injected init container image is qualified now; the image reference is qualified now, so only a cluster run can move this verdict",
        "requires": "qualified image references (plan item 8)",
    },
    "search_rate_retry_collapse_slack_spine": {
        "verdict": "fixable",
        "reason": "namespaced Service and retry edits only",
        "requires": None,
    },
}

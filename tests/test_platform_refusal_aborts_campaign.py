"""A platform refusal ends the campaign, not one problem at a time.

A refusal is a statement about the cluster, so running the next problem against it repeats a
check that has already failed and records a `deploy_failed` row that looks like a benchmark
fault. These tests pin the message and pin that the abort carries the one row it wrote.
"""

import importlib.util
import sys
from pathlib import Path


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_refusal_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_message_names_the_refusal_and_the_cause():
    benchmark_main = _load_main_module()
    cause = benchmark_main.PlatformVerificationError("declared openshift, cluster reports kind")

    message = benchmark_main.platform_refusal_abort_reason(cause)

    assert "does not confirm the declared platform" in message
    assert "declared openshift, cluster reports kind" in message
    assert "\n" not in message, "an operator reads this once, so it is one line"


def test_the_abort_carries_the_single_row_it_wrote():
    """The abort is what the driver raises, and it carries the one row already written."""
    benchmark_main = _load_main_module()
    rows = [{"problem_id": "network_policy_block", "attempt": 1, "deploy_failed": True}]
    cause = benchmark_main.PlatformVerificationError("declared openshift, cluster reports kind")

    abort = benchmark_main.campaign_abort_for_platform_refusal(cause, [{"claudecode": rows}])

    assert isinstance(abort, benchmark_main.BenchmarkCampaignAborted)
    assert abort.partial_results == [{"claudecode": rows}]
    assert "network_policy_block" in abort.partial_results[0]["claudecode"][0]["problem_id"]


def test_the_driver_aborts_instead_of_skipping_to_the_next_problem():
    """The driver loop must raise, not log a skip line and carry on."""
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    branch = source.split("if platform_refusal is not None:", 1)

    assert len(branch) == 2, "the refusal branch is gone"
    body = branch[1].split("console.log(f\"\u23ed", 1)[0]
    assert "campaign_abort_for_platform_refusal(" in body
    assert "stop_k8s_proxy()" in body

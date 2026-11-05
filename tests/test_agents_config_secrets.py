"""No credential may sit in the tracked agent registry.

``agents.yaml`` is a tracked file. A gateway key, a provider token and anything else that authenticates
a run belong in the environment of the run, not in a file that is one commit away from history. This
module reads the *committed* registry through git, so a developer whose local copy holds a real key
still passes: that copy is theirs, and it is not what this test is about.

A value may be an environment reference such as ``${GATEWAY_KEY}``. Resolution at launch is not
implemented yet; see ``.openshift-compatibility/EXECUTION.md``, item 1.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Values that are not credentials by construction: a URL, a path, a number, a mode name, or a
# placeholder the file tells the reader to replace. Checked before the shape rules, so a UUID of
# zeroes and a ``<your-api-key>`` cannot trip an opaque-token rule.
KNOWN_SAFE_VALUES = {
    "http://host.docker.internal:9283/mcp",
    "https://app.tierzero.ai",
    "<your-api-key>",
    "/usr/local/bin/ct",
    "/REPLACE-WITH-YOUR-CLOUDTHINKER-CHECKOUT",
    "00000000-0000-0000-0000-000000000000",
    "model:REPLACE-WITH-YOUR-MODEL",
    "model:scoped",
    "scoped",
    "rca_flow",
    "3600",
    "20",
    "80",
    "0.667",
}

REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

CREDENTIAL_SHAPES = (
    # Provider and platform key prefixes.
    re.compile(r"^(sk|rk|pk)[-_][A-Za-z0-9_-]{8,}$"),
    re.compile(r"^(ghp|gho|ghu|ghs|github_pat|glpat)_[A-Za-z0-9_-]{8,}$"),
    re.compile(r"^(xox[abpsr])-[A-Za-z0-9-]{8,}$"),
    re.compile(r"^(AKIA|ASIA|AIza|hf_)[A-Za-z0-9_-]{8,}$"),
    # A JSON Web Token.
    re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
    # An opaque secret: one long word of key-ish characters mixing case and digits.
    re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])[A-Za-z0-9+/=_-]{24,}$"),
)


def committed_registry_text(path: str = "agents.yaml") -> str:
    """The registry as committed in HEAD, not as it sits in the working tree."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        pytest.skip(f"{path} is not tracked in HEAD")
    return shown.stdout


def committed_env_values() -> list[tuple[str, str, str]]:
    """(agent, variable, value) for every committed ``kickoff_env`` entry."""
    data = yaml.safe_load(committed_registry_text()) or {"agents": []}
    out = []
    for agent in data.get("agents", []):
        for key, value in (agent.get("kickoff_env") or {}).items():
            out.append((agent["name"], key, str(value)))
    return out


def looks_like_a_credential(value: str) -> bool:
    if value in KNOWN_SAFE_VALUES or REFERENCE.match(value):
        return False
    if value.startswith(("http://", "https://", "/")):
        return False
    if "<" in value and ">" in value:  # a documented placeholder
        return False
    return any(shape.match(value) for shape in CREDENTIAL_SHAPES)


def test_committed_registry_holds_no_credential_shaped_value():
    offenders = [
        f"{agent}.{key} = {value[:8]}…"
        for agent, key, value in committed_env_values()
        if looks_like_a_credential(value)
    ]

    assert offenders == [], (
        "A tracked agents.yaml must not hold a credential. Put the environment reference in the file "
        f"and the value in the run environment instead. Offending entries: {offenders}"
    )


def test_the_registry_still_has_kickoff_env_entries():
    # Without this, a registry that parsed to nothing would pass the secret scan vacuously.
    values = committed_env_values()

    assert len(values) >= 10
    assert any(key == "CT_BIN" for _agent, key, _value in values)


def test_no_gateway_key_reached_the_committed_registry():
    # The regression this guards: a corporate gateway key was staged in agents.yaml on a side branch,
    # one commit away from history.
    text = committed_registry_text().lower()

    assert "enmaas" not in text
    assert "api_key" not in text.replace("org_api_key", "").replace("<your-api-key>", "")


def test_the_credential_shapes_are_the_ones_we_think():
    # The allowlist and the shapes must not be so loose that the test above can never fail.
    assert looks_like_a_credential("sk-proj-abcdefgh12345678")
    assert looks_like_a_credential("ghp_abcdefgh12345678")
    assert looks_like_a_credential("AKIAIOSFODNN7EXAMPLE")
    assert looks_like_a_credential("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc")
    assert looks_like_a_credential("AbCdEf0123456789AbCdEf0123456789")
    assert not looks_like_a_credential("${GATEWAY_KEY}")
    assert not looks_like_a_credential("https://app.tierzero.ai")
    assert not looks_like_a_credential("<your-api-key>")

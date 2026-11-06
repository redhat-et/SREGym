"""The image qualifier rewrites only unqualified `image:` lines."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "qualify_submodule_images", Path(__file__).resolve().parent.parent / "scripts" / "qualify_submodule_images.py"
)
q = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = q
_spec.loader.exec_module(q)


def test_bare_and_namespaced_names_get_docker_hub():
    assert q.qualify("mongo:4.4.6") == "docker.io/library/mongo:4.4.6"
    assert q.qualify("hashicorp/consul:1.22.3") == "docker.io/hashicorp/consul:1.22.3"


def test_only_listed_image_lines_change_and_layout_is_kept():
    text = "  - image: mongo:4.4.6\n    name: mongo:4.4.6\n  - image: ghcr.io/x/y:1\n"
    new, count = q.rewrite_text(text, {"mongo:4.4.6"})
    assert count == 1
    assert new == "  - image: docker.io/library/mongo:4.4.6\n    name: mongo:4.4.6\n  - image: ghcr.io/x/y:1\n"

"""Storage class selection.

SREGym provisions volumes through a storage class it does not own: the harness
installs OpenEBS and every chart that asks for storage names ``openebs-hostpath``,
which the harness then marks as the cluster default. A managed platform serves its
own default class instead (for example ``gp3-csi`` on AWS), and the OpenEBS class
either does not exist there or must not be made default, so a run on that platform
has to name the class the cluster offers.

The class changes where a run's data lives, so it is declared explicitly
(``--storage-class`` / ``$SREGYM_STORAGE_CLASS``) rather than defaulted from what
the cluster happens to serve. Read it with :func:`get_storage_class`, or with
:func:`uses_openebs`. It is a module-level singleton rather than a field on
``ConductorConfig`` because ``Application`` instances and injectors are
constructed deep inside ``ProblemRegistry`` with no access to the conductor's
config.
"""

import logging
import os
import re

OPENebs = "openebs-hostpath"

_ENV_VAR = "SREGYM_STORAGE_CLASS"

# A storage class name is a Kubernetes object name, so it is a DNS-1123 subdomain:
# lowercase alphanumerics, '-' and '.', starting and ending with an alphanumeric.
_DNS1123_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
_MAX_NAME_LENGTH = 253

logger = logging.getLogger("all.sregym.storage")

_storage_class: str | None = None


def set_storage_class(name: str) -> None:
    """Set the active storage class. Called once from main.py before any deployment."""
    global _storage_class
    _storage_class = name
    logger.info(f"Storage class: {name}")


def get_storage_class() -> str:
    """Return the active storage class, falling back to $SREGYM_STORAGE_CLASS then OpenEBS.

    The env fallback exists so that code paths which bypass main.py (tests, the
    measurement harness, external harnesses) can still select a storage class.
    """
    if _storage_class is not None:
        return _storage_class
    env = os.environ.get(_ENV_VAR)
    if env:
        return env
    return OPENebs


def uses_openebs() -> bool:
    """Return True when the active storage class is the OpenEBS local hostpath class."""
    return get_storage_class() == OPENebs


def validate_storage_class_name(name: str) -> str:
    """Return ``name`` if it can name a storage class, else raise ValueError.

    The name reaches a cluster object, so it is checked before it is used. This is
    the only validator: both entry points call it on the CLI value.
    """
    if len(name) > _MAX_NAME_LENGTH or not _DNS1123_SUBDOMAIN.fullmatch(name):
        raise ValueError(
            f"Invalid storage class name {name!r}: a storage class name is a DNS-1123 subdomain, so it holds "
            "only lowercase alphanumerics, '-' and '.', it starts and ends with an alphanumeric, and it is at "
            f"most {_MAX_NAME_LENGTH} characters long."
        )
    return name

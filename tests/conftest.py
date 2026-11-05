"""Test-session setup.

`logger.init_logger()` installs handlers on the ``all`` logger and sets
``propagate = False`` on it, so records from ``all.*`` never reach pytest's root
handler. A test that asserts on ``caplog`` therefore passes when it runs alone
and fails inside the full suite, depending on which test initialised logging
first. Three separate failures in this repository had that cause.

The fixture below makes ``caplog`` deterministic for every test: the ``all``
logger propagates again, and the capture level covers everything the harness
logs. Production behaviour is untouched — this only applies inside a test.
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _logs_reach_caplog(caplog):
    all_logger = logging.getLogger("all")
    previous = all_logger.propagate
    all_logger.propagate = True
    caplog.set_level(logging.DEBUG)
    try:
        yield
    finally:
        all_logger.propagate = previous

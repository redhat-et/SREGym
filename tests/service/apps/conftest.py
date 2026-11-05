"""Pin the storage-class selection so an application render is reproducible."""

import pytest

from sregym import storage


@pytest.fixture(autouse=True)
def reset_storage_class(monkeypatch):
    """Clear the singleton and the environment fallback around every application test.

    Without this, a test that selects another class would leak it into the next test,
    and a developer's exported $SREGYM_STORAGE_CLASS would change what `get_storage_class`
    returns here. A test that selects a class sets the singleton through `monkeypatch`
    itself, so its selection is undone with it.
    """
    monkeypatch.setattr(storage, "_storage_class", None)
    monkeypatch.delenv("SREGYM_STORAGE_CLASS", raising=False)

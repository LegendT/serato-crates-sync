"""Guard the public API of each feature module.

Each module declares ``__all__`` to make its public surface explicit.
This test ensures every name in ``__all__`` actually exists in the
module — so a rename or deletion that forgets to update ``__all__``
shows up as a test failure rather than silently breaking
``from foo import *`` callers.
"""

import importlib

import pytest

MODULES = [
    "serato_crates_sync.library",
    "serato_crates_sync.sync",
    "serato_crates_sync.diagnose",
    "serato_crates_sync.verify_paths",
    "serato_crates_sync.fix_paths",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_all_names_exist(module_name):
    module = importlib.import_module(module_name)
    assert hasattr(module, "__all__"), f"{module_name} should declare __all__"
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert missing == [], f"{module_name}.__all__ references missing names: {missing}"


@pytest.mark.parametrize("module_name", MODULES)
def test_all_is_a_list_of_strings(module_name):
    module = importlib.import_module(module_name)
    assert isinstance(module.__all__, list)
    for name in module.__all__:
        assert isinstance(name, str)
        assert name and not name.startswith("_"), (
            f"{module_name}.__all__ should not include private name {name!r}"
        )

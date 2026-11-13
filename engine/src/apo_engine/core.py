"""Compatibility facade for the decomposed index engine.

The implementation module remains the canonical mutable module object so
existing monkeypatches such as ``apo_engine.core.embed`` continue to change the
globals resolved by indexing and retrieval functions at call time.
"""

from __future__ import annotations

import sys
from types import ModuleType

from . import indexing as _implementation

for _name, _value in vars(_implementation).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


class _CoreFacade(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if not name.startswith("__"):
            _implementation._set_compat_attr(name, value)


sys.modules[__name__].__class__ = _CoreFacade

"""agentflow — portable SDLC orchestration package (ported from the orchestrator skill's lib)."""

import importlib
from typing import TYPE_CHECKING

__all__ = [
    "config",
    "ConfigError",
    "Environment",
    "Project",
    "config_home",
    "load_environment",
    "load_project",
    "render_resolved_block",
    "resolve_project_name",
]

if TYPE_CHECKING:  # type-checker visibility only — no import at runtime
    from . import config
    from .config import (
        ConfigError,
        Environment,
        Project,
        config_home,
        load_environment,
        load_project,
        render_resolved_block,
        resolve_project_name,
    )


def __getattr__(name: str):
    """Resolve the ``.config`` re-exports on first access (PEP 562).

    Same import surface as the old eager ``from .config import ...`` — but because
    importing the package no longer imports the submodule, ``python -m
    agentflow.config`` runs without runpy's double-import RuntimeWarning.
    """
    if name in __all__:
        # import_module, NOT `from . import config`: the latter re-enters this
        # __getattr__ through _handle_fromlist and recurses.
        _config = importlib.import_module(".config", __name__)
        if name == "config":
            return _config
        return getattr(_config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)

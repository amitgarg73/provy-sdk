"""
Provy SDK.

Importing `provy` is lightweight: it pulls in only the ingest client (REST + OTel
exporter), which needs `requests`. The optional pieces load on first use and tell
you which extra to install if it is missing:

  - the LLM-as-judge          → pip install "provy-sdk[judge]"   (anthropic)
  - the local eval/RCA engine → pip install "provy-sdk[engine]"  (supabase; legacy
                                direct-DB path — prefer the ingest API)

So a tenant who just wants to send traces installs the base package and nothing else.
"""
from __future__ import annotations

import importlib
from typing import Any

# ── Eager, lightweight (ingest client — requests only) ──────────────────────────
from provy.client import ProvyClient, ProvyExporter
from provy.session import TraceLogger
from provy.evals import write_eval

__version__ = "0.5.0"

# ── Lazy, heavy (loaded on first access; mapped to the extra that provides them) ──
#   name -> (module, attribute, extra)
_LAZY: dict[str, tuple[str, str, str]] = {
    # LLM-as-judge (anthropic)
    "evaluate_session_outputs":  ("provy.judge", "evaluate_session_outputs", "judge"),
    # local eval + pattern + RCA engine (supabase, legacy direct-DB)
    "EvalResult":                ("provy.engine", "EvalResult", "engine"),
    "Incident":                  ("provy.engine", "Incident", "engine"),
    "run_evals_from_config":     ("provy.engine", "run_evals_from_config", "engine"),
    "run_all_detectors":         ("provy.engine", "run_all_detectors", "engine"),
    "run_quality_detectors":     ("provy.engine", "run_quality_detectors", "engine"),
    "run_evals_and_persist":     ("provy.engine", "run_evals_and_persist", "engine"),
    "run_detectors_and_persist": ("provy.engine", "run_detectors_and_persist", "engine"),
    "compute_shadow_cb_fires":   ("provy.engine", "compute_shadow_cb_fires", "engine"),
    "build_annotated_call_stack":("provy.engine", "build_annotated_call_stack", "engine"),
    "generate_fix_suggestion":   ("provy.engine", "generate_fix_suggestion", "engine"),
    "summarize_incident":        ("provy.engine", "summarize_incident", "engine"),
    "load_pipeline_config":      ("provy.engine", "load_pipeline_config", "engine"),
    "load_eval_configs":         ("provy.engine", "load_eval_configs", "engine"),
    "load_pipeline_agents":      ("provy.engine", "load_pipeline_agents", "engine"),
    "register_eval":             ("provy.engine", "register_eval", "engine"),
    "get_registry":              ("provy.engine", "get_registry", "engine"),
}


def __getattr__(name: str) -> Any:  # PEP 562 — module-level lazy attributes
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'provy' has no attribute {name!r}")
    module, attr, extra = target
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{name!r} needs the '{extra}' extra. Install it with: "
            f'pip install "provy-sdk[{extra}]"'
        ) from exc
    return getattr(mod, attr)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    # ingest client (base)
    "ProvyClient",
    "ProvyExporter",
    "TraceLogger",
    "write_eval",
    # LLM-as-judge (extra: judge)
    "evaluate_session_outputs",
    # local engine (extra: engine)
    "EvalResult",
    "Incident",
    "run_evals_from_config",
    "run_all_detectors",
    "run_quality_detectors",
    "run_evals_and_persist",
    "run_detectors_and_persist",
    "compute_shadow_cb_fires",
    "build_annotated_call_stack",
    "generate_fix_suggestion",
    "summarize_incident",
    "load_pipeline_config",
    "load_eval_configs",
    "load_pipeline_agents",
    "register_eval",
    "get_registry",
]

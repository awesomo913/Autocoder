"""User-editable model configuration loaded from ~/.autocoder/models.json.

The one place a user turns things on/off, picks preferred models, and
resets state between runs. Lives outside the broadcast code so it can
be tweaked without touching anything else.

File format (auto-created on first run):

    {
      "ollama": {
        "host": "http://127.0.0.1:11434",
        "enabled_models": [],
        "auto_rank": true,
        "preferred_model": null
      },
      "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "min_context": 32000,
        "enabled_model_ids": [],
        "auto_rank": true
      },
      "pressure_test": {
        "skip_models": ["Gemini", "Claude"],
        "iteration_focuses": [
            "initial_build", "pressure_test",
            "deep_dive", "review_grade"
        ],
        "per_model_timeout_sec": 600
      },
      "reset_on_launch": true
    }

Every autocoder entry point reads this at startup. `reset_on_launch`
wipes the session state (conversation history, claimed-tab registry,
cache) so each run starts clean.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".autocoder" / "models.json"


@dataclass
class OllamaConfig:
    host: str = "http://127.0.0.1:11434"
    enabled_models: list[str] = field(default_factory=list)
    auto_rank: bool = True
    preferred_model: Optional[str] = None
    num_ctx: int = 8192
    temperature: float = 0.2


@dataclass
class OpenRouterConfig:
    api_key_env: str = "OPENROUTER_API_KEY"
    min_context: int = 32_000
    enabled_model_ids: list[str] = field(default_factory=list)
    auto_rank: bool = True
    temperature: float = 0.2


@dataclass
class PressureTestConfig:
    skip_models: list[str] = field(
        default_factory=lambda: ["Gemini", "Claude"]
    )
    iteration_focuses: list[str] = field(
        default_factory=lambda: [
            "initial_build", "pressure_test",
            "deep_dive", "review_grade",
        ]
    )
    per_model_timeout_sec: int = 600


@dataclass
class ModelConfig:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    pressure_test: PressureTestConfig = field(default_factory=PressureTestConfig)
    reset_on_launch: bool = True


# ── I/O ──────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path = CONFIG_PATH) -> ModelConfig:
    """Load user config. Auto-creates a default file on first run."""
    if not path.exists():
        cfg = ModelConfig()
        save_config(cfg, path)
        logger.info("Created default model config at %s", path)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to parse %s: %s — using defaults", path, e)
        return ModelConfig()

    # Nested-dataclass friendly load
    cfg = ModelConfig(
        ollama=OllamaConfig(**data.get("ollama", {})),
        openrouter=OpenRouterConfig(**data.get("openrouter", {})),
        pressure_test=PressureTestConfig(**data.get("pressure_test", {})),
        reset_on_launch=data.get("reset_on_launch", True),
    )
    return cfg


def save_config(cfg: ModelConfig, path: Path = CONFIG_PATH) -> None:
    """Write the config back to disk."""
    data = {
        "ollama": asdict(cfg.ollama),
        "openrouter": asdict(cfg.openrouter),
        "pressure_test": asdict(cfg.pressure_test),
        "reset_on_launch": cfg.reset_on_launch,
    }
    _atomic_write_json(path, data)


# ── Reset ────────────────────────────────────────────────────────────

def reset_state() -> dict:
    """Clear transient autocoder state. Call at the top of any run.

    Wipes:
      - claimed-tab registry in cdp_client (so stale tabs don't block
        new sessions)
      - session manager's running threads
      - any cached chunk index

    Returns a report dict so the caller can log what was cleared.
    """
    report = {"cleared": []}

    # Clear CDP claimed-tab registry
    try:
        from . import cdp_client
        with cdp_client._claimed_ws_lock:
            count = len(cdp_client._claimed_ws_urls)
            cdp_client._claimed_ws_urls.clear()
            report["cleared"].append(f"cdp_claimed_tabs={count}")
    except Exception as e:
        report["cleared"].append(f"cdp_claimed_tabs=error({e})")

    # Clear any process-wide broadcast state file
    state_file = Path.home() / ".autocoder" / "broadcast_state.json"
    if state_file.exists():
        try:
            state_file.unlink()
            report["cleared"].append("broadcast_state.json")
        except Exception as e:
            report["cleared"].append(f"broadcast_state.json=error({e})")

    logger.info("reset_state: %s", report)
    return report


# ── Quick-access helpers ─────────────────────────────────────────────

def active_ollama_client(cfg: Optional[ModelConfig] = None):
    """Return an OllamaAPIClient ready to use, or None if unavailable.

    Honors the user's preferred_model → enabled_models → auto_rank
    cascade from config.
    """
    cfg = cfg or load_config()
    from .ollama_api_client import (
        OllamaAPIClient, rank_models_for_code, list_installed_models,
    )
    installed = list_installed_models(host=cfg.ollama.host)
    if not installed:
        logger.warning("No local Ollama models — is `ollama serve` running?")
        return None

    if cfg.ollama.preferred_model:
        chosen = cfg.ollama.preferred_model
    elif cfg.ollama.enabled_models:
        chosen = cfg.ollama.enabled_models[0]
    elif cfg.ollama.auto_rank:
        ranked = rank_models_for_code(host=cfg.ollama.host)
        chosen = ranked[0] if ranked else None
    else:
        chosen = installed[0].name

    if not chosen:
        return None
    return OllamaAPIClient(
        model=chosen,
        host=cfg.ollama.host,
        temperature=cfg.ollama.temperature,
        num_ctx=cfg.ollama.num_ctx,
    )


def active_openrouter_client(cfg: Optional[ModelConfig] = None):
    """Return an OpenRouterAPIClient with rotation configured, or None."""
    import os
    cfg = cfg or load_config()
    key = os.environ.get(cfg.openrouter.api_key_env, "").strip()
    if not key:
        # Fall back to the file path convention used by the client itself
        keyfile = Path.home() / ".autocoder" / "openrouter.key"
        if not keyfile.exists():
            logger.warning(
                "OpenRouter: no API key. Set %s env var or write "
                "~/.autocoder/openrouter.key.", cfg.openrouter.api_key_env,
            )
            return None
    try:
        from .openrouter_api_client import OpenRouterAPIClient
        model_ids = cfg.openrouter.enabled_model_ids or None
        return OpenRouterAPIClient(
            model_ids=model_ids,
            temperature=cfg.openrouter.temperature,
            min_context=cfg.openrouter.min_context,
        )
    except Exception as e:
        logger.warning("OpenRouter client init failed: %s", e)
        return None


# ── CLI: print current config ───────────────────────────────────────

if __name__ == "__main__":
    import sys
    cfg = load_config()
    print(f"Config: {CONFIG_PATH}\n")
    print(json.dumps({
        "ollama": asdict(cfg.ollama),
        "openrouter": asdict(cfg.openrouter),
        "pressure_test": asdict(cfg.pressure_test),
        "reset_on_launch": cfg.reset_on_launch,
    }, indent=2))
    if "--reset" in sys.argv:
        print("\nResetting transient state…")
        report = reset_state()
        print(json.dumps(report, indent=2))

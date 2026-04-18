"""OpenRouter HTTP client with automatic free-model fallover.

Why this exists
---------------
OpenRouter's web UI was flaky under CDP automation (selectors drift
fast). But OpenRouter exposes a plain chat-completions API, and many
strong models are offered free (subject to rate limits).

The value-add here isn't just "use the API" — it's the *rotation*:

  If model A rate-limits → try model B.
  If model B rate-limits → try model C.
  ...
  Always start from the strongest free model, fall over as needed.

The list of free models is discovered live from /api/v1/models so we
stay current as OpenRouter adds/removes them.

Setup
-----
Get an API key at https://openrouter.ai/keys (free). Put it in:
  - env var  OPENROUTER_API_KEY         (preferred)
  - or file  ~/.autocoder/openrouter.key

Usage
-----
    client = OpenRouterAPIClient()  # auto-picks free models
    result = client.generate(prompt)  # rotates on rate-limit
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
import urllib.error
import urllib.request


logger = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 300  # 5 min per call

# Strength ranking — updated for 2026-era models on OpenRouter.
# Matching is by substring (case-insensitive). Higher score = tried first.
# The rotation logic walks this list top-down, cooling down rate-limited
# models for 30-90s before retrying.
_STRENGTH_RANK = [
    # Top tier (2026): frontier coders + ultra-large general models
    ("qwen3-coder", 98),              # Latest coder-tuned, strong for code
    ("qwen-2.5-coder-32b", 95),       # Previous-gen coder, still excellent
    ("deepseek-v3", 94),              # DeepSeek V3 flagship
    ("deepseek-chat-v3", 93),
    ("nemotron-3-super", 92),         # NVIDIA's 120B
    ("nemotron-3", 88),
    ("hermes-3-llama-3.1-405b", 90),  # 405B params, 131K context
    ("llama-3.3-70b", 85),
    ("llama-3.1-405b", 88),
    ("qwen3-next-80b", 87),
    ("qwen-3", 80),
    # Strong general-purpose
    ("gemma-4", 82),                  # Google's newest Gemma
    ("gemma-3", 72),
    ("claude-3.5-sonnet", 100),       # If ever free (rare) — best coder
    ("gpt-4o", 95),
    ("gpt-4", 88),
    ("gemini-2.0-flash", 85),
    ("gemini-2", 82),
    ("mistral-large", 78),
    ("mixtral-8x22b", 72),
    # Mid tier
    ("kimi-k2", 75),
    ("qwen-2.5-coder", 82),           # Includes 7b variants
    ("qwen-2.5-72b", 75),
    ("deepseek-coder", 85),
    ("deepseek", 70),
    ("llama-3.1-70b", 68),
    ("llama-3", 55),
    # Lower tier
    ("mistral-7b", 45),
    ("phi", 40),
    ("gemma-2", 42),
    ("hermes-3", 60),
]

# Substrings that signal a model is NOT for text / code generation.
# These get filtered out of the rotation list entirely.
_EXCLUDE_SUBSTRINGS = [
    "lyria",     # Google Lyria — audio generation
    "imagen",    # Image generation
    "veo",       # Video generation
    "whisper",   # Speech-to-text
    "tts",       # Text-to-speech
    "dall-e",    # Image
    "vision",    # Vision-only models tend to be weaker at pure text code
    "embed",     # Embedding models
    "reranker",
    "moderation",
]


@dataclass
class OpenRouterModel:
    id: str  # e.g. "meta-llama/llama-3.3-70b-instruct:free"
    name: str = ""
    context_length: int = 0
    pricing_prompt: float = 0.0   # $/1M tokens
    pricing_completion: float = 0.0

    @property
    def is_free(self) -> bool:
        return (
            self.id.endswith(":free")
            or (self.pricing_prompt == 0 and self.pricing_completion == 0)
        )


# ── Auth ─────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Find an OpenRouter API key. Empty string if not configured."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    keyfile = Path.home() / ".autocoder" / "openrouter.key"
    if keyfile.exists():
        try:
            return keyfile.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


# ── Model discovery + ranking ────────────────────────────────────────

def list_free_models(timeout: float = 10.0) -> list[OpenRouterModel]:
    """Query OpenRouter's /models endpoint and return free-tier entries."""
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/models",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("OpenRouter list_free_models failed: %s", e)
        return []

    out: list[OpenRouterModel] = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        pricing = m.get("pricing", {}) or {}
        try:
            pp = float(pricing.get("prompt", 0) or 0)
            pc = float(pricing.get("completion", 0) or 0)
        except (TypeError, ValueError):
            pp, pc = 0.0, 0.0
        model = OpenRouterModel(
            id=mid,
            name=m.get("name", mid),
            context_length=int(m.get("context_length", 0) or 0),
            pricing_prompt=pp,
            pricing_completion=pc,
        )
        if model.is_free:
            out.append(model)
    return out


def _strength_score(model_id: str) -> int:
    """Rough score for how capable a model is. Higher = stronger."""
    mid = model_id.lower()
    best = 0
    for substr, score in _STRENGTH_RANK:
        if substr in mid:
            best = max(best, score)
    return best if best else 1


def rank_free_models_by_strength(
    min_context: int = 32_000,
) -> list[OpenRouterModel]:
    """Return free models ordered strongest → weakest.

    Filters:
      - Context length below `min_context` (unless unknown/0)
      - Non-text models (image, audio, vision, embedding)
      - Models scoring 0 on the strength rank (unknown, don't trust)
    """
    models = list_free_models()
    eligible: list[OpenRouterModel] = []
    for m in models:
        mid = m.id.lower()
        if any(ex in mid for ex in _EXCLUDE_SUBSTRINGS):
            continue
        if m.context_length and m.context_length < min_context:
            continue
        if _strength_score(m.id) < 10:
            continue   # Unknown models — skip rather than rank blindly
        eligible.append(m)
    eligible.sort(
        key=lambda m: (-_strength_score(m.id), -m.context_length),
    )
    return eligible


# ── HTTP client ──────────────────────────────────────────────────────

class OpenRouterAPIClient:
    """Adapter matching the UniversalBrowserClient minimal interface.

    Maintains a rotation list of free models. generate() tries the
    strongest first, falls over on rate-limit or server errors,
    eventually raises if all models exhausted.

    Thread-safe. Cancellable.
    """

    def __init__(
        self,
        model_ids: Optional[list[str]] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        min_context: int = 32_000,
    ) -> None:
        self._api_key = (api_key or _load_api_key()).strip()
        if not self._api_key:
            raise RuntimeError(
                "OpenRouter API key not found. Set OPENROUTER_API_KEY env "
                "var or write to ~/.autocoder/openrouter.key. Get a free "
                "key at https://openrouter.ai/keys."
            )
        self.temperature = temperature
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

        # Discover free models if caller didn't specify
        if model_ids is None:
            ranked = rank_free_models_by_strength(min_context=min_context)
            if not ranked:
                raise RuntimeError(
                    "OpenRouter returned zero free models — check network"
                )
            self.model_ids = [m.id for m in ranked]
        else:
            self.model_ids = list(model_ids)
        logger.info(
            "OpenRouter client initialized with %d free models: %s",
            len(self.model_ids), self.model_ids[:5],
        )

        # Cooldown state per model (model_id → time it's rate-limited until)
        self._cooldowns: dict[str, float] = {}

        self._configured = True
        self._cdp_available = False
        self._cdp = None

    @property
    def is_configured(self) -> bool:
        return True

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        """Stateless API — no-op."""
        return True

    # ── Rotation core ───────────────────────────────────────────────
    def _next_available_model(self) -> Optional[str]:
        """Return the highest-ranked model that isn't in cooldown."""
        now = time.time()
        for mid in self.model_ids:
            cd = self._cooldowns.get(mid, 0)
            if cd > now:
                continue
            return mid
        return None

    def _put_in_cooldown(self, model_id: str, seconds: float = 60.0) -> None:
        self._cooldowns[model_id] = time.time() + seconds
        logger.info(
            "OpenRouter: cooling down %s for %.0fs", model_id, seconds,
        )

    # ── Generate with rotation ──────────────────────────────────────
    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,
    ) -> str:
        self._cancel_event.clear()
        with self._lock:
            return self._generate_with_rotation(
                prompt, system_instruction, on_progress,
            )

    def _generate_with_rotation(
        self,
        prompt: str,
        system_instruction: str,
        on_progress: Optional[Callable[[str], None]],
    ) -> str:
        tried: list[tuple[str, str]] = []  # (model, reason)
        max_attempts = min(8, len(self.model_ids))

        for _ in range(max_attempts):
            if self._cancel_event.is_set():
                raise InterruptedError("OpenRouter cancelled")
            model_id = self._next_available_model()
            if model_id is None:
                # All cooled-down — wait a bit and try again
                if tried and all(
                    "cooled" in reason for _, reason in tried
                ):
                    # Everything's rate-limited. Wait for the soonest to recover.
                    soonest = min(self._cooldowns.values(), default=time.time() + 60)
                    sleep_for = max(5, min(60, soonest - time.time()))
                    logger.info(
                        "OpenRouter: all models cooled. Sleeping %ds.",
                        int(sleep_for),
                    )
                    time.sleep(sleep_for)
                    continue
                break

            if on_progress:
                try:
                    on_progress(f"OpenRouter → {model_id}")
                except Exception:
                    pass

            try:
                result = self._call_model(
                    model_id, prompt, system_instruction,
                )
                if result and len(result.strip()) > 10:
                    logger.info(
                        "OpenRouter/%s: success (%d chars)",
                        model_id, len(result),
                    )
                    return result
                tried.append((model_id, "empty response"))
                self._put_in_cooldown(model_id, 30)
            except _RateLimited:
                tried.append((model_id, "rate-limited — cooled"))
                self._put_in_cooldown(model_id, 90)
            except _ServerError as e:
                tried.append((model_id, f"server error: {e}"))
                self._put_in_cooldown(model_id, 60)
            except Exception as e:
                tried.append((model_id, f"exception: {str(e)[:80]}"))
                self._put_in_cooldown(model_id, 30)

        raise RuntimeError(
            "OpenRouter: all free models failed. Tried: "
            + "; ".join(f"{m}={r}" for m, r in tried)
        )

    def _call_model(
        self, model_id: str, prompt: str, system_instruction: str,
    ) -> str:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model_id,
            "messages": messages,
            "temperature": self.temperature,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/awesomo913/Autocoder",
                "X-Title": "Autocoder Pressure Test",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 429:
                raise _RateLimited(body_text) from e
            if 500 <= e.code < 600:
                raise _ServerError(f"{e.code} {body_text}") from e
            # 4xx non-rate-limit = bad request, likely unrecoverable
            raise RuntimeError(f"OpenRouter {e.code}: {body_text}") from e
        except urllib.error.URLError as e:
            raise _ServerError(str(e)) from e

        if status != 200:
            raise _ServerError(f"status {status}: {raw[:200]}")
        parsed = json.loads(raw)
        choices = parsed.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        return content


class _RateLimited(Exception):
    pass


class _ServerError(Exception):
    pass


# ── CLI helper to check which models are free today ─────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    models = rank_free_models_by_strength()
    if not models:
        print("No free models reachable — check your internet.")
        sys.exit(1)
    print(f"Top {min(20, len(models))} free models on OpenRouter (strongest first):")
    print()
    for i, m in enumerate(models[:20], 1):
        ctx = f"{m.context_length//1000}K" if m.context_length else "?"
        score = _strength_score(m.id)
        print(f"  {i:2d}. {m.id:<55} ctx={ctx:<5} strength={score}")

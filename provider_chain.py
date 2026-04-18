"""Provider chain — falls back across AI providers with per-provider cooldown.

Quacks like UniversalBrowserClient (.generate / .new_conversation / .cancel)
so anywhere the broadcast expects a "single client" can take a chain and
get automatic rank-based fallover.

Default ranking (set by today's pressure-test results):
    1. OpenRouter API          107K chars, A grade
    2. Gemini (browser)         66K chars, A grade
    3. ChatGPT (browser)        45K chars, A grade (when selectors hold)
    4. Ollama API (local)        3K chars, C grade — slow but always available
    5. Copilot (browser)         0 chars, D grade — last-resort
    + DeepSeek API (stub)       not yet wired — see _make_deepseek
    + Qwen API (stub)           not yet wired — see _make_qwen

On failure: cool the provider for 120s, try the next. On all-exhausted:
wait for the soonest cooldown to expire, then retry. Never deadlocks.

Usage
-----
    from gemini_coder_web.provider_chain import build_default_chain
    chain = build_default_chain()
    result = chain.generate("Write a hello world in Python")
    # → tries OpenRouter; if rate-limited, falls over to Gemini; etc.

Edit the user's ~/.autocoder/models.json `provider_chain` section to
reorder, enable/disable, or add API keys.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# ── Data ─────────────────────────────────────────────────────────────

@dataclass
class ProviderEntry:
    """One rung in the chain."""
    name: str
    factory: Callable[[], object]  # () -> client-like object, or None
    # Runtime state
    client: Optional[object] = None
    cooldown_until: float = 0.0
    failures: int = 0
    successes: int = 0
    # Config
    cooldown_sec_on_failure: float = 120.0
    cooldown_sec_on_rate_limit: float = 300.0


# ── The chain ────────────────────────────────────────────────────────

class ProviderChain:
    """Tries providers in order, falls over on failure, never deadlocks.

    Matches the minimal interface of UniversalBrowserClient:
      .generate(prompt, ...)       → str
      .new_conversation()           → bool (no-op; individual providers
                                     handle their own conversation state)
      .cancel()                     → None (propagates to active client)
      .is_configured               → True if ANY provider is reachable
    """

    def __init__(self, providers: list[ProviderEntry]) -> None:
        if not providers:
            raise ValueError("ProviderChain needs at least one provider")
        self.providers = providers
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._active_name: Optional[str] = None

    # ── Properties the broadcast looks for ──────────────────────────
    @property
    def is_configured(self) -> bool:
        """True if at least one provider can be constructed right now."""
        for p in self.providers:
            try:
                if p.client is not None or p.factory() is not None:
                    return True
            except Exception:
                continue
        return False

    def cancel(self) -> None:
        self._cancel.set()
        if self._active_name:
            client = self._client_for(self._active_name)
            if client is not None and hasattr(client, "cancel"):
                try:
                    client.cancel()
                except Exception:
                    pass

    def new_conversation(self) -> bool:
        """No-op at the chain level — each provider manages its own state."""
        return True

    # ── Rotation core ───────────────────────────────────────────────
    def _client_for(self, name: str) -> Optional[object]:
        for p in self.providers:
            if p.name == name:
                return p.client
        return None

    def _get_or_create(self, p: ProviderEntry) -> Optional[object]:
        if p.client is not None:
            return p.client
        try:
            c = p.factory()
            if c is not None:
                p.client = c
            return c
        except Exception as e:
            logger.warning("Provider '%s' factory failed: %s", p.name, e)
            return None

    def _in_cooldown(self, p: ProviderEntry) -> bool:
        return p.cooldown_until > time.time()

    def _cool_down(self, p: ProviderEntry, seconds: float, reason: str) -> None:
        p.cooldown_until = time.time() + seconds
        logger.info(
            "[chain] cooling down %s for %.0fs — %s",
            p.name, seconds, reason,
        )

    def _pick_next(self) -> Optional[ProviderEntry]:
        for p in self.providers:
            if self._in_cooldown(p):
                continue
            # Lazy init — skip providers whose factory returns None
            # (e.g. no API key, no local Ollama, no browser tab)
            if self._get_or_create(p) is None:
                self._cool_down(p, 600, "factory returned None")
                continue
            return p
        return None

    # ── Generate with fallover ──────────────────────────────────────
    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,
    ) -> str:
        self._cancel.clear()
        tried: list[tuple[str, str]] = []

        with self._lock:
            attempts = 0
            max_full_rotations = 3
            while attempts < len(self.providers) * max_full_rotations:
                if self._cancel.is_set():
                    raise InterruptedError("Chain generate cancelled")

                p = self._pick_next()
                if p is None:
                    # All cooled — sleep until soonest recovery
                    soonest = min(
                        (q.cooldown_until for q in self.providers),
                        default=time.time() + 60,
                    )
                    wait = max(5, min(120, soonest - time.time()))
                    logger.info(
                        "[chain] all providers cooled. Waiting %.0fs.", wait,
                    )
                    time.sleep(wait)
                    attempts += 1
                    continue

                self._active_name = p.name
                if on_progress:
                    try:
                        on_progress(f"Chain → {p.name}")
                    except Exception:
                        pass
                try:
                    client = p.client
                    result = client.generate(
                        prompt=prompt,
                        system_instruction=system_instruction,
                        on_progress=on_progress,
                        conversation=conversation,
                    ) if _accepts_kwargs(client.generate) else client.generate(prompt)

                    if result and len(result.strip()) > 20:
                        p.successes += 1
                        logger.info(
                            "[chain] %s succeeded (%d chars, %d successes total)",
                            p.name, len(result), p.successes,
                        )
                        return result
                    tried.append((p.name, f"empty response ({len(result or '')} chars)"))
                    p.failures += 1
                    self._cool_down(p, p.cooldown_sec_on_failure, "empty response")
                except InterruptedError:
                    raise
                except Exception as e:
                    msg = str(e)[:120]
                    tried.append((p.name, f"exception: {msg}"))
                    p.failures += 1
                    # Rate-limit signals = longer cooldown
                    lowered = msg.lower()
                    if "rate" in lowered or "429" in lowered or "quota" in lowered:
                        self._cool_down(
                            p, p.cooldown_sec_on_rate_limit, "rate-limited",
                        )
                    else:
                        self._cool_down(p, p.cooldown_sec_on_failure, "exception")
                finally:
                    self._active_name = None

                attempts += 1

        raise RuntimeError(
            "ProviderChain: all providers exhausted across retries. "
            "History: " + "; ".join(f"{n}={r}" for n, r in tried[-10:])
        )


def _accepts_kwargs(fn) -> bool:
    """Check if a method accepts the full generate(..) kwargs set."""
    try:
        import inspect
        sig = inspect.signature(fn)
        return "system_instruction" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except Exception:
        return False


# ── Factories for each named provider ───────────────────────────────

def _make_openrouter():
    """Rank 1. HTTP-direct, with internal free-model rotation."""
    from .openrouter_api_client import OpenRouterAPIClient
    try:
        return OpenRouterAPIClient()
    except Exception as e:
        logger.info("[chain] OpenRouter unavailable: %s", e)
        return None


def _make_gemini():
    """Rank 2. Browser-driven. Requires an open, logged-in Gemini tab."""
    from .cdp_client import connect_to_ai_site, DEFAULT_CDP_PORT
    from .ai_profiles import get_profile
    profile = get_profile("Gemini")
    if profile is None:
        return None
    cdp = connect_to_ai_site(
        "Gemini",
        url_pattern=profile.url_pattern,
        title_pattern=profile.title_pattern,
        port=DEFAULT_CDP_PORT,
    )
    if cdp is None:
        logger.info("[chain] Gemini tab not open")
        return None
    # Wrap in a minimal adapter so generate() signature matches
    return _CDPClientAdapter(cdp, profile_name="Gemini")


def _make_chatgpt():
    """Rank 3. Browser-driven. Currently broken on recent ChatGPT UI."""
    from .cdp_client import connect_to_ai_site, DEFAULT_CDP_PORT
    from .ai_profiles import get_profile
    profile = get_profile("ChatGPT")
    if profile is None:
        return None
    cdp = connect_to_ai_site(
        "ChatGPT",
        url_pattern=profile.url_pattern,
        title_pattern=profile.title_pattern,
        port=DEFAULT_CDP_PORT,
    )
    if cdp is None:
        logger.info("[chain] ChatGPT tab not open")
        return None
    return _CDPClientAdapter(cdp, profile_name="ChatGPT")


def _make_ollama():
    """Rank 4. Local HTTP to ollama serve on :11434."""
    from .ollama_api_client import OllamaAPIClient, rank_models_for_code
    ranked = rank_models_for_code()
    if not ranked:
        logger.info("[chain] Ollama has no local models")
        return None
    return OllamaAPIClient(model=ranked[0])


def _make_copilot():
    """Rank 5. Last-resort. Browser-driven. Current selectors broken."""
    from .cdp_client import connect_to_ai_site, DEFAULT_CDP_PORT
    from .ai_profiles import get_profile
    profile = get_profile("Copilot")
    if profile is None:
        return None
    cdp = connect_to_ai_site(
        "Copilot",
        url_pattern=profile.url_pattern,
        title_pattern=profile.title_pattern,
        port=DEFAULT_CDP_PORT,
    )
    if cdp is None:
        logger.info("[chain] Copilot tab not open")
        return None
    return _CDPClientAdapter(cdp, profile_name="Copilot")


def _make_deepseek():
    """DeepSeek direct API (api.deepseek.com, OpenAI-compatible).

    Not yet wired — when you're ready:
      1. Get an API key at https://platform.deepseek.com/api_keys
      2. Put it in   ~/.autocoder/deepseek.key   OR   env DEEPSEEK_API_KEY
      3. This factory will pick it up automatically

    DeepSeek's deepseek-chat and deepseek-coder are excellent for code
    and have aggressive free-tier credit (often $5-$10 on signup).
    """
    import os
    from pathlib import Path
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        keyfile = Path.home() / ".autocoder" / "deepseek.key"
        if keyfile.exists():
            try:
                key = keyfile.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    if not key:
        logger.info(
            "[chain] DeepSeek skipped — set DEEPSEEK_API_KEY or "
            "~/.autocoder/deepseek.key",
        )
        return None

    # DeepSeek is OpenAI-compatible — we can reuse the OpenRouter
    # client plumbing by pointing it at api.deepseek.com/v1 with the
    # right model names. Simpler than writing a whole new client.
    # For now return a thin adapter class:
    return _DeepSeekAPIClient(api_key=key)


def _make_qwen():
    """Qwen direct API (Alibaba DashScope, OpenAI-compatible endpoint).

    Not yet wired — when you're ready:
      1. Get a key at https://dashscope.aliyun.com/
         (Alibaba Cloud account required; free tier available)
      2. Put it in   ~/.autocoder/qwen.key   OR   env DASHSCOPE_API_KEY
      3. This factory will pick it up automatically

    Qwen3-coder via Alibaba has the strongest code-generation results
    of the free models as of 2026; worth wiring directly for priority
    access when OpenRouter's free qwen3-coder:free is rate-limited.
    """
    import os
    from pathlib import Path
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        keyfile = Path.home() / ".autocoder" / "qwen.key"
        if keyfile.exists():
            try:
                key = keyfile.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    if not key:
        logger.info(
            "[chain] Qwen skipped — set DASHSCOPE_API_KEY or "
            "~/.autocoder/qwen.key",
        )
        return None
    return _QwenAPIClient(api_key=key)


# ── Adapters ─────────────────────────────────────────────────────────

class _CDPClientAdapter:
    """Wraps a CDPChatAutomation so it looks like the broadcast client.

    Matches the .generate(prompt, ...) signature + .new_conversation +
    .cancel that ProviderChain expects from a provider.
    """

    def __init__(self, cdp, profile_name: str) -> None:
        self._cdp = cdp
        self._profile_name = profile_name
        self._cancel_event = threading.Event()
        self._configured = True

    @property
    def is_configured(self) -> bool:
        return True

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        try:
            return self._cdp.new_conversation()
        except Exception:
            return False

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,
    ) -> str:
        # Fresh conversation before each send (fixes Gemini Angular bug,
        # safe on others)
        try:
            self.new_conversation()
            time.sleep(1)
        except Exception:
            pass
        return self._cdp.send_and_read(
            prompt=prompt,
            timeout=300,
            on_progress=on_progress,
            cancel_event=self._cancel_event,
        )


class _DeepSeekAPIClient:
    """OpenAI-compatible client for api.deepseek.com/v1/chat/completions."""

    ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
    # DeepSeek models (prefer coder for our task)
    DEFAULT_MODELS = ["deepseek-coder", "deepseek-chat"]

    def __init__(self, api_key: str, model: str = "deepseek-coder",
                 temperature: float = 0.2) -> None:
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self._cancel_event = threading.Event()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        return True  # Stateless

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,
    ) -> str:
        import json as _json, urllib.request, urllib.error
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        body = _json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"DeepSeek {e.code}: {body_text}") from e
        parsed = _json.loads(raw)
        choices = parsed.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")


class _QwenAPIClient:
    """OpenAI-compatible client for Qwen via Alibaba DashScope.

    DashScope exposes an OpenAI-compatible endpoint at:
        https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions
    """

    ENDPOINT = (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
    )

    def __init__(self, api_key: str, model: str = "qwen3-coder-plus",
                 temperature: float = 0.2) -> None:
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self._cancel_event = threading.Event()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        return True

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,
    ) -> str:
        import json as _json, urllib.request, urllib.error
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        body = _json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"Qwen {e.code}: {body_text}") from e
        parsed = _json.loads(raw)
        choices = parsed.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")


# ── Default chain factory ────────────────────────────────────────────

def build_default_chain() -> ProviderChain:
    """Build the production chain in today's empirically-ranked order.

    Order reflects actual pressure-test results:
      1. OpenRouter API   — A grade, 107K chars, rate-limit-resistant
      2. Gemini           — A grade when not throttled
      3. ChatGPT          — A grade when selectors hold (fragile)
      4. Ollama (local)   — slow but always available
      5. Copilot          — last-resort, currently broken
      + DeepSeek API      — stub, will auto-enable when key is set
      + Qwen API          — stub, will auto-enable when key is set
    """
    return ProviderChain([
        ProviderEntry("OpenRouter API", _make_openrouter),
        ProviderEntry("Gemini", _make_gemini),
        ProviderEntry("ChatGPT", _make_chatgpt),
        ProviderEntry("Ollama API", _make_ollama),
        ProviderEntry("Copilot", _make_copilot),
        # Pre-wired, auto-skip if no key configured yet
        ProviderEntry("DeepSeek API", _make_deepseek),
        ProviderEntry("Qwen API", _make_qwen),
    ])


def build_chain_from_names(names: list[str]) -> ProviderChain:
    """Build a chain from a custom ordered list of provider names.

    Used by the user's ~/.autocoder/models.json to override the default
    order. Unknown names are skipped with a warning.
    """
    factory_map = {
        "OpenRouter API": _make_openrouter,
        "Gemini": _make_gemini,
        "ChatGPT": _make_chatgpt,
        "Ollama API": _make_ollama,
        "Copilot": _make_copilot,
        "DeepSeek API": _make_deepseek,
        "Qwen API": _make_qwen,
    }
    entries: list[ProviderEntry] = []
    for n in names:
        fn = factory_map.get(n)
        if fn is None:
            logger.warning(
                "[chain] unknown provider name: %s (valid: %s)",
                n, list(factory_map.keys()),
            )
            continue
        entries.append(ProviderEntry(n, fn))
    if not entries:
        logger.warning("[chain] empty chain — falling back to default")
        return build_default_chain()
    return ProviderChain(entries)

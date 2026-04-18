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


def _read_key_file(name: str, env_var: Optional[str] = None) -> str:
    """Read an API key from env or ~/.autocoder/<name>.key.

    Shared helper for every direct-HTTP provider factory. Returns empty
    string if no key is configured — the caller should then return None
    so the chain auto-skips the rung.
    """
    import os
    from pathlib import Path
    env_var = env_var or f"{name.upper()}_API_KEY"
    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    keyfile = Path.home() / ".autocoder" / f"{name}.key"
    if keyfile.exists():
        try:
            return keyfile.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


# Shared state for OpenCode subprocess — spawned once, reused across calls
_opencode_process: Optional[object] = None
_opencode_port: int = 4096


def _make_opencode():
    """OpenCode as a meta-provider — a single rung that routes to
    DeepSeek, Anthropic, Groq, and more via its own credential store.

    OpenCode is installed as a desktop app with a CLI at:
        C:\\Users\\computer\\AppData\\Local\\OpenCode\\opencode-cli.exe

    We drive it via `opencode-cli serve` which exposes an HTTP server.
    First call spawns the subprocess; subsequent calls reuse it. An
    atexit hook terminates the server when the parent Python exits.
    """
    import os
    import atexit
    import socket
    import subprocess
    import time as _time

    global _opencode_process

    # Candidate executable paths — OpenCode installs under AppData/Local
    candidates = [
        r"C:\Users\computer\AppData\Local\OpenCode\opencode-cli.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\OpenCode\opencode-cli.exe"),
    ]
    exe_path = next((p for p in candidates if os.path.isfile(p)), None)
    if exe_path is None:
        logger.info("[chain] OpenCode skipped — opencode-cli.exe not found")
        return None

    # Is the server already running?
    def _server_alive() -> bool:
        try:
            with socket.create_connection(
                ("127.0.0.1", _opencode_port), timeout=0.5,
            ):
                return True
        except OSError:
            return False

    if not _server_alive():
        if _opencode_process is not None and _opencode_process.poll() is None:
            # We spawned one but the socket check failed — kill and respawn
            try:
                _opencode_process.terminate()
            except Exception:
                pass
            _opencode_process = None

        try:
            logger.info(
                "[chain] spawning opencode serve on :%d", _opencode_port,
            )
            _opencode_process = subprocess.Popen(
                [exe_path, "serve", "--port", str(_opencode_port),
                 "--hostname", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            def _cleanup():
                if _opencode_process and _opencode_process.poll() is None:
                    try:
                        _opencode_process.terminate()
                    except Exception:
                        pass

            atexit.register(_cleanup)
        except Exception as e:
            logger.warning("[chain] OpenCode spawn failed: %s", e)
            return None

        # Wait up to 10s for the server to be listening
        for _ in range(40):
            if _server_alive():
                break
            _time.sleep(0.25)
        else:
            logger.warning("[chain] OpenCode serve didn't become ready")
            return None

    return _OpenCodeAPIClient(base=f"http://127.0.0.1:{_opencode_port}")


def _make_deepseek():
    """DEPRECATED: kept so old configs still load without error.

    OpenCode now handles DeepSeek routing via its credential store —
    having both would give us two rungs for the same backend. This
    factory just logs a hint and returns None.
    """
    logger.info(
        "[chain] DeepSeek direct rung is deprecated — OpenCode handles "
        "DeepSeek routing. Configure DeepSeek inside OpenCode instead: "
        "`opencode-cli providers login deepseek`"
    )
    return None


def _make_qwen():
    """Qwen via Alibaba DashScope (OpenAI-compatible).

    Get a key at https://dashscope.aliyun.com/ (free tier available).
    Put it in ~/.autocoder/qwen.key OR env DASHSCOPE_API_KEY.
    """
    key = _read_key_file("qwen", "DASHSCOPE_API_KEY")
    if not key:
        logger.info(
            "[chain] Qwen skipped — set DASHSCOPE_API_KEY or "
            "~/.autocoder/qwen.key",
        )
        return None
    return _OpenAICompatClient(
        endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key=key,
        model="qwen3-coder-plus",
        provider_label="Qwen",
    )


def _make_groq():
    """Groq — fastest free Llama 3.3 70B inference.

    Get a key at https://console.groq.com/keys (free tier: 30 req/min).
    Put it in ~/.autocoder/groq.key OR env GROQ_API_KEY.
    """
    key = _read_key_file("groq", "GROQ_API_KEY")
    if not key:
        logger.info(
            "[chain] Groq skipped — set GROQ_API_KEY or ~/.autocoder/groq.key"
        )
        return None
    return _OpenAICompatClient(
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        api_key=key,
        model="llama-3.3-70b-versatile",
        provider_label="Groq",
    )


def _make_cerebras():
    """Cerebras — fastest inference Llama 3.3 70B on custom chips.

    Get a key at https://cloud.cerebras.ai/ (free tier available).
    Put it in ~/.autocoder/cerebras.key OR env CEREBRAS_API_KEY.
    """
    key = _read_key_file("cerebras", "CEREBRAS_API_KEY")
    if not key:
        logger.info(
            "[chain] Cerebras skipped — set CEREBRAS_API_KEY or "
            "~/.autocoder/cerebras.key"
        )
        return None
    return _OpenAICompatClient(
        endpoint="https://api.cerebras.ai/v1/chat/completions",
        api_key=key,
        model="llama3.3-70b",
        provider_label="Cerebras",
    )


# ── Gemini mode variants ────────────────────────────────────────────
#
# Google's Gemini web UI supports multiple modes. Each mode lives on a
# distinct URL (or is toggled via a button). We give each mode its own
# rung so iterations cycle through them and the pressure test ranks
# them independently.
#
# Tab disambiguation: connect_to_ai_site uses url_pattern, so as long
# as each mode's tab URL contains a distinct pattern, the existing
# tab-claiming logic routes correctly.

def _make_gemini_with_mode(label: str, url_pattern_hint: str,
                           fresh_url: Optional[str] = None):
    """Shared helper: find (or open) a Gemini tab matching the hint.

    Each Gemini mode factory uses this. The returned adapter labels
    itself with the mode so logs and saved outputs are distinguishable.
    """
    from .cdp_client import connect_to_ai_site, DEFAULT_CDP_PORT
    cdp = connect_to_ai_site(
        f"Gemini {label}",
        url_pattern=url_pattern_hint,
        title_pattern="",
        port=DEFAULT_CDP_PORT,
    )
    if cdp is None:
        # Auto-open tab if a fresh URL is provided
        if fresh_url:
            import urllib.parse, urllib.request
            endpoint = (
                f"http://127.0.0.1:{DEFAULT_CDP_PORT}/json/new"
                f"?{urllib.parse.quote(fresh_url, safe=':/?=&%')}"
            )
            try:
                req = urllib.request.Request(endpoint, method="PUT")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status < 400:
                        import time as _t
                        _t.sleep(6)  # let the page load
                        cdp = connect_to_ai_site(
                            f"Gemini {label}",
                            url_pattern=url_pattern_hint,
                            port=DEFAULT_CDP_PORT,
                        )
            except Exception as e:
                logger.info(
                    "[chain] Gemini %s tab auto-open failed: %s", label, e,
                )
    if cdp is None:
        logger.info("[chain] Gemini %s tab not available", label)
        return None
    return _CDPClientAdapter(cdp, profile_name=f"Gemini {label}")


def _make_gemini_25_flash():
    """Gemini 2.5 Flash — default free-tier model. Fast, good quality."""
    return _make_gemini_with_mode(
        "2.5 Flash",
        url_pattern_hint="gemini.google.com/app",
        fresh_url="https://gemini.google.com/app?model=gemini-2.5-flash",
    )


def _make_gemini_25_pro():
    """Gemini 2.5 Pro — requires Gemini Advanced subscription.

    The user said "every single one" so we always try. If the account
    doesn't have access the DOM will fall back to 2.5 Flash and the
    rung still works (just not with Pro quality).
    """
    return _make_gemini_with_mode(
        "2.5 Pro",
        url_pattern_hint="gemini.google.com/app",
        fresh_url="https://gemini.google.com/app?model=gemini-2.5-pro",
    )


def _make_gemini_canvas():
    """Canvas mode — interactive document editor in Gemini."""
    return _make_gemini_with_mode(
        "Canvas",
        url_pattern_hint="gemini.google.com",
        fresh_url="https://gemini.google.com/app?canvas=1",
    )


def _make_gemini_deep_research():
    """Deep Research mode — multi-step research that takes minutes."""
    return _make_gemini_with_mode(
        "Deep Research",
        url_pattern_hint="gemini.google.com",
        fresh_url="https://gemini.google.com/app?deepresearch=1",
    )


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


class _OpenAICompatClient:
    """Generic OpenAI-compatible HTTP client.

    Used by every direct-API provider that exposes the standard
    /chat/completions schema (messages array, Bearer auth, JSON body).
    Replaces the previous DeepSeek/Qwen-specific duplicate classes.

    Providers currently using this:
      - OpenCode             (local HTTP server)
      - Groq                 (api.groq.com)
      - Cerebras             (api.cerebras.ai)
      - Qwen / DashScope     (dashscope-intl.aliyuncs.com)
      - DeepSeek (deprecated, kept for back-compat via alias)
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        extra_headers: Optional[dict] = None,
        provider_label: str = "OpenAI-compat",
        timeout: int = 300,
    ) -> None:
        self.endpoint = endpoint
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self._extra_headers = extra_headers or {}
        self._provider_label = provider_label
        self._timeout = timeout
        self._cancel_event = threading.Event()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) or self._api_key == ""  # Some servers don't need auth (e.g. local)

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        return True  # All OpenAI-compat calls are stateless

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

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)

        req = urllib.request.Request(
            self.endpoint, data=body, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            # Propagate rate-limit / server errors so ProviderChain
            # can detect them and cool down the rung appropriately
            if e.code == 429:
                raise RuntimeError(
                    f"{self._provider_label} 429 rate-limited: {body_text}"
                ) from e
            raise RuntimeError(
                f"{self._provider_label} {e.code}: {body_text}"
            ) from e
        parsed = _json.loads(raw)
        choices = parsed.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")


class _OpenCodeAPIClient(_OpenAICompatClient):
    """Thin wrapper around _OpenAICompatClient pointed at a local OpenCode
    server. OpenCode handles routing to whichever upstream provider the
    user has credentialed — we just treat it as a stateless chat endpoint.
    """

    def __init__(self, base: str = "http://127.0.0.1:4096",
                 model: str = "auto") -> None:
        # OpenCode's HTTP API is OpenAI-compatible at /v1/chat/completions
        super().__init__(
            endpoint=f"{base}/v1/chat/completions",
            api_key="",  # Local server — no auth
            model=model,
            temperature=0.2,
            provider_label="OpenCode",
            timeout=600,  # OpenCode can be slow when it's the first call
        )


# Back-compat aliases — if anything else imports these old class names
# they'll keep working via the unified client.
_DeepSeekAPIClient = _OpenAICompatClient
_QwenAPIClient = _OpenAICompatClient


# ── Default chain factory ────────────────────────────────────────────

def build_default_chain() -> ProviderChain:
    """Build the production chain, expanded to include OpenCode,
    Groq, Cerebras, and all four Gemini modes.

    Rank rationale (today's pressure-test + user priorities):
      1. OpenRouter API        — 107K chars, A grade, rate-limit-resistant
      2. Groq                  — fast free Llama 3.3 70B (30 req/min)
      3. Cerebras              — fastest free Llama 3.3 70B inference
      4. Gemini 2.5 Pro        — highest Gemini quality (if Advanced)
      5. Gemini 2.5 Flash      — default free Gemini, fast
      6. Gemini Canvas         — interactive document mode
      7. Gemini Deep Research  — multi-step reports
      8. ChatGPT               — cloud, browser-driven
      9. Ollama API            — local fallback, always on
     10. OpenCode              — meta-router: DeepSeek/Anthropic/Groq/etc.
     11. Copilot               — last resort
     12. Qwen API              — direct DashScope (if key configured)

    Unconfigured rungs auto-skip; broken tabs auto-open via CDP.
    """
    return ProviderChain([
        ProviderEntry("OpenRouter API", _make_openrouter),
        ProviderEntry("Groq", _make_groq),
        ProviderEntry("Cerebras", _make_cerebras),
        ProviderEntry("Gemini 2.5 Pro", _make_gemini_25_pro),
        ProviderEntry("Gemini 2.5 Flash", _make_gemini_25_flash),
        ProviderEntry("Gemini Canvas", _make_gemini_canvas),
        ProviderEntry("Gemini Deep Research", _make_gemini_deep_research),
        ProviderEntry("Gemini", _make_gemini),          # generic fallback
        ProviderEntry("ChatGPT", _make_chatgpt),
        ProviderEntry("Ollama API", _make_ollama),
        ProviderEntry("OpenCode", _make_opencode),
        ProviderEntry("Copilot", _make_copilot),
        ProviderEntry("Qwen API", _make_qwen),
        # DeepSeek direct deprecated — routed through OpenCode instead
    ])


def build_chain_from_names(names: list[str]) -> ProviderChain:
    """Build a chain from a custom ordered list of provider names.

    Used by the user's ~/.autocoder/models.json to override the default
    order. Unknown names are skipped with a warning.
    """
    factory_map = {
        "OpenRouter API": _make_openrouter,
        "Groq": _make_groq,
        "Cerebras": _make_cerebras,
        "Gemini 2.5 Pro": _make_gemini_25_pro,
        "Gemini 2.5 Flash": _make_gemini_25_flash,
        "Gemini Canvas": _make_gemini_canvas,
        "Gemini Deep Research": _make_gemini_deep_research,
        "Gemini": _make_gemini,
        "ChatGPT": _make_chatgpt,
        "Ollama API": _make_ollama,
        "OpenCode": _make_opencode,
        "Copilot": _make_copilot,
        "Qwen API": _make_qwen,
        # Back-compat aliases for old configs
        "DeepSeek API": _make_deepseek,  # now a deprecation stub
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

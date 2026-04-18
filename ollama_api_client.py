"""Direct Ollama HTTP client — bypass browser automation for local models.

Why this exists
---------------
The "Ollama Web UI" profile drives open-webui via CDP like the cloud AIs.
That requires:
  1. open-webui installed and running on localhost:3000
  2. selectors matching the current open-webui build
  3. a Chrome tab logged in

All three are fragile. Meanwhile Ollama itself already exposes a clean
HTTP API at http://127.0.0.1:11434 — we can just POST and get a
response. No browser, no selectors, no session.

This module exposes:
  - list_installed_models()    → what the user has locally
  - OllamaAPIClient            → quacks like UniversalBrowserClient
                                   (.generate, .new_conversation, .cancel)
  - rank_models_for_code()     → default ranking for code generation
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
import urllib.error
import urllib.request


logger = logging.getLogger(__name__)

DEFAULT_HOST = "http://127.0.0.1:11434"
GENERATE_TIMEOUT = 600  # 10 min per call — local models can be slow

# Default ranking. Higher = better for code generation. When the user
# hasn't set a preference, we pick from the top of this list that they
# actually have installed.
_CODE_FIRST_RANK = [
    # Coder-tuned models first — these are built for code tasks
    ("deepseek-coder-v2", 100),
    ("deepseek-coder", 95),
    ("qwen2.5-coder", 90),
    ("codellama", 85),
    ("codestral", 85),
    ("starcoder", 80),
    # Large general-purpose next — may outperform small coders on reasoning
    ("dolphin-llama3:70b", 75),
    ("llama3.3:70b", 75),
    ("llama3:70b", 70),
    ("mixtral:8x7b", 65),
    # Medium general-purpose
    ("dolphin-llama3:8b", 55),
    ("llama3.1:8b", 55),
    ("llama3:8b", 50),
    ("mistral", 45),
    # Small / less capable last
    ("phi", 30),
    ("gemma:2b", 25),
    # Vision models - worst for pure code (but might still work)
    ("llava", 10),
    # Truly unknown
    ("my-", 5),
]


@dataclass
class OllamaModel:
    name: str
    size_bytes: int
    param_size: str = ""
    family: str = ""

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)


def list_installed_models(host: str = DEFAULT_HOST) -> list[OllamaModel]:
    """Return models currently installed in the local Ollama server.

    Empty list if Ollama isn't running or the API is unreachable.
    """
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("Ollama not reachable at %s: %s", host, e)
        return []
    except Exception as e:
        logger.warning("Ollama list_installed_models failed: %s", e)
        return []

    models: list[OllamaModel] = []
    for m in data.get("models", []):
        details = m.get("details", {}) or {}
        models.append(OllamaModel(
            name=m.get("name", "?"),
            size_bytes=int(m.get("size", 0) or 0),
            param_size=details.get("parameter_size", ""),
            family=details.get("family", ""),
        ))
    return models


def _rank_score(model_name: str) -> int:
    """Return a ranking score for a model name based on _CODE_FIRST_RANK.

    Matches by prefix so 'deepseek-coder-v2:16b-lite-instruct-q4_K_M'
    picks up the 'deepseek-coder-v2' rank (100).
    """
    name_lower = model_name.lower()
    for prefix, score in _CODE_FIRST_RANK:
        if name_lower.startswith(prefix.lower()):
            return score
    return 1  # Unknown — low priority


def rank_models_for_code(host: str = DEFAULT_HOST) -> list[str]:
    """Return locally-installed models, ranked by suitability for code.

    Strongest-for-code first. Pass the head of this list as the default
    model when the user hasn't picked one.
    """
    models = list_installed_models(host=host)
    ranked = sorted(
        models,
        key=lambda m: (-_rank_score(m.name), -m.size_bytes),
    )
    return [m.name for m in ranked]


class OllamaAPIClient:
    """Adapter that implements the minimal interface of UniversalBrowserClient.

    Lets the multi-model pressure test and the broadcast treat a local
    Ollama model like any other AI — you call .generate() and get a
    string back. No browser, no CDP.

    Thread-safe. Cancellable (set .cancel_event to interrupt a running
    generate()).
    """

    def __init__(
        self,
        model: str,
        host: str = DEFAULT_HOST,
        temperature: float = 0.2,
        num_ctx: int = 8192,
    ) -> None:
        self.model = model
        self.host = host
        self.temperature = temperature
        self.num_ctx = num_ctx
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        # Mimic the browser-client attributes the broadcast checks
        self._configured = True
        self._cdp_available = False
        self._cdp = None

    # ── Interface parity with UniversalBrowserClient ────────────────
    @property
    def is_configured(self) -> bool:
        return True

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        """No-op for Ollama — each /api/generate call is stateless.

        The HTTP endpoint doesn't retain conversation context between
        calls unless we pass it a `context` vector. Each iteration
        starting fresh is exactly what the broadcast wants.
        """
        return True

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        conversation=None,   # Ignored — stateless
    ) -> str:
        """POST /api/generate and return the response text.

        Uses streaming so we can surface progress AND check the cancel
        flag between tokens. Matches the timeout behavior of the CDP
        path.
        """
        self._cancel_event.clear()
        with self._lock:
            return self._generate_impl(prompt, system_instruction, on_progress)

    def _generate_impl(
        self,
        prompt: str,
        system_instruction: str,
        on_progress: Optional[Callable[[str], None]],
    ) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if system_instruction:
            body["system"] = system_instruction

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        start = time.time()
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=GENERATE_TIMEOUT) as resp:
                for raw_line in resp:
                    if self._cancel_event.is_set():
                        raise InterruptedError("Ollama generate cancelled")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk_obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = chunk_obj.get("response", "")
                    if token:
                        chunks.append(token)
                        if on_progress:
                            # Throttle — one call per ~50 tokens
                            if len(chunks) % 50 == 0:
                                try:
                                    on_progress(
                                        f"Ollama/{self.model}: "
                                        f"{len(chunks)} tokens, "
                                        f"{int(time.time() - start)}s"
                                    )
                                except Exception:
                                    pass
                    if chunk_obj.get("done"):
                        break
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Ollama HTTP error at {self.host}: {e}"
            ) from e
        except TimeoutError:
            raise RuntimeError(
                f"Ollama generate timed out after {GENERATE_TIMEOUT}s"
            )

        result = "".join(chunks)
        elapsed = time.time() - start
        logger.info(
            "Ollama/%s: %d tokens in %.1fs (%d chars)",
            self.model, len(chunks), elapsed, len(result),
        )
        return result


# ── Convenience for multi_model_pressure_test integration ────────────

@dataclass
class OllamaModelChoice:
    """User's model selection loaded from ~/.autocoder/local_models.json."""
    host: str = DEFAULT_HOST
    enabled: list[str] = field(default_factory=list)
    auto_rank: bool = True  # If no enabled list, use rank_models_for_code()

    @classmethod
    def default(cls) -> "OllamaModelChoice":
        return cls()

    def resolve(self) -> list[str]:
        """Return ordered list of model names to actually try."""
        if self.enabled:
            return self.enabled
        if self.auto_rank:
            return rank_models_for_code(host=self.host)
        return []

"""Chrome DevTools Protocol (CDP) client for browser automation.

Replaces pyautogui blind-clicking with direct DOM manipulation.
Connects to Chrome/Edge via their remote debugging port and:
- Finds DOM elements by CSS selector (no guessing pixel coordinates)
- Types into input fields directly (no clipboard hijacking)
- Clicks buttons by selector (no blind coordinate math)
- Reads response text from specific elements (no Ctrl+A full page capture)
- Detects AI completion by polling DOM changes (no hardcoded waits)

Requirements:
- Chrome/Edge launched with --remote-debugging-port=9222
- websocket-client package (already in requirements.txt)

Each browser window has its own CDP target (tab). We find the right tab
by matching the URL pattern from the AIProfile.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

try:
    import urllib.request
    URLLIB_AVAILABLE = True
except ImportError:
    URLLIB_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222
CDP_TIMEOUT = 30  # seconds for individual CDP commands (needs headroom for large text)
COMPLETION_POLL_INTERVAL = 1.5  # seconds between completion checks
MAX_COMPLETION_WAIT = 300  # max seconds to wait for AI response

# Per-corner CDP ports for isolated Chrome instances
CDP_CORNER_PORTS = {
    "top-left": 9222,
    "top-right": 9223,
    "bottom-left": 9224,
    "bottom-right": 9225,
}


def get_cdp_port_for_corner(corner: str) -> int:
    """Get the CDP port assigned to a screen corner."""
    return CDP_CORNER_PORTS.get(corner, DEFAULT_CDP_PORT)


@dataclass
class CDPSelectors:
    """CSS selectors for interacting with a specific AI chat site.

    Each AI site has different DOM structure. This captures the selectors
    needed to automate that specific site.
    """
    # Input area — where to type the prompt
    input_selector: str = "textarea"
    # Send button (if Enter doesn't work)
    send_button_selector: str = ""
    # Use Enter key to send (most sites)
    send_with_enter: bool = True
    # Response containers — the AI's messages
    response_selector: str = ".message"
    # The last/latest response specifically
    last_response_selector: str = ".message:last-child"
    # Loading/generating indicator (present while AI is typing)
    loading_selector: str = ""
    # Stop button (present while generating, disappears when done)
    stop_button_selector: str = ""
    # Selector for response text content within a response container
    response_text_selector: str = ""
    # Whether input is a contenteditable div (vs textarea)
    input_is_contenteditable: bool = False
    # Additional JS to run after page load (e.g., dismiss popups)
    init_script: str = ""


# ── Per-site selector presets ─────────────────────────────────────

GEMINI_SELECTORS = CDPSelectors(
    input_selector='.ql-editor[contenteditable="true"], div.input-area-container [contenteditable="true"], .text-input-field_textarea textarea',
    input_is_contenteditable=True,
    send_with_enter=False,  # Must click send button; Enter in Quill = newline
    send_button_selector='button[aria-label="Send message"], button.send-button, button[mattooltip="Send"]',
    response_selector='.model-response-text, message-content, .response-container .markdown',
    last_response_selector='.model-response-text:last-of-type, message-content:last-of-type',
    loading_selector='mat-progress-spinner, .loading-indicator, [aria-label="Loading"]',
    stop_button_selector='button[aria-label="Stop response"], button.stop-button',
)

CHATGPT_SELECTORS = CDPSelectors(
    input_selector='#prompt-textarea, div[contenteditable="true"][id="prompt-textarea"]',
    input_is_contenteditable=True,
    send_with_enter=True,
    # send button may not appear until text is typed — Enter is primary send method
    send_button_selector='button[data-testid="send-button"], button[data-testid="composer-send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"]',
    response_selector='div[data-message-author-role="assistant"] .markdown, div[data-message-author-role="assistant"]',
    last_response_selector='div[data-message-author-role="assistant"]:last-of-type .markdown, div[data-message-author-role="assistant"]:last-of-type',
    # .result-streaming class on the response div is the most reliable completion signal
    loading_selector='.result-streaming, button[data-testid="stop-button"]',
    stop_button_selector='button[data-testid="stop-button"], button[aria-label="Stop generating"]',
)

CLAUDE_SELECTORS = CDPSelectors(
    input_selector='div.ProseMirror[contenteditable="true"], div[contenteditable="true"].ProseMirror, fieldset div[contenteditable="true"]',
    input_is_contenteditable=True,
    send_with_enter=True,
    send_button_selector='button[aria-label="Send Message"], button[aria-label="Send message"]',
    response_selector='div[data-is-streaming] .font-claude-message, .font-claude-message, [data-testid="chat-message-content"]',
    last_response_selector='[data-is-streaming]:last-of-type .font-claude-message, .font-claude-message:last-of-type',
    # data-is-streaming="true" flips to "false" when done — most reliable signal
    loading_selector='div[data-is-streaming="true"]',
    stop_button_selector='button[aria-label="Stop Response"], button[aria-label="Stop response"]',
)

OPENROUTER_SELECTORS = CDPSelectors(
    input_selector='textarea[placeholder*="message"], textarea[placeholder*="Message"], textarea',
    input_is_contenteditable=False,
    send_with_enter=False,
    send_button_selector='button[aria-label="Send message"], button[type="submit"], button[aria-label="Send"]',
    response_selector='.prose, .markdown-body, [class*="message"] .prose',
    last_response_selector='.prose:last-of-type',
    loading_selector='[class*="animate-pulse"], [class*="animate-spin"]',
    stop_button_selector='button[aria-label="Stop"], button[aria-label="Stop generating"]',
)

OLLAMA_WEBUI_SELECTORS = CDPSelectors(
    input_selector='textarea#chat-textarea, textarea[placeholder*="message"], #chat-textarea',
    input_is_contenteditable=False,
    send_with_enter=True,
    send_button_selector='button#send-message-button, button[type="submit"]',
    response_selector='.chat-assistant .prose, [data-role="assistant"] .prose, .assistant-message',
    last_response_selector='.chat-assistant:last-of-type .prose, [data-role="assistant"]:last-of-type .prose',
    loading_selector='[class*="generating"], .chat-assistant .animate-pulse',
    stop_button_selector='button#stop-response-button, button[aria-label="Stop"]',
)

COPILOT_SELECTORS = CDPSelectors(
    input_selector='textarea[placeholder*="message"], #searchbox textarea, textarea',
    input_is_contenteditable=False,
    send_with_enter=True,
    send_button_selector='button[aria-label="Submit"], button[type="submit"]',
    response_selector='.ac-container .ac-textBlock, [class*="response"] p',
    last_response_selector='.ac-container:last-of-type .ac-textBlock',
    loading_selector='[class*="typing"], [class*="loading"]',
    stop_button_selector='button[aria-label="Stop responding"]',
)

# Map profile names to selector presets (hardcoded fallback)
SELECTOR_PRESETS: dict[str, CDPSelectors] = {
    "Gemini": GEMINI_SELECTORS,
    "ChatGPT": CHATGPT_SELECTORS,
    "Claude": CLAUDE_SELECTORS,
    "OpenRouter": OPENROUTER_SELECTORS,
    "Ollama Web UI": OLLAMA_WEBUI_SELECTORS,
    "Copilot": COPILOT_SELECTORS,
}

# Cached loaded selectors (loaded once at first use)
_loaded_selectors: Optional[dict[str, CDPSelectors]] = None


def _cdp_selectors_from_dict(data: dict) -> CDPSelectors:
    """Create a CDPSelectors from a JSON-style dict."""
    known_fields = {f.name for f in CDPSelectors.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    return CDPSelectors(**filtered)


def _load_selectors_from_json() -> dict[str, CDPSelectors]:
    """Load selector overrides from JSON config files.

    Priority chain:
    1. ~/.autocoder/selectors.json (user overrides — edit this to fix broken selectors)
    2. default_selectors.json (shipped with package — updated with releases)
    3. Hardcoded SELECTOR_PRESETS (ultimate fallback, always works)

    The user override file only needs to contain the profiles they want to change.
    Missing profiles fall through to the next level.
    """
    from pathlib import Path

    result: dict[str, CDPSelectors] = {}

    # Level 2: Package defaults
    try:
        pkg_json = Path(__file__).parent / "default_selectors.json"
        if pkg_json.exists():
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            for name, sel_data in data.items():
                result[name] = _cdp_selectors_from_dict(sel_data)
            logger.debug("Loaded %d selector presets from %s", len(result), pkg_json)
    except Exception as e:
        logger.warning("Failed to load default_selectors.json: %s", e)

    # Level 1: User overrides (merges on top — only overrides what's present)
    try:
        user_json = Path.home() / ".autocoder" / "selectors.json"
        if user_json.exists():
            data = json.loads(user_json.read_text(encoding="utf-8"))
            for name, sel_data in data.items():
                result[name] = _cdp_selectors_from_dict(sel_data)
            logger.info("Loaded %d selector overrides from %s", len(data), user_json)
    except Exception as e:
        logger.warning("Failed to load user selectors.json: %s", e)

    return result


def get_selectors_for_profile(profile_name: str) -> CDPSelectors:
    """Get CDP selectors for a given AI profile name.

    Checks JSON config files first (user overrides + package defaults),
    then falls back to hardcoded SELECTOR_PRESETS.
    """
    global _loaded_selectors
    if _loaded_selectors is None:
        _loaded_selectors = _load_selectors_from_json()

    # JSON overrides take priority
    if profile_name in _loaded_selectors:
        return _loaded_selectors[profile_name]

    # Hardcoded fallback
    return SELECTOR_PRESETS.get(profile_name, CDPSelectors())


# ── CDP Connection ────────────────────────────────────────────────

class CDPConnection:
    """WebSocket connection to a single Chrome/Edge tab via CDP.

    Handles:
    - Connecting to the debugging WebSocket
    - Sending commands and receiving results
    - Evaluating JavaScript in the page context
    - Finding elements and interacting with them
    """

    def __init__(self, ws_url: str, timeout: float = CDP_TIMEOUT) -> None:
        self._ws_url = ws_url
        self._timeout = timeout
        self._ws: Optional[websocket.WebSocket] = None
        self._msg_id = 0
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Open WebSocket connection to the CDP target."""
        try:
            self._ws = websocket.create_connection(
                self._ws_url,
                timeout=self._timeout,
                suppress_origin=True,
            )
            logger.info("CDP connected: %s", self._ws_url[:80])
            return True
        except Exception as e:
            logger.error("CDP connection failed: %s", e)
            return False

    def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.connected

    def send_command(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a CDP command and wait for the result."""
        if not self.is_connected:
            raise RuntimeError("CDP not connected")

        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id

            message = {"id": msg_id, "method": method}
            if params:
                message["params"] = params

            self._ws.send(json.dumps(message))

            # Read responses until we get our result
            deadline = time.time() + self._timeout
            while time.time() < deadline:
                try:
                    self._ws.settimeout(max(0.1, deadline - time.time()))
                    raw = self._ws.recv()
                    response = json.loads(raw)

                    if response.get("id") == msg_id:
                        if "error" in response:
                            error = response["error"]
                            raise RuntimeError(
                                f"CDP error {error.get('code')}: {error.get('message')}"
                            )
                        return response.get("result", {})

                    # Not our response — it's an event, ignore it
                except websocket.WebSocketTimeoutException:
                    continue

            raise TimeoutError(f"CDP command timed out: {method}")

    def evaluate_js(self, expression: str, await_promise: bool = False) -> Any:
        """Evaluate JavaScript in the page and return the result.

        This is the workhorse — most interactions go through JS evaluation.
        """
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }
        result = self.send_command("Runtime.evaluate", params)

        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            text = exc.get("text", "")
            desc = exc.get("exception", {}).get("description", "")
            raise RuntimeError(f"JS error: {text} {desc}")

        value = result.get("result", {}).get("value")
        return value

    def find_element(self, selector: str) -> bool:
        """Check if a DOM element matching the selector exists."""
        js = f"!!document.querySelector({json.dumps(selector)})"
        try:
            return bool(self.evaluate_js(js))
        except Exception:
            return False

    def get_element_text(self, selector: str) -> str:
        """Get the text content of an element."""
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            return el ? el.innerText || el.textContent || '' : '';
        }})()
        """
        try:
            result = self.evaluate_js(js)
            return str(result) if result else ""
        except Exception:
            return ""

    def get_all_elements_text(self, selector: str) -> list[str]:
        """Get text content of ALL elements matching a selector."""
        js = f"""
        (() => {{
            const els = document.querySelectorAll({json.dumps(selector)});
            return Array.from(els).map(el => el.innerText || el.textContent || '');
        }})()
        """
        try:
            result = self.evaluate_js(js)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def click_element(self, selector: str) -> bool:
        """Click a DOM element by selector.

        Dispatches a full pointer + mouse event sequence (pointerdown,
        mousedown, pointerup, mouseup, click) via JS dispatchEvent.
        Angular/Material components need these bubbling events to trigger
        their handlers. Falls back to el.click() if dispatchEvent fails.
        """
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            const events = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
            for (const type of events) {{
                el.dispatchEvent(new MouseEvent(type, {{
                    bubbles: true, cancelable: true, view: window
                }}));
            }}
            return true;
        }})()
        """
        try:
            result = bool(self.evaluate_js(js))
            if result:
                return True
        except Exception:
            pass
        # Fallback: simple el.click()
        fallback_js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.click();
            return true;
        }})()
        """
        try:
            return bool(self.evaluate_js(fallback_js))
        except Exception:
            return False

    def set_input_value(self, selector: str, text: str, is_contenteditable: bool = False) -> bool:
        """Set the value of an input element.

        Handles both <textarea> and contenteditable <div> elements.
        For contenteditable (ProseMirror, Quill), uses CDP Input.insertText
        which is more reliable than execCommand and properly triggers
        framework state updates.
        """
        escaped_text = json.dumps(text)

        if is_contenteditable:
            # Insert text using execCommand which triggers the editor's
            # mutation observer and framework model updates (Angular, React).
            # This is critical for Gemini — CDP Input.insertText and
            # Quill's setText() bypass Angular bindings, so the send
            # button won't work afterwards.
            #
            # For large texts (>50KB), use the bulk upload approach.
            import time as _time
            LARGE_THRESHOLD = 50_000

            if len(text) <= LARGE_THRESHOLD:
                try:
                    insert_js = f"""
                    (() => {{
                        const el = document.querySelector({json.dumps(selector)});
                        if (!el) return false;
                        el.focus();
                        document.execCommand('selectAll');
                        document.execCommand('insertText', false, {json.dumps(text)});
                        return el.textContent.length > 0;
                    }})()
                    """
                    result = self.evaluate_js(insert_js)
                    if result:
                        logger.debug("Inserted %d chars via execCommand", len(text))
                        return True
                    logger.warning("execCommand returned false for %d chars", len(text))
                except Exception as e:
                    logger.warning("execCommand failed for %d chars: %s", len(text), e)

            # ── Large text: store in JS variable via chunks, then insert ──
            try:
                return self._insert_large_text(selector, text)
            except Exception as e:
                logger.error("Large text insertion failed: %s", e)
                return False
        else:
            # Standard textarea/input
            js = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                el.focus();
                // Use native setter to bypass React's synthetic events
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set || Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set;
                if (nativeSetter) {{
                    nativeSetter.call(el, {escaped_text});
                }} else {{
                    el.value = {escaped_text};
                }}
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }})()
            """
            try:
                return bool(self.evaluate_js(js))
            except Exception as e:
                logger.error("Failed to set textarea value: %s", e)
                return False

    def _insert_large_text(self, selector: str, text: str) -> bool:
        """Insert large text into a contenteditable element efficiently.

        Strategy: upload text to the page in chunks via a JS global variable,
        then use a synthetic clipboard paste event to insert it all at once.
        This avoids Quill re-rendering per-chunk (which freezes the page).
        """
        import time as _time

        # Phase 1: Upload text to page in 50KB JS-safe chunks
        UPLOAD_CHUNK = 50_000
        total = len(text)

        # Initialize the accumulator
        self.evaluate_js("window.__cdp_bulk_text = '';")

        sent = 0
        chunk_idx = 0
        while sent < total:
            chunk = text[sent:sent + UPLOAD_CHUNK]
            escaped_chunk = json.dumps(chunk)
            self.evaluate_js(f"window.__cdp_bulk_text += {escaped_chunk};")
            sent += len(chunk)
            chunk_idx += 1
            if chunk_idx % 5 == 0:
                _time.sleep(0.05)  # Breathe every 5 chunks

        # Verify upload. Tolerate small differences — JS strings count
        # UTF-16 code units, not bytes, so characters outside the BMP
        # (emoji, some CJK) count as 2. Up to ~1% drift is normal for
        # text with any non-ASCII; reject only on wildly-off counts.
        uploaded_len = self.evaluate_js("window.__cdp_bulk_text.length")
        try:
            uploaded_int = int(uploaded_len) if uploaded_len is not None else -1
        except (TypeError, ValueError):
            uploaded_int = -1
        tolerance = max(16, int(total * 0.01))  # ≥16 chars OR 1% of total
        if uploaded_int < 0 or abs(uploaded_int - total) > tolerance:
            logger.error(
                "Upload mismatch: expected %d, got %s (tolerance ±%d)",
                total, uploaded_len, tolerance,
            )
            self.evaluate_js("delete window.__cdp_bulk_text;")
            return False
        if uploaded_int != total:
            logger.info(
                "Upload length drift: expected %d, got %d (within tolerance)",
                total, uploaded_int,
            )

        logger.info("Uploaded %d chars to page in %d chunks", total, chunk_idx)

        # Phase 2: Insert using execCommand which triggers Quill's
        # mutation observer and Angular's model binding.
        paste_js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'no_element';
            el.focus();

            const text = window.__cdp_bulk_text;
            delete window.__cdp_bulk_text;

            // execCommand: triggers Quill mutation observer + Angular binding
            document.execCommand('selectAll');
            const ok = document.execCommand('insertText', false, text);
            if (ok && el.textContent.length > 100) return 'execCommand';

            // Fallback: Quill API (send button may not work — Angular bypass)
            const quillContainer = el.closest('.ql-container');
            if (quillContainer && quillContainer.__quill) {{
                const quill = quillContainer.__quill;
                const len = quill.getLength();
                if (len > 1) quill.deleteText(0, len);
                quill.insertText(0, text, 'user');
                quill.update('user');
                el.classList.remove('ql-blank');
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                return 'quill_api';
            }}

            // Last resort: direct textContent
            el.textContent = text;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            return 'textContent';
        }})()
        """
        result = self.evaluate_js(paste_js)
        logger.info("Large text insertion method: %s (%d chars)", result, total)

        if result == 'no_element':
            return False

        _time.sleep(0.3)  # Let the editor settle
        return True

    def press_enter(self) -> bool:
        """Simulate pressing Enter on the focused element."""
        js = """
        (() => {
            const el = document.activeElement;
            if (!el) return false;
            const events = ['keydown', 'keypress', 'keyup'];
            for (const type of events) {
                el.dispatchEvent(new KeyboardEvent(type, {
                    key: 'Enter', code: 'Enter', keyCode: 13,
                    which: 13, bubbles: true, cancelable: true,
                }));
            }
            return true;
        })()
        """
        try:
            return bool(self.evaluate_js(js))
        except Exception:
            return False

    def dispatch_enter_on(self, selector: str) -> bool:
        """Dispatch Enter key event on a specific element.

        Uses CDP Input.dispatchKeyEvent for maximum compatibility
        (works with ProseMirror, Quill, React, etc.)
        """
        # First focus the element
        focus_js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.focus();
            return true;
        }})()
        """
        try:
            focused = self.evaluate_js(focus_js)
            if not focused:
                return False
        except Exception:
            return False

        # Then dispatch Enter via CDP protocol (more reliable than JS events)
        try:
            self.send_command("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            })
            self.send_command("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            })
            return True
        except Exception as e:
            logger.warning("CDP Enter key failed: %s, trying JS fallback", e)

        # Fallback: JS-level events
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.focus();
            for (const type of ['keydown', 'keypress', 'keyup']) {{
                el.dispatchEvent(new KeyboardEvent(type, {{
                    key: 'Enter', code: 'Enter', keyCode: 13,
                    which: 13, bubbles: true, cancelable: true,
                }}));
            }}
            return true;
        }})()
        """
        try:
            return bool(self.evaluate_js(js))
        except Exception:
            return False

    def get_page_url(self) -> str:
        """Get the current page URL."""
        try:
            return str(self.evaluate_js("window.location.href") or "")
        except Exception:
            return ""

    def get_page_title(self) -> str:
        """Get the current page title."""
        try:
            return str(self.evaluate_js("document.title") or "")
        except Exception:
            return ""

    def navigate_to(self, url: str, wait_seconds: float = 3.0) -> bool:
        """Navigate the current tab to a new URL.

        Uses Page.navigate CDP command. Waits for the page to settle.
        Returns True if navigation succeeded.
        """
        try:
            result = self.send_command("Page.navigate", {"url": url})
            if "error" in result:
                logger.error("Navigation failed: %s", result)
                return False
            time.sleep(wait_seconds)
            return True
        except Exception as e:
            logger.error("Navigation error: %s", e)
            return False

    def scroll_to_bottom(self) -> None:
        """Scroll the page to the bottom (useful for chat views)."""
        self.evaluate_js("window.scrollTo(0, document.body.scrollHeight)")

    def count_elements(self, selector: str) -> int:
        """Count how many elements match a selector."""
        js = f"document.querySelectorAll({json.dumps(selector)}).length"
        try:
            return int(self.evaluate_js(js) or 0)
        except Exception:
            return 0


# ── CDP Tab Discovery ─────────────────────────────────────────────

@dataclass
class CDPTarget:
    """A Chrome/Edge tab available for CDP connection."""
    target_id: str
    title: str
    url: str
    ws_url: str
    tab_type: str = "page"


def discover_cdp_targets(port: int = DEFAULT_CDP_PORT, host: str = "127.0.0.1") -> list[CDPTarget]:
    """Query Chrome's /json endpoint to find all debuggable tabs."""
    if not URLLIB_AVAILABLE:
        return []

    try:
        url = f"http://{host}:{port}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())

        targets = []
        for item in data:
            if item.get("type") != "page":
                continue
            ws_url = item.get("webSocketDebuggerUrl", "")
            if not ws_url:
                continue
            targets.append(CDPTarget(
                target_id=item.get("id", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                ws_url=ws_url,
                tab_type=item.get("type", "page"),
            ))

        logger.debug("Found %d CDP targets on port %d", len(targets), port)
        return targets

    except Exception as e:
        logger.debug("CDP discovery failed on port %d: %s", port, e)
        return []


def find_target_by_url(url_pattern: str, port: int = DEFAULT_CDP_PORT) -> Optional[CDPTarget]:
    """Find a CDP target whose URL contains the given pattern."""
    targets = discover_cdp_targets(port)
    pattern_lower = url_pattern.lower()
    for target in targets:
        if pattern_lower in target.url.lower():
            return target
    return None


def find_target_by_title(title_pattern: str, port: int = DEFAULT_CDP_PORT) -> Optional[CDPTarget]:
    """Find a CDP target whose title contains the given pattern."""
    targets = discover_cdp_targets(port)
    pattern_lower = title_pattern.lower()
    for target in targets:
        if pattern_lower in target.title.lower():
            return target
    return None


def is_cdp_available(port: int = DEFAULT_CDP_PORT) -> bool:
    """Check if Chrome is running with remote debugging enabled."""
    try:
        targets = discover_cdp_targets(port)
        return len(targets) > 0
    except Exception:
        return False


# ── High-level AI Chat Automation ─────────────────────────────────

class CDPChatAutomation:
    """High-level chat automation using CDP.

    Combines CDPConnection + CDPSelectors to provide simple methods:
    - send_prompt(text) -> sends text to the chat input and submits
    - wait_for_response() -> waits until the AI finishes generating
    - read_last_response() -> returns the AI's latest response text
    - send_and_read(text) -> full cycle: send, wait, read
    """

    def __init__(
        self,
        connection: CDPConnection,
        selectors: CDPSelectors,
        profile_name: str = "Unknown",
    ) -> None:
        self._conn = connection
        self._sel = selectors
        self._profile_name = profile_name
        self._response_count_before: int = 0

    @property
    def connection(self) -> CDPConnection:
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn.is_connected

    def new_conversation(self, wait_seconds: float = 6.0) -> bool:
        """Start a fresh conversation by navigating to the AI's base URL.

        This resets the conversation context — critical when the AI's
        context window is full and responses become stale/repetitive.
        Detects the current site and navigates to its fresh-chat URL.
        Waits for the chat input to be ready before returning.
        """
        url = self._conn.get_page_url().lower()
        fresh_urls = {
            "gemini.google.com": "https://gemini.google.com/app",
            "chatgpt.com": "https://chatgpt.com/",
            "chat.openai.com": "https://chatgpt.com/",
            "claude.ai": "https://claude.ai/new",
        }
        target_url = ""
        for pattern, fresh_url in fresh_urls.items():
            if pattern in url:
                target_url = fresh_url
                break

        if not target_url:
            logger.warning("Unknown AI site, reloading page instead of new conversation")
            self._conn.evaluate_js("location.reload()")
            time.sleep(wait_seconds)
            return True

        logger.info("Starting new conversation: %s -> %s", self._profile_name, target_url)
        success = self._conn.navigate_to(target_url, wait_seconds=wait_seconds)
        if success:
            # Reset response tracking — fresh page has zero responses
            self._response_count_before = 0

            # Wait for the chat input element to appear (page fully loaded)
            for attempt in range(10):
                for selector in self._sel.input_selector.split(","):
                    selector = selector.strip()
                    if self._conn.find_element(selector):
                        logger.info("New conversation ready for %s (input found after %.1fs)",
                                    self._profile_name, (attempt + 1) * 1.0)
                        return True
                time.sleep(1.0)

            logger.warning("New conversation: input not found after %ds for %s",
                           10, self._profile_name)
            # Still return True — the page navigated, input may appear later
        return success

    def send_prompt(self, text: str) -> bool:
        """Type a prompt into the chat input and send it.

        Returns True if the prompt was sent successfully.
        """
        if not self._conn.is_connected:
            logger.error("CDP not connected for %s", self._profile_name)
            return False

        # Record current response count for completion detection
        self._response_count_before = self._count_responses()

        # Find and focus the input element
        # Try each selector in the comma-separated list
        input_found = False
        for selector in self._sel.input_selector.split(","):
            selector = selector.strip()
            if self._conn.find_element(selector):
                if self._conn.set_input_value(
                    selector, text,
                    is_contenteditable=self._sel.input_is_contenteditable,
                ):
                    input_found = True
                    logger.debug("Input found with selector: %s", selector)
                    break

        if not input_found:
            logger.error("Could not find chat input for %s", self._profile_name)
            return False

        # Small delay for UI to register the input
        time.sleep(0.3)

        # Send the message
        if self._sel.send_with_enter:
            # Dispatch Enter on the input
            for selector in self._sel.input_selector.split(","):
                selector = selector.strip()
                if self._conn.find_element(selector):
                    self._conn.dispatch_enter_on(selector)
                    break
        else:
            # Click the send button
            sent = False
            for selector in self._sel.send_button_selector.split(","):
                selector = selector.strip()
                if self._conn.click_element(selector):
                    sent = True
                    break
            if not sent:
                # Fallback: try Enter anyway
                logger.warning("Send button not found, trying Enter key")
                self._conn.press_enter()

        logger.info("Prompt sent to %s (%d chars)", self._profile_name, len(text))
        return True

    def wait_for_response(
        self,
        timeout: float = MAX_COMPLETION_WAIT,
        poll_interval: float = COMPLETION_POLL_INTERVAL,
        on_progress: Optional[Any] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """Wait for the AI to finish generating its response.

        Uses multiple detection strategies:
        1. New response element appears (count increases)
        2. Loading/stop indicators appear then disappear
        3. Response text stops changing (stabilizes)

        Returns True if response detected, False on timeout.
        """
        start = time.time()
        last_text = ""
        stable_count = 0
        STABLE_THRESHOLD = 3  # Text unchanged for 3 polls = done
        new_response_detected = False
        initial_text = self._get_latest_response_text()

        # Initial delay to let generation start
        time.sleep(1.5)

        while time.time() - start < timeout:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Cancelled during wait")

            elapsed = time.time() - start

            # Check indicators
            has_loading = self._check_loading()
            has_stop = self._check_stop_button()

            # Check response count
            current_count = self._count_responses()
            if current_count > self._response_count_before:
                new_response_detected = True

            # Get latest response text
            current_text = self._get_latest_response_text()

            # Ignore temporary empty reads (DOM re-rendering)
            if not current_text and last_text:
                current_text = last_text  # Keep previous value

            # Only count as stable if a NEW response appeared and text changed
            if new_response_detected and current_text != initial_text:
                if current_text and current_text == last_text and len(current_text) > 5:
                    stable_count += 1
                elif current_text and current_text != last_text:
                    stable_count = 0  # Still changing
                # Don't reset stable_count for empty reads
            else:
                stable_count = 0
            last_text = current_text

            # Report progress
            if on_progress:
                remaining = int(timeout - elapsed)
                status = "generating" if (has_loading or has_stop) else "checking"
                on_progress(
                    f"{self._profile_name}: {status}... "
                    f"{remaining}s remaining | {len(current_text)} chars"
                )

            # Decision: Are we done?
            if new_response_detected and current_text != initial_text:
                # New response appeared and text is different from before.
                # Only loading indicator blocks — stop button is ignored
                # because some sites (Gemini) keep it visible after completion.
                if not has_loading and stable_count >= STABLE_THRESHOLD:
                    logger.info(
                        "%s finished generating (%.1fs, %d chars)",
                        self._profile_name, elapsed, len(current_text)
                    )
                    return True
            elif elapsed > 20 and not new_response_detected:
                # 20s and no new response element — maybe the site uses
                # a different structure. Check if text changed at all.
                if current_text and current_text != initial_text and stable_count >= STABLE_THRESHOLD:
                    logger.info("%s: text changed and stabilized after %.1fs", self._profile_name, elapsed)
                    return True

            time.sleep(poll_interval)

        logger.warning("%s: response wait timed out after %.0fs", self._profile_name, timeout)
        return False

    @staticmethod
    def _is_sidebar_junk(text: str) -> bool:
        """Detect if text is sidebar/navigation junk instead of an AI response.

        When navigating to a fresh chat page, selectors may match sidebar
        elements (chat history, menu items) instead of actual responses.
        """
        if not text or len(text.strip()) < 3:
            return True

        # Sidebar hallmarks: lots of short lines, menu keywords
        sidebar_markers = [
            "Expand menu", "New chat", "Search chats", "Add files",
            "Use microphone", "Fullscreen", "Submit",
            "Ctrl+Shift+K", "Ctrl+Shift+O",
            "Learning coach", "Productivity planner",
            "Meet Gemini", "your personal AI assistant",
        ]
        marker_count = sum(1 for m in sidebar_markers if m in text)
        if marker_count >= 2:
            return True

        # If it's mostly short fragments joined together (sidebar chat titles)
        lines = text.strip().split('\n')
        if len(lines) < 3 and len(text) < 500:
            # Very short text with no code indicators — likely junk
            code_indicators = ['def ', 'class ', 'import ', 'function ', 'const ',
                               'return ', '```', 'if ', 'for ', 'while ']
            if not any(ind in text for ind in code_indicators):
                # Check ratio of capitalized words (menu items tend to be Title Case)
                words = text.split()
                if words and sum(1 for w in words if w[0].isupper()) / len(words) > 0.6:
                    return True

        return False

    def read_last_response(self) -> str:
        """Read the text of the AI's most recent response.

        Always gets ALL response elements and takes the LAST one.
        CSS :last-of-type selectors are unreliable for nested chat structures.
        Rejects sidebar/navigation text that can appear on fresh chat pages.
        """
        if not self._conn.is_connected:
            return ""

        # Primary: Get all responses via response_selector, take last
        if self._sel.response_selector:
            for selector in self._sel.response_selector.split(","):
                selector = selector.strip()
                texts = self._conn.get_all_elements_text(selector)
                if texts:
                    meaningful = [t for t in texts
                                  if len(t.strip()) > 5
                                  and not self._is_sidebar_junk(t)]
                    if meaningful:
                        return meaningful[-1].strip()

        # Fallback: try common selectors
        fallback_selectors = [
            ".markdown", ".prose", "[class*='response'] .markdown",
            "[class*='message']", "[class*='answer']",
            "article", ".content",
        ]
        for selector in fallback_selectors:
            texts = self._conn.get_all_elements_text(selector)
            meaningful = [t for t in texts
                          if len(t.strip()) > 10
                          and not self._is_sidebar_junk(t)]
            if meaningful:
                return meaningful[-1].strip()

        logger.warning("Could not find response text for %s", self._profile_name)
        return ""

    def send_and_read(
        self,
        prompt: str,
        timeout: float = MAX_COMPLETION_WAIT,
        on_progress: Optional[Any] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Full cycle: send prompt, wait for response, read it."""
        if not self.send_prompt(prompt):
            raise RuntimeError(f"Failed to send prompt to {self._profile_name}")

        if not self.wait_for_response(
            timeout=timeout,
            on_progress=on_progress,
            cancel_event=cancel_event,
        ):
            logger.warning("%s: wait timed out, reading whatever is available", self._profile_name)

        # Small delay for final rendering
        time.sleep(1.0)
        return self.read_last_response()

    def _check_loading(self) -> bool:
        """Check if any VISIBLE loading indicator is present.

        Uses offsetParent + offsetWidth to exclude hidden/zero-size elements
        (e.g. Gemini keeps a hidden mat-progress-spinner in the DOM always).
        """
        if not self._sel.loading_selector:
            return False
        for selector in self._sel.loading_selector.split(","):
            selector = selector.strip()
            visible = self._conn.evaluate_js(
                f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
                f"return el ? (el.offsetParent !== null && el.offsetWidth > 0) : false; }})()"
            )
            if visible:
                return True
        return False

    def _check_stop_button(self) -> bool:
        """Check if a VISIBLE stop/cancel generation button is present."""
        if not self._sel.stop_button_selector:
            return False
        for selector in self._sel.stop_button_selector.split(","):
            selector = selector.strip()
            visible = self._conn.evaluate_js(
                f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
                f"return el ? (el.offsetParent !== null && el.offsetWidth > 0) : false; }})()"
            )
            if visible:
                return True
        return False

    def _get_latest_response_text(self) -> str:
        """Get the text of the LAST (newest) response for change detection.

        Always uses get_all_elements_text and takes [-1] to ensure
        we're reading the newest response, not an older one.
        Filters out sidebar/navigation junk on fresh chat pages.
        """
        # Strategy: get ALL response elements and take the last one
        if self._sel.response_selector:
            for selector in self._sel.response_selector.split(","):
                selector = selector.strip()
                texts = self._conn.get_all_elements_text(selector)
                if texts:
                    meaningful = [t for t in texts
                                  if len(t.strip()) > 5
                                  and not self._is_sidebar_junk(t)]
                    if meaningful:
                        return meaningful[-1]
        # Fallback to last_response_selector
        if self._sel.last_response_selector:
            for selector in self._sel.last_response_selector.split(","):
                selector = selector.strip()
                text = self._conn.get_element_text(selector)
                if text and len(text.strip()) > 5 and not self._is_sidebar_junk(text):
                    return text
        return ""

    def _count_responses(self) -> int:
        """Count the number of response elements on the page."""
        if self._sel.response_selector:
            for selector in self._sel.response_selector.split(","):
                selector = selector.strip()
                count = self._conn.count_elements(selector)
                if count > 0:
                    return count
        return 0


# ── Browser Launch Helpers ────────────────────────────────────────

def get_chrome_debug_args(port: int = DEFAULT_CDP_PORT) -> list[str]:
    """Return Chrome command-line args needed for CDP debugging."""
    return [
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def launch_chrome_with_cdp(
    url: str = "",
    port: int = DEFAULT_CDP_PORT,
    browser_exe: str = "",
    extra_args: Optional[list[str]] = None,
    corner: str = "",
) -> bool:
    """Launch Chrome/Edge with remote debugging enabled.

    Uses a dedicated Autocoder Chrome profile (~/.autocoder/chrome_profile).
    The user logs in once to each AI site and it's remembered across sessions.
    Chrome's real profile can't be used with --remote-debugging-port.
    """
    import subprocess
    import os
    from pathlib import Path

    if not browser_exe:
        from .window_manager import _find_browser_exe
        browser_exe = _find_browser_exe("chrome") or _find_browser_exe("edge") or ""

    if not browser_exe:
        logger.error("No browser found")
        return False

    # If a corner is specified, use its dedicated port
    if corner and corner in CDP_CORNER_PORTS:
        port = CDP_CORNER_PORTS[corner]

    args = [browser_exe] + get_chrome_debug_args(port)

    # Always use a dedicated Autocoder profile.
    # Chrome won't honor --remote-debugging-port with the real profile
    # because of its singleton process model. The user logs in once
    # to each AI site in this profile and it's remembered.
    profile_dir = str(Path.home() / ".autocoder" / "chrome_profile")
    args.append(f"--user-data-dir={profile_dir}")

    if extra_args:
        args.extend(extra_args)

    if url:
        args.append(url)

    try:
        subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("Launched browser with CDP on port %d: %s", port, url)
        # Wait for browser to start
        time.sleep(4)
        return is_cdp_available(port)
    except Exception as e:
        logger.error("Failed to launch browser with CDP: %s", e)
        return False


def is_chrome_running() -> bool:
    """Check if Chrome is currently running."""
    import subprocess
    try:
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq chrome.exe'],
            capture_output=True, text=True, timeout=5,
        )
        return 'chrome.exe' in result.stdout
    except Exception:
        return False


def kill_chrome() -> bool:
    """Kill all Chrome processes (needed to relaunch with CDP)."""
    import subprocess
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'],
                       capture_output=True, timeout=10)
        time.sleep(2)
        return not is_chrome_running()
    except Exception:
        return False


# ── Convenience: connect to a specific AI site ────────────────────

# Track which WebSocket URLs are already claimed by a session.
# This prevents multiple sessions from sharing the same tab,
# which causes WebSocket corruption and crashes.
_claimed_ws_urls: set[str] = set()
_claimed_ws_lock = threading.Lock()


def claim_ws_url(ws_url: str) -> bool:
    """Claim a WebSocket URL so no other session uses it."""
    with _claimed_ws_lock:
        if ws_url in _claimed_ws_urls:
            return False
        _claimed_ws_urls.add(ws_url)
        return True


def release_ws_url(ws_url: str) -> None:
    """Release a WebSocket URL when a session disconnects."""
    with _claimed_ws_lock:
        _claimed_ws_urls.discard(ws_url)


def _find_unclaimed_target(
    url_pattern: str = "",
    title_pattern: str = "",
    port: int = DEFAULT_CDP_PORT,
) -> Optional[CDPTarget]:
    """Find a CDP target matching the pattern that isn't already claimed."""
    targets = discover_cdp_targets(port)
    for target in targets:
        # Check URL match
        if url_pattern and url_pattern.lower() not in target.url.lower():
            continue
        # Check title match (if no url_pattern or as fallback)
        if not url_pattern and title_pattern:
            if title_pattern.lower() not in target.title.lower():
                continue

        # Skip if already claimed by another session
        with _claimed_ws_lock:
            if target.ws_url in _claimed_ws_urls:
                continue

        return target
    return None


def _derive_profile_hints(profile_name: str) -> tuple[str, str]:
    """Pull the url_pattern + title_pattern off a named profile.

    Safety net: if the caller doesn't pass a url_pattern, we still match
    the right tab. Without this fallback, `connect_to_ai_site('Gemini')`
    picks whatever tab is first in the list — a real bug that surfaces
    as soon as multiple AI tabs are open.
    """
    try:
        from .ai_profiles import get_profile
        p = get_profile(profile_name)
        if p is not None:
            return (
                getattr(p, "url_pattern", "") or "",
                getattr(p, "title_pattern", "") or "",
            )
    except Exception:
        pass
    return "", ""


def connect_to_ai_site(
    profile_name: str,
    url_pattern: str = "",
    title_pattern: str = "",
    port: int = DEFAULT_CDP_PORT,
) -> Optional[CDPChatAutomation]:
    """Find a browser tab matching the AI site and return a chat automation object.

    Each call claims a DIFFERENT tab. If there are 4 Gemini tabs and
    4 sessions, each session gets its own tab. This prevents WebSocket
    corruption from multiple threads sharing one connection.

    Tries the specified port first, then scans all corner ports.

    Args:
        profile_name: Name of the AI profile (e.g., "Gemini", "ChatGPT")
        url_pattern: URL substring to match (e.g., "gemini.google.com")
        title_pattern: Window title substring to match
        port: CDP debugging port (tries this first, then others)

    Returns:
        CDPChatAutomation ready to use, or None if not found.
    """
    # If caller didn't supply matchers, fall back to the profile's own
    # url_pattern / title_pattern. Without this, with multiple AI tabs
    # open, every connect_to_ai_site() call picks whatever tab is first
    # in the list — they all end up pointing at the same tab.
    if not url_pattern and not title_pattern:
        url_pattern, title_pattern = _derive_profile_hints(profile_name)

    # Build list of ports to try: specified port first, then all corner ports
    ports_to_try = [port]
    for p in CDP_CORNER_PORTS.values():
        if p not in ports_to_try:
            ports_to_try.append(p)

    for try_port in ports_to_try:
        target = _find_unclaimed_target(url_pattern, title_pattern, try_port)

        if target:
            # Claim this tab BEFORE connecting
            if not claim_ws_url(target.ws_url):
                continue  # Race condition — someone else got it

            conn = CDPConnection(target.ws_url)
            if conn.connect():
                selectors = get_selectors_for_profile(profile_name)
                logger.info("CDP connected to %s on port %d (tab %s)",
                            profile_name, try_port, target.ws_url[-20:])
                return CDPChatAutomation(conn, selectors, profile_name)
            else:
                # Connection failed — release the claim
                release_ws_url(target.ws_url)

    logger.warning("No CDP target found for %s (url=%s, title=%s) on any port",
                    profile_name, url_pattern, title_pattern)
    return None

"""Broadcast mode — send a task to all active sessions and loop endlessly.

When enabled, the user types ONE task description and it gets:
1. Engineered into a proper prompt via Prompt Architect's engine
2. Sent to ALL active sessions (or selected ones)
3. After each session completes, it extracts the code and feeds it back
   with the next improvement prompt (context-aware loop)
4. Loops until the user clicks Stop or hits Ctrl+K

Each session works independently — they all start at the same time and
the Traffic Controller serializes their mouse access. The prompts are
customized per-session using the AI profile name for context.

IMPORTANT: Each improvement iteration includes the FULL previous codebase
in the prompt. This prevents context amnesia where the AI loses track of
what it built in earlier iterations.
"""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from gemini_coder.task_manager import CodingTask, TaskStatus
from .auto_save import save_task_output

logger = logging.getLogger(__name__)

# Import prompt engine for prompt engineering
try:
    from prompt_engine import (
        generate_code_prompt,
        BUILD_TARGETS,
        ENHANCEMENTS,
    )
    PROMPT_ENGINE_AVAILABLE = True
except ImportError:
    PROMPT_ENGINE_AVAILABLE = False
    logger.warning("prompt_engine not found — prompts will be sent raw")


def engineer_prompt(
    task: str,
    build_target: str = "PC Desktop App",
    enhancements: Optional[list[str]] = None,
    context: str = "",
) -> str:
    """Use Prompt Architect's engine to build a production-grade prompt.

    Takes a simple task like "Build a calculator" and expands it into
    a full engineered prompt with role, platform context, reasoning,
    constraints, and quality gates.
    """
    if not PROMPT_ENGINE_AVAILABLE:
        return task

    if enhancements is None:
        enhancements = ["More Features + Robustness"]

    return generate_code_prompt(
        task=task,
        build_target=build_target,
        enhancements=enhancements,
        context=context,
        reasoning="Structured CoT (SCoT)",
        output_format="Code Only",
        constraint_presets=["Python Best Practices", "Robustness"],
    )


# ── Selectable Improvement Focuses ──────────────────────────────────
# Each focus is a named improvement pass the AI runs on the codebase.
# Users pick which ones to enable via checkboxes in the UI.
# The "Perfection Loop" master option cycles all selected focuses and
# repeats until the codebase stops improving.

IMPROVEMENT_FOCUSES: dict[str, dict] = {
    "deep_dive": {
        "label": "Deep Code Dive",
        "description": "Analyze architecture, refactor, simplify complex logic",
        "prompt": (
            "Perform a DEEP CODE DIVE on this codebase.\n\n"
            "1. ARCHITECTURE AUDIT: Map the dependency graph. Identify circular "
            "imports, god classes, and modules doing too much. Refactor into "
            "clean, single-responsibility components.\n"
            "2. COMPLEXITY REDUCTION: Find every function over 30 lines or with "
            "nested depth > 3. Break them into smaller, testable units.\n"
            "3. DEAD CODE & DEBT: Remove unreachable code, unused imports, "
            "commented-out blocks. Eliminate TODO/FIXME by implementing them.\n"
            "4. NAMING & READABILITY: Rename vague variables (x, tmp, data) to "
            "descriptive names. Ensure consistent naming conventions throughout.\n"
            "5. TYPE SAFETY: Add type hints to every function signature and "
            "critical variables. Use dataclasses/NamedTuples for structured data.\n\n"
            "Apply ALL improvements to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders, no stubs."
        ),
    },
    "extra_features": {
        "label": "Extra Features",
        "description": "Add 2-3 useful features a real user would want",
        "prompt": (
            "ADD 2-3 genuinely useful features to this codebase.\n\n"
            "Think like a POWER USER who uses this daily. What's missing?\n"
            "- Configuration options (settings file, CLI args, env vars)\n"
            "- Undo/redo, history, bookmarks, favorites\n"
            "- Import/export (CSV, JSON, clipboard)\n"
            "- Search, filter, sort capabilities\n"
            "- Keyboard shortcuts, accessibility\n"
            "- Progress indicators, status feedback\n"
            "- Smart defaults, auto-save, remember-last-state\n\n"
            "Each feature must be COMPLETE — not a stub. Wire it into the "
            "existing UI/logic so it actually works end-to-end.\n\n"
            "Apply these improvements to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "pressure_test": {
        "label": "Pressure Test",
        "description": "Stress test every path, harden edge cases, bulletproof error handling",
        "prompt": (
            "PRESSURE TEST this codebase like a hostile QA engineer.\n\n"
            "1. INPUT FUZZING: What happens with empty strings, None, negative "
            "numbers, massive inputs (10MB string, 1M items), Unicode/emoji, "
            "path traversal strings, SQL injection attempts? Add guards.\n"
            "2. CONCURRENCY: Are there race conditions? Shared mutable state "
            "without locks? Add thread safety where needed.\n"
            "3. RESOURCE EXHAUSTION: Memory leaks? File handles left open? "
            "Unbounded caches? Add cleanup, context managers, limits.\n"
            "4. NETWORK/IO: What if the disk is full? Network times out? "
            "File is locked by another process? Add retry, timeout, fallback.\n"
            "5. ERROR PATHS: Every try/except must handle specific exceptions. "
            "No bare except. Log the actual error. Provide recovery path.\n"
            "6. GRACEFUL DEGRADATION: If a non-critical feature fails, the "
            "app should continue working. Add fallbacks for every optional feature.\n\n"
            "Fix every issue you find IN THE CODE. Don't just list problems.\n"
            "Apply ALL fixes to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "explore_expand": {
        "label": "Explore & Expand",
        "description": "Push boundaries, add ambitious new capabilities",
        "prompt": (
            "EXPLORE AND EXPAND this codebase's potential.\n\n"
            "You've been asked to take this from a good program to an IMPRESSIVE "
            "one. Think creatively:\n"
            "- What would make someone say 'wow, it does THAT too?'\n"
            "- Add a capability that transforms this from a tool into a platform\n"
            "- Integrate a complementary feature no one asked for but everyone needs\n"
            "- Add intelligence: caching of frequent operations, auto-detection "
            "of patterns, smart suggestions, learn from usage\n"
            "- Add a plugin/extension point so users can customize behavior\n\n"
            "Be BOLD but COMPLETE. Every new feature must work end-to-end.\n"
            "Wire everything into the existing codebase seamlessly.\n\n"
            "Apply these expansions to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "beautiful_gui": {
        "label": "Beautiful GUI",
        "description": "Build/improve GUI separately, ensure it's polished and modern",
        "prompt": (
            "REBUILD the GUI layer of this codebase to be BEAUTIFUL.\n\n"
            "SEPARATION: The GUI must be cleanly separated from business logic. "
            "If it isn't, refactor now: business logic in one module, GUI in another. "
            "The GUI imports and calls the logic, never the reverse.\n\n"
            "VISUAL DESIGN:\n"
            "- Modern look: rounded corners, subtle shadows, smooth transitions\n"
            "- Consistent color palette: define colors as constants, use them everywhere\n"
            "- Typography hierarchy: headings, body, captions with distinct sizes/weights\n"
            "- Whitespace: generous padding, breathing room between elements\n"
            "- Icons or emoji for visual anchors on buttons and sections\n"
            "- Status indicators with color coding (green=good, yellow=warning, red=error)\n\n"
            "UX POLISH:\n"
            "- Loading states for async operations\n"
            "- Hover effects on interactive elements\n"
            "- Disabled state styling for unavailable actions\n"
            "- Responsive layout that handles window resizing gracefully\n"
            "- Keyboard navigation and focus indicators\n"
            "- Tooltips on non-obvious controls\n\n"
            "Apply ALL improvements to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "solid_functional": {
        "label": "Solid & Functional",
        "description": "Verify all buttons work, all paths execute, code is production-solid",
        "prompt": (
            "VERIFY and FIX every interactive element in this codebase.\n\n"
            "Walk through the code as if you're CLICKING EVERY BUTTON:\n"
            "1. BUTTONS & ACTIONS: Trace every button/command handler. Does it "
            "actually call the right function? Does that function exist? Are "
            "callbacks wired correctly? Fix any broken connections.\n"
            "2. STATE MANAGEMENT: Are variables initialized before use? Does the "
            "UI update after state changes? Are there stale references?\n"
            "3. MENU ITEMS: Every menu option must be connected to real functionality. "
            "Remove or implement any placeholder menu items.\n"
            "4. DATA FLOW: Trace data from input to processing to output. "
            "Does every step work? Are transformations correct?\n"
            "5. STARTUP & SHUTDOWN: Does the app start cleanly? Does it save "
            "state on exit? Does it handle being closed mid-operation?\n"
            "6. INTEGRATION: If there are multiple modules, do they talk to each "
            "other correctly? Are interfaces consistent?\n\n"
            "FIX everything you find broken. Don't just note it — repair it.\n"
            "Apply ALL fixes to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "reference_images": {
        "label": "Reference Images",
        "description": "Generate ASCII/text mockups and detailed visual specifications",
        "prompt": (
            "Generate REFERENCE DOCUMENTATION for this program's visual design.\n\n"
            "Create detailed visual specifications:\n"
            "1. ASCII MOCKUPS: Draw ASCII art representations of every screen, "
            "dialog, and panel in the application. Show layout, widget placement, "
            "and proportions using box-drawing characters.\n"
            "2. COLOR SPECIFICATION: List every color used as hex values with "
            "their purpose (bg_primary, text_heading, accent_button, etc.)\n"
            "3. COMPONENT CATALOG: For each UI widget (button, input, label, "
            "list, etc.), describe: size, font, colors, padding, border, states "
            "(normal, hover, pressed, disabled).\n"
            "4. LAYOUT SPEC: Describe the grid/pack/place geometry. What anchors "
            "where? What stretches on resize? Min/max sizes.\n"
            "5. INTERACTION FLOW: Describe what happens visually when the user "
            "clicks each button, types in each field, selects each option.\n\n"
            "Include these reference specs AS COMMENTS or a docstring in the code "
            "so developers can reference the intended design.\n\n"
            "Apply these additions to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "review_grade": {
        "label": "Review & Grade",
        "description": "Senior review, grade the code, then improve based on findings",
        "prompt": (
            "Perform a SENIOR DEVELOPER CODE REVIEW with grading.\n\n"
            "STEP 1 — GRADE (be honest, not generous):\n"
            "Rate each dimension 1-10 with specific justification:\n"
            "- Architecture & Design: [score] — [why]\n"
            "- Code Quality & Readability: [score] — [why]\n"
            "- Error Handling & Robustness: [score] — [why]\n"
            "- Feature Completeness: [score] — [why]\n"
            "- UI/UX Polish: [score] — [why]\n"
            "- Performance: [score] — [why]\n"
            "- Overall: [average] / 10\n\n"
            "STEP 2 — IMPROVE:\n"
            "Now fix EVERY issue you identified in the grading. Target score 9+.\n"
            "Don't just describe what to fix — actually rewrite the code.\n"
            "Focus especially on the lowest-scoring dimensions.\n\n"
            "STEP 3 — RE-GRADE:\n"
            "Add a comment block at the top showing before/after scores.\n\n"
            "Apply ALL improvements to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
}

# Default order for focus cycling
FOCUS_ORDER = [
    "deep_dive", "extra_features", "pressure_test", "explore_expand",
    "beautiful_gui", "solid_functional", "reference_images", "review_grade",
]

# Default focuses when none are selected
DEFAULT_FOCUSES = ["extra_features", "pressure_test", "solid_functional", "review_grade"]


def engineer_improvement_prompt(
    task: str,
    iteration: int,
    ai_name: str = "",
    selected_focuses: Optional[list[str]] = None,
) -> tuple[str, str]:
    """Generate an improvement prompt for subsequent iterations.

    Cycles through the user's selected improvement focuses so each round
    makes the code meaningfully better in a different dimension.

    Returns (prompt_text, focus_label) tuple.
    """
    # Use selected focuses or fall back to defaults
    focuses = selected_focuses if selected_focuses else DEFAULT_FOCUSES
    # Filter to valid keys only
    focuses = [f for f in focuses if f in IMPROVEMENT_FOCUSES]
    if not focuses:
        focuses = DEFAULT_FOCUSES

    idx = iteration % len(focuses)
    focus_key = focuses[idx]
    focus = IMPROVEMENT_FOCUSES[focus_key]

    prompt = focus["prompt"]
    if ai_name:
        prompt = f"[You are {ai_name}] {prompt}"

    return prompt, focus["label"]


@dataclass
class BroadcastConfig:
    """Configuration for a broadcast run."""
    task: str = ""
    build_target: str = "PC Desktop App"
    enhancements: list[str] = field(default_factory=lambda: ["More Features + Robustness"])
    context: str = ""
    session_ids: list[str] = field(default_factory=list)  # Empty = all active
    endless: bool = True
    max_iterations: int = 999  # Safety cap
    time_limit_minutes: int = 0  # 0 = no limit
    mode: str = "uniform"  # "uniform" = all same, "architect" = 1+N, "pipeline" = sequential relay
    expand_on_stagnation: bool = False  # When code stops changing, switch to creating new functions
    selected_focuses: list[str] = field(default_factory=list)  # Which improvement focuses to use
    perfection_loop: bool = False  # Cycle all selected focuses, repeat until no more improvement
    attached_files: list[str] = field(default_factory=list)  # File paths to include as context


BROADCAST_STATE_FILE = "broadcast_state.json"


def _state_path() -> Path:
    """Path to the broadcast resume state file."""
    state_dir = Path.home() / ".autocoder"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / BROADCAST_STATE_FILE


class BroadcastController:
    """Broadcasts a task to multiple sessions and loops improvements.

    Usage:
        bc = BroadcastController(session_manager)
        bc.set_callbacks(on_output=..., on_status=...)
        bc.start(BroadcastConfig(task="Build a calculator"))
        # ... runs until bc.stop()
        # Later:
        bc.resume()  # picks up where it left off
    """

    def __init__(self, session_manager) -> None:
        self._sm = session_manager
        self._running = False
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active_thread_count = 0
        self._active_thread_lock = threading.Lock()
        self._iteration_counts: dict[str, int] = {}
        self._results: dict[str, str] = {}  # session_id -> latest actual result
        self._codebases: dict[str, str] = {}  # session_id -> latest extracted code
        self._config: Optional[BroadcastConfig] = None  # Current/last config

        self._on_output: Optional[Callable] = None
        self._on_status: Optional[Callable] = None
        self._on_iteration: Optional[Callable] = None
        self._on_complete: Optional[Callable] = None
        self._file_context: str = ""  # Formatted attached file contents

    @property
    def is_running(self) -> bool:
        return self._running

    def _thread_started(self) -> None:
        """Track a new broadcast thread starting."""
        with self._active_thread_lock:
            self._active_thread_count += 1

    def _thread_finished(self) -> None:
        """Track a broadcast thread finishing. Auto-reset _running when all done."""
        with self._active_thread_lock:
            self._active_thread_count -= 1
            if self._active_thread_count <= 0:
                self._active_thread_count = 0
                if self._running:
                    logger.info("All broadcast threads finished — resetting running state")
                    self._running = False
                    if self._on_complete:
                        self._on_complete(dict(self._iteration_counts))

    def set_callbacks(
        self,
        on_output: Optional[Callable] = None,
        on_status: Optional[Callable] = None,
        on_iteration: Optional[Callable] = None,
        on_complete: Optional[Callable] = None,
    ) -> None:
        self._on_output = on_output
        self._on_status = on_status
        self._on_iteration = on_iteration
        self._on_complete = on_complete

    def start(self, config: BroadcastConfig) -> None:
        """Start autocoding on a single session.

        Uses the first configured session. Sends the task, extracts code,
        then endlessly improves it with rotating focus (features, polish,
        robustness, performance, review).
        """
        if self._running:
            return

        self._stop_event.clear()
        self._running = True
        self._config = config
        self._iteration_counts.clear()
        self._codebases.clear()
        self._threads.clear()
        self._active_thread_count = 0

        # Find first configured session
        if config.session_ids:
            sessions = [
                self._sm.get_session(sid)
                for sid in config.session_ids
                if self._sm.get_session(sid)
            ]
        else:
            sessions = self._sm.active_sessions

        configured = [s for s in sessions if s and s.is_configured]
        if not configured:
            logger.warning("No configured sessions — can't start autocode")
            self._running = False
            return

        # Use just the FIRST configured session
        session = configured[0]
        logger.info("Autocoding on single session: %s (%s)",
                     session.ai_profile.name, session.corner)

        if self._on_status:
            self._on_status(
                f"Autocoding on {session.ai_profile.name} ({session.corner})"
            )

        # Read attached files (once, upfront)
        if config.attached_files:
            files = self._read_attached_files(config.attached_files)
            self._file_context = self._format_file_context(files)
            if files:
                logger.info("Loaded %d attached files (%d chars of context)",
                            len(files), len(self._file_context))
                if self._on_status:
                    self._on_status(f"Loaded {len(files)} reference files")
        else:
            self._file_context = ""

        # Engineer the initial prompt (include file context)
        file_ctx = self._file_context
        task_with_files = config.task
        if file_ctx:
            task_with_files = f"{config.task}\n\n{file_ctx}"

        engineered = engineer_prompt(
            task=task_with_files,
            build_target=config.build_target,
            enhancements=config.enhancements,
            context=config.context,
        )

        # Single session — single thread
        self._thread_started()
        t = threading.Thread(
            target=self._session_loop,
            args=(session, config, engineered),
            daemon=True,
        )
        self._threads.append(t)
        t.start()

    def stop(self) -> None:
        """Stop all broadcast loops and save state for resume."""
        self._stop_event.set()
        self._running = False

        # Stop all session executors
        for session in self._sm.sessions:
            if session.client:
                session.client.cancel()

        # Save state so we can resume later
        self._save_state()

        logger.info("Broadcast stopped (state saved for resume)")
        if self._on_complete:
            self._on_complete(dict(self._iteration_counts))

    def resume(self) -> bool:
        """Resume a previously stopped broadcast from where it left off.

        Loads the saved state (task, config, iteration counts, codebases)
        and continues the improvement loop for each session. Each session
        skips the initial build and jumps straight to the next improvement
        iteration with its saved codebase.

        Returns True if resume started, False if no state to resume.
        """
        if self._running:
            logger.warning("Broadcast already running, can't resume")
            return False

        state = self._load_state()
        if not state:
            logger.warning("No broadcast state to resume")
            return False

        # Restore config
        config_data = state.get("config", {})
        config = BroadcastConfig(
            task=config_data.get("task", ""),
            build_target=config_data.get("build_target", "PC Desktop App"),
            enhancements=config_data.get("enhancements", ["More Features + Robustness"]),
            context=config_data.get("context", ""),
            session_ids=config_data.get("session_ids", []),
            endless=config_data.get("endless", True),
            max_iterations=config_data.get("max_iterations", 999),
            time_limit_minutes=config_data.get("time_limit_minutes", 0),
            expand_on_stagnation=config_data.get("expand_on_stagnation", False),
            selected_focuses=config_data.get("selected_focuses", []),
            perfection_loop=config_data.get("perfection_loop", False),
            attached_files=config_data.get("attached_files", []),
        )

        if not config.task:
            logger.warning("Saved state has no task")
            return False

        self._config = config
        self._stop_event.clear()
        self._running = True
        self._threads.clear()

        # Reload attached files
        if config.attached_files:
            files = self._read_attached_files(config.attached_files)
            self._file_context = self._format_file_context(files)
        else:
            self._file_context = ""

        # Restore per-session state
        session_states = state.get("sessions", {})

        # Match saved corners to current active sessions
        sessions = self._sm.active_sessions if not config.session_ids else [
            self._sm.get_session(sid) for sid in config.session_ids
            if self._sm.get_session(sid)
        ]

        if not sessions:
            logger.warning("No active sessions to resume broadcast")
            self._running = False
            return False

        resumed_count = 0
        for session in sessions:
            if not session.is_configured:
                continue

            # Find saved state for this session by corner
            saved = session_states.get(session.corner, {})
            resume_iteration = saved.get("iteration", 0)
            resume_codebase = saved.get("codebase", "")

            if resume_iteration == 0 and not resume_codebase:
                # No saved state for this corner — start fresh
                logger.info("No saved state for %s, starting fresh", session.corner)
                engineered = engineer_prompt(
                    task=config.task,
                    build_target=config.build_target,
                    enhancements=config.enhancements,
                    context=config.context,
                )
                t = threading.Thread(
                    target=self._session_loop,
                    args=(session, config, engineered),
                    daemon=True,
                )
            else:
                # Resume from saved state
                logger.info(
                    "Resuming %s at iteration %d with %d chars of code",
                    session.corner, resume_iteration, len(resume_codebase)
                )
                t = threading.Thread(
                    target=self._session_loop_resume,
                    args=(session, config, resume_iteration, resume_codebase),
                    daemon=True,
                )
                resumed_count += 1

            self._thread_started()
            self._threads.append(t)
            t.start()

        if self._on_status:
            self._on_status(
                f"Resumed broadcast: {resumed_count} sessions continuing, "
                f"task: {config.task[:50]}"
            )

        return True

    def has_saved_state(self) -> bool:
        """Check if there's a saved broadcast state to resume."""
        path = _state_path()
        return path.exists() and path.stat().st_size > 10

    def get_saved_summary(self) -> Optional[str]:
        """Get a human-readable summary of saved state."""
        state = self._load_state()
        if not state:
            return None

        task = state.get("config", {}).get("task", "Unknown")
        sessions = state.get("sessions", {})
        parts = []
        for corner, data in sessions.items():
            it = data.get("iteration", 0)
            code_len = len(data.get("codebase", ""))
            parts.append(f"{corner}: iter {it} ({code_len} chars)")

        return f"Task: {task[:60]}\n" + "\n".join(parts)

    def _save_state(self) -> None:
        """Save broadcast state to disk for resume."""
        if not self._config:
            return

        state = {
            "config": {
                "task": self._config.task,
                "build_target": self._config.build_target,
                "enhancements": self._config.enhancements,
                "context": self._config.context,
                "session_ids": self._config.session_ids,
                "endless": self._config.endless,
                "max_iterations": self._config.max_iterations,
                "time_limit_minutes": self._config.time_limit_minutes,
                "expand_on_stagnation": self._config.expand_on_stagnation,
                "selected_focuses": self._config.selected_focuses,
                "perfection_loop": self._config.perfection_loop,
                "attached_files": self._config.attached_files,
            },
            "sessions": {},
            "saved_at": time.time(),
        }

        # Save per-session state keyed by corner (corners persist across restarts)
        if self._sm:
            for session in self._sm.active_sessions:
                sid = session.session_id
                state["sessions"][session.corner] = {
                    "iteration": self._iteration_counts.get(sid, 0),
                    "codebase": self._codebases.get(sid, ""),
                    "ai_name": session.ai_profile.name,
                }

        try:
            path = _state_path()
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            logger.info("Broadcast state saved: %s", path)
        except Exception as e:
            logger.error("Failed to save broadcast state: %s", e)

    def _load_state(self) -> Optional[dict]:
        """Load broadcast state from disk."""
        try:
            path = _state_path()
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "config" not in data:
                return None
            return data
        except Exception as e:
            logger.error("Failed to load broadcast state: %s", e)
            return None

    def clear_saved_state(self) -> None:
        """Delete saved state (after successful resume or manual clear)."""
        try:
            path = _state_path()
            if path.exists():
                path.unlink()
                logger.info("Cleared saved broadcast state")
        except Exception as e:
            logger.warning("Failed to clear broadcast state: %s", e)

    @staticmethod
    def _extract_code_blocks(text: str, previous_codebase: str = "") -> str:
        """Extract code from an AI response.

        Finds all fenced code blocks (```...```) and joins them.
        If no fenced blocks found, returns the raw text stripped of
        obvious conversational filler (keeps anything that looks like code).

        IMPORTANT: If `previous_codebase` is provided, we skip any code block
        whose content matches it (i.e. it was echoed from the prompt, not
        generated by the AI). This prevents the pipeline from extracting
        its own prompt input instead of the AI's output.
        """
        if not text or not text.strip():
            return ""

        # ── Step 1: Try to isolate AI response if prompt is echoed ──
        # Pyautogui captures whole page: "You said [prompt] Gemini said [response]"
        # Even with the browser_actions fix, add defense-in-depth here.
        ai_markers = ["Gemini said", "ChatGPT said", "Claude said",
                       "Model said", "Assistant said"]
        for marker in ai_markers:
            pos = text.rfind(marker)
            if pos >= 0:
                candidate = text[pos + len(marker):].strip()
                if len(candidate) > 50:
                    text = candidate
                    break

        # ── Step 2: Find all fenced code blocks ─────────────────────
        blocks = re.findall(r'```(?:\w*)\n?(.*?)```', text, re.DOTALL)
        if blocks:
            # Filter out blocks that match the previous codebase (prompt echo)
            if previous_codebase and len(previous_codebase) > 100:
                prev_normalized = previous_codebase.strip()[:500]
                filtered = []
                for block in blocks:
                    block_stripped = block.strip()
                    if not block_stripped:
                        continue
                    # Skip if this block's start matches the previous codebase
                    if block_stripped[:500].strip() == prev_normalized:
                        logger.debug("Skipping echoed codebase block (%d chars)", len(block_stripped))
                        continue
                    filtered.append(block_stripped)
                if filtered:
                    blocks = filtered

            extracted = "\n\n".join(block.strip() for block in blocks if block.strip())
            if extracted:
                return extracted

        # No fenced blocks — the AI returned raw code or unfenced text.
        # Heuristic: if it has common code indicators, keep it as-is.
        code_indicators = ['def ', 'class ', 'import ', 'from ', 'function ',
                           'const ', 'let ', 'var ', '#include', 'package ']
        for indicator in code_indicators:
            if indicator in text:
                return text.strip()

        # Last resort: only return raw text if it actually looks like code
        if BroadcastController._is_likely_code(text):
            return text.strip()

        # Text doesn't look like code — return empty to signal extraction failure
        logger.debug("_extract_code_blocks: raw text rejected (not code-like, %d chars)", len(text))
        return ""

    @staticmethod
    def _read_attached_files(file_paths: list[str]) -> list[dict[str, str]]:
        """Read attached files and return list of {path, name, ext, content}.

        Handles .py, .txt, .json, .c, .h, .md, .js, .ts, .html, .css,
        .yaml, .toml, .cfg, .ini, .rs, .go, .java — any text-based code file.
        Skips files >500KB or binary files gracefully.
        """
        MAX_SIZE = 500_000  # 500KB per file
        results = []
        for fp in file_paths:
            p = Path(fp)
            if not p.exists():
                logger.warning("Attached file not found: %s", fp)
                continue
            if p.stat().st_size > MAX_SIZE:
                logger.warning("Attached file too large (%d KB), skipping: %s",
                               p.stat().st_size // 1024, fp)
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                results.append({
                    "path": str(p),
                    "name": p.name,
                    "ext": p.suffix.lstrip("."),
                    "content": content,
                })
            except Exception as e:
                logger.warning("Failed to read attached file %s: %s", fp, e)
        return results

    @staticmethod
    def _format_file_context(files: list[dict[str, str]]) -> str:
        """Format attached file contents for inclusion in prompts.

        Returns a block like:
        REFERENCE FILES:
        === filename.py ===
        <content>
        === config.json ===
        <content>
        """
        if not files:
            return ""

        sections = []
        for f in files:
            lang = f["ext"] or "text"
            sections.append(
                f"=== {f['name']} ===\n"
                f"```{lang}\n{f['content']}\n```"
            )

        return "REFERENCE FILES (use as context, follow existing patterns):\n\n" + "\n\n".join(sections)

    def _build_context_prompt(self, directive: str, codebase: str,
                              file_context: str = "") -> str:
        """Build an improvement prompt that includes the current codebase.

        This is the key fix for context amnesia — every iteration sees
        the actual code from the previous iteration, not just a vague
        instruction to "improve the code you wrote."
        """
        parts = [directive]

        if file_context:
            parts.append(file_context)

        if codebase:
            parts.append(f"CURRENT CODEBASE:\n```\n{codebase}\n```")

        return "\n\n".join(parts)

    @staticmethod
    def _code_hash(code: str) -> str:
        """Quick hash of code content for stagnation detection."""
        normalized = re.sub(r'\s+', ' ', code.strip())
        return hashlib.md5(normalized.encode()).hexdigest()

    @staticmethod
    def _is_stagnant(current: str, previous: str, threshold: float = 0.02) -> bool:
        """Detect if code stopped meaningfully changing between iterations.

        Returns True if the code is essentially the same — either identical
        hash or less than `threshold` (2%) change in size with same hash prefix.
        """
        if not current or not previous:
            return False

        cur_hash = BroadcastController._code_hash(current)
        prev_hash = BroadcastController._code_hash(previous)

        if cur_hash == prev_hash:
            return True

        # Even if hash differs, if size changed <2% the AI is just
        # shuffling whitespace or comments around
        size_ratio = abs(len(current) - len(previous)) / max(len(previous), 1)
        if size_ratio < threshold:
            # Double-check: compare first 2000 chars stripped
            if current.strip()[:2000] == previous.strip()[:2000]:
                return True

        return False

    @staticmethod
    def _is_likely_code(text: str, threshold: float = 0.3) -> bool:
        """Check whether text is likely code vs conversational chat.

        Returns True if at least `threshold` (30%) of non-blank lines
        contain code indicators. Catches cases where Gemini stops
        returning code and starts returning suggestions/chat.
        """
        if not text or len(text.strip()) < 20:
            return False

        lines = text.strip().splitlines()
        non_blank = [line for line in lines if line.strip()]
        if not non_blank:
            return False

        # Structural code indicators (language-agnostic)
        code_re = re.compile(
            r'(?:'
            r'[{}\[\]();]'
            r'|^\s*(?:def |class |import |from |function |const |let |var '
            r'|return |if |elif |else:|for |while |try:|except |catch '
            r'|#include|package |pub |fn |impl |match |async |await '
            r'|export |module |require\(|print\(|console\.)'
            r'|=\s*["\'\d\[\{(]'
            r'|\w+\(.*\)'
            r')'
        )

        # Chat/prose indicators
        chat_re = re.compile(
            r'(?:'
            r'^(?:I |You |We |They |It |This |That |Here |Sure|Of course|Let me|'
            r'However|Additionally|Furthermore|In summary|To summarize|'
            r'I suggest|I recommend|I hope|Feel free|Happy to|'
            r'Would you|Could you|Should you|Do you|'
            r'Great|Excellent|Certainly|Absolutely)'
            r'|(?:improve|suggest|recommend|consider|alternatively)[\s,.]'
            r'|\btips?\b|\bsteps?\b|\bapproach\b'
            r')',
            re.IGNORECASE
        )

        code_lines = sum(1 for line in non_blank if code_re.search(line))
        chat_lines = sum(1 for line in non_blank if chat_re.search(line))

        code_ratio = code_lines / len(non_blank)
        chat_ratio = chat_lines / len(non_blank)

        # High chat + low code = not code
        if chat_ratio > 0.4 and code_ratio < threshold:
            return False

        return code_ratio >= threshold

    def _build_self_recovery_prompt(self, task: str, codebase: str, problem: str) -> str:
        """Ask the AI to diagnose redundancy/stagnation and self-correct.

        Instead of blindly retrying, gives Gemini the codebase and asks it
        to identify what's redundant, what's missing, and produce a fresh
        improved version with a concrete plan.
        """
        parts = [
            "RECOVERY MODE — Your previous responses stopped producing useful code.\n\n"
            f"PROBLEM DETECTED: {problem}\n\n"
            "STEP 1 — DIAGNOSE:\n"
            "Analyze the codebase below. Identify:\n"
            "- Redundant/duplicate code that adds no value\n"
            "- Functions that exist but don't work or aren't connected\n"
            "- Missing critical functionality for the original task\n"
            "- Code that's overengineered vs what's actually needed\n\n"
            "STEP 2 — PLAN:\n"
            "List 3-5 specific, concrete improvements (not vague suggestions).\n"
            "Each must name the exact function/class and what changes.\n\n"
            "STEP 3 — EXECUTE:\n"
            "Apply ALL your planned improvements. Output the ENTIRE updated codebase.\n"
            "Remove redundant code. Fix broken connections. Add what's missing.\n"
            "No placeholders, no stubs, no explanations outside code comments.\n\n"
            f"ORIGINAL TASK: {task}",
        ]

        # Include reference files if attached
        if self._file_context:
            parts.append(self._file_context)

        parts.append(f"CURRENT CODEBASE:\n```\n{codebase}\n```")

        return "\n\n".join(parts)

    def _attempt_self_recovery(
        self, session, config: BroadcastConfig, codebase: str, problem: str,
        sid: str, ai_name: str,
    ) -> str:
        """Reset conversation and ask AI to self-diagnose and recover.

        Returns recovered code if successful, empty string if recovery failed.
        """
        if self._on_output:
            self._on_output(
                sid, "system",
                f"\n{'='*50}\n"
                f"[{ai_name}] RECOVERY MODE: {problem}\n"
                f"Resetting conversation — asking AI to diagnose and fix...\n"
                f"{'='*50}\n"
            )

        # Reset conversation
        try:
            session.client.new_conversation()
            time.sleep(2)
        except Exception as e:
            logger.warning("[%s] new_conversation() failed during recovery: %s", ai_name, e)
            return ""

        # Send self-recovery prompt
        recovery_prompt = self._build_self_recovery_prompt(config.task, codebase, problem)

        try:
            result = session.client.generate(
                prompt=recovery_prompt,
                on_progress=lambda t, s=sid: (
                    self._on_output(s, "code", t) if self._on_output else None
                ),
            )
        except Exception as e:
            logger.warning("[%s] Recovery generate failed: %s", ai_name, e)
            return ""

        # Extract code from recovery response
        recovered = self._extract_code_blocks(result, previous_codebase=codebase)
        if recovered:
            logger.info("[%s] Recovery succeeded: got %d chars of code", ai_name, len(recovered))
            if self._on_output:
                self._on_output(
                    sid, "system",
                    f"[{ai_name}] Recovery successful — {len(recovered)} chars of improved code\n"
                )
        else:
            logger.warning("[%s] Recovery failed — AI still not returning code", ai_name)
            if self._on_output:
                self._on_output(
                    sid, "system",
                    f"[{ai_name}] Recovery failed — AI could not produce code\n"
                )

        return recovered

    def _build_expansion_prompt(self, task: str, codebase: str, expansion_round: int) -> str:
        """Build a prompt that asks the AI to create NEW functions/modules.

        Instead of improving the same code, this tells the AI to write
        companion functionality — new features, utilities, plugins, or
        modules that EXTEND the existing codebase.
        """
        expansion_focuses = [
            {
                "focus": "Utility Functions",
                "prompt": (
                    "The codebase below has reached a mature state. "
                    "DO NOT rewrite or re-output the existing code.\n\n"
                    "Instead, write NEW UTILITY FUNCTIONS that complement it:\n"
                    "- Helper functions the main code could call\n"
                    "- Data validation/transformation utilities\n"
                    "- File I/O helpers, formatters, parsers\n"
                    "- Logging/debugging utilities\n\n"
                    "Write 3-5 complete, production-ready utility functions.\n"
                    "Each function should have docstrings, type hints, and error handling.\n"
                    "Output ONLY the new code — do NOT repeat the existing codebase."
                ),
            },
            {
                "focus": "Plugin/Extension Module",
                "prompt": (
                    "The codebase below has reached a mature state. "
                    "DO NOT rewrite or re-output the existing code.\n\n"
                    "Instead, write a NEW PLUGIN or EXTENSION MODULE for it:\n"
                    "- A plugin that adds a completely new capability\n"
                    "- New classes that extend the existing ones\n"
                    "- An integration layer (API, CLI wrapper, config system)\n"
                    "- A testing/monitoring companion module\n\n"
                    "Write a complete, importable module (200+ lines).\n"
                    "It should integrate with the existing codebase's classes/functions.\n"
                    "Output ONLY the new module — do NOT repeat the existing codebase."
                ),
            },
            {
                "focus": "Test Suite",
                "prompt": (
                    "The codebase below has reached a mature state. "
                    "DO NOT rewrite or re-output the existing code.\n\n"
                    "Instead, write a COMPREHENSIVE TEST SUITE for it:\n"
                    "- Unit tests for every public function/method\n"
                    "- Edge case tests (empty input, large input, invalid types)\n"
                    "- Integration tests for key workflows\n"
                    "- Use pytest with fixtures and parametrize\n\n"
                    "Write a complete test file with 15+ test functions.\n"
                    "Output ONLY the test code — do NOT repeat the existing codebase."
                ),
            },
            {
                "focus": "CLI & Configuration",
                "prompt": (
                    "The codebase below has reached a mature state. "
                    "DO NOT rewrite or re-output the existing code.\n\n"
                    "Instead, write NEW companion modules:\n"
                    "- A CLI interface (argparse/click) that wraps the main functionality\n"
                    "- A configuration system (YAML/JSON config file loader)\n"
                    "- A setup.py or pyproject.toml for packaging\n"
                    "- A logging configuration module\n\n"
                    "Write complete, production-ready code for each.\n"
                    "Output ONLY the new code — do NOT repeat the existing codebase."
                ),
            },
            {
                "focus": "Advanced Features",
                "prompt": (
                    "The codebase below has reached a mature state. "
                    "DO NOT rewrite or re-output the existing code.\n\n"
                    "Instead, write NEW ADVANCED FEATURES as separate functions/classes:\n"
                    "- Async/concurrent version of key operations\n"
                    "- Caching layer or memoization decorators\n"
                    "- Event system or observer pattern hooks\n"
                    "- Data export (CSV, JSON, HTML report generation)\n\n"
                    "Write 3-5 substantial new components (each 50+ lines).\n"
                    "Output ONLY the new code — do NOT repeat the existing codebase."
                ),
            },
        ]

        idx = expansion_round % len(expansion_focuses)
        focus_entry = expansion_focuses[idx]

        prompt = (
            f"{focus_entry['prompt']}\n\n"
            f"EXISTING CODEBASE (for reference — DO NOT re-output this):\n"
            f"```\n{codebase}\n```"
        )

        return prompt

    @staticmethod
    def _expansion_focus_name(expansion_round: int) -> str:
        """Get the human-readable name for an expansion round."""
        names = [
            "New Utility Functions",
            "Plugin/Extension Module",
            "Test Suite",
            "CLI & Configuration",
            "Advanced Features",
        ]
        return names[expansion_round % len(names)]

    @staticmethod
    def _make_feature_name(task: str) -> str:
        """Extract a short descriptive feature name from the task.

        Handles both simple tasks ("Build a calculator") and full
        engineered prompts with markdown headers, role sections, etc.
        Produces a clean, readable filename slug like "calculator_history".
        """
        if not task or not task.strip():
            return "task"

        text = task.strip()

        # ── Step 1: Extract the core task from structured prompts ────
        # Try known section headers that contain the actual task
        task_patterns = [
            r'##?\s*(?:TASK|PROJECT|OBJECTIVE|GOAL|BUILD)\s*[:\-]\s*(.+)',
            r'(?:^|\n)\s*(?:TASK|BUILD|CREATE|PROJECT)\s*[:\-]\s*(.+)',
            r'(?:^|\n)\s*Build\s+(?:a\s+)?(.+?)(?:\n|$)',
        ]
        for pattern in task_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                candidate = match.group(1).strip()
                # Only use if it's a reasonable length (not another header)
                if 3 < len(candidate) < 200 and '#' not in candidate[:5]:
                    text = candidate
                    break

        # ── Step 2: If still looks like a mega-prompt, take first real sentence ─
        if len(text) > 300 or text.startswith('#'):
            # Skip markdown headers and find first real content line
            for line in text.split('\n'):
                line = line.strip()
                # Skip headers, empty lines, all-caps section labels
                if not line or line.startswith('#') or line.startswith('---'):
                    continue
                if line.isupper() and len(line) < 60:
                    continue
                if re.match(r'^[A-Z\s_\-:]+$', line) and len(line) < 60:
                    continue
                # Found a real content line
                text = line
                break

        # ── Step 3: Clean down to a short slug ───────────────────────
        text = text.lower().strip()

        # Strip leading action verbs
        for prefix in ['build a ', 'create a ', 'make a ', 'write a ',
                        'implement a ', 'add a ', 'build ', 'create ',
                        'make ', 'write ', 'implement ', 'add ']:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break

        filler = {
            'a', 'an', 'the', 'that', 'this', 'is', 'are', 'was', 'were',
            'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
            'will', 'would', 'could', 'should', 'may', 'might', 'shall',
            'can', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
            'from', 'it', 'its', 'and', 'or', 'but', 'not', 'so', 'if',
            'my', 'your', 'our', 'me', 'i', 'we', 'you', 'they', 'them',
            'please', 'lets', "let's", 'want', 'need', 'whats', 'using',
            'follow', 'without', 'asking', 'below', 'above', 'here',
            'project', 'context', 'conventions', 'instructions',
        }

        words = re.sub(r'[^a-z0-9\s]', '', text).split()
        kept = [w for w in words if w not in filler and len(w) > 1]

        if not kept:
            kept = text.split()[:4]

        name = '_'.join(kept[:5])
        return name[:50] if name else 'task'

    @staticmethod
    def _detect_language_ext(code: str) -> str:
        """Detect the programming language from code content and return extension."""
        if not code:
            return ".txt"
        # Check first few lines for strong indicators
        head = code[:2000]
        if 'import customtkinter' in head or 'import tkinter' in head:
            return ".py"
        if 'def ' in head and ('import ' in head or 'from ' in head):
            return ".py"
        if 'class ' in head and 'self' in head:
            return ".py"
        if '#!/usr/bin/env python' in head or '#!/usr/bin/python' in head:
            return ".py"
        if 'function ' in head and ('{' in head or '=>' in head):
            return ".js"
        if 'const ' in head and 'require(' in head:
            return ".js"
        if 'import React' in head or 'export default' in head:
            return ".jsx"
        if '#include' in head:
            return ".c"
        if 'package main' in head:
            return ".go"
        if 'fn main' in head or 'impl ' in head:
            return ".rs"
        if '<html' in head.lower() or '<!doctype' in head.lower():
            return ".html"
        if 'public class' in head or 'public static void main' in head:
            return ".java"
        # Default to .py since most tasks produce Python
        if 'print(' in head or 'if __name__' in head:
            return ".py"
        return ".txt"

    def _save_result(self, session, task: str, iteration: int,
                     focus: str, result: str, start_time: float,
                     extracted_code: str = "") -> None:
        """Save an iteration's output to Downloads as a .txt file."""
        try:
            feature = self._make_feature_name(task)
            ai_name = session.ai_profile.name
            corner = session.corner
            elapsed = time.time() - start_time

            code = extracted_code or self._codebases.get(session.session_id, "")
            title = f"{feature}_v{iteration}_{focus.replace(' ', '_')}"
            path = save_task_output(
                title=title,
                output=code if code else result,
                ai_name=ai_name,
                corner=corner,
                elapsed_seconds=elapsed,
                iterations=iteration,
            )
            if path and self._on_status:
                self._on_status(f"Saved: {path.name}")
        except Exception as e:
            logger.warning("Auto-save failed for %s: %s", session.display_name, e)

    # ── Architect mode helpers ─────────────────────────────────────────

    def _build_architect_prompt(self, task: str, num_workers: int) -> str:
        """Build the prompt for the architect session.

        The architect designs the overall architecture and breaks the task
        into independent modules that workers can implement in parallel.
        """
        return (
            f"You are the LEAD SOFTWARE ARCHITECT. Your job is to design the "
            f"architecture and direct {num_workers} developer AIs.\n\n"
            f"PROJECT TASK: {task}\n\n"
            f"Your deliverables:\n"
            f"1. **Architecture Overview** — High-level design, key patterns, data flow\n"
            f"2. **Module Breakdown** — Split the project into exactly {num_workers} independent modules\n"
            f"3. For EACH module, provide:\n"
            f"   - Module name and purpose\n"
            f"   - Public API (function signatures, classes, interfaces)\n"
            f"   - Data structures it owns\n"
            f"   - Integration points with other modules\n"
            f"   - Implementation notes and constraints\n\n"
            f"Format your response with clear headers:\n"
            f"## Architecture Overview\n"
            f"## Module 1: [Name]\n"
            f"## Module 2: [Name]\n"
            f"## Module 3: [Name]\n"
            f"(etc.)\n\n"
            f"Be specific and detailed enough that each developer can implement "
            f"their module independently without asking questions."
        )

    def _build_architect_review_prompt(
        self, task: str, worker_codebases: dict[str, str], iteration: int
    ) -> str:
        """Build a review prompt for the architect after workers produce code.

        The architect sees ALL worker outputs and provides integration feedback,
        identifies issues, and designs the next round of improvements.
        """
        review_type = [
            "integration review — check that modules connect properly",
            "quality review — identify bugs, missing error handling, edge cases",
            "feature review — what's missing? what should be enhanced?",
            "performance review — bottlenecks, efficiency, optimization opportunities",
            "final integration — ensure everything works as a complete system",
        ][iteration % 5]

        sections = []
        for i, (name, code) in enumerate(worker_codebases.items(), 1):
            sections.append(
                f"### Worker {i} ({name}) — {len(code)} chars:\n```\n{code}\n```"
            )
        all_code = "\n\n".join(sections)

        return (
            f"You are the LEAD ARCHITECT doing a {review_type}.\n\n"
            f"PROJECT: {task}\n\n"
            f"Below is the current code from all {len(worker_codebases)} workers.\n"
            f"Review it and provide:\n"
            f"1. **Issues Found** — bugs, integration problems, missing pieces\n"
            f"2. **Improvement Directives** — specific instructions for each worker\n"
            f"   Format as: WORKER 1: [instructions], WORKER 2: [instructions], etc.\n"
            f"3. **Integration Code** — any glue/main entry point code needed\n\n"
            f"Be SPECIFIC. Don't say 'improve error handling' — say exactly WHAT "
            f"to handle and HOW.\n\n"
            f"CURRENT CODE FROM ALL WORKERS:\n{all_code}"
        )

    def _build_worker_prompt(
        self, task: str, architect_spec: str, worker_index: int, total_workers: int
    ) -> str:
        """Build the initial prompt for a worker session.

        Includes the architect's spec and tells the worker which module to implement.
        """
        return (
            f"You are Developer {worker_index + 1} of {total_workers}. "
            f"Implement the module assigned to you by the Lead Architect below.\n\n"
            f"PROJECT: {task}\n\n"
            f"ARCHITECT'S SPECIFICATION:\n{architect_spec}\n\n"
            f"YOUR ASSIGNMENT: Implement **Module {worker_index + 1}** as described above.\n"
            f"Write COMPLETE, PRODUCTION-READY code. No placeholders, no stubs.\n"
            f"Include all imports, classes, functions, and a usage example.\n"
            f"Output ONLY code — no explanations."
        )

    def _build_worker_improvement_prompt(
        self, architect_feedback: str, worker_index: int, codebase: str
    ) -> str:
        """Build an improvement prompt for a worker based on architect feedback.

        Extracts the worker-specific directives from the architect's review
        and combines them with the worker's current code.
        """
        # Try to extract worker-specific feedback
        worker_specific = ""
        patterns = [
            rf'WORKER\s*{worker_index + 1}\s*:\s*(.*?)(?=WORKER\s*\d|$)',
            rf'Worker\s*{worker_index + 1}\s*:\s*(.*?)(?=Worker\s*\d|$)',
            rf'Module\s*{worker_index + 1}\s*:\s*(.*?)(?=Module\s*\d|$)',
        ]
        for pattern in patterns:
            match = re.search(pattern, architect_feedback, re.DOTALL | re.IGNORECASE)
            if match:
                worker_specific = match.group(1).strip()
                break

        if not worker_specific:
            # Couldn't parse specific feedback — send full review
            worker_specific = architect_feedback

        return (
            f"The Lead Architect reviewed your code and has these directives:\n\n"
            f"{worker_specific}\n\n"
            f"Apply ALL the architect's feedback to your current code below.\n"
            f"Output the ENTIRE updated module. No placeholders.\n\n"
            f"YOUR CURRENT CODE:\n```\n{codebase}\n```"
        )

    def _extract_module_sections(self, architect_response: str, num_modules: int) -> list[str]:
        """Split the architect's response into per-module specs.

        Looks for ## Module N headers and splits on them. Falls back to
        splitting the response into roughly equal parts if no headers found.
        """
        sections = []

        # Try to find "## Module N" headers
        pattern = r'##\s*Module\s*(\d+)[^\n]*\n(.*?)(?=##\s*Module\s*\d|$)'
        matches = re.findall(pattern, architect_response, re.DOTALL | re.IGNORECASE)

        if len(matches) >= num_modules:
            for _, content in matches[:num_modules]:
                sections.append(content.strip())
            return sections

        # Fallback: try splitting on "## " headers
        parts = re.split(r'\n##\s+', architect_response)
        if len(parts) > num_modules:
            # Skip the first part (architecture overview) and take worker parts
            sections = [p.strip() for p in parts[1:num_modules + 1]]
            return sections

        # Last resort: send full spec to all workers (they'll focus on their module #)
        return [architect_response] * num_modules

    def _architect_mode(self, config: BroadcastConfig) -> None:
        """Run architect mode: 1 architect + N workers.

        Flow:
        1. Architect designs architecture and module specs
        2. Workers each implement their assigned module
        3. Architect reviews all worker output
        4. Workers improve based on architect feedback
        5. Repeat steps 3-4 until stopped
        """
        sessions = self._sm.active_sessions if not config.session_ids else [
            self._sm.get_session(sid) for sid in config.session_ids
            if self._sm.get_session(sid)
        ]
        configured = [s for s in sessions if s.is_configured]

        if len(configured) < 2:
            logger.error("Architect mode needs at least 2 sessions (1 architect + 1 worker)")
            if self._on_status:
                self._on_status("Need at least 2 sessions for architect mode")
            self._running = False
            return

        architect = configured[0]
        workers = configured[1:]
        num_workers = len(workers)

        arch_sid = architect.session_id
        arch_name = architect.ai_profile.name
        start_time = time.time()
        self._iteration_counts[arch_sid] = 0

        if self._on_status:
            self._on_status(
                f"Architect mode: {arch_name} (architect) + "
                f"{num_workers} workers"
            )
        if self._on_output:
            self._on_output(
                arch_sid, "system",
                f"[ARCHITECT: {arch_name}] Designing architecture for: {config.task}\n"
                f"Workers: {', '.join(w.ai_profile.name + ' (' + w.corner + ')' for w in workers)}\n"
                f"{'='*60}\n"
            )

        try:
            # ── Phase 1: Architect designs the architecture ──────────
            arch_prompt = self._build_architect_prompt(config.task, num_workers)
            arch_response = architect.client.generate(
                prompt=arch_prompt,
                system_instruction=(
                    "You are a senior software architect. Design clear, detailed "
                    "module specifications that developers can implement independently."
                ),
                on_progress=lambda t, s=arch_sid: (
                    self._on_output(s, "code", t) if self._on_output else None
                ),
            )

            if self._stop_event.is_set():
                return

            self._codebases[arch_sid] = arch_response
            self._results[arch_sid] = arch_response
            self._iteration_counts[arch_sid] = 1
            self._save_result(
                architect, config.task, 1, "Architecture_Design",
                arch_response, start_time
            )
            if self._on_iteration:
                self._on_iteration(arch_sid, 1, "Architecture Design", arch_name)

            logger.info("[ARCHITECT] Design complete: %d chars", len(arch_response))

            # ── Phase 2: Workers implement their modules in parallel ──
            module_specs = self._extract_module_sections(arch_response, num_workers)
            worker_threads = []
            worker_codebases: dict[str, str] = {}  # corner -> code
            worker_lock = threading.Lock()

            def worker_build(worker_session, spec, idx):
                wsid = worker_session.session_id
                wname = worker_session.ai_profile.name
                self._iteration_counts[wsid] = 0

                if self._on_output:
                    self._on_output(
                        wsid, "system",
                        f"[WORKER {idx + 1}: {wname}] Building Module {idx + 1}\n"
                        f"{'-'*40}\n"
                    )

                prompt = self._build_worker_prompt(
                    config.task, spec, idx, num_workers
                )

                try:
                    result = worker_session.client.generate(
                        prompt=prompt,
                        system_instruction=(
                            f"You are Developer {idx + 1}. Write complete, production-ready "
                            f"code for your assigned module. No placeholders."
                        ),
                        on_progress=lambda t, s=wsid: (
                            self._on_output(s, "code", t) if self._on_output else None
                        ),
                    )

                    extracted = self._extract_code_blocks(result)
                    code = extracted if extracted else result
                    self._codebases[wsid] = code
                    self._results[wsid] = result
                    self._iteration_counts[wsid] = 1

                    with worker_lock:
                        worker_codebases[worker_session.corner] = code

                    self._save_result(
                        worker_session, config.task, 1,
                        f"Module_{idx + 1}_Initial", result, start_time,
                        extracted_code=code,
                    )
                    if self._on_iteration:
                        self._on_iteration(wsid, 1, f"Module {idx + 1} Build", wname)

                    logger.info("[WORKER %d: %s] Built %d chars", idx + 1, wname, len(code))
                except Exception as e:
                    logger.error("[WORKER %d: %s] Build failed: %s", idx + 1, wname, e)
                    with worker_lock:
                        worker_codebases[worker_session.corner] = f"# Build failed: {e}"

            # Launch all workers in parallel
            for i, worker in enumerate(workers):
                spec = module_specs[i] if i < len(module_specs) else arch_response
                wt = threading.Thread(
                    target=worker_build, args=(worker, spec, i), daemon=True
                )
                worker_threads.append(wt)
                wt.start()

            # Wait for all workers to finish
            for wt in worker_threads:
                wt.join()

            if self._stop_event.is_set():
                return

            # ── Phase 3+: Architect review → Worker improvement loop ──
            iteration = 1
            while not self._stop_event.is_set():
                if iteration >= config.max_iterations:
                    break

                # Architect reviews all worker code
                if self._on_output:
                    self._on_output(
                        arch_sid, "system",
                        f"\n[ARCHITECT] Review round {iteration} — "
                        f"reviewing {len(worker_codebases)} worker outputs\n"
                        f"{'='*60}\n"
                    )

                review_prompt = self._build_architect_review_prompt(
                    config.task, worker_codebases, iteration
                )

                try:
                    review_response = architect.client.generate(
                        prompt=review_prompt,
                        on_progress=lambda t, s=arch_sid: (
                            self._on_output(s, "code", t) if self._on_output else None
                        ),
                    )
                except Exception as e:
                    logger.error("[ARCHITECT] Review failed: %s", e)
                    time.sleep(5)
                    continue

                self._codebases[arch_sid] = review_response
                self._results[arch_sid] = review_response
                iteration_num = iteration + 1
                self._iteration_counts[arch_sid] = iteration_num
                self._save_result(
                    architect, config.task, iteration_num,
                    f"Review_Round_{iteration}", review_response, start_time
                )
                if self._on_iteration:
                    self._on_iteration(
                        arch_sid, iteration_num,
                        f"Review Round {iteration}", arch_name
                    )

                if self._stop_event.is_set():
                    return

                # Workers improve based on architect feedback (in parallel)
                worker_threads = []

                def worker_improve(worker_session, idx, feedback):
                    wsid = worker_session.session_id
                    wname = worker_session.ai_profile.name
                    current_code = self._codebases.get(wsid, "")

                    if self._on_output:
                        self._on_output(
                            wsid, "system",
                            f"\n[WORKER {idx + 1}: {wname}] Applying architect feedback\n"
                            f"{'-'*40}\n"
                        )

                    improve_prompt = self._build_worker_improvement_prompt(
                        feedback, idx, current_code
                    )

                    try:
                        result = worker_session.client.generate(
                            prompt=improve_prompt,
                            on_progress=lambda t, s=wsid: (
                                self._on_output(s, "code", t)
                                if self._on_output else None
                            ),
                        )

                        extracted = self._extract_code_blocks(result, previous_codebase=current_code)
                        code = extracted if extracted else current_code
                        self._codebases[wsid] = code
                        self._results[wsid] = result

                        w_iter = self._iteration_counts.get(wsid, 1) + 1
                        self._iteration_counts[wsid] = w_iter

                        with worker_lock:
                            worker_codebases[worker_session.corner] = code

                        self._save_result(
                            worker_session, config.task, w_iter,
                            f"Module_{idx + 1}_Rev{iteration}", result, start_time,
                            extracted_code=code,
                        )
                        if self._on_iteration:
                            self._on_iteration(
                                wsid, w_iter,
                                f"Module {idx + 1} Revision {iteration}", wname
                            )

                        logger.info(
                            "[WORKER %d: %s] Revision %d: %d chars",
                            idx + 1, wname, iteration, len(code)
                        )
                    except Exception as e:
                        logger.error(
                            "[WORKER %d: %s] Improvement failed: %s",
                            idx + 1, wname, e
                        )

                for i, worker in enumerate(workers):
                    wt = threading.Thread(
                        target=worker_improve,
                        args=(worker, i, review_response),
                        daemon=True,
                    )
                    worker_threads.append(wt)
                    wt.start()

                for wt in worker_threads:
                    wt.join()

                iteration += 1

        except InterruptedError:
            logger.info("[ARCHITECT] Mode cancelled")
        except Exception as e:
            logger.error("[ARCHITECT] Fatal error: %s", e)
            if self._on_output:
                self._on_output(arch_sid, "error", f"[ARCHITECT] Fatal: {e}\n")
        finally:
            elapsed = time.time() - start_time
            logger.info("[ARCHITECT] Mode ended after %.0fs", elapsed)
            self._thread_finished()

    # ── Pipeline mode ─────────────────────────────────────────────

    def _pipeline_mode(self, config: BroadcastConfig) -> None:
        """Run pipeline mode: sessions take turns improving the same codebase.

        Flow:
        1. Leader (first session, ideally CDP) builds initial code
        2. Code passes to session 2 for improvement
        3. Code passes to session 3 for improvement
        4. Code passes to session 4 for improvement
        5. Leader reviews and refines
        6. Repeat from step 2

        Only ONE session is active at a time — no mouse contention,
        no WebSocket conflicts, no Traffic Controller needed.
        The code relay ensures each AI builds on the previous AI's work.
        """
        sessions = self._sm.active_sessions if not config.session_ids else [
            self._sm.get_session(sid) for sid in config.session_ids
            if self._sm.get_session(sid)
        ]
        configured = [s for s in sessions if s.is_configured]

        if not configured:
            logger.error("Pipeline mode needs at least 1 configured session")
            self._running = False
            return

        # Put CDP sessions first (they're faster and more reliable)
        cdp_first = sorted(
            configured,
            key=lambda s: 0 if (s.client and s.client.using_cdp) else 1
        )

        # ── Deduplicate by hwnd ─────────────────────────────────
        # Pyautogui sessions sharing the same hwnd will read identical
        # content (Ctrl+A captures the same page). Keep only ONE session
        # per unique hwnd/CDP connection to avoid stagnant relays.
        seen_hwnds: set[int] = set()
        unique_sessions = []
        for s in cdp_first:
            if s.client and s.client.using_cdp:
                # CDP sessions are always unique (different WebSocket)
                unique_sessions.append(s)
            elif s.client and hasattr(s.client, '_hwnd') and s.client._hwnd:
                hwnd = s.client._hwnd
                if hwnd not in seen_hwnds:
                    seen_hwnds.add(hwnd)
                    unique_sessions.append(s)
                else:
                    logger.info(
                        "[PIPELINE] Skipping %s — shares hwnd %d with another session",
                        s.corner, hwnd
                    )
            else:
                unique_sessions.append(s)

        if len(unique_sessions) < len(cdp_first):
            logger.info(
                "[PIPELINE] Deduplicated %d → %d sessions (shared hwnds removed)",
                len(cdp_first), len(unique_sessions)
            )

        leader = unique_sessions[0]
        all_sessions = unique_sessions

        leader_sid = leader.session_id
        leader_name = leader.ai_profile.name
        start_time = time.time()

        if self._on_status:
            self._on_status(
                f"Pipeline mode: {len(all_sessions)} sessions in relay. "
                f"Leader: {leader_name} ({leader.corner})"
            )

        try:
            # ── Phase 1: Leader builds initial code ──────────────
            if self._stop_event.is_set():
                return

            initial_prompt = engineer_prompt(
                task=config.task,
                build_target=config.build_target,
                enhancements=config.enhancements,
                context=config.context,
            )

            if self._on_output:
                self._on_output(
                    leader_sid, "system",
                    f"[PIPELINE] Leader ({leader_name}) building initial code...\n"
                    f"{'='*60}\n"
                )

            result = leader.client.generate(
                prompt=initial_prompt,
                system_instruction=(
                    "You are an expert coding assistant. Write clean, complete, "
                    "production-ready code. No placeholders."
                ),
                on_progress=lambda t, s=leader_sid: (
                    self._on_output(s, "code", t) if self._on_output else None
                ),
            )

            if self._stop_event.is_set():
                return

            current_codebase = self._extract_code_blocks(result)
            if not current_codebase:
                current_codebase = result

            self._codebases[leader_sid] = current_codebase
            self._results[leader_sid] = result
            self._iteration_counts[leader_sid] = 1
            self._save_result(
                leader, config.task, 1, "Initial_Build", result, start_time,
                extracted_code=current_codebase,
            )
            if self._on_iteration:
                self._on_iteration(leader_sid, 1, "Initial Build", leader_name)

            logger.info("[PIPELINE] Leader built %d chars", len(current_codebase))

            # ── Phase 2+: Relay loop ─────────────────────────────
            round_num = 1
            pipeline_focuses = [
                ("Add Features", "ADD 2-3 useful features. Config, logging, validation."),
                ("Polish & Structure", "Clean up structure, naming, type hints, docstrings."),
                ("Harden", "Error handling, edge cases, input validation, retry logic."),
                ("Optimize", "Fix bottlenecks, efficient data structures, caching."),
                ("Review & Expand", "Senior review. Fix issues. Add one more capability."),
            ]

            while not self._stop_event.is_set():
                if round_num > config.max_iterations:
                    break

                # Each round: cycle through all sessions
                for i, session in enumerate(all_sessions):
                    if self._stop_event.is_set():
                        break

                    sid = session.session_id
                    name = session.ai_profile.name
                    is_leader = (session == leader)

                    # Pick focus based on position in pipeline
                    focus_idx = (round_num * len(all_sessions) + i) % len(pipeline_focuses)
                    focus_name, focus_desc = pipeline_focuses[focus_idx]

                    # Build prompt with current codebase
                    if is_leader and i > 0:
                        # Leader reviewing workers' improvements
                        directive = (
                            f"You are the LEAD DEVELOPER reviewing code improved by your team.\n"
                            f"Review the code below for bugs, integration issues, and quality.\n"
                            f"Fix any problems and apply: {focus_desc}\n"
                            f"Output the ENTIRE updated codebase. No placeholders."
                        )
                    else:
                        directive = (
                            f"[You are {name}] Improve this code:\n"
                            f"{focus_desc}\n\n"
                            f"Apply improvements to the CURRENT CODEBASE below.\n"
                            f"Output the ENTIRE updated codebase. No placeholders."
                        )

                    full_prompt = self._build_context_prompt(directive, current_codebase)

                    role = "LEADER" if is_leader else f"WORKER-{i}"
                    if self._on_output:
                        self._on_output(
                            sid, "system",
                            f"\n[PIPELINE R{round_num}] {role} ({name}): {focus_name} "
                            f"({len(current_codebase)} chars in)\n"
                            f"{'-'*40}\n"
                        )

                    try:
                        result = session.client.generate(
                            prompt=full_prompt,
                            on_progress=lambda t, s=sid: (
                                self._on_output(s, "code", t)
                                if self._on_output else None
                            ),
                        )
                    except Exception as e:
                        logger.warning("[PIPELINE] %s failed: %s", name, e)
                        if self._on_output:
                            self._on_output(sid, "error", f"Error: {e}\n")
                        continue  # Skip this session, keep the pipeline going

                    # Extract code and relay to next session
                    # Pass previous codebase so we can skip echoed prompt code
                    extracted = self._extract_code_blocks(result, previous_codebase=current_codebase)
                    if extracted and extracted != current_codebase:
                        logger.info(
                            "[PIPELINE] %s produced %d chars (was %d)",
                            name, len(extracted), len(current_codebase)
                        )
                        current_codebase = extracted
                    elif extracted:
                        # Got something but it's the same — AI may have echoed
                        logger.warning("[PIPELINE] %s: extraction identical to input (%d chars), keeping", name, len(extracted))
                        current_codebase = extracted
                    else:
                        logger.warning("[PIPELINE] %s: extraction failed, keeping previous", name)

                    self._codebases[sid] = current_codebase
                    self._results[sid] = result

                    iter_num = self._iteration_counts.get(sid, 0) + 1
                    self._iteration_counts[sid] = iter_num
                    self._save_result(
                        session, config.task, iter_num,
                        f"R{round_num}_{focus_name.replace(' ', '_')}", result, start_time,
                        extracted_code=current_codebase,
                    )
                    if self._on_iteration:
                        self._on_iteration(sid, iter_num, f"R{round_num} {focus_name}", name)

                round_num += 1

        except InterruptedError:
            logger.info("[PIPELINE] Cancelled")
        except Exception as e:
            logger.error("[PIPELINE] Fatal: %s", e)
            if self._on_output:
                self._on_output(leader_sid, "error", f"[PIPELINE] Fatal: {e}\n")
        finally:
            elapsed = time.time() - start_time
            total_iters = sum(self._iteration_counts.values())
            logger.info("[PIPELINE] Ended: %d total iterations in %.0fs", total_iters, elapsed)
            self._thread_finished()

    def _session_loop(self, session, config: BroadcastConfig, initial_prompt: str) -> None:
        """Run the context-aware improvement loop for one session.

        KEY DESIGN: Each iteration extracts the code from the AI's response
        and includes it verbatim in the next prompt. The AI never has to
        reconstruct code from memory — it always operates on the concrete,
        latest codebase. This prevents context amnesia across iterations.
        """
        sid = session.session_id
        ai_name = session.ai_profile.name
        self._iteration_counts[sid] = 0
        start_time = time.time()
        current_codebase = ""  # Tracks the latest extracted code
        stagnation_count = 0   # Consecutive stagnant iterations
        expansion_round = 0    # Which expansion focus we're on
        in_expansion_mode = False  # True once stagnation triggers expansion
        last_expansion_hash = ""  # Track expansion stagnation too
        cycle_start_hash = ""    # Perfection Loop: hash at start of each full cycle
        cycle_stagnant_count = 0  # Perfection Loop: consecutive cycles with no improvement
        non_code_count = 0       # Consecutive iterations returning non-code
        non_code_resets = 0      # Times we've reset conversation due to non-code
        consecutive_errors = 0   # Consecutive generate() exceptions

        try:
            # ── Iteration 0: Initial build with engineered prompt ────
            if self._stop_event.is_set():
                return

            if self._on_output:
                self._on_output(
                    sid, "system",
                    f"[{ai_name}] Starting: {config.task}\n"
                    f"{'='*50}\n"
                )

            result = session.client.generate(
                prompt=initial_prompt,
                system_instruction=(
                    "You are an expert coding assistant. Write clean, complete, "
                    "production-ready code. No placeholders."
                ),
                on_progress=lambda t, s=sid: (
                    self._on_output(s, "code", t) if self._on_output else None
                ),
            )

            # Extract code from response for context continuity
            extracted = self._extract_code_blocks(result)
            if extracted:
                current_codebase = extracted
                logger.info("[%s] Extracted %d chars of code from initial build",
                            ai_name, len(current_codebase))
            else:
                logger.warning("[%s] Could not extract code from initial response", ai_name)
                current_codebase = result  # Use raw response as fallback

            # Store for resume capability
            self._codebases[sid] = current_codebase
            self._results[sid] = result
            if self._on_output:
                self._on_output(sid, "result", result)
            self._save_result(session, config.task, 1, "Initial Build", result, start_time,
                              extracted_code=current_codebase)

            self._iteration_counts[sid] = 1
            if self._on_iteration:
                self._on_iteration(sid, 1, "Initial Build", ai_name)

            # ── Improvement loop (context-aware) ─────────────────────
            iteration = 1
            while not self._stop_event.is_set():
                # Check limits
                if iteration >= config.max_iterations:
                    break
                if config.time_limit_minutes > 0:
                    elapsed_min = (time.time() - start_time) / 60
                    if elapsed_min >= config.time_limit_minutes:
                        break

                previous_codebase = current_codebase

                if in_expansion_mode:
                    # ── Expansion mode: create NEW functions/modules ──
                    full_prompt = self._build_expansion_prompt(
                        config.task, current_codebase, expansion_round
                    )
                    focus = self._expansion_focus_name(expansion_round)
                    expansion_round += 1

                    if self._on_output:
                        self._on_output(
                            sid, "system",
                            f"\n[{ai_name}] 🔀 EXPANSION #{expansion_round}: {focus} "
                            f"(base codebase: {len(current_codebase)} chars)\n"
                            f"{'-'*40}\n"
                        )
                else:
                    # ── Normal improvement mode (selected focuses) ───
                    selected = config.selected_focuses or None
                    improvement_directive, focus = engineer_improvement_prompt(
                        task=config.task,
                        iteration=iteration,
                        ai_name=ai_name,
                        selected_focuses=selected,
                    )

                    # Build the FULL prompt: directive + current codebase + files
                    full_prompt = self._build_context_prompt(
                        improvement_directive, current_codebase,
                        file_context=self._file_context,
                    )

                    # Perfection Loop: track cycle progress
                    if config.perfection_loop and selected:
                        cycle_pos = iteration % len(selected)
                        cycle_num = iteration // len(selected) + 1
                        focus_tag = f"[Cycle {cycle_num}] {focus}"
                    else:
                        focus_tag = focus

                    if self._on_output:
                        self._on_output(
                            sid, "system",
                            f"\n[{ai_name}] Improvement #{iteration + 1}: {focus_tag} "
                            f"(feeding {len(current_codebase)} chars of code)\n"
                            f"{'-'*40}\n"
                        )

                try:
                    result = session.client.generate(
                        prompt=full_prompt,
                        on_progress=lambda t, s=sid: (
                            self._on_output(s, "code", t)
                            if self._on_output else None
                        ),
                    )
                except Exception as e:
                    consecutive_errors += 1
                    logger.warning("[%s] Improvement failed (%d in a row): %s",
                                   ai_name, consecutive_errors, e)
                    if self._on_output:
                        self._on_output(sid, "error", f"Error ({consecutive_errors}x): {e}\n")
                    if consecutive_errors >= 5:
                        logger.error("[%s] %d consecutive errors — stopping", ai_name, consecutive_errors)
                        if self._on_output:
                            self._on_output(
                                sid, "system",
                                f"\n{'='*50}\n"
                                f"[{ai_name}] STOPPED: {consecutive_errors} consecutive errors\n"
                                f"Last error: {e}\n"
                                f"{'='*50}\n"
                            )
                        break
                    time.sleep(5)
                    continue

                consecutive_errors = 0  # Reset on successful generate

                # Extract code from this iteration's response
                # Pass previous codebase to skip echoed prompt code
                extracted = self._extract_code_blocks(result, previous_codebase=current_codebase)

                if in_expansion_mode:
                    # In expansion mode, the new code is ADDITIVE — don't replace the base
                    if extracted:
                        logger.info("[%s] Expansion %d: got %d chars of new code",
                                    ai_name, expansion_round, len(extracted))
                    else:
                        # Only use raw response if it looks like code
                        if self._is_likely_code(result):
                            logger.warning("[%s] Expansion %d: no fenced blocks, using raw code", ai_name, expansion_round)
                            extracted = result
                        else:
                            logger.warning("[%s] Expansion %d: response is not code, skipping", ai_name, expansion_round)
                            extracted = ""

                    if not extracted:
                        # Nothing usable from this expansion — skip save/hash
                        iteration += 1
                        self._iteration_counts[sid] = iteration
                        continue

                    # Detect stagnation within expansion mode
                    exp_hash = self._code_hash(extracted)
                    if exp_hash == last_expansion_hash:
                        logger.warning("[%s] Expansion output is STAGNANT — resetting conversation", ai_name)
                        if self._on_output:
                            self._on_output(
                                sid, "system",
                                f"[{ai_name}] Expansion stagnant — starting fresh conversation\n"
                            )
                        try:
                            session.client.new_conversation()
                            time.sleep(2)
                        except Exception as e:
                            logger.warning("[%s] new_conversation() failed: %s", ai_name, e)
                    last_expansion_hash = exp_hash

                    # Save expansion result
                    iteration += 1
                    self._iteration_counts[sid] = iteration
                    self._save_result(session, config.task, iteration,
                                      f"Expand_{focus.replace(' ', '_')}", result, start_time,
                                      extracted_code=extracted)
                    if self._on_iteration:
                        self._on_iteration(sid, iteration, f"Expand: {focus}", ai_name)
                    if self._on_output:
                        self._on_output(sid, "result", result)
                    continue

                # ── Normal mode: update codebase ─────────────────────
                if extracted:
                    prev_len = len(current_codebase)
                    current_codebase = extracted
                    non_code_count = 0  # Got real code
                    logger.info("[%s] Iteration %d: extracted %d chars (was %d)",
                                ai_name, iteration + 1, len(current_codebase), prev_len)
                else:
                    # No code extracted — AI may have returned chat text
                    non_code_count += 1
                    logger.warning(
                        "[%s] Iteration %d: no code extracted (%d consecutive), "
                        "keeping previous codebase (%d chars)",
                        ai_name, iteration + 1, non_code_count, len(current_codebase)
                    )

                    if non_code_count >= 2 and current_codebase:
                        if non_code_resets >= 2:
                            # Already tried recovery twice — stop
                            logger.error(
                                "[%s] Non-code output persists after %d recovery attempts — stopping",
                                ai_name, non_code_resets
                            )
                            if self._on_output:
                                self._on_output(
                                    sid, "system",
                                    f"\n{'='*50}\n"
                                    f"[{ai_name}] STOPPED: AI unable to return code "
                                    f"after {non_code_resets} recovery attempts\n"
                                    f"{'='*50}\n"
                                )
                            break

                        # Try self-recovery: ask Gemini to diagnose and fix
                        non_code_resets += 1
                        recovered = self._attempt_self_recovery(
                            session, config, current_codebase,
                            problem=(
                                f"Returned chat text instead of code for "
                                f"{non_code_count} consecutive iterations"
                            ),
                            sid=sid, ai_name=ai_name,
                        )
                        if recovered:
                            current_codebase = recovered
                            non_code_count = 0
                        # If recovery failed, loop continues with old codebase

                # ── Stagnation detection ─────────────────────────────
                if self._is_stagnant(current_codebase, previous_codebase):
                    stagnation_count += 1
                    logger.warning(
                        "[%s] Iteration %d: STAGNANT (%d in a row, hash=%s)",
                        ai_name, iteration + 1, stagnation_count,
                        self._code_hash(current_codebase)[:8]
                    )

                    if config.expand_on_stagnation and stagnation_count >= 2:
                        in_expansion_mode = True
                        logger.info(
                            "[%s] Stagnation detected after %d identical iterations — "
                            "switching to EXPANSION MODE (creating new functions)",
                            ai_name, stagnation_count
                        )
                        if self._on_output:
                            self._on_output(
                                sid, "system",
                                f"\n{'='*50}\n"
                                f"[{ai_name}] Code hit ceiling at {len(current_codebase)} chars.\n"
                                f"Starting fresh conversation + EXPANSION MODE.\n"
                                f"{'='*50}\n"
                            )
                        if self._on_status:
                            self._on_status(
                                f"Expansion mode: creating new functions "
                                f"(base: {len(current_codebase)} chars)"
                            )
                        try:
                            if session.client.new_conversation():
                                logger.info("[%s] New conversation started for expansion mode", ai_name)
                                time.sleep(2)
                            else:
                                logger.warning("[%s] Could not start new conversation", ai_name)
                        except Exception as e:
                            logger.warning("[%s] new_conversation() failed: %s", ai_name, e)

                    elif stagnation_count >= 2 and current_codebase:
                        # Not using expansion mode — try self-recovery instead
                        recovered = self._attempt_self_recovery(
                            session, config, current_codebase,
                            problem=(
                                f"Code stopped improving — identical output for "
                                f"{stagnation_count} iterations "
                                f"(hash: {self._code_hash(current_codebase)[:8]})"
                            ),
                            sid=sid, ai_name=ai_name,
                        )
                        if recovered and not self._is_stagnant(recovered, current_codebase):
                            current_codebase = recovered
                            stagnation_count = 0
                else:
                    stagnation_count = 0  # Reset on meaningful change

                # ── Perfection Loop: cycle-level auto-stop ──────────
                if config.perfection_loop and config.selected_focuses:
                    num_focuses = len(config.selected_focuses)
                    cycle_pos = iteration % num_focuses
                    # At the start of each cycle, snapshot the codebase hash
                    if cycle_pos == 0:
                        current_hash = self._code_hash(current_codebase)
                        if cycle_start_hash and current_hash == cycle_start_hash:
                            cycle_stagnant_count += 1
                            logger.info(
                                "[%s] Perfection cycle completed with NO improvement "
                                "(%d consecutive stagnant cycles)",
                                ai_name, cycle_stagnant_count
                            )
                            if cycle_stagnant_count >= 2:
                                if self._on_output:
                                    self._on_output(
                                        sid, "system",
                                        f"\n{'='*50}\n"
                                        f"[{ai_name}] PERFECTION LOOP COMPLETE\n"
                                        f"Code is fully optimized — 2 full cycles with "
                                        f"no further improvement detected.\n"
                                        f"Final codebase: {len(current_codebase)} chars\n"
                                        f"{'='*50}\n"
                                    )
                                if self._on_status:
                                    self._on_status("Perfection Loop complete — code fully optimized")
                                break  # Auto-stop
                        else:
                            cycle_stagnant_count = 0
                        cycle_start_hash = self._code_hash(current_codebase)

                # Store for resume capability
                self._codebases[sid] = current_codebase
                self._results[sid] = result
                if self._on_output:
                    self._on_output(sid, "result", result)
                iteration += 1
                self._save_result(session, config.task, iteration, focus, result, start_time,
                                  extracted_code=current_codebase)

                self._iteration_counts[sid] = iteration
                if self._on_iteration:
                    self._on_iteration(sid, iteration, focus, ai_name)

        except InterruptedError:
            logger.info("[%s] Broadcast cancelled", ai_name)
        except Exception as e:
            logger.error("[%s] Broadcast error: %s", ai_name, e)
            if self._on_output:
                self._on_output(sid, "error", f"[{ai_name}] Fatal: {e}\n")
        finally:
            count = self._iteration_counts.get(sid, 0)
            elapsed = time.time() - start_time
            logger.info("[%s] Broadcast loop ended: %d iterations in %.0fs",
                         ai_name, count, elapsed)
            self._thread_finished()

    def _session_loop_resume(
        self,
        session,
        config: BroadcastConfig,
        start_iteration: int,
        saved_codebase: str,
    ) -> None:
        """Resume the improvement loop from a saved state.

        Skips the initial build — jumps straight into the improvement cycle
        using the saved codebase and iteration count.
        """
        sid = session.session_id
        ai_name = session.ai_profile.name
        self._iteration_counts[sid] = start_iteration
        self._codebases[sid] = saved_codebase
        start_time = time.time()
        current_codebase = saved_codebase
        stagnation_count = 0
        expansion_round = 0
        in_expansion_mode = False
        last_expansion_hash = ""
        cycle_start_hash = ""
        cycle_stagnant_count = 0
        non_code_count = 0
        non_code_resets = 0
        consecutive_errors = 0

        try:
            if self._on_output:
                self._on_output(
                    sid, "system",
                    f"[{ai_name}] Resuming from iteration {start_iteration} "
                    f"({len(current_codebase)} chars of saved code)\n"
                    f"{'='*50}\n"
                )

            iteration = start_iteration
            while not self._stop_event.is_set():
                if iteration >= config.max_iterations:
                    break
                if config.time_limit_minutes > 0:
                    elapsed_min = (time.time() - start_time) / 60
                    if elapsed_min >= config.time_limit_minutes:
                        break

                previous_codebase = current_codebase

                if in_expansion_mode:
                    full_prompt = self._build_expansion_prompt(
                        config.task, current_codebase, expansion_round
                    )
                    focus = self._expansion_focus_name(expansion_round)
                    expansion_round += 1

                    if self._on_output:
                        self._on_output(
                            sid, "system",
                            f"\n[{ai_name}] 🔀 EXPANSION #{expansion_round}: {focus} "
                            f"(base codebase: {len(current_codebase)} chars)\n"
                            f"{'-'*40}\n"
                        )
                else:
                    selected = config.selected_focuses or None
                    improvement_directive, focus = engineer_improvement_prompt(
                        task=config.task,
                        iteration=iteration,
                        ai_name=ai_name,
                        selected_focuses=selected,
                    )

                    full_prompt = self._build_context_prompt(
                        improvement_directive, current_codebase,
                        file_context=self._file_context,
                    )

                    if config.perfection_loop and selected:
                        cycle_pos = iteration % len(selected)
                        cycle_num = iteration // len(selected) + 1
                        focus_tag = f"[Cycle {cycle_num}] {focus}"
                    else:
                        focus_tag = focus

                    if self._on_output:
                        self._on_output(
                            sid, "system",
                            f"\n[{ai_name}] Improvement #{iteration + 1}: {focus_tag} "
                            f"(feeding {len(current_codebase)} chars of code)\n"
                            f"{'-'*40}\n"
                        )

                try:
                    result = session.client.generate(
                        prompt=full_prompt,
                        on_progress=lambda t, s=sid: (
                            self._on_output(s, "code", t)
                            if self._on_output else None
                        ),
                    )
                except Exception as e:
                    consecutive_errors += 1
                    logger.warning("[%s] Improvement failed (%d in a row): %s",
                                   ai_name, consecutive_errors, e)
                    if self._on_output:
                        self._on_output(sid, "error", f"Error ({consecutive_errors}x): {e}\n")
                    if consecutive_errors >= 5:
                        logger.error("[%s] %d consecutive errors — stopping", ai_name, consecutive_errors)
                        if self._on_output:
                            self._on_output(
                                sid, "system",
                                f"\n{'='*50}\n"
                                f"[{ai_name}] STOPPED: {consecutive_errors} consecutive errors\n"
                                f"Last error: {e}\n"
                                f"{'='*50}\n"
                            )
                        break
                    time.sleep(5)
                    continue

                consecutive_errors = 0

                extracted = self._extract_code_blocks(result, previous_codebase=current_codebase)

                if in_expansion_mode:
                    if extracted:
                        logger.info("[%s] Expansion %d: got %d chars of new code",
                                    ai_name, expansion_round, len(extracted))
                    else:
                        if self._is_likely_code(result):
                            logger.warning("[%s] Expansion %d: no fenced blocks, using raw code", ai_name, expansion_round)
                            extracted = result
                        else:
                            logger.warning("[%s] Expansion %d: response is not code, skipping", ai_name, expansion_round)
                            extracted = ""

                    if not extracted:
                        iteration += 1
                        self._iteration_counts[sid] = iteration
                        continue

                    exp_hash = self._code_hash(extracted)
                    if exp_hash == last_expansion_hash:
                        logger.warning("[%s] Expansion STAGNANT — resetting conversation", ai_name)
                        try:
                            session.client.new_conversation()
                            time.sleep(2)
                        except Exception as e:
                            logger.warning("[%s] new_conversation() failed: %s", ai_name, e)
                    last_expansion_hash = exp_hash

                    iteration += 1
                    self._iteration_counts[sid] = iteration
                    self._save_result(session, config.task, iteration,
                                      f"Expand_{focus.replace(' ', '_')}", result, start_time,
                                      extracted_code=extracted)
                    if self._on_iteration:
                        self._on_iteration(sid, iteration, f"Expand: {focus}", ai_name)
                    if self._on_output:
                        self._on_output(sid, "result", result)
                    continue

                # ── Normal mode: update codebase ─────────────────────
                if extracted:
                    current_codebase = extracted
                    non_code_count = 0
                else:
                    non_code_count += 1
                    logger.warning(
                        "[%s] Iteration %d: no code extracted (%d consecutive), "
                        "keeping previous (%d chars)",
                        ai_name, iteration + 1, non_code_count, len(current_codebase)
                    )

                    if non_code_count >= 2 and current_codebase:
                        if non_code_resets >= 2:
                            logger.error(
                                "[%s] Non-code output persists after %d recovery attempts — stopping",
                                ai_name, non_code_resets
                            )
                            if self._on_output:
                                self._on_output(
                                    sid, "system",
                                    f"\n{'='*50}\n"
                                    f"[{ai_name}] STOPPED: AI unable to return code "
                                    f"after {non_code_resets} recovery attempts\n"
                                    f"{'='*50}\n"
                                )
                            break

                        non_code_resets += 1
                        recovered = self._attempt_self_recovery(
                            session, config, current_codebase,
                            problem=(
                                f"Returned chat text instead of code for "
                                f"{non_code_count} consecutive iterations"
                            ),
                            sid=sid, ai_name=ai_name,
                        )
                        if recovered:
                            current_codebase = recovered
                            non_code_count = 0

                # Stagnation detection
                if self._is_stagnant(current_codebase, previous_codebase):
                    stagnation_count += 1
                    logger.warning("[%s] Iteration %d: STAGNANT (%d in a row)",
                                   ai_name, iteration + 1, stagnation_count)

                    if config.expand_on_stagnation and stagnation_count >= 2:
                        in_expansion_mode = True
                        logger.info("[%s] Switching to EXPANSION MODE", ai_name)
                        if self._on_output:
                            self._on_output(
                                sid, "system",
                                f"\n{'='*50}\n"
                                f"[{ai_name}] Code hit ceiling — fresh conversation + EXPANSION MODE.\n"
                                f"{'='*50}\n"
                            )
                        try:
                            if session.client.new_conversation():
                                logger.info("[%s] New conversation started for expansion", ai_name)
                                time.sleep(2)
                        except Exception as e:
                            logger.warning("[%s] new_conversation() failed: %s", ai_name, e)

                    elif stagnation_count >= 2 and current_codebase:
                        recovered = self._attempt_self_recovery(
                            session, config, current_codebase,
                            problem=(
                                f"Code stopped improving — identical output for "
                                f"{stagnation_count} iterations "
                                f"(hash: {self._code_hash(current_codebase)[:8]})"
                            ),
                            sid=sid, ai_name=ai_name,
                        )
                        if recovered and not self._is_stagnant(recovered, current_codebase):
                            current_codebase = recovered
                            stagnation_count = 0
                else:
                    stagnation_count = 0

                # Perfection Loop: cycle-level auto-stop
                if config.perfection_loop and config.selected_focuses:
                    num_focuses = len(config.selected_focuses)
                    cycle_pos = iteration % num_focuses
                    if cycle_pos == 0:
                        current_hash = self._code_hash(current_codebase)
                        if cycle_start_hash and current_hash == cycle_start_hash:
                            cycle_stagnant_count += 1
                            logger.info(
                                "[%s] Perfection cycle: NO improvement (%d stagnant cycles)",
                                ai_name, cycle_stagnant_count
                            )
                            if cycle_stagnant_count >= 2:
                                if self._on_output:
                                    self._on_output(
                                        sid, "system",
                                        f"\n{'='*50}\n"
                                        f"[{ai_name}] PERFECTION LOOP COMPLETE\n"
                                        f"Code fully optimized — no further improvement.\n"
                                        f"Final: {len(current_codebase)} chars\n"
                                        f"{'='*50}\n"
                                    )
                                if self._on_status:
                                    self._on_status("Perfection Loop complete — code fully optimized")
                                break
                        else:
                            cycle_stagnant_count = 0
                        cycle_start_hash = self._code_hash(current_codebase)

                self._codebases[sid] = current_codebase
                self._results[sid] = result
                if self._on_output:
                    self._on_output(sid, "result", result)
                iteration += 1
                self._save_result(session, config.task, iteration, focus, result, start_time,
                                  extracted_code=current_codebase)

                self._iteration_counts[sid] = iteration
                if self._on_iteration:
                    self._on_iteration(sid, iteration, focus, ai_name)

        except InterruptedError:
            logger.info("[%s] Broadcast resumed loop cancelled", ai_name)
        except Exception as e:
            logger.error("[%s] Broadcast resume error: %s", ai_name, e)
            if self._on_output:
                self._on_output(sid, "error", f"[{ai_name}] Fatal: {e}\n")
        finally:
            count = self._iteration_counts.get(sid, 0)
            elapsed = time.time() - start_time
            logger.info("[%s] Resumed loop ended: %d total iterations",
                         ai_name, count)
            self._thread_finished()

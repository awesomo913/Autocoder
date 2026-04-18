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
from typing import Any, Callable, Optional

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
        "description": (
            "Cleans up messy code — finds duplicated parts, oversized "
            "functions, and confusing names, then simplifies them. "
            "Pick this when the code feels tangled or you've made lots "
            "of quick edits and want to tidy up."
        ),
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
        "description": (
            "Adds 2–3 useful features users typically want — things "
            "like save/load, keyboard shortcuts, settings, or search. "
            "Pick this when the core works but it feels bare-bones."
        ),
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
        "description": (
            "Tries to break the app on purpose — with giant inputs, "
            "weird characters, simulated crashes — then fixes every "
            "weakness it finds. Pick this when users are complaining "
            "about random errors or the app crashes sometimes."
        ),
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
        "description": (
            "Adds bold, impressive new capabilities that turn a plain "
            "tool into something memorable. Pick this when you want "
            "the 'wow, it does THAT too?' factor — not polish, new "
            "abilities."
        ),
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
        "description": (
            "Makes the visual interface modern and polished — proper "
            "spacing, colors, hover effects, loading spinners, smooth "
            "animations. Pick this when the app works but looks dated "
            "or amateurish."
        ),
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
        "description": (
            "Walks through the code as if clicking every button. Fixes "
            "anything that's broken, disconnected, or only half-wired. "
            "Pick this when features seem to exist but nothing actually "
            "works end-to-end yet."
        ),
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
        "description": (
            "Writes a visual spec showing exactly what each screen "
            "should look like, using text-art mockups + color/layout "
            "notes. Pick this before redesigning the UI or when handing "
            "the design to another developer."
        ),
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
        "description": (
            "Scores the code 1–10 on quality, architecture, polish, "
            "error-handling, performance — then fixes the lowest-scoring "
            "parts. Pick this for a quality checkpoint when you want an "
            "honest 'how good is this?' reading."
        ),
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
    # ── Focuses 9-16: added to cover orthogonal engineering dimensions ──
    "performance": {
        "label": "Performance Optimization",
        "description": (
            "Makes the app noticeably faster — finds slow loops and "
            "expensive operations, replaces them with efficient ones, "
            "caches repeat work. Pick this when users say 'this is "
            "slow' or when load/save takes too long."
        ),
        "prompt": (
            "OPTIMIZE THIS CODEBASE for speed and resource efficiency.\n\n"
            "1. HOT-PATH AUDIT: Identify the 3 most frequently executed code "
            "paths. Measure or estimate their cost. Rewrite any O(n²)/O(n³) "
            "loop into O(n log n) or O(n) using dicts, sets, heaps, or caches.\n"
            "2. ALGORITHMIC WINS: Replace naive linear scans with indexed "
            "lookups. Use `collections.deque` for FIFO, `heapq` for priority, "
            "`bisect` for sorted inserts, `functools.lru_cache` for pure funcs.\n"
            "3. I/O BATCHING: Group small reads/writes into batched ops. "
            "Replace per-item network/disk calls with bulk operations. Buffer "
            "before flush.\n"
            "4. LAZY EVALUATION: Defer expensive computation until actually "
            "needed. Use generators over list comprehensions where streaming "
            "is acceptable. Compute-once-cache-forever for immutable derived "
            "state.\n"
            "5. MEMORY FOOTPRINT: Find anywhere a list/dict is held longer "
            "than necessary. Use `__slots__` on high-count classes. Replace "
            "full copies with views/slices where safe.\n"
            "6. STARTUP TIME: Defer imports of heavy modules behind the first "
            "use. Cache parsed config, compiled regexes, and loaded data at "
            "module load — not per-call.\n\n"
            "Add inline comments like `# perf: was O(n²), now O(n) via dict` "
            "next to each optimization so reviewers see what changed and why.\n"
            "Apply ALL optimizations to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "security": {
        "label": "Security Hardening",
        "description": (
            "Blocks attacks and bad data — checks all inputs, protects "
            "passwords/keys, prevents hackers from breaking in through "
            "file paths or fake requests. Pick this for anything that "
            "stores passwords, accepts uploads, or runs on the internet."
        ),
        "prompt": (
            "SECURITY-HARDEN this codebase against a hostile threat model.\n\n"
            "1. INPUT VALIDATION: Every external input (user, file, network, "
            "env var, argv) gets validated at the boundary. Reject early with "
            "a clear error. Whitelist allowed characters/shapes. Cap sizes.\n"
            "2. INJECTION DEFENSE: Any dynamic string that ends up in a shell, "
            "SQL, HTML, path, regex, or subprocess invocation must be escaped "
            "or parameterized. Replace f-string SQL with parameterized queries. "
            "Replace `shell=True` with `subprocess.run([...])` argument lists.\n"
            "3. SECRETS HANDLING: No hardcoded tokens, passwords, or keys. "
            "Load from `os.environ` or a secret manager. Strip secrets from "
            "logs and exception messages. Never pass secrets via command line.\n"
            "4. DESERIALIZATION: Replace `pickle.load`/`yaml.load` with "
            "`yaml.safe_load` / explicit schemas. Never unpickle untrusted data.\n"
            "5. PATH TRAVERSAL: Every file path derived from user input gets "
            "resolved via `Path(base).resolve()` and verified to stay within "
            "the intended directory. Reject `..` and absolute paths upfront.\n"
            "6. CRYPTO & AUTH: Use `secrets` (not `random`) for tokens. Use "
            "`hashlib.pbkdf2_hmac` / `bcrypt` for passwords, never raw hashes. "
            "Constant-time comparison for secrets (`hmac.compare_digest`).\n"
            "7. DoS RESISTANCE: Cap regex backtracking, JSON depth, recursion, "
            "upload sizes, and any unbounded growth.\n\n"
            "Apply every fix as real code changes. No `# TODO: validate`.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "test_suite": {
        "label": "Test Suite",
        "description": (
            "Writes automated tests that check the app still works "
            "after every code change — so you instantly know if "
            "something breaks. Pick this once you have code you care "
            "about not breaking during future edits."
        ),
        "prompt": (
            "BUILD A PROPER TEST SUITE for this codebase.\n\n"
            "Use pytest (AAA pattern: Arrange-Act-Assert). Include tests as a "
            "`tests/` section at the end of the same file IF the codebase is a "
            "single module; otherwise add a `tests/test_<module>.py` file for "
            "each module of substance.\n\n"
            "1. UNIT TESTS: For every pure function, write at least:\n"
            "   - a happy-path test\n"
            "   - an edge-case test (empty, None, zero, negative, huge)\n"
            "   - an error-path test that asserts the right exception\n"
            "2. FIXTURES: Use `@pytest.fixture` for shared setup — a temp dir, "
            "a mock client, an example config. `@pytest.fixture(scope='module')` "
            "for expensive fixtures.\n"
            "3. PARAMETRIZE: Collapse copy-paste test variants into "
            "`@pytest.mark.parametrize` with a clear id for each case.\n"
            "4. MOCKING: Use `unittest.mock.patch` for external dependencies "
            "(network, filesystem writes, sleeps, clocks, random). Tests must "
            "be fast (<100ms each) and deterministic.\n"
            "5. ASSERTIONS: Prefer specific asserts (`assert result == 42`) "
            "over `assert result` alone. Use `pytest.approx` for floats. Use "
            "`pytest.raises(Error, match=...)` for exception shape.\n"
            "6. INTEGRATION: One or two end-to-end tests that exercise the "
            "main workflow top-to-bottom without mocks.\n"
            "7. COVERAGE: Tests should exercise every branch, every error "
            "path, and every public function. Aim for >80% coverage.\n\n"
            "Also add a brief `# How to run:` comment at the top of the test "
            "module showing `pytest -v` and any required env setup.\n"
            "Apply to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase PLUS the test code."
        ),
    },
    "documentation": {
        "label": "Documentation",
        "description": (
            "Writes clear instructions, code comments, usage examples, "
            "and a README so a new developer (or future-you) can get "
            "productive in 15 minutes. Pick this before handing the "
            "code to someone else, going public, or if you'll revisit "
            "it in 6 months."
        ),
        "prompt": (
            "DOCUMENT this codebase so a new developer can onboard in 15 min.\n\n"
            "1. MODULE DOCSTRING: At the top of the main file, write a "
            "docstring that answers: What does this do? Who uses it? How "
            "does it fit into the larger system? Include a 5-10 line "
            "example of typical usage.\n"
            "2. FUNCTION/CLASS DOCSTRINGS: Every public function and class "
            "gets a Google-style docstring: one-line summary, then Args, "
            "Returns, Raises sections. Include a short Example for non-"
            "obvious usage.\n"
            "3. TYPE HINTS: Every function signature gets full type hints "
            "(including generics: `list[str]`, `dict[str, int]`, "
            "`Optional[X]`, `Callable[[int], bool]`). Add `from __future__ "
            "import annotations` if needed.\n"
            "4. INLINE COMMENTS: Add `# why` comments (not `# what`) for "
            "any non-obvious decision — 'uses X because Y' / 'order matters "
            "because Z'. Remove comments that restate the code.\n"
            "5. README-BLOCK: Near the top, embed a triple-quoted README "
            "covering: installation, basic usage, configuration, common "
            "errors & fixes, and where to look for each major subsystem.\n"
            "6. CHANGELOG: Add a `CHANGELOG` block at the top with a "
            "version entry describing what this iteration changes vs the "
            "previous codebase.\n"
            "7. EXAMPLES: Add an `if __name__ == '__main__':` demo that "
            "exercises the headline features so a reader can run the file "
            "directly and see it work.\n\n"
            "Apply ALL documentation additions to the CURRENT CODEBASE "
            "below without changing behavior.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "logging_observability": {
        "label": "Logging & Observability",
        "description": (
            "Records what the app is doing as it runs — timestamps, "
            "errors, timings, health status — so when something goes "
            "wrong you have a clear trail to debug with. Pick this for "
            "anything running unattended or anything going to production."
        ),
        "prompt": (
            "ADD PRODUCTION-GRADE OBSERVABILITY to this codebase.\n\n"
            "1. STRUCTURED LOGGING: Replace every `print` with the `logging` "
            "module. Configure once at startup with a clear format including "
            "timestamp, level, logger-name, and message. Use `logger.info` "
            "for lifecycle events, `logger.debug` for detail, `logger.warning` "
            "for recoverable issues, `logger.error` with `exc_info=True` for "
            "exceptions.\n"
            "2. LOG LEVEL CONTROL: Level should be configurable via env var "
            "(e.g. `LOG_LEVEL=DEBUG`) AND via CLI flag. Default INFO in "
            "production, DEBUG if `DEBUG=1`.\n"
            "3. STRUCTURED CONTEXT: For multi-step workflows, bind a "
            "correlation id (uuid) at the entry point and include it in every "
            "log line for that flow. Use `logger = logging.getLogger(__name__).getChild(run_id)`\n"
            "4. METRICS: Expose counters (events happened), gauges (current "
            "values), and timers (how long did X take?) via a simple in-memory "
            "`Metrics` class. Dump them on exit to `metrics.json` for later "
            "analysis.\n"
            "5. HEALTH CHECK: Add a `health_check()` function that verifies "
            "critical subsystems (connection up? config valid? required data "
            "loaded?) and returns a structured status dict.\n"
            "6. TRACING SHIMS: Add `@traced` decorator or context manager that "
            "logs entry/exit of key operations with timing. Use it on the "
            "top 5 most important functions.\n"
            "7. ERROR TELEMETRY: On every exception caught, log the full "
            "traceback + inputs that triggered it. Never silently swallow.\n\n"
            "Apply to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "concurrency": {
        "label": "Concurrency & Parallelism",
        "description": (
            "Lets the app do multiple things at once without them "
            "stepping on each other — so the UI doesn't freeze while "
            "downloads happen or data loads. Pick this when the app "
            "does heavy work that shouldn't block everything else."
        ),
        "prompt": (
            "UPGRADE THE CONCURRENCY MODEL of this codebase.\n\n"
            "1. IDENTIFY OPPORTUNITIES: Find operations that are IO-bound "
            "and run sequentially — perfect candidates for async or thread-"
            "pool parallelism. Estimate the speedup.\n"
            "2. CHOOSE THE RIGHT TOOL:\n"
            "   - `asyncio` for many IO-bound tasks (network, disk) in a "
            "single process\n"
            "   - `concurrent.futures.ThreadPoolExecutor` for bounded IO-"
            "bound parallelism with sync APIs\n"
            "   - `multiprocessing.Pool` for CPU-bound fan-out\n"
            "   - `threading.Thread` only for specific background loops\n"
            "3. THREAD SAFETY: Any shared mutable state gets protected by "
            "`threading.Lock` / `RLock` / `queue.Queue`. Document the "
            "invariant the lock protects. No lock = the data must be "
            "immutable or thread-local.\n"
            "4. BACKPRESSURE: Replace unbounded queues with `Queue(maxsize=N)` "
            "so producers slow down when consumers fall behind. Use bounded "
            "`Semaphore` to cap concurrent external calls.\n"
            "5. CANCELLATION: Every long-running worker respects a "
            "`threading.Event` / `asyncio.CancelledError` so the app can shut "
            "down cleanly within ~1s. Daemon threads don't block exit.\n"
            "6. TIMEOUTS: Every `.get()`, `.join()`, network call, and "
            "`await` has a timeout. No indefinite blocking.\n"
            "7. NO DATA RACES: Audit for check-then-act patterns on shared "
            "state (`if x not in cache: cache[x] = ...`) — replace with "
            "`cache.setdefault` or a locked block.\n\n"
            "Apply ALL changes to the CURRENT CODEBASE below. Keep public "
            "APIs backward-compatible where practical.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "configuration": {
        "label": "Configuration & Env",
        "description": (
            "Lets users customize the app via settings files, command-"
            "line flags, or environment variables — no editing code to "
            "change behavior. Pick this when you want different setups "
            "(dev/staging/prod) or non-devs need to configure it."
        ),
        "prompt": (
            "INTRODUCE FIRST-CLASS CONFIGURATION to this codebase.\n\n"
            "1. CENTRAL CONFIG: Create a single `Config` dataclass "
            "(`@dataclass(frozen=True)`) that holds every tunable — paths, "
            "hosts, timeouts, feature flags, limits. NO more magic numbers "
            "scattered in the code.\n"
            "2. LOAD ORDER (highest priority last): built-in defaults → "
            "`config.toml` / `config.json` on disk → environment variables "
            "→ CLI flags. Document the precedence.\n"
            "3. CLI: Use `argparse` (or `typer`/`click` if already present) "
            "with clear `--help`, reasonable defaults, and sensible group "
            "names (`--runtime`, `--debug`, `--output`). Every flag maps "
            "to a Config field.\n"
            "4. ENV: Map `APPNAME_FIELD=value` (uppercase, underscore) to "
            "`config.field`. Type-convert based on the dataclass field type "
            "(bool, int, list-of-str). Document each env var in the README.\n"
            "5. VALIDATION: On construction, the Config validates every "
            "field (path exists? port in range? timeout > 0?) and fails "
            "fast with a specific error message on bad input.\n"
            "6. PROFILES: Support named profiles (`--profile dev|staging|"
            "prod`) that layer on top of defaults — so a user doesn't have "
            "to pass 15 flags every time.\n"
            "7. DEBUGGING AID: `config.describe()` prints the effective "
            "config with the SOURCE of each value (default/file/env/cli), "
            "which makes 'why is it doing X' trivial to diagnose.\n"
            "8. NO GLOBALS: Inject config via constructor / function "
            "argument. Don't reach out to `os.environ` from deep in the code.\n\n"
            "Apply to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    "error_recovery": {
        "label": "Error Recovery & Resilience",
        "description": (
            "Teaches the app to handle failures without crashing — "
            "retries when the network blips, recovers from stuck "
            "states, cleans up after interruptions. Pick this for "
            "anything that talks to the internet, disk, or other "
            "services and needs to 'just keep working'."
        ),
        "prompt": (
            "MAKE THIS CODEBASE SURVIVE REAL-WORLD FAILURES.\n\n"
            "Pressure-testing finds bugs. Resilience is about what happens "
            "AFTER the bug fires: does the system crash, or does it recover?\n\n"
            "1. RETRY WITH BACKOFF: Every flaky external call (network, "
            "disk, subprocess, DB) gets wrapped in retry logic: exponential "
            "backoff (e.g. 1s → 2s → 4s → 8s) capped at N retries with "
            "jitter. Only retry on transient errors (timeout, 503, connection "
            "reset) — not on 4xx/auth/programming errors.\n"
            "2. CIRCUIT BREAKER: For dependencies that can stay broken for "
            "minutes, implement a simple circuit breaker: after K consecutive "
            "failures, open the circuit for T seconds and fail fast; after T "
            "seconds try a single probe request; if it succeeds, close the "
            "circuit.\n"
            "3. FALLBACK PATHS: Every non-critical dependency has a fallback "
            "(cached value, default, degraded mode). Log the degradation at "
            "WARNING level so operators know.\n"
            "4. IDEMPOTENCY: Any operation that might be retried must be "
            "safe to run N times. Use idempotency keys / check-before-write "
            "patterns.\n"
            "5. PARTIAL FAILURE: For batch operations, track per-item success "
            "and keep going — don't let one bad item kill the whole batch. "
            "Return a structured result with both successes and failures.\n"
            "6. STATE RECOVERY: On startup, detect and recover from a crash "
            "mid-operation (incomplete temp file, dirty lock, half-committed "
            "state). Test this path explicitly.\n"
            "7. WATCHDOG / HEARTBEAT: Long-running loops emit a heartbeat "
            "every N seconds; a watchdog restarts them if silent too long.\n"
            "8. CLEAN SHUTDOWN: Handle SIGINT/SIGTERM by flushing in-flight "
            "work, closing resources, and committing state before exit.\n\n"
            "Apply to the CURRENT CODEBASE below.\n"
            "Output the ENTIRE updated codebase. No placeholders."
        ),
    },
    # ── Focuses 17-32: Cursor-level depth for apps, games, and systems ──
    "plugin_system": {
        "label": "Plugin / Extension System",
        "description": (
            "Lets other developers (or power users) add their own "
            "features on top of your app without touching your code — "
            "like VS Code extensions or WordPress plugins. Pick this "
            "when you want a community or ecosystem to grow around it."
        ),
        "prompt": (
            "TURN THIS CODEBASE INTO A PLATFORM with a real plugin system.\n\n"
            "1. HOOK INVENTORY: Identify every place a user might want to "
            "customize behavior. Define named hook points (events, filters, "
            "transformers, validators) with clear contracts.\n"
            "2. PLUGIN BASE CLASS / PROTOCOL: Define `class Plugin` (or a "
            "`Protocol`) that every plugin implements. Include metadata fields "
            "(name, version, required_core_version, dependencies).\n"
            "3. DISCOVERY: Auto-discover plugins from a `plugins/` directory, "
            "entry points in installed packages, or explicit registration. "
            "Support enable/disable without code changes.\n"
            "4. DYNAMIC DISPATCH: A `PluginManager` builds an O(1) map from "
            "hook name → ordered list of registered callbacks. Plugins can "
            "declare priority for ordering.\n"
            "5. VERSION COMPAT: Every plugin declares the core API version "
            "it needs. The loader refuses to load mismatched plugins with a "
            "clear error. Add a deprecation path for retired hooks.\n"
            "6. SANDBOXING: Catch plugin exceptions — one bad plugin never "
            "crashes the host. Log the failure with plugin name + hook.\n"
            "7. CONFIG PER PLUGIN: Each plugin can declare its own config "
            "schema; the host validates + passes it on activation.\n"
            "8. LIFECYCLE: `on_load`, `on_enable`, `on_disable`, `on_unload` "
            "hooks. Unload must release resources cleanly.\n\n"
            "Ship at least one example plugin demonstrating the API.\n"
            "Apply to the CURRENT CODEBASE below. Output the ENTIRE codebase."
        ),
    },
    "integration_layer": {
        "label": "External Integrations",
        "description": (
            "Adds clean, swappable connections to outside services "
            "(Stripe, Slack, OpenAI, email, anything with a web API) "
            "without tangling them into your core code. Pick this when "
            "the app has to talk to third-party services and you want "
            "it to be easy to switch providers or test without them."
        ),
        "prompt": (
            "BUILD AN INTEGRATION LAYER for external services in this codebase.\n\n"
            "1. ADAPTER PATTERN: Every external dependency (HTTP API, SDK, "
            "database, message broker) gets a dedicated `Adapter` class with "
            "a clean interface. Core code talks to the adapter, not the raw "
            "dependency. This lets you swap implementations without touching "
            "business logic.\n"
            "2. EXPLICIT CONTRACTS: Each adapter exposes a typed interface "
            "(Protocol / ABC) with clear method signatures. Input/output "
            "types are domain objects, not vendor SDK types.\n"
            "3. RETRY + TIMEOUT: Every outbound call gets a retry policy "
            "(exponential backoff, jitter, max attempts), a per-call timeout, "
            "and a circuit breaker for repeatedly-failing endpoints.\n"
            "4. OBSERVABILITY: Log every outbound call (method, endpoint, "
            "status, latency) at DEBUG, and every failure at WARNING with "
            "the request body (secrets redacted).\n"
            "5. MOCK FOR TESTS: Provide a `FakeAdapter` or `MockAdapter` "
            "for every real adapter, so tests run without hitting the network.\n"
            "6. RATE LIMITING / BACKPRESSURE: Respect upstream quotas. Add a "
            "token bucket if needed.\n"
            "7. WEBHOOK HANDLERS (if applicable): Verify signatures, "
            "deduplicate by event ID, process idempotently.\n"
            "8. CONFIGURATION: Endpoints, API keys, timeouts live in config — "
            "never hardcoded. Different environments get different bases.\n\n"
            "Apply to the CURRENT CODEBASE below. Output the ENTIRE codebase."
        ),
    },
    "memory_optimization": {
        "label": "Memory Optimization",
        "description": (
            "Reduces how much RAM the app uses and hunts down memory "
            "leaks (memory that's held forever and never freed). Pick "
            "this if the app gets slower the longer it runs, crashes "
            "with 'out of memory', or needs to run on phones/older PCs."
        ),
        "prompt": (
            "REDUCE MEMORY FOOTPRINT and eliminate leaks in this codebase.\n\n"
            "1. HIGH-COUNT CLASSES: Any class instantiated >1000 times "
            "gets `__slots__` — typically halves per-instance memory by "
            "eliminating `__dict__`.\n"
            "2. OBJECT POOLING: For short-lived objects created in hot "
            "loops (events, vectors, tokens), add a pool so the GC doesn't "
            "churn. Clear state on return-to-pool.\n"
            "3. WEAK REFS: Break reference cycles (parent ↔ child, "
            "observer ↔ subject) with `weakref.proxy` or `WeakValueDictionary` "
            "so the GC can actually free them.\n"
            "4. STREAMING OVER LOADING: Replace `data = f.read()` + parse "
            "with streaming parsers where the data is large. Use generators "
            "for transforms over lists.\n"
            "5. LAZY ATTRIBUTES: Heavy computed properties become "
            "`@cached_property`. Huge configs load lazily on first access.\n"
            "6. CACHE AUDIT: Every cache has a bound (size or TTL). No "
            "unbounded `dict` growth. Use `functools.lru_cache(maxsize=N)`, "
            "not `maxsize=None`.\n"
            "7. INTERN STRINGS: Repeated keys / enum-like strings get "
            "`sys.intern`'d to dedupe.\n"
            "8. LEAK DETECTION: Add a `memory_report()` helper that uses "
            "`tracemalloc` to show the top-10 allocations so regressions "
            "are easy to spot.\n\n"
            "Add `# mem:` inline comments next to each change explaining the "
            "saving. Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "network_resilience": {
        "label": "Network Resilience",
        "description": (
            "Handles flaky internet gracefully — reconnects when "
            "connections drop, retries with smart delays, times out "
            "cleanly instead of hanging forever, reuses connections "
            "for speed. Pick this for any app that calls web APIs, "
            "downloads files, or depends on a server."
        ),
        "prompt": (
            "HARDEN NETWORK I/O throughout this codebase.\n\n"
            "1. CONNECTION POOLING: Replace per-call `socket`/`urllib` with "
            "a session/pool (e.g. `httpx.Client`, `aiohttp.ClientSession`, "
            "`requests.Session`). One pool per upstream, configurable max "
            "conn count. Close on shutdown.\n"
            "2. TLS: HTTPS by default. Verify certs (no `verify=False`). "
            "Use the system CA bundle. Pin hostname via SNI.\n"
            "3. LAYERED TIMEOUTS: Set (connect, read, write, total) "
            "timeouts separately. No defaults-of-None.\n"
            "4. RETRIES: Idempotent operations retry on connect-errors and "
            "5xx; non-idempotent (POST) only retry with an idempotency key. "
            "Use exponential backoff + jitter, max 3 retries.\n"
            "5. COMPRESSION: Enable `gzip`/`br` for text bodies; decompress "
            "transparently. Skip for already-compressed types.\n"
            "6. KEEPALIVE: Reuse connections within a pool. Add periodic "
            "keepalive on long-lived connections. Close half-open sockets.\n"
            "7. STREAMING RESPONSES: Never `.read()` huge bodies into "
            "memory — iterate chunks. Close streams in `finally` or use a "
            "context manager.\n"
            "8. DIAGNOSABILITY: Every request logs (method, url, status, "
            "duration, bytes); errors log the response body (truncated + "
            "secrets redacted).\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "caching_strategy": {
        "label": "Caching Strategy",
        "description": (
            "Remembers recent results so the app doesn't redo the "
            "same expensive work over and over — like remembering a "
            "web page so you don't re-download it every time. Pick "
            "this when the app does repeated fetches, heavy math, "
            "or database queries with the same inputs."
        ),
        "prompt": (
            "INTRODUCE A PROPER CACHING STRATEGY to this codebase.\n\n"
            "1. CLASSIFY DATA: For each expensive derived value, decide "
            "(a) is it cacheable? (b) per-process or shared? (c) how stale "
            "is acceptable? Document the choice inline.\n"
            "2. TIERS: Where appropriate, layer (i) in-process LRU (fastest), "
            "(ii) on-disk (shared across runs), (iii) distributed "
            "(multi-process/host). Each tier falls through to the next.\n"
            "3. KEY DESIGN: Cache keys are deterministic, canonical strings "
            "that include every input that affects the output (version + "
            "inputs). Never cache across versions of your code without a "
            "namespace bump.\n"
            "4. TTL: Every entry has an explicit expiry. Short TTL for data "
            "that can be stale; long TTL + explicit invalidate for heavy-to-"
            "compute immutable data.\n"
            "5. INVALIDATION: Every mutation that affects cached data calls "
            "an explicit invalidation. Prefer `cache.invalidate(pattern)` "
            "over clearing everything.\n"
            "6. STAMPEDE PROTECTION: Use a lock or `singleflight` so N "
            "concurrent misses result in 1 backend call, not N.\n"
            "7. NEGATIVE CACHING: Cache 'not found' results (with a short "
            "TTL) so repeated failing lookups don't thrash the backend.\n"
            "8. METRICS: Expose hit_rate, miss_rate, evictions per cache. "
            "If hit_rate <50%, the cache may be miswired or key-unstable.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "accessibility": {
        "label": "Accessibility",
        "description": (
            "Makes the app usable by people with disabilities — works "
            "with screen readers for blind users, navigable by keyboard "
            "only, high-contrast text, respects 'reduce motion' settings. "
            "Pick this for anything public-facing, legally required "
            "(government/education/enterprise), or you want everyone to "
            "be able to use."
        ),
        "prompt": (
            "MAKE THIS APP ACCESSIBLE to users with disabilities (WCAG 2.1 AA).\n\n"
            "1. KEYBOARD NAVIGATION: Every interactive control reachable "
            "via Tab. Visible focus indicator. Logical tab order. "
            "Keyboard shortcuts for common actions (with a '?' help overlay).\n"
            "2. SCREEN READER SUPPORT: Every UI element has an accessible "
            "name (aria-label, alt text, or semantic tag). Dynamic updates "
            "announce via `aria-live` regions. Icon-only buttons get labels.\n"
            "3. COLOR CONTRAST: Text-on-background meets 4.5:1 (normal) "
            "or 3:1 (large). Don't convey state by color alone — add icon "
            "or text.\n"
            "4. TEXT SCALING: Layout survives 200% zoom. No fixed pixel "
            "heights for text containers.\n"
            "5. MOTION: Respect `prefers-reduced-motion`. Animations can "
            "be disabled. No auto-playing media.\n"
            "6. FORM FEEDBACK: Each input has a visible label (not just "
            "placeholder). Errors are announced and linked to the field via "
            "`aria-describedby`. Required fields marked.\n"
            "7. IMAGES: All informative images have alt text; decorative "
            "images have `alt=''`.\n"
            "8. LANDMARKS: Use `<main>`, `<nav>`, `<header>`, `<footer>` "
            "(or their Tk/Qt equivalents) so screen readers can jump "
            "between regions.\n\n"
            "Add a `# a11y:` comment next to each accessibility change.\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "i18n_l10n": {
        "label": "Multiple Languages",
        "description": (
            "Prepares the app to support other languages and regions — "
            "pulls all user-visible text out into translation files, "
            "formats dates/numbers/currency the way each country "
            "expects, handles right-to-left languages like Arabic. "
            "Pick this before launching outside your home market."
        ),
        "prompt": (
            "PREPARE THIS CODEBASE FOR MULTIPLE LOCALES.\n\n"
            "1. STRING EXTRACTION: Every user-facing string moves out of "
            "code into a translation table. Use `gettext` or a simple "
            "`strings['en']['key']` dict. Keys are stable, descriptive "
            "('btn.save' not 'str_023').\n"
            "2. BUNDLED DEFAULT LOCALE: Ship with at least `en` fully "
            "populated; add a stub `es` or `fr` showing the translation "
            "workflow works end-to-end.\n"
            "3. PLURAL FORMS: Use `ngettext` or an equivalent — English "
            "has 2 plural forms but many languages have more.\n"
            "4. DATES / NUMBERS / CURRENCY: Format via `babel` / "
            "`locale.format` respecting the user's locale (never "
            "`f'{date:%m/%d/%Y}'` with hardcoded order).\n"
            "5. COLLATION: Sorting lists of user-visible strings uses a "
            "locale-aware collator, not default `str` comparison.\n"
            "6. BIDI / RTL: Layout works in RTL locales (Arabic, Hebrew). "
            "Strings flow direction-aware; no hardcoded left/right.\n"
            "7. MESSAGE INTERPOLATION: Use named placeholders "
            "(`'{count} items'`) not positional — translators may reorder.\n"
            "8. LOCALE SELECTION: Detect via env var / OS, allow user "
            "override, persist across sessions.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "cli_ux": {
        "label": "Command-Line Polish",
        "description": (
            "Makes command-line tools pleasant to use — colored output, "
            "progress bars during long tasks, clear error messages, "
            "helpful `--help` pages, tab-completion in the shell, "
            "safety prompts before destructive actions. Pick this if "
            "people will type commands to use your program."
        ),
        "prompt": (
            "MAKE THE CLI A JOY TO USE.\n\n"
            "1. ARGPARSE / TYPER: Use proper subcommands for distinct "
            "actions (`myapp run`, `myapp config`, `myapp debug`). Every "
            "flag has `--long` + `-s` short form + a clear help string.\n"
            "2. HELP QUALITY: `--help` shows examples. `myapp <subcmd> "
            "--help` shows what that subcommand does. Epilogs for common "
            "recipes.\n"
            "3. COLOR + ICONS: Success = green check, warn = yellow, "
            "error = red X. Respect `NO_COLOR` env var. Detect non-TTY "
            "and disable colors automatically.\n"
            "4. PROGRESS: Long operations show a progress bar (tqdm / "
            "rich.progress) with ETA, rate, and counters — never silent.\n"
            "5. INTERACTIVE PROMPTS: Where appropriate (destructive ops), "
            "prompt for confirmation with a default. Allow `--yes` to skip.\n"
            "6. --DRY-RUN: Anything destructive supports `--dry-run` to "
            "preview without applying.\n"
            "7. TAB COMPLETION: Generate shell completion scripts "
            "(`myapp completion bash > ~/.bash_completion.d/myapp`).\n"
            "8. ERROR EXIT CODES: Distinct non-zero codes for distinct "
            "failures (`2=bad args`, `3=config error`, `4=network`, etc.). "
            "Document in README.\n"
            "9. INPUT RECEIVED VS INTERPRETED: On ambiguous input, echo "
            "what the CLI understood so the user can correct.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "data_layer": {
        "label": "Database & Persistence",
        "description": (
            "Designs a proper way to store data — tables, safe schema "
            "changes (migrations), fast searches (indexes), all-or-"
            "nothing updates (transactions). Pick this when the app "
            "stores real user data you can't afford to lose or corrupt."
        ),
        "prompt": (
            "BUILD A SOLID DATA LAYER for this codebase.\n\n"
            "1. SCHEMA DESIGN: Normalize tables to 3NF unless there's a "
            "specific read-performance reason. Primary keys on every table. "
            "Foreign keys enforced. Correct types (not TEXT for everything).\n"
            "2. MIGRATIONS: Every schema change is a numbered, up/down "
            "migration file. Never edit a past migration. Use alembic / "
            "django-migrations / a simple home-grown migrator.\n"
            "3. INDEXES: Every `WHERE`, `JOIN`, and `ORDER BY` column the "
            "app actually queries gets an index. Composite indexes match "
            "query order. Log queries >N ms to spot missing ones.\n"
            "4. TRANSACTIONS: Multi-statement writes use a transaction "
            "with clear commit/rollback. Nested ops use savepoints. "
            "Explicit isolation level where it matters.\n"
            "5. CONNECTION POOL: Shared pool sized to load. Connections "
            "checked for liveness. No per-call connect/disconnect.\n"
            "6. DAL (Data Access Layer): Each domain has a repository "
            "class with typed methods (`get_user(id) -> User | None`). "
            "Business logic never builds SQL strings.\n"
            "7. SAFE QUERIES: ONLY parameterized queries. Never f-string "
            "SQL. Use an ORM or a query builder.\n"
            "8. SOFT DELETES / AUDIT: Where relevant, `deleted_at` + "
            "`created_at`/`updated_at` columns. Never hard-delete by default.\n"
            "9. SEED DATA: A `seed.py` populates fixtures for local dev "
            "and tests.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "api_design": {
        "label": "API Design",
        "description": (
            "Builds a clean public interface so other programs or "
            "websites can talk to yours — clear URLs, documented inputs "
            "and outputs, versioning so you can change things later "
            "without breaking people. Pick this when making a backend "
            "service, a public SDK, or a web app's server layer."
        ),
        "prompt": (
            "DESIGN A CLEAN PUBLIC API for this codebase.\n\n"
            "1. RESOURCE MODEL: Identify the nouns. Each resource has a "
            "consistent URL shape (`/users/{id}/orders/{id}`). HTTP verbs "
            "match intent (`GET` safe, `POST` create, `PUT` idempotent "
            "replace, `PATCH` partial, `DELETE` remove).\n"
            "2. INPUT SCHEMAS: Every endpoint validates its request via "
            "`pydantic`/`dataclass`+`validate`/JSON Schema. Reject unknown "
            "fields when strict. Return a structured 400 on validation "
            "failure showing which field(s) + why.\n"
            "3. OUTPUT SCHEMAS: Responses have a stable, documented shape. "
            "Use envelope or direct-object consistently. Include pagination "
            "metadata for list endpoints.\n"
            "4. ERRORS: Every error response has `{code, message, detail, "
            "request_id}`. `code` is a machine-readable string like "
            "'user.not_found'. `message` is human-readable.\n"
            "5. VERSIONING: Choose `/v1/` prefix OR `Accept: "
            "application/vnd.app.v1+json`. Every breaking change bumps "
            "version. Maintain v1 until users migrate.\n"
            "6. OPENAPI SPEC: Auto-generate or hand-write an OpenAPI 3.x "
            "spec. Serve at `/openapi.json`. Add a Swagger UI at `/docs`.\n"
            "7. AUTHN/AUTHZ: Consistent scheme (Bearer JWT / API key / "
            "cookie). Every endpoint states its required scope(s).\n"
            "8. RATE LIMITING: Per-user and per-endpoint. Return `429` "
            "with `Retry-After`.\n"
            "9. IDEMPOTENCY: Non-safe methods accept `Idempotency-Key` "
            "header; same key + body = same response.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "state_management": {
        "label": "State Management",
        "description": (
            "Organizes all the 'what's happening right now' data in "
            "one clean place instead of scattered everywhere — makes "
            "debugging simple and enables undo/redo almost for free. "
            "Pick this for complex apps with lots of moving parts or "
            "whenever the code feels spaghetti because state is tangled."
        ),
        "prompt": (
            "REFACTOR STATE MANAGEMENT into a disciplined, debuggable model.\n\n"
            "1. SINGLE SOURCE OF TRUTH: All mutable state lives in one "
            "`AppState` dataclass (or a small set of them). No scattered "
            "globals, no ad-hoc class attributes drifting out of sync.\n"
            "2. IMMUTABLE BY DEFAULT: Use `@dataclass(frozen=True)` or a "
            "similar immutable model. Updates return a new state; mutation "
            "via `dataclasses.replace(state, field=new)`.\n"
            "3. EVENTS / REDUCERS: Every state change is an explicit "
            "`Event` (or action) dispatched through a reducer: "
            "`new_state = reduce(state, event)`. One reducer per aggregate.\n"
            "4. EVENT LOG: Keep the last N events so you can replay, "
            "debug, or implement undo/redo trivially.\n"
            "5. UNDO / REDO: With an event log + pure reducers, add "
            "`undo()` / `redo()` almost free.\n"
            "6. SUBSCRIBERS: UI / external listeners subscribe to state "
            "changes. No polling — push updates.\n"
            "7. SELECTORS: Derived values (e.g. `visible_items = [...]`) "
            "go through memoized selectors so recompute is cheap.\n"
            "8. TIME-TRAVEL DEBUGGING: A debug mode that logs every event "
            "with a timestamp and lets you replay to any point.\n"
            "9. PERSISTENCE: State can be snapshotted to disk and "
            "restored verbatim — useful for crash recovery + tests.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "game_loop": {
        "label": "Game Loop / Real-Time",
        "description": (
            "Builds a smooth, stutter-free frame loop for games or "
            "anything real-time — consistent physics regardless of "
            "frame-rate, no input lag, no 'spiral of death' when the "
            "app slows down. Pick this for games, simulations, "
            "animations, or anything running at 30–60 frames per second."
        ),
        "prompt": (
            "BUILD A PROPER REAL-TIME LOOP for this codebase.\n\n"
            "1. FIXED TIMESTEP: Simulation advances in fixed-dt ticks "
            "(e.g. 60 Hz). Decouple render from sim: render as often as "
            "the hardware allows; sim ticks at a constant rate.\n"
            "2. ACCUMULATOR: Standard pattern — `accumulator += frame_time; "
            "while accumulator >= dt: sim.step(dt); accumulator -= dt`. "
            "Leftover accumulator drives interpolation at render time.\n"
            "3. INTERPOLATION: Between sim ticks, render interpolates "
            "positions: `render_pos = prev_pos * (1-alpha) + cur_pos * "
            "alpha`. Smooth motion independent of sim rate.\n"
            "4. FRAME BUDGET: Tick has a budget (e.g. 16ms for 60fps). "
            "Profile what eats it. Cap variable-cost work (physics, AI).\n"
            "5. INPUT LAG: Sample input as late as possible before sim "
            "tick. Skip one-frame of interpolation on input to feel snappy.\n"
            "6. SPIRAL-OF-DEATH GUARD: If `frame_time` exceeds a cap "
            "(e.g. 250ms), clamp accumulator so the sim doesn't run "
            "1000 catch-up ticks.\n"
            "7. DETERMINISM: Sim is deterministic given the same input "
            "stream and seed — critical for replays, multiplayer, testing.\n"
            "8. PAUSE / SLOW-MO: Tick rate can be scaled or frozen via "
            "a `time_scale` multiplier applied to `dt`.\n"
            "9. PROFILER HOOKS: Instrument tick phases (input, sim, "
            "render, present) with per-phase timing displayed as an overlay.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "ai_behavior": {
        "label": "AI Behavior / Decision Making",
        "description": (
            "Gives characters, enemies, or autonomous agents smart "
            "decision-making — instead of hardcoded 'if this then "
            "that', they use state machines or behavior trees to "
            "react intelligently. Pick this for enemies in a game, "
            "NPCs, chatbots, or any bot that needs to make real-time "
            "decisions."
        ),
        "prompt": (
            "DESIGN ROBUST AUTONOMOUS BEHAVIOR for this codebase.\n\n"
            "1. PICK THE MODEL: Choose one primary style and commit — a "
            "finite state machine (FSM) for few discrete states, a "
            "behavior tree (BT) for composable reactive logic, or utility "
            "AI (score-and-pick) for continuous trade-offs.\n"
            "2. STATES / NODES: Each state/node has a clear name, an "
            "`enter`, `tick`, and `exit`. No state dumps all its logic "
            "in one blob.\n"
            "3. TRANSITIONS: Explicit and named. Draw them in a comment "
            "as an ASCII diagram at the top of the file. Reject "
            "ambiguous transitions at design time.\n"
            "4. PERCEPTION LAYER: Sensors gather world state once per "
            "tick into a `Blackboard`. Behavior reads from the "
            "blackboard — never from raw globals — so it's testable.\n"
            "5. ACTION LAYER: Actions are atomic (`MoveTo(x,y)`, "
            "`PickBestMove()`), report `RUNNING/SUCCESS/FAILURE`, and "
            "can be cancelled cleanly mid-execution.\n"
            "6. INTERRUPTIBILITY: Every long-running behavior can be "
            "interrupted by a higher-priority stimulus (damage, dialog, "
            "stuck). No behavior holds the agent hostage.\n"
            "7. DEBUGGABILITY: A live overlay shows current state + "
            "recent transitions + blackboard values. Logs show every "
            "decision with its reason.\n"
            "8. TESTING: Replay recorded perception streams through the "
            "behavior deterministically to regression-test decisions.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "save_system": {
        "label": "Save System",
        "description": (
            "Saves user progress reliably — automatic saves every few "
            "minutes, manual save slots, survives crashes without "
            "corrupting the save file, handles older save formats "
            "when the app updates. Pick this for games or any app "
            "where losing user work would be painful."
        ),
        "prompt": (
            "BUILD A ROBUST SAVE SYSTEM for this codebase.\n\n"
            "1. SERIALIZATION FORMAT: Pick one and document it. JSON for "
            "debuggability, msgpack/protobuf for size/speed. Avoid pickle "
            "unless trust boundary permits.\n"
            "2. VERSIONED SCHEMA: Every save includes a `schema_version`. "
            "Loader has an explicit migration table from v1→v2→v3. Never "
            "load an unknown version without erroring.\n"
            "3. ATOMIC WRITES: Write to `save.tmp`, fsync, then rename. "
            "If the process crashes mid-write, the old save survives "
            "intact.\n"
            "4. CHECKPOINTS: Automatic save at well-defined moments "
            "(level complete, pre-boss, map change). Manual save anytime "
            "(unless in an unsafe state).\n"
            "5. AUTOSAVE: Periodic autosave on a timer, to a rotating "
            "slot so one bad autosave can't corrupt history.\n"
            "6. MULTI-SLOT: User can pick between N save slots with "
            "metadata preview (name, timestamp, play-time, location).\n"
            "7. CORRUPTION RECOVERY: On load failure, fall back to the "
            "most recent good save. Keep a .bak of the previous save.\n"
            "8. SAVE-ANYWHERE vs CHECKPOINT-ONLY: Pick a policy and "
            "honor it consistently. Don't mix.\n"
            "9. CLOUD-SYNC PREP: Keep saves as self-contained files, "
            "not SQLite dbs with side-car files — easier to sync.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "packaging": {
        "label": "Packaging & Distribution",
        "description": (
            "Makes the app installable by regular users — they can "
            "`pip install` it, run a `.exe`, or pull a Docker image — "
            "no need to be a developer. Pick this when ready to share "
            "the app with people who won't (or can't) build it from "
            "source themselves."
        ),
        "prompt": (
            "MAKE THIS CODEBASE DISTRIBUTABLE.\n\n"
            "1. `pyproject.toml`: Fill in project name, version, "
            "description, authors, license, Python constraint, runtime "
            "deps, dev-deps, and classifiers. Follow PEP 621.\n"
            "2. ENTRY POINTS: Define console scripts so users get a "
            "`myapp` command on install (`[project.scripts]` table).\n"
            "3. PINNED DEPS: Runtime deps use compatible version ranges "
            "(`>=X.Y,<X+1`). Dev deps can be tighter. Include a "
            "`requirements-lock.txt` for reproducible builds.\n"
            "4. PACKAGE DATA: Non-Python files (templates, icons, data) "
            "declared in `[tool.setuptools.package-data]` — otherwise they "
            "won't ship.\n"
            "5. BUILD BACKEND: `hatchling` or `setuptools` configured. "
            "Output both wheel and sdist. `python -m build` works from "
            "clean.\n"
            "6. DOCKERFILE: Multi-stage build (builder → runtime). Non-"
            "root user. Minimal base image. Pinned image digest. Health "
            "check.\n"
            "7. STANDALONE BINARY (optional): PyInstaller / Briefcase / "
            "Nuitka spec for users who don't have Python. Document "
            "platform matrix.\n"
            "8. INSTALL DOCS: README has `pip install myapp` + Docker "
            "one-liner + local-dev instructions.\n"
            "9. CHANGELOG + SEMVER: `CHANGELOG.md` with a clear entry "
            "per release; semver bumps match the change.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "onboarding_firstrun": {
        "label": "Onboarding & First-Run",
        "description": (
            "Guides new users through their first experience — a "
            "welcome flow, a short tutorial, helpful empty-state "
            "messages ('no items yet — try clicking here'), tooltips "
            "that explain what each thing does. Pick this when users "
            "open the app and don't know what to do first."
        ),
        "prompt": (
            "ADD A FIRST-RUN EXPERIENCE that makes this app feel "
            "welcoming to a brand-new user.\n\n"
            "1. DETECT FIRST RUN: Track whether this is the user's "
            "first launch (a marker file, a setting, or empty state). "
            "Show the onboarding flow only then.\n"
            "2. WELCOME SCREEN: Short, friendly intro — what the app "
            "does in 1-2 sentences, a primary action to get started.\n"
            "3. INTERACTIVE TUTORIAL: Walk through the 3-5 most-used "
            "features with tooltips/spotlights. Skippable at any time.\n"
            "4. EMPTY STATES: Every list/table/panel that starts empty "
            "gets a friendly explanation + a call-to-action button "
            "('No projects yet — Create your first').\n"
            "5. PROGRESSIVE DISCLOSURE: Advanced settings are hidden "
            "behind a 'Show advanced' toggle. Don't overwhelm.\n"
            "6. CONTEXTUAL TOOLTIPS: Hover hints on non-obvious "
            "controls. A '?' help icon next to complex features.\n"
            "7. SAMPLE DATA: One-click 'Load example project' so "
            "users can explore features with realistic content "
            "instead of an empty app.\n"
            "8. RE-ENABLE: Let users re-trigger the tutorial from a "
            "menu ('Show tutorial again') without having to reinstall.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "import_export": {
        "label": "Import / Export",
        "description": (
            "Lets users move data in and out of the app — import "
            "CSV/JSON files, drag-and-drop, copy-paste from clipboard, "
            "export in common formats. Pick this when users need to "
            "bring in existing data or send results to another tool."
        ),
        "prompt": (
            "ADD IMPORT AND EXPORT CAPABILITIES to this codebase.\n\n"
            "1. FORMAT SUPPORT: Identify the core data shapes the app "
            "owns. For each, support import + export in the most "
            "common formats: CSV, JSON, and one binary/compact format "
            "(msgpack, Parquet). Excel (.xlsx) where it fits.\n"
            "2. DRAG-AND-DROP: If there's a GUI, support drop-to-"
            "import on relevant surfaces. Accept multiple files at once.\n"
            "3. CLIPBOARD: 'Copy as JSON' / 'Paste from clipboard' for "
            "quick ad-hoc interchange.\n"
            "4. STREAMING: Large files import as a stream with a "
            "progress bar and cancel button — never freeze the UI.\n"
            "5. VALIDATION: Parse strictly. If a row is malformed, "
            "skip it with a reported warning; don't silently corrupt "
            "the destination. Show a summary of successes/skips at the "
            "end.\n"
            "6. CHARACTER ENCODING: Default to UTF-8 but sniff BOM / "
            "common encodings on import. Export uses UTF-8 with BOM "
            "on Windows for Excel compatibility.\n"
            "7. ROUND-TRIP FIDELITY: Exporting then re-importing the "
            "same data produces the same result. Test this.\n"
            "8. COMMAND-LINE: `app import <file>` and `app export "
            "<dest>` for scripting, mirroring the GUI behavior.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "search_filter": {
        "label": "Search & Filter",
        "description": (
            "Adds fast search and filtering so users can find things "
            "in big lists — text search, fuzzy matching, multi-field "
            "filters, sorting. Pick this when users are scrolling "
            "through lists or can't find what they're looking for."
        ),
        "prompt": (
            "ADD SEARCH AND FILTER CAPABILITIES wherever users browse "
            "lists, tables, or collections.\n\n"
            "1. INSTANT SEARCH: A visible search box. As the user "
            "types, results filter live (debounced ~200ms so it's "
            "smooth). Case-insensitive, matches anywhere in the text.\n"
            "2. FUZZY MATCHING: Support small typos via fuzzy match "
            "(fzf / Levenshtein / token). 'prjct' finds 'project'.\n"
            "3. FIELD-SPECIFIC FILTERS: For structured data, let users "
            "filter by each column: dropdown for categories, date "
            "range pickers, min/max for numbers.\n"
            "4. COMBINED FILTERS: Multiple filters stack (AND logic). "
            "A 'Clear all filters' button is always visible when any "
            "are active.\n"
            "5. SAVED SEARCHES: Users can save a filter combo as a "
            "named preset ('My open tasks') and recall it.\n"
            "6. HIGHLIGHTING: Matching text in results is highlighted "
            "so users see WHY each result matched.\n"
            "7. SORT: Every column is sortable (asc/desc). Remember "
            "last sort across sessions.\n"
            "8. EMPTY STATE: 'No results for X' with suggestions to "
            "relax filters. Never a blank screen.\n"
            "9. PERFORMANCE: For large lists (>10K items), indexing "
            "so search stays snappy (<100ms).\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "shortcuts_hotkeys": {
        "label": "Keyboard Shortcuts",
        "description": (
            "Makes the app fast for power users — hotkeys for common "
            "actions, a command palette (Ctrl+K) that searches every "
            "feature, a cheat-sheet showing all shortcuts. Pick this "
            "when power users want keyboard-first workflows."
        ),
        "prompt": (
            "ADD A KEYBOARD-FIRST EXPERIENCE for power users.\n\n"
            "1. SHORTCUT MAP: Every common action gets a shortcut. "
            "Follow platform conventions (Ctrl+S save, Ctrl+Z undo). "
            "Use modifier+letter, never just a letter (conflicts with "
            "typing).\n"
            "2. COMMAND PALETTE: Ctrl+K / Cmd+K opens a searchable "
            "list of every command. As the user types, fuzzy-match "
            "commands by name. Enter executes.\n"
            "3. DISCOVERABILITY: Every menu item and button shows its "
            "shortcut next to its label. A '?' overlay lists every "
            "shortcut.\n"
            "4. CONTEXT-AWARE: Shortcuts change by active view — "
            "editor shortcuts when editing, list-view shortcuts when "
            "browsing. No conflicts.\n"
            "5. CUSTOMIZABLE: Users can rebind shortcuts via a "
            "settings panel. Persist rebinds across sessions.\n"
            "6. CHORDS: Support 2-key sequences (Ctrl+K then Ctrl+S) "
            "for advanced flows à la VS Code.\n"
            "7. VIM / EMACS MODE (optional): If your power users "
            "would love it, offer a vim/emacs keybinding set.\n"
            "8. NO CONFLICTS WITH BROWSER / OS: Avoid overriding "
            "Ctrl+W, Ctrl+T, Ctrl+N when running in a browser.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "notifications_feedback": {
        "label": "Notifications & Feedback",
        "description": (
            "Adds clear feedback for every user action — success "
            "toasts, error messages, confirmation dialogs before "
            "destructive actions, loading spinners during waits. "
            "Pick this when users say 'I don't know if it worked' "
            "or actions feel silent."
        ),
        "prompt": (
            "MAKE EVERY USER ACTION GIVE CLEAR FEEDBACK in this app.\n\n"
            "1. TOAST NOTIFICATIONS: Short, non-blocking messages in "
            "a corner — success (green), warning (yellow), error "
            "(red), info (blue). Auto-dismiss after 3-5s, pinnable "
            "for errors. Keyboard-accessible.\n"
            "2. CONFIRMATION DIALOGS: Before any destructive action "
            "(delete, overwrite, discard changes), show a modal with "
            "a clear summary + 'Cancel' as the default-focused button "
            "so accidental Enter doesn't confirm.\n"
            "3. LOADING STATES: Any action >200ms shows a spinner or "
            "skeleton loader. Any action >2s shows an explicit progress "
            "bar with what's happening.\n"
            "4. ERROR MESSAGES: When something fails, tell the user "
            "(a) what broke in plain language, (b) what they can do "
            "about it, (c) how to report the issue if not fixable.\n"
            "5. UNDO TOASTS: For destructive actions that are "
            "reversible (delete with undo support), show a toast with "
            "'Deleted — [Undo]' instead of asking for confirmation.\n"
            "6. INLINE VALIDATION: Form errors appear next to the "
            "broken field as the user types, not only on submit.\n"
            "7. AUTO-SAVE FEEDBACK: When auto-saving, show a subtle "
            "'Saved' indicator. Never a modal.\n"
            "8. SOUND (optional): A subtle success/error chime where "
            "appropriate, respecting a user mute toggle.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "authentication": {
        "label": "Authentication / Login",
        "description": (
            "Adds a secure login system — signup, login, password "
            "reset, 'stay signed in', optional Google/GitHub sign-in. "
            "Pick this when the app has user accounts or needs to "
            "remember who's using it across sessions."
        ),
        "prompt": (
            "ADD A PRODUCTION-GRADE AUTHENTICATION SYSTEM.\n\n"
            "1. SIGNUP + LOGIN: Email + password with strong password "
            "rules (min 12 chars, common-password blocklist). Signup "
            "requires email verification.\n"
            "2. PASSWORD HANDLING: Store only bcrypt/argon2 hashes "
            "(never plain or reversible). Support password reset via "
            "a one-time email token that expires in 1 hour.\n"
            "3. SESSIONS / JWT: Chose sessions (cookie + server store) "
            "OR JWT (stateless). Document the choice. Short access-"
            "token TTL + long refresh-token TTL with rotation.\n"
            "4. REMEMBER ME: Long-lived refresh cookie on opt-in. "
            "Secure + HttpOnly + SameSite=Lax flags.\n"
            "5. OAUTH / SSO: Add Sign-in-with-Google / GitHub / Apple "
            "behind a feature flag. Map OAuth accounts to local user "
            "records safely (verify email match).\n"
            "6. MFA (optional but recommended): TOTP-based 2FA with "
            "recovery codes. Clear setup flow.\n"
            "7. LOGOUT: Invalidate session server-side; clear cookies; "
            "redirect to a clean state. 'Log out everywhere' option.\n"
            "8. ACCOUNT MANAGEMENT: Change email, change password, "
            "delete account. All destructive ops require re-auth.\n"
            "9. RATE LIMITING: Login attempts throttled per IP + per "
            "account. Lockout with exponential backoff on abuse.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "authorization_permissions": {
        "label": "Permissions & Roles",
        "description": (
            "Controls who can do what — user roles (admin/member/"
            "viewer), resource-level permissions, sharing settings. "
            "Pick this when different users should see different "
            "things, or you're building multi-user collaboration."
        ),
        "prompt": (
            "ADD A PERMISSIONS SYSTEM so different users get different "
            "access levels.\n\n"
            "1. ROLE MODEL: Define explicit roles (e.g. admin, owner, "
            "editor, viewer). Every user has a role. Roles are data, "
            "not hardcoded — an admin can create new roles.\n"
            "2. RESOURCE-LEVEL: For every resource (project, "
            "document, workspace), track who owns it and who's "
            "shared-with + their role on that resource. A user can be "
            "owner on one thing and viewer on another.\n"
            "3. PERMISSION CHECKS: Every protected endpoint / handler "
            "calls a central `can(user, action, resource)` function. "
            "Business logic never inlines `if user.is_admin`.\n"
            "4. UI AWARENESS: Buttons for actions the current user "
            "can't do are hidden or disabled with an explanation on "
            "hover — never show a button that errors on click.\n"
            "5. SHARING: Owners can invite other users with a role, "
            "revoke access, transfer ownership. Every change is audit-"
            "logged.\n"
            "6. AUDIT LOG: Permission changes + sensitive actions "
            "(export, delete) are logged with user, timestamp, target.\n"
            "7. DEFAULTS: Conservative by default — new users see only "
            "what they're explicitly shared on.\n"
            "8. API LAYER: Every API endpoint documents required role "
            "and enforces it consistently.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "background_jobs": {
        "label": "Background Jobs & Scheduling",
        "description": (
            "Runs work in the background — scheduled tasks (daily "
            "reports, cleanup), delayed jobs (retry after 5 min), "
            "queue workers for heavy processing. Pick this for long "
            "operations that shouldn't block users or recurring tasks."
        ),
        "prompt": (
            "ADD A BACKGROUND JOB SYSTEM to this codebase.\n\n"
            "1. JOB QUEUE: A durable queue (Redis/RQ/Celery/native) "
            "where long-running tasks go. Workers pull and execute. "
            "UI queues the job and returns immediately.\n"
            "2. JOB TYPES: Distinguish (a) immediate background jobs "
            "(kicked off by user action), (b) scheduled / cron jobs "
            "(daily reports, cleanup), (c) delayed jobs (retry in 5 "
            "minutes), (d) periodic jobs (every N seconds).\n"
            "3. IDEMPOTENCY: Jobs are idempotent — running twice = "
            "running once. Use idempotency keys / check-before-write.\n"
            "4. RETRIES: Failed jobs retry with exponential backoff "
            "up to N times. After exhaustion, move to a dead-letter "
            "queue with the error + context for inspection.\n"
            "5. PROGRESS TRACKING: Long jobs update a `progress` field "
            "(0.0-1.0 + status message) the UI can poll or subscribe "
            "to. A 'Your job is done' notification when complete.\n"
            "6. CANCELLATION: User can cancel a queued job. Running "
            "jobs check a cancel flag periodically and exit cleanly.\n"
            "7. TIMEOUTS: Every job has a max wall-clock time. "
            "Timeout kills it and marks it failed — not silently stuck.\n"
            "8. ADMIN UI: A page that shows active/queued/failed "
            "jobs, with retry/cancel/inspect actions.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "monitoring_alerting": {
        "label": "Monitoring & Alerting",
        "description": (
            "Tracks the app's health in production — dashboards, "
            "alerts when things break, SLO (service-level objective) "
            "measurements, error-rate tracking. Pick this when the "
            "app runs in production and you can't be glued to the "
            "logs 24/7."
        ),
        "prompt": (
            "ADD PRODUCTION MONITORING AND ALERTING to this codebase.\n\n"
            "1. METRICS: Export Prometheus-style counters, gauges, "
            "histograms at `/metrics`. Track: request count by "
            "endpoint, error rate, latency (p50/p95/p99), queue depth, "
            "active sessions, business KPIs.\n"
            "2. HEALTH ENDPOINTS: `/healthz` (liveness — am I alive?), "
            "`/readyz` (readiness — can I serve traffic? all upstreams "
            "OK?). Return structured JSON with subsystem status.\n"
            "3. STRUCTURED LOGS: Every log line is JSON with "
            "timestamp, level, logger, message, and trace_id. Pipe to "
            "stdout — let the orchestrator handle shipping.\n"
            "4. DISTRIBUTED TRACING: OpenTelemetry instrumentation "
            "around HTTP handlers, DB queries, outbound calls. Each "
            "request gets a trace_id propagated through.\n"
            "5. SLO DEFINITIONS: Write down the app's SLOs (e.g. "
            "'99.9% of requests <500ms'). Monitor actual vs. target. "
            "Alert on error budget burn.\n"
            "6. ALERTS: Define pager-worthy alerts — error rate "
            ">5% for 5min, p95 latency >2s for 10min, queue depth "
            ">1000 for 5min. Include playbook link in alert.\n"
            "7. DASHBOARDS: Provide a Grafana dashboard JSON that "
            "visualizes golden signals (latency, traffic, errors, "
            "saturation) — so ops can see the system at a glance.\n"
            "8. RUNBOOK: Markdown doc for common alerts: 'if X fires, "
            "check Y, then Z'.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "type_safety": {
        "label": "Type Safety",
        "description": (
            "Adds type information everywhere so mistakes are caught "
            "in the editor instead of when running — your code editor "
            "can warn you 'this function expects a number, you gave a "
            "word' before you hit Run. Pick this when you want "
            "catches-bugs-before-they-ship confidence and smarter "
            "autocomplete while typing."
        ),
        "prompt": (
            "MAKE THIS CODEBASE FULLY TYPE-SAFE.\n\n"
            "1. FULL ANNOTATIONS: Every function signature, every "
            "class attribute, every module-level constant gets a "
            "proper type annotation. No `Any` unless truly necessary "
            "(and commented why).\n"
            "2. STRICT CHECKER CONFIG: Configure mypy (or pyright) "
            "with `--strict`. Add a `mypy.ini` / `pyproject.toml` "
            "section. Zero errors at the end.\n"
            "3. DOMAIN TYPES: Replace raw `str` / `int` with "
            "`NewType` for identifiers (`UserId = NewType('UserId', "
            "int)`) so you can't accidentally pass a `ProjectId` "
            "where a `UserId` is expected.\n"
            "4. LITERAL TYPES + ENUMS: Replace string flags with "
            "`Literal['asc', 'desc']` or `enum.Enum`. The checker "
            "then rejects typos at edit time.\n"
            "5. PROTOCOLS: For pluggable interfaces, use "
            "`typing.Protocol` instead of ABCs — better for testing "
            "with duck-typed mocks.\n"
            "6. TYPED DICTS / DATACLASSES: Replace free-form dicts "
            "with `TypedDict` or frozen dataclasses so the shape is "
            "enforced and autocompletes.\n"
            "7. GENERICS: Use `list[T]`, `dict[K, V]`, `Iterator[T]`, "
            "`Callable[..., T]` where the element type matters. "
            "Avoid bare `list` / `dict`.\n"
            "8. RUNTIME VALIDATION: At trust boundaries (API "
            "deserialization, config loading), validate with "
            "`pydantic` so static types + runtime shape agree.\n"
            "9. CI GATE: `mypy` runs on every PR and must pass.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "modular_refactor": {
        "label": "Modular Architecture",
        "description": (
            "Splits the codebase into clean, independent modules/"
            "packages with clear boundaries — so features can evolve "
            "separately and the whole thing is easier to navigate. "
            "Pick this when the codebase has grown into a giant file "
            "or everything depends on everything."
        ),
        "prompt": (
            "REFACTOR THIS CODEBASE INTO CLEAN MODULES / PACKAGES.\n\n"
            "1. IDENTIFY BOUNDED CONTEXTS: Find the major areas of "
            "responsibility (e.g. 'users', 'billing', 'search', "
            "'notifications'). Each becomes a package.\n"
            "2. PACKAGE LAYOUT: Each package has its own `__init__.py` "
            "exporting a stable public API. Internal helpers stay "
            "internal (`_private.py` or `_helpers/`). No cross-"
            "package reaches into private modules.\n"
            "3. DEPENDENCY DIRECTION: Packages form a DAG. Core "
            "(domain) packages depend on nothing. Infrastructure "
            "(DB, HTTP) depends on core. App layer wires them "
            "together. No cycles.\n"
            "4. EXPLICIT INTERFACES: Where a package needs something "
            "from another, the consumer defines a `Protocol` / ABC, "
            "and the provider implements it. Injection at the edges, "
            "not tight coupling.\n"
            "5. SHARED KERNEL: Common types (domain entities, error "
            "types) live in a small, rarely-changing `shared/` or "
            "`core/` package.\n"
            "6. NO CIRCULAR IMPORTS: A linter enforces zero cycles. "
            "If two things need each other, extract the shared piece.\n"
            "7. ENTRY POINTS: Top-level `main.py` / `app.py` / "
            "`cli.py` is the only place that wires everything together "
            "— everything else is a library.\n"
            "8. TESTS CO-LOCATED: Each package has its own `tests/` "
            "folder matching its structure.\n\n"
            "Apply to the CURRENT CODEBASE below. If it's already one "
            "file, produce a multi-file layout with clear package "
            "boundaries. Output ENTIRE codebase."
        ),
    },
    "animations_transitions": {
        "label": "Animations & Transitions",
        "description": (
            "Adds smooth visual transitions — fade-ins, slide-ups, "
            "state-change animations — so the UI feels polished "
            "instead of jumpy. Pick this when UI changes feel "
            "abrupt or visual feedback is lacking."
        ),
        "prompt": (
            "ADD SMOOTH ANIMATIONS AND TRANSITIONS to this app.\n\n"
            "1. TRANSITION MOMENTS: Identify every state change a "
            "user sees — panel open/close, list add/remove, "
            "navigation, modal in/out. Each gets a tasteful motion.\n"
            "2. EASING: Use ease-in-out or similar, never linear "
            "(feels mechanical). Short durations (150-300ms) for UI "
            "interactions; longer (300-500ms) for major transitions.\n"
            "3. TWEENS: Implement a reusable tween system "
            "(value-over-time interpolator) so animations are one-"
            "liners: `tween(obj.alpha, 0→1, 200ms, ease_out)`.\n"
            "4. STATE MACHINE: UI animations driven by a small FSM — "
            "each state has enter/exit animations, no conflicts.\n"
            "5. NO LAYOUT THRASH: Prefer `transform` / `opacity` "
            "(GPU-accelerated) over width/height/margin (trigger "
            "layout). Target 60fps.\n"
            "6. REDUCE-MOTION: Honor the user's 'reduce motion' "
            "system setting — swap animations for instant transitions.\n"
            "7. LOADING ANIMATIONS: Skeleton screens for slow loads; "
            "shimmer effect rather than spinners for content-shaped "
            "placeholders.\n"
            "8. MICRO-INTERACTIONS: Button press, toggle flip, icon "
            "morph — small touches that make the UI feel alive.\n"
            "9. ANIMATION KILL-SWITCH: Global 'animations off' for "
            "testing / low-end devices / accessibility.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "prompt_engineering": {
        "label": "LLM / Prompt Engineering",
        "description": (
            "For apps that talk to Large Language Models — crafts "
            "system prompts, manages context size, handles streaming "
            "responses, retries and token budgets. Pick this when "
            "the app uses ChatGPT, Claude, Gemini, or any LLM API."
        ),
        "prompt": (
            "BUILD A ROBUST LLM INTEGRATION LAYER for this codebase.\n\n"
            "1. PROMPT TEMPLATES: Every LLM call uses a named, "
            "versioned template (`prompts/summarize_v3.md`) — not "
            "f-strings scattered in code. Easy to A/B test.\n"
            "2. SYSTEM PROMPT: A clear role + constraints + output "
            "format. Test with adversarial inputs before shipping.\n"
            "3. TOKEN BUDGET: Measure input + expected output against "
            "the model's context window. When inputs are large, apply "
            "a chunking or summarization strategy up front.\n"
            "4. STREAMING: For user-facing chat, stream the response "
            "token-by-token via the model's SSE/streaming API. Render "
            "as it arrives.\n"
            "5. RETRIES + FALLBACK: 429/500 errors retry with "
            "backoff. If primary model is down, fallback to a "
            "secondary model with equivalent prompt. Log which path.\n"
            "6. STRUCTURED OUTPUT: When you need JSON, use the "
            "model's native JSON mode / function-calling — not "
            "'please return JSON' prose. Validate against a schema on "
            "receipt.\n"
            "7. COST + LATENCY METRICS: Log input tokens, output "
            "tokens, latency, cost per call. Surface top spenders in "
            "a dashboard.\n"
            "8. PROMPT INJECTION DEFENSE: Sanitize user-provided text "
            "before it enters system prompts. Treat model output as "
            "untrusted when it drives actions.\n"
            "9. CACHING: Identical (prompt, params, model) = cached "
            "response. Respect cacheability flags.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "rag_vector_search": {
        "label": "RAG / Semantic Search",
        "description": (
            "Adds meaning-based search — users can ask questions in "
            "plain English and find relevant info, not just exact "
            "matches. Uses embeddings + a vector database. Pick this "
            "for AI-powered Q&A, 'chat with your docs', or smart "
            "recommendations."
        ),
        "prompt": (
            "BUILD A RETRIEVAL-AUGMENTED GENERATION (RAG) system on "
            "top of this codebase's content.\n\n"
            "1. CORPUS: Identify the content to make searchable — "
            "docs, DB rows, file contents. Pipeline to extract, "
            "clean, and chunk into ~500-1000 token passages with "
            "overlap.\n"
            "2. EMBEDDING: Use an embedding model (OpenAI / Cohere / "
            "local SentenceTransformers). Embed every chunk once; "
            "store vectors + metadata + original text.\n"
            "3. VECTOR DB: Choose one (chromadb, pgvector, qdrant, "
            "faiss). Build a single index. Support incremental add/"
            "update/delete as content changes.\n"
            "4. QUERY FLOW: (a) embed the user question, (b) "
            "retrieve top-K similar chunks via cosine/dot, (c) "
            "re-rank with a cross-encoder for quality, (d) pass top-N "
            "to the LLM as context.\n"
            "5. CITATIONS: Every answer cites which chunks / "
            "documents it used so users can verify. Link back to the "
            "source.\n"
            "6. HYBRID SEARCH: Combine vector search with keyword "
            "(BM25) for better recall on acronyms/names. Reciprocal "
            "rank fusion for merging.\n"
            "7. EVALUATION: A small eval set of (question, expected "
            "doc) pairs. Measure hit-rate@5, MRR. Regression-test on "
            "every change.\n"
            "8. GUARDRAILS: If retrieval quality is low (scores "
            "below a threshold), say 'I don't have info on that' "
            "instead of hallucinating.\n"
            "9. PRIVACY: Per-user permissions on what chunks are "
            "searchable. Strict row-level filtering.\n\n"
            "Apply to the CURRENT CODEBASE below. Output ENTIRE codebase."
        ),
    },
    "ci_cd": {
        "label": "Automated Build & Release",
        "description": (
            "Sets up automation that runs all your tests on every "
            "code change and auto-publishes releases — catches bugs "
            "before they ship and takes the manual work out of "
            "releasing. Pick this once you have tests worth running "
            "and a project you want to release regularly."
        ),
        "prompt": (
            "AUTOMATE BUILD / TEST / RELEASE for this codebase.\n\n"
            "1. CI ON EVERY PR: A GitHub Actions (or equivalent) workflow "
            "that runs on push + PR: install deps, lint, type-check, "
            "tests with coverage, build artifacts.\n"
            "2. MATRIX BUILDS: Run against every supported Python version "
            "(and OS if platform-specific) in parallel. Fail-fast on any.\n"
            "3. LINT + FORMAT: `ruff check` + `black --check` + "
            "`isort --check-only`. PR fails if not formatted. Pre-commit "
            "hook locally mirrors CI.\n"
            "4. TYPE CHECK: `mypy` (or `pyright`) with `--strict` on "
            "new code; grandfather existing. Fail CI on new errors.\n"
            "5. COVERAGE GATE: Tests must pass AND coverage ≥ threshold "
            "(e.g. 80% for touched lines). Upload to codecov/similar.\n"
            "6. SECURITY SCANS: `pip-audit` + `bandit` on every run. "
            "Fail on high-severity CVEs.\n"
            "7. RELEASE AUTOMATION: Tag-triggered workflow builds + "
            "publishes to PyPI/registry + builds Docker image + creates "
            "a GitHub Release with auto-generated notes.\n"
            "8. CACHED DEPS: Cache pip/uv/poetry downloads between runs "
            "so CI is fast.\n"
            "9. ARTIFACTS: Every build uploads the wheel + sdist + "
            "coverage report as artifacts for inspection.\n\n"
            "Provide the .github/workflows/*.yml files inline + the "
            "`.pre-commit-config.yaml`. Apply to the CURRENT CODEBASE. "
            "Output ENTIRE codebase + the workflow files."
        ),
    },
}

# Default order for focus cycling — 46 dimensions a coder iterates on for
# Cursor-level apps, programs, and games. Early rounds = foundation work,
# later rounds = polish / deployment / hygiene (once foundation is solid).
FOCUS_ORDER = [
    # Foundation & correctness (5)
    "deep_dive", "modular_refactor", "solid_functional", "pressure_test",
    "error_recovery",
    # Code quality & review (3)
    "type_safety", "review_grade", "test_suite",
    # Capability expansion (4)
    "extra_features", "explore_expand", "plugin_system", "integration_layer",
    # Non-functional properties (6)
    "performance", "memory_optimization", "security",
    "concurrency", "network_resilience", "caching_strategy",
    # Data & APIs (4)
    "data_layer", "api_design", "state_management", "background_jobs",
    # Auth & multi-user (2)
    "authentication", "authorization_permissions",
    # AI-specific (2)
    "prompt_engineering", "rag_vector_search",
    # Observability & configurability (3)
    "logging_observability", "configuration", "monitoring_alerting",
    # User-facing features (5)
    "onboarding_firstrun", "import_export", "search_filter",
    "shortcuts_hotkeys", "notifications_feedback",
    # Presentation & UX (6)
    "beautiful_gui", "reference_images", "animations_transitions",
    "accessibility", "i18n_l10n", "cli_ux",
    # Game & real-time (3)
    "game_loop", "ai_behavior", "save_system",
    # Hygiene & deployment (3)
    "documentation", "packaging", "ci_cd",
]
# Safety: filter to only keys that exist (in case we miss a rename)
FOCUS_ORDER = [k for k in FOCUS_ORDER if k in IMPROVEMENT_FOCUSES]
assert len(FOCUS_ORDER) == len(IMPROVEMENT_FOCUSES), (
    f"FOCUS_ORDER has {len(FOCUS_ORDER)} but IMPROVEMENT_FOCUSES has "
    f"{len(IMPROVEMENT_FOCUSES)}. Missing: "
    f"{set(IMPROVEMENT_FOCUSES) - set(FOCUS_ORDER)}"
)

# Default focuses when none are selected — small, broadly-applicable starter set
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

    The task description is prepended to every focus directive so the AI
    interprets generic focus prompts (e.g. "Add 2-3 features") through the
    lens of the actual task. Without this, a focus that suggests "undo/redo,
    keyboard shortcuts" gets applied to a headless backend and produces
    nonsense or pushback instead of code.

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

    # Trim task to stay within prompt budget — we only need enough to
    # anchor the AI to the right domain.
    task_snippet = task.strip()
    if len(task_snippet) > 1_200:
        task_snippet = task_snippet[:1_200].rstrip() + " …"

    prompt_parts = []
    if ai_name:
        prompt_parts.append(f"[You are {ai_name}]")
    prompt_parts.append(
        f"TASK CONTEXT (what the overall project is):\n{task_snippet}\n"
    )
    prompt_parts.append(
        "IMPROVEMENT DIRECTIVE FOR THIS ITERATION:\n"
        "Apply the focus below to the current codebase WITHIN the domain "
        "of the project described above. Skip any example from the focus "
        "list that doesn't apply to this kind of codebase (e.g. skip GUI "
        "suggestions for a headless script) — pick only items that make "
        "genuine sense for this specific project."
    )
    prompt_parts.append(focus["prompt"])
    prompt = "\n\n".join(prompt_parts)

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
        # Graceful-end flag: lets the user request a clean wrap-up.
        # When set, the loop finishes the current iteration, then produces
        # either a FINAL consolidated version (if >33% done) or a HANDOFF
        # document (if <33% done) describing what remains, then exits.
        self._graceful_end = threading.Event()
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
        self._file_list: list[dict[str, str]] = []  # Raw file dicts for smart selection
        self._chunks: list[dict[str, Any]] = []  # File → chunks cache for retrieval

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
        self._graceful_end.clear()
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
            self._file_list = files
            self._chunks = []  # Reset chunk cache — will rebuild on first query
            self._file_context = self._format_file_context(files)
            if files:
                logger.info("Loaded %d attached files (%d chars of context)",
                            len(files), len(self._file_context))
                if self._on_status:
                    self._on_status(f"Loaded {len(files)} reference files")
        else:
            self._file_list = []
            self._chunks = []
            self._file_context = ""

        # Engineer the initial prompt with smart file context embedding.
        # Gemini has no file system access — reference files must be embedded
        # directly. Since the full context (256K+) exceeds Gemini's ~32K input
        # limit, we use smart prioritization: files named in the task prompt
        # get full content included, lower-priority files get a manifest entry.
        # Retrieval-augmented: extract the highest-relevance SECTIONS from
        # all 26 files (functions, doc sections) based on the task text.
        # Lets us pack content from many more files than whole-file embedding
        # because we skip the irrelevant sections of each file entirely.
        embedded_context = self._build_retrieval_context(
            budget=28_000,
            query=config.task,
            focus_name="deep_dive",
            task_prompt=config.task,
        )
        if embedded_context:
            logger.info("Initial prompt file context: %d chars "
                        "(manifest + prioritized files)", len(embedded_context))

        task_with_context = config.task
        if embedded_context:
            task_with_context = (
                f"{embedded_context}\n\n"
                f"═══════════════ YOUR TASK ═══════════════\n"
                f"{config.task}"
            )

        engineered = engineer_prompt(
            task=task_with_context,
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

    def request_graceful_end(self) -> None:
        """Ask the broadcast to wrap up cleanly after the current iteration.

        Unlike `stop()` (which halts mid-flight), graceful_end lets the AI:
        - finish whatever it's generating right now, then
        - produce ONE final output:
            - if the project is > ~1/3 done: a consolidated FINAL version
              of the total idea
            - if < 1/3 done: a HANDOFF document describing what's done,
              what's remaining, and what the last iteration was working
              on — so a human or another run can pick up the thread

        The session loop polls this flag after each iteration completes.
        """
        logger.info("Graceful-end requested — will wrap up after current iteration")
        self._graceful_end.set()

    def _build_graceful_end_prompt(self, task: str, codebase: str,
                                   completion_pct: int) -> str:
        """Prompt Gemini to produce the final wrap-up output.

        Two modes:
        - FINAL MODE (>=33%): tie everything together, remove stubs,
          produce a production-ready consolidated version.
        - HANDOFF MODE (<33%): document what's done, what's missing, and
          the next concrete steps so someone else can continue.
        """
        if completion_pct >= 33:
            return (
                "GRACEFUL END — FINAL CONSOLIDATED VERSION\n\n"
                f"This is the LAST iteration for this project. Based on the "
                f"ORIGINAL TASK and the CURRENT CODEBASE below, produce ONE "
                f"final polished version that ties everything together.\n\n"
                f"Estimated completion: {completion_pct}% (>33% threshold → "
                f"produce a finished-enough consolidated version, not a "
                f"handoff doc).\n\n"
                f"REQUIREMENTS:\n"
                f"1. Remove all TODO / STUB / placeholder markers — either "
                f"implement them with a reasonable default or delete them.\n"
                f"2. Ensure every function is connected and callable. Delete "
                f"dead code.\n"
                f"3. Add a top-of-file docstring summarizing what this "
                f"program does, how to run it, and its key limitations.\n"
                f"4. Add a short 'FINAL STATUS' comment block near the top "
                f"listing what's fully working, what's partially working, "
                f"and what's out of scope.\n"
                f"5. If anything genuinely requires external data that "
                f"isn't available (like the file references mentioned in "
                f"the reference material), leave a clear `raise "
                f"NotImplementedError('needs X')` rather than a silent stub.\n"
                f"6. Output the ENTIRE polished codebase. No commentary "
                f"outside the code.\n\n"
                f"ORIGINAL TASK:\n{task}\n\n"
                f"CURRENT CODEBASE (will be replaced by your output):\n"
                f"```\n{codebase}\n```"
            )
        return (
            "GRACEFUL END — HANDOFF DOCUMENT\n\n"
            f"This is the LAST iteration for this project. Based on the "
            f"ORIGINAL TASK and the CURRENT CODEBASE below, produce a "
            f"HANDOFF DOCUMENT so someone else can pick up where this left "
            f"off.\n\n"
            f"Estimated completion: {completion_pct}% (<33% threshold → "
            f"produce a handoff doc, not a final version — the project is "
            f"closer to the start than to finished).\n\n"
            f"OUTPUT FORMAT (one document, in this order):\n"
            f"1. === PROJECT OVERVIEW ===\n"
            f"   One paragraph: what this project is supposed to be, per "
            f"the original task.\n"
            f"2. === COMPLETION STATUS ===\n"
            f"   Estimated {completion_pct}% complete. Brief justification.\n"
            f"3. === WHAT'S DONE ===\n"
            f"   Bullet list of features / modules that work end-to-end.\n"
            f"4. === WHAT'S PARTIALLY DONE ===\n"
            f"   Bullet list of things that exist as scaffolding but aren't "
            f"functional yet. Note WHY (missing data, missing API, etc.).\n"
            f"5. === WHAT'S MISSING ===\n"
            f"   Bullet list of major required items from the task that "
            f"haven't been started. Prioritize P0 > P1 > P2 > P3 where "
            f"applicable.\n"
            f"6. === LAST ITERATION FOCUS ===\n"
            f"   What this iteration was specifically working on when the "
            f"session ended. Whatever was mid-flight, finish it in the "
            f"codebase below.\n"
            f"7. === CRITICAL NOTES FOR THE NEXT PERSON ===\n"
            f"   Gotchas, non-obvious decisions, file paths / API keys / "
            f"external dependencies they'll need.\n"
            f"8. === NEXT CONCRETE STEPS ===\n"
            f"   Ordered 5-10 item list of exactly what to do next.\n"
            f"9. === FINAL CODEBASE ===\n"
            f"   The current codebase, with the last iteration's work "
            f"completed in it (don't leave mid-function). Every stub marked "
            f"`# TODO(handoff): <specific-thing>` so it's greppable.\n\n"
            f"ORIGINAL TASK:\n{task}\n\n"
            f"CURRENT CODEBASE:\n```\n{codebase}\n```"
        )

    def _estimate_completion_pct(self, task: str, codebase: str,
                                  iterations_done: int) -> int:
        """Rough heuristic for how complete the project is (0-100).

        Signals:
        - codebase size vs typical "done" size (~40K chars is substantial)
        - count of TODO/STUB/NotImplementedError markers (fewer = more done)
        - iterations done vs typical "decent pass" count (~15-20)

        Not precise — just good enough to pick between FINAL and HANDOFF
        modes. Caller can override with an explicit threshold.
        """
        if not codebase:
            return 0

        # Size signal: 40K = "reasonably complete" single-file project
        size_score = min(100, int(len(codebase) / 400))  # 40K → 100

        # Stub density (negative signal) — more stubs = less done
        stub_markers = [
            "TODO", "FIXME", "XXX", "STUB",
            "NotImplementedError", "# placeholder",
            "raise NotImplemented", "pass  # TODO",
        ]
        stub_count = sum(codebase.upper().count(m.upper()) for m in stub_markers)
        # ~10 stubs in 40K is normal; >50 means scaffolding-heavy
        stub_penalty = min(40, stub_count * 2)

        # Iteration signal: 15-20 clean iterations = decent pass
        iter_score = min(100, int(iterations_done * 5))  # 20 iters → 100

        # Weighted average: size is the strongest signal
        pct = int(0.55 * size_score + 0.3 * iter_score - stub_penalty)
        return max(0, min(100, pct))

    def _run_graceful_end_for_session(
        self, session, config: "BroadcastConfig",
        current_codebase: str, iteration: int,
    ) -> None:
        """Run the wrap-up iteration and save the final document."""
        sid = session.session_id
        ai_name = session.ai_profile.name

        pct = self._estimate_completion_pct(
            config.task, current_codebase, iteration,
        )
        mode = "FINAL" if pct >= 33 else "HANDOFF"
        logger.info("[%s] Graceful end: %d%% complete → %s mode",
                    ai_name, pct, mode)

        if self._on_output:
            self._on_output(
                sid, "system",
                f"\n{'='*60}\n"
                f"[{ai_name}] GRACEFUL END requested\n"
                f"Estimated {pct}% complete → producing {mode} output.\n"
                f"{'='*60}\n"
            )

        # Fresh conversation so the prompt is clean
        try:
            session.client.new_conversation()
            time.sleep(1)
        except Exception as e:
            logger.warning("[%s] new_conversation() before graceful-end: %s",
                           ai_name, e)

        prompt = self._build_graceful_end_prompt(
            config.task, current_codebase, pct,
        )
        try:
            result = session.client.generate(
                prompt=prompt,
                on_progress=lambda t, s=sid: (
                    self._on_output(s, "code", t) if self._on_output else None
                ),
            )
        except Exception as e:
            logger.error("[%s] Graceful-end generation failed: %s", ai_name, e)
            result = ""

        # Save as a specially-named final file so it's easy to find
        feature = self._make_feature_name(config.task)
        label = f"{feature}_{mode}_{pct}pct"
        try:
            from .auto_save import save_task_output
            path = save_task_output(
                title=label,
                output=result or current_codebase,
                ai_name=ai_name,
                corner=session.corner,
                elapsed_seconds=0.0,
                iterations=iteration + 1,
            )
            if self._on_status:
                self._on_status(
                    f"Graceful end ({mode}, {pct}%): {path.name if path else label}"
                )
            if self._on_output and path:
                self._on_output(
                    sid, "result",
                    f"\n[GRACEFUL END — {mode} @ {pct}% complete]\n"
                    f"Saved to: {path.name}\n\n" + (result or "")
                )
        except Exception as e:
            logger.error("[%s] Graceful-end save failed: %s", ai_name, e)

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
        self._graceful_end.clear()
        self._running = True
        self._threads.clear()

        # Reload attached files
        if config.attached_files:
            files = self._read_attached_files(config.attached_files)
            self._file_list = files
            self._chunks = []
            self._file_context = self._format_file_context(files)
        else:
            self._file_list = []
            self._chunks = []
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

    @staticmethod
    def _summarize_file(f: dict[str, str], max_chars: int = 180) -> str:
        """Extract a 1-line summary of a file for the manifest.

        Strategy: use the first non-blank line that looks like a description
        (markdown header, docstring, or first comment). Falls back to the
        filename + first 80 chars of content.
        """
        content = f.get("content", "")
        lines = content.splitlines()
        for line in lines[:30]:
            s = line.strip()
            if not s:
                continue
            # Skip visual separator lines (====, ----, ~~~~, ____)
            if re.fullmatch(r'[=\-~_─═#]{4,}', s):
                continue
            # Skip obvious non-descriptive lines
            if s.startswith(("import ", "from ", "#!", "# -*-")):
                continue
            # Strip common markup
            for prefix in ("# ", "## ", "### ", '"""', "'''", "//", "/*", "* ", "- "):
                if s.startswith(prefix):
                    s = s[len(prefix):].strip()
            # After stripping, re-check for separator
            if re.fullmatch(r'[=\-~_─═#]{4,}', s):
                continue
            if len(s) > 10:
                return s[:max_chars]
        # Fallback: bytes count
        return f"({len(content)} chars, {f.get('ext', 'text')})"

    def _build_manifest(self, files: list[dict[str, str]]) -> str:
        """Build a compact table showing all available reference files.

        Lets the AI know exactly what files exist in the full reference set
        even when only a subset fits in the prompt.
        """
        if not files:
            return ""
        rows = []
        for f in files:
            size_k = len(f.get("content", "")) // 1000
            summary = self._summarize_file(f)
            rows.append(f"  {f['name']:<32} ({size_k:>3}K)  {summary}")
        return (
            "REFERENCE FILE MANIFEST (complete list of available files):\n"
            + "\n".join(rows)
        )

    @staticmethod
    def _prioritize_files(
        files: list[dict[str, str]],
        task_prompt: str,
        focus_name: str = "",
    ) -> list[dict[str, str]]:
        """Order files by relevance for the current iteration.

        Priority rules (highest first):
        1. Files explicitly named in the task prompt
        2. Files matching keywords in the current focus
        3. Architecture/overview docs (01_ARCHITECTURE, 00_README)
        4. Required functions / roadmap docs
        5. Everything else in original order
        """
        focus_keywords = {
            # Foundation & correctness
            "deep_dive": ["architecture", "required", "roadmap", "framework"],
            "solid_functional": ["required", "testing", "architecture", "ui"],
            "pressure_test": ["protections", "testing", "bug", "debug"],
            "error_recovery": ["protections", "bug", "gdb", "crash", "retry"],
            "review_grade": ["architecture", "required", "roadmap", "testing"],
            # Capability expansion
            "extra_features": ["features", "required", "roadmap"],
            "explore_expand": ["roadmap", "architecture", "data", "expand"],
            "plugin_system": ["reflex", "framework", "architecture", "extension"],
            "integration_layer": ["gdb", "api", "protocol", "transport"],
            # Non-functional
            "performance": ["performance", "memory", "cache", "optimization"],
            "memory_optimization": ["memory", "map", "cache", "performance"],
            "security": ["protections", "input", "validation", "auth"],
            "concurrency": ["reflex", "async", "thread", "timing"],
            "network_resilience": ["gdb", "protocol", "transport", "retry"],
            "caching_strategy": ["cache", "memory", "performance", "data"],
            # Data & APIs
            "data_layer": ["data", "memory", "map", "required"],
            "api_design": ["api", "required", "architecture", "protocol"],
            "state_management": ["architecture", "reflex", "ui", "framework"],
            # Observability & config
            "logging_observability": ["testing", "debug", "bug", "monitor"],
            "configuration": ["config", "env", "settings", "roadmap"],
            # Presentation & UX
            "beautiful_gui": ["ui", "battle", "menu", "flow"],
            "reference_images": ["ui", "menu", "flow", "battle"],
            "accessibility": ["ui", "menu", "flow"],
            "i18n_l10n": ["ui", "menu", "flow"],
            "cli_ux": ["config", "testing", "debug"],
            # Game & real-time
            "game_loop": ["reflex", "timing", "battle", "ui"],
            "ai_behavior": ["reflex", "battle", "navigation", "ai"],
            "save_system": ["data", "memory", "map", "gdb"],
            # Hygiene
            "documentation": ["readme", "architecture", "required"],
            "test_suite": ["testing", "debug", "protections", "required"],
            # Deployment
            "packaging": ["readme", "config", "architecture"],
            "ci_cd": ["testing", "debug", "protections"],
            # User-facing features (new)
            "onboarding_firstrun": ["ui", "readme", "flow"],
            "import_export": ["data", "config", "memory"],
            "search_filter": ["data", "required", "memory"],
            "shortcuts_hotkeys": ["ui", "input", "flow"],
            "notifications_feedback": ["ui", "flow", "testing"],
            # Auth & access (new)
            "authentication": ["protections", "input", "validation"],
            "authorization_permissions": ["protections", "validation", "required"],
            # Jobs & ops (new)
            "background_jobs": ["reflex", "async", "timing"],
            "monitoring_alerting": ["testing", "debug", "bug", "monitor"],
            # Code quality (new)
            "type_safety": ["required", "architecture", "testing"],
            "modular_refactor": ["architecture", "framework", "required"],
            # UX polish (new)
            "animations_transitions": ["ui", "menu", "flow", "timing"],
            # AI-specific (new)
            "prompt_engineering": ["ai", "reflex", "framework"],
            "rag_vector_search": ["data", "memory", "required"],
        }

        task_lower = task_prompt.lower()
        focus_kw = focus_keywords.get(focus_name.lower().replace(" ", "_"), [])

        def priority(f: dict[str, str]) -> tuple:
            name = f["name"]
            name_lower = name.lower()
            stem = name_lower.replace(".py.txt", "").replace(".txt", "")
            # 1. Explicit mention in task prompt (highest)
            if name in task_prompt or stem in task_lower:
                p1 = 0
            else:
                p1 = 3
            # 2. Focus keyword match
            if any(kw in name_lower for kw in focus_kw):
                p2 = 0
            else:
                p2 = 3
            # 3. Low-numbered docs (architecture, required) come first
            num_prefix = name[:2] if name[:2].isdigit() else "99"
            try:
                p3 = int(num_prefix)
            except ValueError:
                p3 = 99
            # 4. Docs before code files
            p4 = 0 if "code/" not in f.get("path", "") and "code\\" not in f.get("path", "") else 1
            return (p1, p2, p4, p3)

        return sorted(files, key=priority)

    def _build_smart_file_context(
        self,
        budget: int = 26_000,
        task_prompt: str = "",
        focus_name: str = "",
    ) -> str:
        """Build file context that fits within budget, prioritized by relevance.

        Returns a prompt block with:
        - Clear framing ("these are the ONLY files you have access to")
        - Full manifest showing ALL available files with summaries
        - Full content of top-priority files (as many as fit)
        - Short excerpts of remaining files (first ~500 chars each)
        - Explicit list of any files still completely omitted

        This prevents Gemini from hallucinating references to files it
        doesn't actually have in the current context, while giving at
        least partial visibility into every reference file.
        """
        if not self._file_list:
            return ""

        manifest = self._build_manifest(self._file_list)
        prioritized = self._prioritize_files(
            self._file_list, task_prompt, focus_name
        )

        # Reserve budget for manifest + framing text + omitted-files note
        HEADER_RESERVE = len(manifest) + 1_000
        content_budget = max(budget - HEADER_RESERVE, 2_000)

        EXCERPT_LEN = 500  # First 500 chars of each omitted file
        SMALL_FILE_FULL = 1_000  # Files under this get full content in the excerpt pass
        MAX_FULL_FILES = 2  # Cap on full-content files so excerpts fit too

        included_full: list[dict[str, str]] = []
        included_excerpt: list[dict[str, str]] = []
        omitted: list[dict[str, str]] = []
        used = 0

        # Pass 1: include up to MAX_FULL_FILES of the highest-priority
        # files in full, keeping budget headroom for excerpts of the rest.
        # Reserve ~40% of content_budget for the excerpt pass so most
        # files get at least partial visibility.
        EXCERPT_RESERVE = int(content_budget * 0.4)
        full_budget = content_budget - EXCERPT_RESERVE

        for f in prioritized:
            if len(included_full) >= MAX_FULL_FILES:
                omitted.append(f)
                continue
            lang = f["ext"] or "text"
            full_section = (
                f"=== {f['name']} ===\n"
                f"```{lang}\n{f['content']}\n```"
            )
            section_len = len(full_section) + 2
            if used + section_len <= full_budget:
                included_full.append(f)
                used += section_len
            else:
                omitted.append(f)

        # Pass 2: for files that didn't fit in full, try to include either
        # a full copy (if small) or a short excerpt so the AI has at least
        # a taste of each.
        still_omitted: list[dict[str, str]] = []
        for f in omitted:
            lang = f["ext"] or "text"
            content = f["content"]
            if len(content) <= SMALL_FILE_FULL:
                # Small file — include in full
                excerpt_section = (
                    f"=== {f['name']} (full — small file) ===\n"
                    f"```{lang}\n{content}\n```"
                )
            else:
                snippet = content[:EXCERPT_LEN].rstrip()
                remaining = len(content) - len(snippet)
                excerpt_section = (
                    f"=== {f['name']} (excerpt — first {len(snippet)} of "
                    f"{len(content)} chars; {remaining} chars truncated) ===\n"
                    f"```{lang}\n{snippet}\n...\n```"
                )
            if used + len(excerpt_section) + 2 <= content_budget:
                included_excerpt.append(f)
                used += len(excerpt_section) + 2
            else:
                still_omitted.append(f)

        # Build final context
        parts = [
            "═══════════════ REFERENCE MATERIAL ═══════════════",
            "You DO NOT have file system access. The material below is",
            "the ONLY reference you have. Every filename referenced in",
            "your task can be found as a section header within this",
            "block — do NOT attempt to 'open', 'read', 'fetch', or",
            "'load' any file from disk.",
            "",
            manifest,
        ]
        if included_full:
            parts.append(
                f"\n─── FULL CONTENT: {len(included_full)} files "
                f"(highest priority for this iteration) ───"
            )
            for f in included_full:
                lang = f["ext"] or "text"
                parts.append(
                    f"=== {f['name']} ===\n"
                    f"```{lang}\n{f['content']}\n```"
                )
        if included_excerpt:
            parts.append(
                f"\n─── EXCERPTS: {len(included_excerpt)} files "
                f"(first {EXCERPT_LEN} chars each; see manifest for summaries) ───"
            )
            for f in included_excerpt:
                lang = f["ext"] or "text"
                content = f["content"]
                if len(content) <= SMALL_FILE_FULL:
                    parts.append(
                        f"=== {f['name']} (full — small file) ===\n"
                        f"```{lang}\n{content}\n```"
                    )
                else:
                    snippet = content[:EXCERPT_LEN].rstrip()
                    remaining = len(content) - len(snippet)
                    parts.append(
                        f"=== {f['name']} (excerpt — first {len(snippet)} of "
                        f"{len(content)} chars; {remaining} chars truncated) ===\n"
                        f"```{lang}\n{snippet}\n...\n```"
                    )
        if still_omitted:
            omitted_names = ", ".join(f["name"] for f in still_omitted)
            parts.append(
                f"\n[{len(still_omitted)} files completely omitted this turn "
                f"(see manifest for summaries): {omitted_names}]"
            )
        parts.append("═══════════════ END REFERENCE MATERIAL ═══════════════")

        return "\n".join(parts)

    # ── Retrieval-Augmented Context (chunk-level extraction) ────────
    #
    # When reference files exceed the model's input limit, we can't embed
    # everything. Smart-context above handles this by picking whole files
    # via priority, but many files have only a few sections relevant to
    # each iteration. Retrieval-based chunking unlocks much better signal:
    # parse each file into logical sections (Python functions/classes,
    # doc sections), then rank every chunk against the iteration's actual
    # directive and pack the highest-scoring chunks into the budget.

    # Common stopwords — not useful as retrieval keywords
    _STOPWORDS = frozenset({
        "the", "and", "for", "with", "this", "that", "from", "into", "your",
        "their", "have", "will", "must", "should", "each", "every", "make",
        "ensure", "follow", "apply", "step", "output", "please", "they",
        "there", "these", "those", "what", "when", "where", "which", "while",
        "being", "been", "were", "than", "then", "them", "they're", "about",
        "below", "above", "more", "less", "very", "some", "most", "other",
        "also", "only", "just", "only", "does", "done", "doing", "would",
        "could", "might", "same", "such", "used", "uses", "like", "over",
        "this", "needs", "need", "task", "code", "file", "files", "system",
        "function", "class", "build", "write", "create", "implement", "based",
        "using", "method", "methods", "name", "names", "type", "types",
    })

    @staticmethod
    def _is_python_file(f: dict[str, str]) -> bool:
        """Detect a Python source file (possibly named foo.py.txt)."""
        name = f.get("name", "")
        return name.endswith(".py") or ".py." in name or f.get("ext") == "py"

    def _chunk_python(self, f: dict[str, str]) -> list[dict[str, Any]]:
        """Split a Python file into logical chunks (functions, classes, top-level).

        Preserves module-level imports/constants as one chunk at the head so
        the AI sees how functions are defined globally when it sees them.
        """
        content = f["content"]
        lines = content.splitlines()
        chunks: list[dict[str, Any]] = []
        current: list[str] = []
        current_name = "<module header>"

        def flush() -> None:
            if current:
                text = "\n".join(current).strip("\n")
                if text.strip():
                    chunks.append({
                        "file": f["name"],
                        "name": current_name,
                        "content": text,
                        "ext": "py",
                        "kind": "py",
                    })

        # Regex for top-level def / class (no leading spaces), or
        # first-level methods (exactly 4 spaces leading).
        top_def = re.compile(r'^(def |class |async def )\w')
        method_def = re.compile(r'^    (def |async def )\w')

        for line in lines:
            if top_def.match(line) or method_def.match(line):
                flush()
                current_name = line.strip()[:80]
                current = [line]
            else:
                current.append(line)
        flush()
        return chunks

    def _chunk_doc(self, f: dict[str, str]) -> list[dict[str, Any]]:
        """Split a documentation file into sections by visual separators.

        Detects headers, separator lines (==========, --------), and
        markdown-style ## / ### headers. Each chunk keeps its own header
        so the AI knows what part of the doc it's reading.
        """
        content = f["content"]
        lines = content.splitlines()
        chunks: list[dict[str, Any]] = []
        current: list[str] = []
        current_name = "<preamble>"

        def flush() -> None:
            if current:
                text = "\n".join(current).strip("\n")
                # Skip tiny whitespace/separator-only chunks
                if text.strip() and len(text.strip()) > 30:
                    chunks.append({
                        "file": f["name"],
                        "name": current_name,
                        "content": text,
                        "ext": f.get("ext", "txt"),
                        "kind": "doc",
                    })

        sep_line = re.compile(r'^[=\-─═]{6,}\s*$')
        md_header = re.compile(r'^#{1,4}\s+\S')

        i = 0
        while i < len(lines):
            line = lines[i]
            # Pattern A: `===` line, then title line, then `===` line
            if (sep_line.match(line)
                    and i + 2 < len(lines)
                    and sep_line.match(lines[i + 2])
                    and lines[i + 1].strip()):
                flush()
                current_name = lines[i + 1].strip()[:120]
                current = [line, lines[i + 1], lines[i + 2]]
                i += 3
                continue
            # Pattern B: markdown header
            if md_header.match(line):
                flush()
                current_name = line.strip().lstrip("# ")[:120]
                current = [line]
                i += 1
                continue
            current.append(line)
            i += 1
        flush()

        # If there were zero boundaries, keep the whole doc as one chunk
        if not chunks and content.strip():
            chunks.append({
                "file": f["name"],
                "name": f["name"],
                "content": content,
                "ext": f.get("ext", "txt"),
                "kind": "doc",
            })
        return chunks

    def _chunk_file(self, f: dict[str, str]) -> list[dict[str, Any]]:
        """Dispatch to the right chunker based on file type."""
        if self._is_python_file(f):
            return self._chunk_python(f)
        return self._chunk_doc(f)

    def _build_chunk_index(self) -> None:
        """Chunk every file in self._file_list and cache in self._chunks.

        Runs once per broadcast (not per iteration). Each chunk is a dict:
            {file, name, content, ext, kind}
        """
        if self._chunks or not self._file_list:
            return
        total_chunks = 0
        for f in self._file_list:
            try:
                pieces = self._chunk_file(f)
                self._chunks.extend(pieces)
                total_chunks += len(pieces)
            except Exception as e:
                logger.warning("Chunking failed for %s: %s — keeping whole",
                               f.get("name"), e)
                self._chunks.append({
                    "file": f["name"],
                    "name": f["name"],
                    "content": f["content"],
                    "ext": f.get("ext", "text"),
                    "kind": "whole",
                })
                total_chunks += 1
        logger.info("Chunked %d files into %d retrievable sections",
                    len(self._file_list), total_chunks)

    @classmethod
    def _extract_keywords(cls, text: str) -> list[str]:
        """Extract retrieval keywords from a directive/task/prompt.

        Returns lowercase tokens of length ≥4 that aren't stopwords. Also
        extracts CamelCase/snake_case identifiers as high-signal terms.
        """
        if not text:
            return []
        # Pull out code-like identifiers in full (snake_case, camelCase,
        # dotted paths) — these are usually function/class/file names.
        idents = re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}(?:\.[A-Za-z_]\w*)*', text)
        # Also split camelCase into parts so "MGBAControl" scores on "mgba"
        extras = []
        for ident in idents:
            # split on CamelCase boundaries
            parts = re.findall(r'[A-Z]+[a-z0-9]*|[a-z0-9]+', ident)
            extras.extend(parts)
        tokens = {t.lower() for t in idents + extras}
        # Also basic word tokens
        words = re.findall(r'\b[A-Za-z][A-Za-z_-]{3,}\b', text.lower())
        tokens.update(words)
        return [t for t in tokens if t and t not in cls._STOPWORDS and len(t) >= 3]

    @staticmethod
    def _score_chunk(chunk: dict[str, Any], keywords: list[str]) -> float:
        """Score a chunk by keyword frequency in its content + name.

        Name matches are weighted 3x (function/section names are strong signal).
        Content matches add log-scaled frequency so a chunk that mentions a
        term 20x doesn't dominate over chunks with good coverage.
        """
        if not keywords:
            return 0.0
        name = chunk.get("name", "").lower()
        content = chunk.get("content", "").lower()
        file_name = chunk.get("file", "").lower()

        score = 0.0
        import math
        for kw in keywords:
            # File-name hit (strongest)
            if kw in file_name:
                score += 5.0
            # Chunk-name hit (strong)
            if kw in name:
                score += 3.0
            # Content hits (diminishing return)
            count = content.count(kw)
            if count:
                score += 1.0 + math.log1p(count)
        return score

    @staticmethod
    def _score_chunk_weighted(
        chunk: dict[str, Any], kw_weights: dict[str, float]
    ) -> float:
        """Score a chunk using per-keyword weights (directive > focus > task).

        Same location weighting as _score_chunk (file > name > content), but
        scales each hit by the keyword's caller-provided weight.
        """
        if not kw_weights:
            return 0.0
        import math
        name = chunk.get("name", "").lower()
        content = chunk.get("content", "").lower()
        file_name = chunk.get("file", "").lower()

        score = 0.0
        for kw, weight in kw_weights.items():
            hit = False
            if kw in file_name:
                score += 5.0 * weight
                hit = True
            if kw in name:
                score += 3.0 * weight
                hit = True
            count = content.count(kw)
            if count:
                score += (1.0 + math.log1p(count)) * weight
                hit = True
            # Small penalty-avoidance: no-hit keywords don't subtract
            _ = hit
        return score

    def _build_retrieval_context(
        self,
        budget: int,
        query: str,
        focus_name: str = "",
        task_prompt: str = "",
    ) -> str:
        """Build a context block of the highest-relevance chunks for this query.

        Uses chunk-level retrieval so a 10K-char file where only a 500-char
        function matters won't crowd out sections from other files. Every
        returned chunk is tagged with (filename → section-name) so the AI
        sees exactly which piece of which file it's reading.
        """
        if not self._file_list:
            return ""
        self._build_chunk_index()

        # Extract keywords separately from each source so we can weight them.
        # Directive is THE signal for this turn — weight it heavily.
        kw_directive = self._extract_keywords(query)
        kw_focus = self._extract_keywords(focus_name)
        kw_task = self._extract_keywords(task_prompt)

        # Deduplicate while keeping directive weight highest
        kw_weights: dict[str, float] = {}
        for k in kw_directive:
            kw_weights[k] = kw_weights.get(k, 0) + 4.0   # Directive: 4x
        for k in kw_focus:
            kw_weights[k] = kw_weights.get(k, 0) + 2.0   # Focus: 2x
        for k in kw_task:
            # Task prompt keywords get base weight only, and are skipped
            # entirely if they're in a generic "project background" bucket
            # (appears in >50% of chunks — too broad to help retrieval)
            kw_weights[k] = kw_weights.get(k, 0) + 1.0

        if not kw_weights:
            # Fall back to whole-file smart context if nothing to retrieve on
            return self._build_smart_file_context(
                budget=budget, task_prompt=task_prompt, focus_name=focus_name,
            )

        # Prune generic keywords — those that appear in a majority of chunks
        # carry no retrieval signal (they just sum noise into every score).
        if len(self._chunks) > 10:
            generic_threshold = len(self._chunks) * 0.5
            noise: list[str] = []
            for kw in list(kw_weights.keys()):
                hits = sum(1 for c in self._chunks if kw in c["content"].lower())
                if hits >= generic_threshold:
                    noise.append(kw)
            for kw in noise:
                kw_weights.pop(kw, None)

        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self._chunks:
            s = self._score_chunk_weighted(chunk, kw_weights)
            if s > 0:
                scored.append((s, chunk))
        scored.sort(key=lambda x: -x[0])

        manifest = self._build_manifest(self._file_list)
        header_reserve = len(manifest) + 1_200
        content_budget = max(budget - header_reserve, 2_000)

        selected: list[dict[str, Any]] = []
        used = 0
        seen_files: set[str] = set()
        for score, chunk in scored:
            lang = chunk.get("ext") or "text"
            body = chunk["content"]
            # Hard-cap individual chunk size so one giant function can't eat
            # the whole budget. Long code chunks get a `... [truncated]` tail.
            MAX_CHUNK_CHARS = 6_000
            if len(body) > MAX_CHUNK_CHARS:
                body = body[:MAX_CHUNK_CHARS].rstrip() + "\n... [chunk truncated]"
            section = (
                f"=== {chunk['file']}  →  {chunk['name']} "
                f"(score {score:.1f}) ===\n"
                f"```{lang}\n{body}\n```"
            )
            section_len = len(section) + 2
            if used + section_len > content_budget:
                continue
            selected.append(chunk)
            seen_files.add(chunk["file"])
            used += section_len

        # Build output
        parts = [
            "═══════════════ REFERENCE MATERIAL ═══════════════",
            "You DO NOT have file system access. The material below is",
            "the ONLY reference you have. Each section is labeled with",
            "its source file and its location within that file. Do NOT",
            "attempt to 'open', 'read', 'fetch', or 'load' any file.",
            "",
            "Context is RETRIEVED: we extracted the highest-relevance",
            "sections from across all reference files based on your",
            "current directive. Sections you need that aren't below",
            "either don't exist yet or were out of budget this turn —",
            "stub them as TODO rather than inventing them.",
            "",
            manifest,
        ]
        if selected:
            files_hit = len(seen_files)
            parts.append(
                f"\n─── RELEVANT SECTIONS: {len(selected)} chunks from "
                f"{files_hit} files (ranked by relevance to this turn) ───"
            )
            for chunk in selected:
                lang = chunk.get("ext") or "text"
                body = chunk["content"]
                MAX_CHUNK_CHARS = 6_000
                if len(body) > MAX_CHUNK_CHARS:
                    body = body[:MAX_CHUNK_CHARS].rstrip() + "\n... [chunk truncated]"
                # Compute score fresh for display (we didn't store it)
                parts.append(
                    f"=== {chunk['file']}  →  {chunk['name']} ===\n"
                    f"```{lang}\n{body}\n```"
                )
        # Let the AI know which files contributed nothing this turn
        all_files = {f["name"] for f in self._file_list}
        unused_files = sorted(all_files - seen_files)
        if unused_files:
            parts.append(
                f"\n[Files with no matching sections this turn "
                f"(see manifest summaries): {', '.join(unused_files)}]"
            )
        parts.append("═══════════════ END REFERENCE MATERIAL ═══════════════")
        return "\n".join(parts)

    def _send_file_context_messages(
        self, session, sid: str, ai_name: str,
    ) -> None:
        """Send reference file context as pre-messages before the task prompt.

        AI web UIs have input limits (~30K chars for Gemini). Instead of
        embedding 256K of files in one prompt, we split them into chunks
        and send each as a separate message. The AI accumulates context
        across the conversation.

        Each chunk gets a header like "Reference files (part 1/4)" and we
        wait for the AI to acknowledge before sending the next chunk.
        """
        MAX_CHUNK = 25_000  # Stay well under Gemini's ~32K limit
        file_ctx = self._file_context
        total_len = len(file_ctx)

        if total_len <= MAX_CHUNK:
            # Small enough for one message
            chunks = [file_ctx]
        else:
            # Split on file boundaries (look for "═══" separators or "---" lines)
            # to avoid cutting mid-file
            chunks = []
            current_chunk = ""
            for line in file_ctx.split("\n"):
                if len(current_chunk) + len(line) + 1 > MAX_CHUNK and current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk.strip():
                chunks.append(current_chunk)

        num_chunks = len(chunks)
        logger.info("[%s] Sending %d file context chunks (%d chars total)",
                    ai_name, num_chunks, total_len)

        for i, chunk in enumerate(chunks):
            if self._stop_event.is_set():
                return

            header = (
                f"REFERENCE FILES (part {i + 1}/{num_chunks}) — "
                f"Read and remember these. I will send the task prompt after "
                f"all parts are uploaded.\n\n"
            )
            prompt = header + chunk

            if self._on_output:
                self._on_output(
                    sid, "system",
                    f"[{ai_name}] Uploading reference files "
                    f"({i + 1}/{num_chunks}, {len(chunk)} chars)...\n"
                )

            try:
                session.client.generate(
                    prompt=prompt,
                    on_progress=lambda t, s=sid: None,  # Suppress progress for context msgs
                )
                logger.info("[%s] File context chunk %d/%d sent (%d chars)",
                            ai_name, i + 1, num_chunks, len(chunk))
            except Exception as e:
                logger.warning("[%s] File context chunk %d failed: %s — continuing",
                               ai_name, i + 1, e)

        if self._on_output:
            self._on_output(
                sid, "system",
                f"[{ai_name}] All reference files uploaded. Sending task prompt...\n"
            )

    def _build_context_prompt(self, directive: str, codebase: str,
                              file_context: str = "",
                              focus_name: str = "",
                              task_prompt: str = "") -> str:
        """Build an improvement prompt that includes the current codebase.

        This is the key fix for context amnesia — every iteration sees
        the actual code from the previous iteration, not just a vague
        instruction to "improve the code you wrote."

        Since each iteration is now a fresh conversation (Gemini's Angular
        state gets stuck after any send), we also include a file manifest
        so the AI knows what reference material exists. Full file content
        is only added if there's budget after codebase + directive.

        BUDGET ENFORCEMENT: Gemini caps input at ~32K chars. Once the
        codebase alone exceeds the budget (happens around iteration 12
        when code reaches 45K+ chars), we SUMMARIZE the codebase — keep
        the imports/classes/function signatures, replace function bodies
        with `...`, and tell the AI to output the full restored file.
        Without this the prompt silently exceeds the limit and the send
        fails with an upload mismatch.
        """
        parts = [directive]

        HARD_LIMIT = 30_000
        # Reserve for directive + our wrapping + headroom
        overhead = len(directive) + 1_200
        # Budget for codebase itself — leave room for file context too
        codebase_max = max(4_000, HARD_LIMIT - overhead - 4_000)

        working_codebase = codebase or ""
        was_trimmed = False
        if len(working_codebase) > codebase_max:
            working_codebase = self._summarize_codebase(
                working_codebase, codebase_max,
            )
            was_trimmed = True

        used_so_far = overhead + len(working_codebase)
        remaining = HARD_LIMIT - used_so_far

        if file_context:
            # Caller provided explicit context — use it as-is
            parts.append(file_context)
        elif self._file_list and remaining > 2_000:
            # Build retrieval-augmented context: extract the SECTIONS from
            # across all files most relevant to the current directive/focus.
            # Chunk-level retrieval gives much better signal per byte than
            # whole-file embedding when we're budget-constrained.
            retrieval_ctx = self._build_retrieval_context(
                budget=min(remaining, 15_000),
                query=directive,
                focus_name=focus_name,
                task_prompt=task_prompt,
            )
            if retrieval_ctx:
                parts.append(retrieval_ctx)
        elif self._file_list:
            # Too tight — just include the manifest so the AI knows
            # what files exist (even if their content isn't embedded)
            manifest = self._build_manifest(self._file_list)
            if manifest:
                parts.append(
                    "REFERENCE MATERIAL (metadata only — content not included "
                    "this turn due to prompt size; work from the codebase):\n"
                    + manifest
                )

        if working_codebase:
            label = "CURRENT CODEBASE"
            if was_trimmed:
                label = (
                    "CURRENT CODEBASE (SUMMARIZED — bodies replaced with `...`. "
                    "Output the ENTIRE restored codebase, not just the changed "
                    "parts; re-implement any summarized function bodies in full.)"
                )
            parts.append(f"{label}:\n```\n{working_codebase}\n```")

        return "\n\n".join(parts)

    @staticmethod
    def _summarize_codebase(code: str, target_size: int) -> str:
        """Reduce codebase to fit `target_size` while keeping structure.

        Strategy: preserve all imports, class definitions, function signatures,
        docstrings, and top-level constants. Replace function bodies with
        `    ...` so the AI can still see what functions exist. Also keeps
        the top-of-file comment block and the `if __name__` guard intact.

        Falls back to simple truncation if the structured approach doesn't
        shrink enough (e.g. the file is mostly data).
        """
        if len(code) <= target_size:
            return code

        lines = code.splitlines()
        kept: list[str] = []
        indent_level = 0

        # Python-ish regexes; works for the kind of single-file output the
        # broadcast produces.
        def_re = re.compile(r'^(\s*)(?:async\s+)?def\s+\w')
        class_re = re.compile(r'^(\s*)class\s+\w')
        decorator_re = re.compile(r'^\s*@\w')
        import_re = re.compile(r'^\s*(?:from|import)\s+\S')

        skipping_body = False
        body_indent = 0
        in_docstring = False
        docstring_quotes = None

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            leading = len(line) - len(stripped)

            # Always keep imports, decorators, class/def declarations, blank
            # lines, top-level comments.
            is_top_level_comment = (
                leading == 0 and stripped.startswith("#")
            )
            is_import = import_re.match(line) is not None
            is_decorator = decorator_re.match(line) is not None
            is_def = def_re.match(line) is not None
            is_class = class_re.match(line) is not None
            is_blank = not stripped

            if skipping_body:
                # Still inside a body we're eliding?
                if is_blank:
                    continue  # drop blanks in elided body
                if leading > body_indent:
                    continue  # part of the body, drop it
                # Leaving the body
                skipping_body = False

            if (is_import or is_decorator or is_class or is_def
                    or is_top_level_comment or is_blank):
                kept.append(line)
                # After a def/class header, insert `...` and start eliding
                if is_def or is_class:
                    body_indent = leading + 4
                    # Keep a leading docstring if the next non-blank line is one
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines):
                        nxt = lines[j].lstrip()
                        if nxt.startswith(('"""', "'''")):
                            # keep docstring — next loop iterations pick it up
                            pass
                    # Mark that we'll skip the body
                    skipping_body = True
                    kept.append(" " * body_indent + "...")
                continue

            # Top-level non-function code: keep if it looks structural
            # (constants, type aliases, `if __name__` guard).
            if leading == 0:
                # Module-level assignment / condition — keep
                kept.append(line)
                continue

            # Anything else — drop (it's inside a def/class body we already
            # elided above)

        summary = "\n".join(kept)

        # If we didn't shrink enough (lots of module-level data), fall back
        # to head+tail truncation so the AI still gets top + bottom context.
        if len(summary) > target_size:
            head = code[: target_size * 2 // 3]
            tail = code[-target_size // 4:]
            summary = (
                head
                + f"\n\n# ... [elided {len(code) - len(head) - len(tail)} chars"
                f" from middle of file] ...\n\n"
                + tail
            )

        return summary

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

                    full_prompt = self._build_context_prompt(
                        directive, current_codebase,
                        focus_name=focus_name, task_prompt=config.task,
                    )

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
            # File context is now embedded in the initial prompt (truncated
            # to ~25K chars) rather than sent as multi-turn pre-messages.
            # Multi-turn is broken on Gemini: Angular's streaming state gets
            # stuck after the first send, preventing all subsequent messages.

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
                # Graceful end requested? Run one final wrap-up iteration
                # (FINAL consolidated version if >33% done, HANDOFF doc if
                # <33% done) and exit.
                if self._graceful_end.is_set():
                    self._run_graceful_end_for_session(
                        session, config, current_codebase, iteration,
                    )
                    break
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

                    # Build the FULL prompt: directive + current codebase.
                    # Since each iteration is a fresh conversation (Gemini
                    # button state workaround), we also pass focus/task so the
                    # prompt can include a smart manifest + relevant files if
                    # the prompt budget allows.
                    full_prompt = self._build_context_prompt(
                        improvement_directive, current_codebase,
                        focus_name=focus, task_prompt=config.task,
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

                # Fresh conversation for each iteration — Gemini's Angular
                # state gets stuck after each send (button stays "Stop
                # response"). We embed the full codebase in every prompt,
                # so conversation history isn't needed.
                try:
                    session.client.new_conversation()
                    time.sleep(1)
                except Exception as e:
                    logger.warning("[%s] new_conversation() before iter %d: %s",
                                   ai_name, iteration + 1, e)

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
                # Graceful end (FINAL / HANDOFF wrap-up) — same policy as
                # _session_loop. Works after resume too.
                if self._graceful_end.is_set():
                    self._run_graceful_end_for_session(
                        session, config, current_codebase, iteration,
                    )
                    break
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

                    # Smart file context: manifest + focus-relevant files if
                    # budget allows (each iteration is a fresh conversation).
                    full_prompt = self._build_context_prompt(
                        improvement_directive, current_codebase,
                        focus_name=focus, task_prompt=config.task,
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

                # Fresh conversation per iteration (Gemini button state fix)
                try:
                    session.client.new_conversation()
                    time.sleep(1)
                except Exception as e:
                    logger.warning("[%s] new_conversation() before resume iter %d: %s",
                                   ai_name, iteration + 1, e)

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

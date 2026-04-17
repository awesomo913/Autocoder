# coding_essentials.py — Merge Handoff

_Generated 2026-04-17T04:34:41.778633 from 46 autocoder iterations_

## Context

Another Claude ran Autocoder against Gemini for ~25 hours, producing the iterations
in `iterations/`. Each file is one pass: Gemini was given the task spec + the
previous iteration's code + an improvement focus (Deep Code Dive, Extra Features,
Pressure Test, etc.), then returned a full replacement of the single-file program.

Your job: merge these into ONE cohesive `coding_essentials.py` that represents
the best of all passes. See **Merge Strategy** below.

## Task Spec (what Gemini was building)

```
Build a single-file Python program called coding_essentials.py — a lightweight
toolkit a developer uses WHILE writing other apps. Runs standalone (python
coding_essentials.py) AND is importable. One file, under 1500 lines, stdlib +
customtkinter only.

DELIVERABLE — five integrated subsystems in one file:
  1. EFFICIENCY CORE — @timed, @memoize (TTL+LRU), debounce/throttle, Batched()
     context manager, lazy_import, tiny Profiler panel.
  2. GUI BACKEND KIT — EventBus (on/emit/off), Store (dataclass-backed reactive
     state with shallow-diff subscribers), UndoStack, run_in_thread helper,
     FormBuilder (dataclass-to-CTk form).
  3. VISUAL POLISH — Theme Store (colors/spacing/fonts, light+dark), styled
     Button/Card/Toast/Badge/Divider/StatusDot, smooth fade-in Toast, EmptyState.
  4. TUTORIAL MODULE — Walkthrough(steps) with dimmed overlay + tooltip + Next/
     Skip/Back, persists completed tutorials, first-run tour fires automatically.
  5. COHESIVE DEMO APP — Playground/Profiler/Settings tabs. Playground exercises
     EVERY subsystem.

CROSS-CUTTING: type hints, docstrings with examples, single main(), no silent
except, graceful shutdown, --selftest flag that PASS/FAILs each subsystem.
```

## What is in this folder

- `BASELINE.py` — the best single iteration by subsystem coverage + size. Start here.
- `iterations/` — all 46 valid iterations, renamed chronologically as
  `iter_NNN__<version>_<focus>__<timestamp>.py`
- `MERGE_HANDOFF.md` — this file

## Size bands across iterations

| Band | Count |
|------|------:|
| 43KB (gold) | 5 |
| 35KB (solid) | 13 |
| 28-30KB | 2 |
| 17-20KB | 10 |
| 13-16KB | 5 |
| 5-13KB | 11 |

## Merge Strategy

1. **Start from `BASELINE.py`.** It has all 5 subsystems and the widest
   class/function coverage.

2. **Diff strategy — scan iterations for ADDITIONS, not replacements.**
   Gemini's improvement loop tends to REGENERATE the whole file each pass, so
   later iterations often shrink rather than grow. The valuable parts are:
   - New widgets or helpers that do not exist in the baseline
   - Better implementations of a specific method (compare line-by-line per class)
   - Small polish like smoother animations, nicer fallback behavior

3. **Per-subsystem merge checklist.** For each subsystem, read the relevant
   section in BASELINE.py, then grep the 5 largest iterations for the same
   section, and pick the strongest version:
   - `@timed` + `Profiler` — look for the cleanest ring-buffer + stats API
   - `@memoize` — verify TTL + LRU eviction both work and are testable
   - `EventBus` — must support off(handle)
   - `Store` — shallow-diff is the hard part; find the impl that correctly
     does NOT re-fire listeners when unrelated fields change
   - `UndoStack` — keyboard bindings should actually exist (some omit them)
   - `run_in_thread` — must marshal on_done/on_error back via .after()
   - `FormBuilder` — the best versions infer widget type from dataclass field type
   - `Theme` Store — toggling should actually re-theme live widgets
   - `Toast` — smooth fade-in wins, others jump
   - `Walkthrough` — overlay cutout drawing is non-trivial; pick the one that
     actually works against arbitrary widget positions
   - Demo app — must actually exercise every other subsystem visibly

4. **Reject these patterns:**
   - Versions with `TODO`, `...`, or `pass` stubs in production paths
   - Versions where `--selftest` is declared but prints nothing
   - Versions where Toast uses time.sleep() on the main thread (freezes the UI)

5. **Constraints to preserve:**
   - Single file, under 1500 lines
   - stdlib + customtkinter ONLY (no external deps)
   - `python coding_essentials.py --selftest` must print PASS per module and exit 0
   - `python coding_essentials.py` runs the demo app with the 4-step first-run tour
   - Log to `~/.coding_essentials/app.log`, state to `~/.coding_essentials/state.json`

6. **Verify before finishing:**
   ```bash
   python coding_essentials.py --selftest   # must pass all 5
   wc -l coding_essentials.py               # must be <= 1500
   python -c "import ast; ast.parse(open('coding_essentials.py').read())"
   ```

## Background: the Autocoder run that produced these

- Tool: Autocoder at `C:\Users\default.LAPTOP-S2O9G7EP\Desktop\AI2\CLaud tool\Autocoder\`
- Model: Gemini via Chrome CDP
- Mode: Perfection Loop across 8 improvement focuses + expand-on-stagnation
- Known issues: ~3 iterations were transcript leaks (pyautogui captured the
  conversation panel). Those were filtered OUT of this bundle. Post-run Fixes
  #25a/b/c now prevent this at the source.

Full handoff for the previous session is at `~/Desktop/Autocoder_Passdown.txt`.

## Minimum viable output

A single file named `coding_essentials.py` in this directory, next to this
handoff, that passes `--selftest`, runs the demo app, and has all 5 subsystems
wired together. Nothing else.

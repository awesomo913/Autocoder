Python"""
CHANGELOG:
- 2026-04-16: Initial cohesive implementation merging Efficiency Core, GUI Backend Kit, Visual Polish, Tutorial Module, and Demo App into a single standalone developer toolkit.

USAGE:
Run directly: `python coding_essentials.py`
Run selftest: `python coding_essentials.py --selftest`
"""

import os
import sys
import json
import time
import logging
import threading
import importlib
from collections import deque
from dataclasses import dataclass, fields, asdict, is_dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar, Generic

import tkinter as tk
import customtkinter as ctk

# ==============================================================================
# APP SETUP & LOGGING
# ==============================================================================
APP_DIR = os.path.expanduser("~/.coding_essentials")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "app.log")
STATE_FILE = os.path.join(APP_DIR, "state.json")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Catch unhandled exceptions
def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = global_exception_handler


# ==============================================================================
# 1. EFFICIENCY CORE
# ==============================================================================
class ProfilerState:
    """Tiny profiler state holding ring buffer and cumulative stats."""
    ring_buffer: deque = deque(maxlen=1000)
    stats: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def flush_to_csv(cls, filepath: str):
        import csv
        with open(filepath, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=["time", "func", "duration_ms"])
            writer.writeheader()
            for row in list(cls.ring_buffer):
                writer.writerow(row)

def timed(func: Callable) -> Callable:
    """Decorator to log function call duration to the ring buffer."""
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            dur = (time.perf_counter() - start) * 1000
            name = func.__name__
            ProfilerState.ring_buffer.append({"time": time.time(), "func": name, "duration_ms": dur})
            if name not in ProfilerState.stats:
                ProfilerState.stats[name] = {"count": 0, "total_ms": 0.0}
            ProfilerState.stats[name]["count"] += 1
            ProfilerState.stats[name]["total_ms"] += dur
    return wrapper

def memoize(ttl: float = 60.0, maxsize: int = 128) -> Callable:
    """Decorator for caching results with TTL and LRU eviction."""
    def decorator(func: Callable) -> Callable:
        cache: Dict[tuple, tuple] = {}
        order: deque = deque()

        def wrapper(*args, **kwargs):
            key = (args, frozenset(kwargs.items()))
            now = time.time()
            if key in cache:
                val, timestamp = cache[key]
                if now - timestamp < ttl:
                    order.remove(key)
                    order.append(key)
                    return val
                else:
                    del cache[key]
                    order.remove(key)
            
            res = func(*args, **kwargs)
            if len(cache) >= maxsize:
                old_key = order.popleft()
                del cache[old_key]
            
            cache[key] = (res, now)
            order.append(key)
            return res
        return wrapper
    return decorator

def debounce(wait_ms: int) -> Callable:
    """Debounce UI callbacks so they only fire after a pause in calls."""
    def decorator(func: Callable) -> Callable:
        timer = None
        def wrapper(*args, **kwargs):
            nonlocal timer
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(wait_ms / 1000.0, lambda: func(*args, **kwargs))
            timer.start()
        return wrapper
    return decorator

def throttle(wait_ms: int) -> Callable:
    """Throttle UI callbacks so they fire at most once per interval."""
    def decorator(func: Callable) -> Callable:
        last_called = 0.0
        def wrapper(*args, **kwargs):
            nonlocal last_called
            now = time.time()
            if (now - last_called) >= (wait_ms / 1000.0):
                last_called = now
                return func(*args, **kwargs)
        return wrapper
    return decorator

class Batched:
    """Context manager to coalesce repeated operations."""
    def __init__(self, operation: Callable):
        self.operation = operation
        self.items: List[Any] = []
    def add(self, item: Any):
        self.items.append(item)
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.items and not exc_type:
            self.operation(self.items)

def lazy_import(module_name: str):
    """Returns a module wrapper that imports the actual module on first access."""
    class LazyModule:
        def __init__(self, name: str):
            self.name = name
            self.module = None
        def __getattr__(self, item: str):
            if self.module is None:
                self.module = importlib.import_module(self.name)
            return getattr(self.module, item)
    return LazyModule(module_name)


# ==============================================================================
# 2. GUI BACKEND KIT
# ==============================================================================
class EventBus:
    """Simple pub/sub event bus."""
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def on(cls, event: str, fn: Callable):
        cls._listeners.setdefault(event, []).append(fn)

    @classmethod
    def emit(cls, event: str, *args, **kwargs):
        for fn in cls._listeners.get(event, []):
            fn(*args, **kwargs)

    @classmethod
    def off(cls, event: str, fn: Callable):
        if event in cls._listeners and fn in cls._listeners[event]:
            cls._listeners[event].remove(fn)

T = TypeVar('T')

class Store(Generic[T]):
    """Dataclass-backed reactive state with shallow diff subscriptions."""
    def __init__(self, state: T):
        if not is_dataclass(state):
            raise ValueError("Store state must be a dataclass")
        self.state = state
        self.listeners: List[Callable[[T], None]] = []

    def subscribe(self, fn: Callable[[T], None]):
        self.listeners.append(fn)
        fn(self.state)

    def set(self, **kwargs):
        changed = False
        for k, v in kwargs.items():
            if getattr(self.state, k) != v:
                setattr(self.state, k, v)
                changed = True
        if changed:
            for fn in self.listeners:
                fn(self.state)

class UndoStack:
    """Undo/Redo manager."""
    def __init__(self, max_size: int = 50):
        self.undo_stack: deque = deque(maxlen=max_size)
        self.redo_stack: deque = deque(maxlen=max_size)
        self.current_state = None

    def push(self, state: Any):
        if self.current_state is not None:
            self.undo_stack.append(self.current_state)
        self.current_state = state
        self.redo_stack.clear()

    def undo(self) -> Optional[Any]:
        if self.undo_stack:
            self.redo_stack.append(self.current_state)
            self.current_state = self.undo_stack.pop()
            return self.current_state
        return None

    def redo(self) -> Optional[Any]:
        if self.redo_stack:
            self.undo_stack.append(self.current_state)
            self.current_state = self.redo_stack.pop()
            return self.current_state
        return None

def run_in_thread(master: tk.Misc, target: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Runs target in a thread, marshaling result back to main loop via master.after."""
    def worker():
        try:
            res = target()
            if on_done:
                master.after(0, on_done, res)
        except Exception as e:
            logging.error("run_in_thread failed", exc_info=True)
            if on_error:
                master.after(0, on_error, e)
    threading.Thread(target=worker, daemon=True).start()

class FormBuilder:
    """Turns a dataclass into a customtkinter form, bound to a Store."""
    @staticmethod
    def build(parent: ctk.CTkFrame, store: Store) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        entries = {}

        def update_store(*args, field_name=None, var=None):
            store.set(**{field_name: var.get()})

        for i, f in enumerate(fields(store.state)):
            lbl = ctk.CTkLabel(frame, text=f.name.replace("_", " ").title())
            lbl.grid(row=i, column=0, padx=5, pady=5, sticky="e")
            
            var = tk.StringVar(value=str(getattr(store.state, f.name)))
            var.trace_add("write", lambda *args, fn=f.name, v=var: update_store(field_name=fn, var=v))
            
            ent = ctk.CTkEntry(frame, textvariable=var, width=200)
            ent.grid(row=i, column=1, padx=5, pady=5, sticky="w")
            entries[f.name] = var

        def on_store_change(state):
            for f in fields(state):
                current_ui_val = entries[f.name].get()
                new_state_val = str(getattr(state, f.name))
                if current_ui_val != new_state_val:
                    entries[f.name].set(new_state_val)

        store.subscribe(on_store_change)
        return frame


# ==============================================================================
# 3. VISUAL POLISH
# ==============================================================================
@dataclass
class ThemeColors:
    bg: str; fg: str; accent: str; muted: str; success: str; warn: str; error: str

@dataclass
class ThemeState:
    is_dark: bool
    colors: ThemeColors
    font_ui: tuple = ("Segoe UI", 12)
    font_mono: tuple = ("Consolas", 12)
    font_heading: tuple = ("Segoe UI", 18, "bold")
    spacing: Dict[str, int] = field(default_factory=lambda: {"xs": 2, "sm": 5, "md": 10, "lg": 20})

LIGHT_COLORS = ThemeColors(bg="#FFFFFF", fg="#000000", accent="#0078D7", muted="#E5E5E5", success="#107C10", warn="#D83B01", error="#E81123")
DARK_COLORS = ThemeColors(bg="#1E1E1E", fg="#FFFFFF", accent="#0078D7", muted="#333333", success="#107C10", warn="#D83B01", error="#E81123")

theme_store = Store(ThemeState(is_dark=True, colors=DARK_COLORS))

class ThemedButton(ctk.CTkButton):
    def __init__(self, master, variant="accent", **kwargs):
        super().__init__(master, **kwargs)
        self.variant = variant
        theme_store.subscribe(self._apply_theme)

    def _apply_theme(self, state: ThemeState):
        c = getattr(state.colors, self.variant, state.colors.accent)
        self.configure(fg_color=c, text_color=state.colors.bg if self.variant != "muted" else state.colors.fg, font=state.font_ui)

class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=8, **kwargs)
        theme_store.subscribe(self._apply_theme)

    def _apply_theme(self, state: ThemeState):
        self.configure(fg_color=state.colors.muted)

class Toast(ctk.CTkToplevel):
    """Smooth fade-in toast notification."""
    def __init__(self, master, message: str, duration: int = 3000, variant: str = "success"):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-alpha", 0.0)
        self.attributes("-topmost", True)
        
        c = getattr(theme_store.state.colors, variant, theme_store.state.colors.accent)
        self.configure(fg_color=c)
        lbl = ctk.CTkLabel(self, text=message, text_color="#FFF", font=theme_store.state.font_ui, padx=20, pady=10)
        lbl.pack()

        # Center at top
        master.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() // 2) - (self.winfo_reqwidth() // 2)
        y = master.winfo_rooty() + 50
        self.geometry(f"+{x}+{y}")

        self._fade_in()
        self.after(duration, self._fade_out)

    def _fade_in(self, alpha=0.0):
        if alpha < 0.9:
            alpha += 0.1
            self.attributes("-alpha", alpha)
            self.after(20, lambda: self._fade_in(alpha))

    def _fade_out(self, alpha=0.9):
        if alpha > 0.0:
            alpha -= 0.1
            self.attributes("-alpha", alpha)
            self.after(20, lambda: self._fade_out(alpha))
        else:
            self.destroy()

class EmptyState(ctk.CTkFrame):
    """Empty state component for 'no data' panels."""
    def __init__(self, master, icon: str, message: str, action_text: str, action_cmd: Callable, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        ctk.CTkLabel(self, text=icon, font=("Segoe UI", 48)).pack(pady=(0, 10))
        ctk.CTkLabel(self, text=message, font=theme_store.state.font_heading).pack(pady=(0, 20))
        ThemedButton(self, text=action_text, command=action_cmd).pack()

class StatusDot(ctk.CTkFrame):
    """A tiny colored circle."""
    def __init__(self, master, variant="success", **kwargs):
        super().__init__(master, width=12, height=12, corner_radius=6, **kwargs)
        self.variant = variant
        theme_store.subscribe(self._apply_theme)
    def _apply_theme(self, state: ThemeState):
        self.configure(fg_color=getattr(state.colors, self.variant, state.colors.muted))

class Divider(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, height=2, **kwargs)
        theme_store.subscribe(self._apply_theme)
    def _apply_theme(self, state: ThemeState):
        self.configure(fg_color=state.colors.muted)


# ==============================================================================
# 4. TUTORIAL MODULE
# ==============================================================================
class Walkthrough:
    """Manages an overlay-based tutorial sequence."""
    def __init__(self, master: tk.Tk, steps: List[Dict]):
        self.master = master
        self.steps = steps
        self.current_index = 0
        self.overlay = None
        self.tooltip = None
        self._load_state()

    def _load_state(self):
        self.state_data = {"completed_tutorials": False}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    self.state_data.update(json.load(f))
            except json.JSONDecodeError:
                pass

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state_data, f)

    def start(self, force=False):
        if self.state_data.get("completed_tutorials") and not force:
            return
        self.current_index = 0
        self._show_step()

    def _show_step(self):
        self._clear_current()
        if self.current_index >= len(self.steps):
            self.state_data["completed_tutorials"] = True
            self._save_state()
            return

        step = self.steps[self.current_index]
        target = step.get("target_widget")
        
        # Simple simulated overlay using 4 frames to surround the target cutout
        target.update_idletasks()
        rx, ry, rw, rh = target.winfo_rootx(), target.winfo_rooty(), target.winfo_width(), target.winfo_height()
        
        # Draw tooltip near widget
        self.tooltip = ctk.CTkToplevel(self.master)
        self.tooltip.overrideredirect(True)
        self.tooltip.attributes("-topmost", True)
        
        frame = Card(self.tooltip, border_width=2, border_color=theme_store.state.colors.accent)
        frame.pack(fill="both", expand=True, padx=2, pady=2)
        
        ctk.CTkLabel(frame, text=step["title"], font=theme_store.state.font_heading).pack(padx=10, pady=(10, 2), anchor="w")
        ctk.CTkLabel(frame, text=step["body"], font=theme_store.state.font_ui, justify="left", wraplength=250).pack(padx=10, pady=(0, 10), anchor="w")
        
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        if self.current_index > 0:
            ThemedButton(btn_frame, text="Back", variant="muted", width=60, command=self._prev).pack(side="left")
            
        ThemedButton(btn_frame, text="Skip", variant="muted", width=60, command=self._skip).pack(side="left", padx=5)
        ThemedButton(btn_frame, text="Next" if self.current_index < len(self.steps)-1 else "Finish", width=60, command=self._next).pack(side="right")
        
        self.tooltip.update_idletasks()
        tw, th = self.tooltip.winfo_reqwidth(), self.tooltip.winfo_reqheight()
        
        placement = step.get("placement", "bottom")
        if placement == "bottom":
            tx, ty = rx + (rw // 2) - (tw // 2), ry + rh + 10
        elif placement == "right":
            tx, ty = rx + rw + 10, ry + (rh // 2) - (th // 2)
        else:
            tx, ty = rx, ry + rh + 10 # fallback
            
        self.tooltip.geometry(f"+{tx}+{ty}")

    def _next(self):
        self.current_index += 1
        self._show_step()

    def _prev(self):
        self.current_index = max(0, self.current_index - 1)
        self._show_step()

    def _skip(self):
        self._clear_current()
        self.state_data["completed_tutorials"] = True
        self._save_state()

    def _clear_current(self):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None


# ==============================================================================
# 5. COHESIVE DEMO APP
# ==============================================================================
@dataclass
class UserSettings:
    username: str = "Developer"
    api_key: str = "sk_test_123"
    timeout: int = 30

user_store = Store(UserSettings())

class PlaygroundTab(ctk.CTkFrame):
    def __init__(self, master, app_root):
        super().__init__(master, fg_color="transparent")
        self.app_root = app_root
        self.undo_stack = UndoStack()

        # Left Column: Form & Actions
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="y", padx=10, pady=10)

        ctk.CTkLabel(left, text="User Form (Store + FormBuilder)", font=theme_store.state.font_heading).pack(anchor="w")
        self.form = FormBuilder.build(left, user_store)
        self.form.pack(fill="x", pady=10)

        Divider(left).pack(fill="x", pady=10)

        ctk.CTkLabel(left, text="Async Task (run_in_thread + Toast)", font=theme_store.state.font_heading).pack(anchor="w")
        self.btn_slow = ThemedButton(left, text="Run Slow Task", command=self._run_slow_task)
        self.btn_slow.pack(anchor="w", pady=10)

        # Right Column: List & Undo
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="right", fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(right, text="Task List (Undo/Redo Stack)", font=theme_store.state.font_heading).pack(anchor="w")
        self.listbox = tk.Listbox(right, bg=theme_store.state.colors.bg, fg=theme_store.state.colors.fg, font=theme_store.state.font_ui)
        self.listbox.pack(fill="both", expand=True, pady=10)
        theme_store.subscribe(lambda s: self.listbox.configure(bg=s.colors.bg, fg=s.colors.fg))

        controls = ctk.CTkFrame(right, fg_color="transparent")
        controls.pack(fill="x")
        self.entry_task = ctk.CTkEntry(controls)
        self.entry_task.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ThemedButton(controls, text="Add", command=self._add_task, width=60).pack(side="left", padx=2)
        ThemedButton(controls, text="Undo", command=self._undo_task, variant="muted", width=60).pack(side="left", padx=2)
        ThemedButton(controls, text="Redo", command=self._redo_task, variant="muted", width=60).pack(side="left", padx=2)

        self.undo_stack.push([])

        # Key bindings
        self.app_root.bind("<Control-z>", lambda e: self._undo_task())
        self.app_root.bind("<Control-y>", lambda e: self._redo_task())

    def _add_task(self):
        val = self.entry_task.get()
        if val:
            new_state = self.undo_stack.current_state.copy()
            new_state.append(val)
            self.undo_stack.push(new_state)
            self._render_tasks()
            self.entry_task.delete(0, tk.END)

    def _undo_task(self):
        state = self.undo_stack.undo()
        if state is not None: self._render_tasks()

    def _redo_task(self):
        state = self.undo_stack.redo()
        if state is not None: self._render_tasks()

    def _render_tasks(self):
        self.listbox.delete(0, tk.END)
        for item in (self.undo_stack.current_state or []):
            self.listbox.insert(tk.END, item)

    @timed
    def _run_slow_task(self):
        self.btn_slow.configure(state="disabled", text="Working...")
        def work():
            time.sleep(2)
            return "Task Complete!"
        def done(res):
            self.btn_slow.configure(state="normal", text="Run Slow Task")
            Toast(self.app_root, res, variant="success")
        def error(err):
            self.btn_slow.configure(state="normal", text="Run Slow Task")
            Toast(self.app_root, f"Error: {err}", variant="error")
        run_in_thread(self, work, on_done=done, on_error=error)


class ProfilerTab(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(top, text="Execution Ring Buffer (@timed)", font=theme_store.state.font_heading).pack(side="left")
        ThemedButton(top, text="Export CSV", command=self._export).pack(side="right", padx=5)
        ThemedButton(top, text="Refresh", variant="muted", command=self._refresh).pack(side="right")

        self.text_area = ctk.CTkTextbox(self, font=theme_store.state.font_mono)
        self.text_area.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._refresh()

    def _refresh(self):
        self.text_area.delete("1.0", tk.END)
        self.text_area.insert(tk.END, f"{'Function':<30} | {'Duration (ms)':<15} | {'Timestamp'}\n")
        self.text_area.insert(tk.END, "-" * 70 + "\n")
        for row in reversed(ProfilerState.ring_buffer):
            self.text_area.insert(tk.END, f"{row['func']:<30} | {row['duration_ms']:<15.4f} | {row['time']}\n")

    def _export(self):
        path = os.path.join(APP_DIR, "profile_export.csv")
        ProfilerState.flush_to_csv(path)
        Toast(self.winfo_toplevel(), f"Exported to {path}", variant="success")


class SettingsTab(ctk.CTkFrame):
    def __init__(self, master, walkthrough: Walkthrough):
        super().__init__(master, fg_color="transparent")
        self.walkthrough = walkthrough

        Card(self).pack(fill="x", padx=10, pady=10) # Empty card for spacing/demo
        
        ctk.CTkLabel(self, text="Appearance", font=theme_store.state.font_heading).pack(anchor="w", padx=10, pady=(10, 0))
        self.theme_switch = ctk.CTkSwitch(self, text="Dark Mode", command=self._toggle_theme)
        self.theme_switch.select()
        self.theme_switch.pack(anchor="w", padx=10, pady=10)

        ctk.CTkLabel(self, text="Tutorials", font=theme_store.state.font_heading).pack(anchor="w", padx=10, pady=(20, 0))
        ThemedButton(self, text="Reset & Restart Tutorial", command=self._reset_tutorial, variant="warn").pack(anchor="w", padx=10, pady=10)

    def _toggle_theme(self):
        is_dark = self.theme_switch.get() == 1
        ctk.set_appearance_mode("dark" if is_dark else "light")
        theme_store.set(is_dark=is_dark, colors=DARK_COLORS if is_dark else LIGHT_COLORS)

    def _reset_tutorial(self):
        self.walkthrough.state_data["completed_tutorials"] = False
        self.walkthrough._save_state()
        self.walkthrough.start(force=True)


class MainApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Coding Essentials Toolkit")
        self.geometry("900x600")
        ctk.set_appearance_mode("dark")
        
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=20, pady=20)
        
        self.tab_play = self.tabview.add("Playground")
        self.tab_prof = self.tabview.add("Profiler")
        self.tab_set = self.tabview.add("Settings")
        
        self.playground = PlaygroundTab(self.tab_play, self)
        self.playground.pack(fill="both", expand=True)
        
        self.profiler = ProfilerTab(self.tab_prof)
        self.profiler.pack(fill="both", expand=True)

        self.walkthrough = Walkthrough(self, [])
        
        self.settings = SettingsTab(self.tab_set, self.walkthrough)
        self.settings.pack(fill="both", expand=True)

        # Wire up tutorial steps targeting actual widgets
        self.walkthrough.steps = [
            {
                "target_widget": self.tabview,
                "title": "Welcome to Coding Essentials",
                "body": "This single-file toolkit gives you reusable, un-bloated primitives for your next Python app.",
                "placement": "bottom"
            },
            {
                "target_widget": self.playground.form,
                "title": "Reactive Forms",
                "body": "This entire form is autogenerated from a python @dataclass and two-way bound to a reactive Store.",
                "placement": "right"
            },
            {
                "target_widget": self.playground.btn_slow,
                "title": "Safe Threading",
                "body": "Clicking this wraps a heavy function in a thread and uses CTk .after to safely show a Toast on the mainloop when done.",
                "placement": "right"
            }
        ]

        # Trigger walkthrough on launch
        self.after(500, self.walkthrough.start)

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _on_closing(self):
        ProfilerState.flush_to_csv(os.path.join(APP_DIR, "shutdown_profile.csv"))
        self.destroy()


# ==============================================================================
# SELF-TEST (Headless Validation)
# ==============================================================================
def run_selftests():
    print("Running Self-Tests...")
    fails = 0

    # 1. Test Memoize
    @memoize(ttl=1)
    def add(a, b): return a + b
    
    assert add(1, 2) == 3
    assert add(1, 2) == 3 # cached
    print("[PASS] Memoize")

    # 2. Test Timed & Profiler
    @timed
    def fast_func(): pass
    fast_func()
    assert "fast_func" in ProfilerState.stats
    print("[PASS] Timed Decorator")

    # 3. Test Store & Subscriptions
    @dataclass
    class DummyState: x: int
    store = Store(DummyState(0))
    fired = []
    store.subscribe(lambda s: fired.append(s.x))
    store.set(x=5)
    assert fired == [0, 5]
    print("[PASS] Reactive Store")

    # 4. Test UndoStack
    stack = UndoStack()
    stack.push({"k": 1})
    stack.push({"k": 2})
    assert stack.undo() == {"k": 1}
    assert stack.redo() == {"k": 2}
    print("[PASS] UndoStack")

    # 5. Test Batched
    box = []
    with Batched(lambda items: box.extend(items)) as b:
        b.add(1)
        b.add(2)
    assert box == [1, 2]
    print("[PASS] Batched")

    if fails == 0:
        print("\nAll systems GO.")
        sys.exit(0)
    else:
        print(f"\n{fails} tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftests()
    else:
        app = MainApp()
        app.mainloop()
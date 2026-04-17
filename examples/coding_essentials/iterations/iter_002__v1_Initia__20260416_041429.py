Python# CHANGELOG
# 2026-04-16: Initial version - Built 5 core subsystems (Efficiency Core, GUI Backend Kit, Visual Polish, Tutorial Module, Demo App) in a single file architecture.

import os
import sys
import time
import json
import logging
import threading
import queue
import csv
from collections import deque, OrderedDict
from dataclasses import dataclass, field, asdict, is_dataclass
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Generic, Type
import importlib

try:
    import customtkinter as ctk # type: ignore
except ImportError:
    print("FATAL: customtkinter not found. Please install: pip install customtkinter")
    sys.exit(1)

# =============================================================================
# GLOBAL SETUP & CONSTANTS
# =============================================================================
APP_DIR = os.path.expanduser("~/.coding_essentials")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "app.log")
STATE_FILE = os.path.join(APP_DIR, "state.json")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

def log_exception(exc: Exception, context: str = ""):
    """Logs exceptions to ~/.coding_essentials/app.log instead of silently failing."""
    logging.error(f"{context} - {str(exc)}", exc_info=True)
    print(f"ERROR: {context} - {str(exc)}")


# =============================================================================
# SUBSYSTEM 1: EFFICIENCY CORE
# =============================================================================

@dataclass
class CallStat:
    count: int = 0
    total_ms: float = 0.0

class Profiler:
    """Tiny profiler showing per-function call count and total ms."""
    ring_buffer: deque = deque(maxlen=1000)
    stats: Dict[str, CallStat] = {}

    @classmethod
    def record(cls, name: str, duration_ms: float):
        cls.ring_buffer.append((time.time(), name, duration_ms))
        if name not in cls.stats:
            cls.stats[name] = CallStat()
        cls.stats[name].count += 1
        cls.stats[name].total_ms += duration_ms

    @classmethod
    def flush_to_csv(cls, path: str):
        try:
            with open(path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Function", "Duration_ms"])
                for entry in cls.ring_buffer:
                    writer.writerow(entry)
        except Exception as e:
            log_exception(e, "Failed to flush profiler to CSV")

def timed(func: Callable) -> Callable:
    """Decorator: logs execution time to the global Profiler ring buffer."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        duration = (time.perf_counter() - start) * 1000.0
        Profiler.record(func.__name__, duration)
        return result
    return wrapper

def memoize(ttl: float = 60.0, maxsize: int = 128) -> Callable:
    """Decorator: caches results with TTL and LRU eviction."""
    def decorator(func: Callable) -> Callable:
        cache: OrderedDict = OrderedDict()
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, frozenset(kwargs.items()))
            now = time.time()
            if key in cache:
                value, timestamp = cache[key]
                if now - timestamp < ttl:
                    cache.move_to_end(key)
                    return value
                else:
                    del cache[key]
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result
        wrapper.cache = cache # type: ignore
        return wrapper
    return decorator

def debounce(wait: float) -> Callable:
    """Decorator: defers function execution until 'wait' seconds have elapsed since last call."""
    def decorator(func: Callable) -> Callable:
        timer: Optional[threading.Timer] = None
        @wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal timer
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(wait, lambda: func(*args, **kwargs))
            timer.start()
        return wrapper
    return decorator

def throttle(wait: float) -> Callable:
    """Decorator: ensures a function is called at most once every 'wait' seconds."""
    def decorator(func: Callable) -> Callable:
        last_called = 0.0
        @wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal last_called
            now = time.time()
            if now - last_called >= wait:
                last_called = now
                return func(*args, **kwargs)
        return wrapper
    return decorator

class Batched:
    """Context manager that coalesces repeated operations (e.g. bulk UI updates)."""
    def __init__(self, action: Callable[[List[Any]], None]):
        self.action = action
        self.items: List[Any] = []
    
    def add(self, item: Any):
        self.items.append(item)
        
    def __enter__(self):
        self.items = []
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.items and not exc_type:
            try:
                self.action(self.items)
            except Exception as e:
                log_exception(e, "Error executing batched action")

def lazy_import(name: str):
    """Helper so heavy modules load only on first use."""
    return importlib.import_module(name)


# =============================================================================
# SUBSYSTEM 2: GUI BACKEND KIT
# =============================================================================

class EventBus:
    """Simple pub/sub event bus."""
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def on(cls, event: str, fn: Callable):
        if event not in cls._listeners:
            cls._listeners[event] = []
        cls._listeners[event].append(fn)

    @classmethod
    def emit(cls, event: str, *args, **kwargs):
        for fn in cls._listeners.get(event, []):
            try:
                fn(*args, **kwargs)
            except Exception as e:
                log_exception(e, f"EventBus error on '{event}'")

    @classmethod
    def off(cls, event: str, fn: Callable):
        if event in cls._listeners and fn in cls._listeners[event]:
            cls._listeners[event].remove(fn)

TState = TypeVar('TState')

class Store(Generic[TState]):
    """Dataclass-backed reactive state with shallow diff subscriptions."""
    def __init__(self, initial_state: TState):
        if not is_dataclass(initial_state):
            raise ValueError("Store initial_state must be a dataclass instance.")
        self.state: TState = initial_state
        self._listeners: List[Tuple[Callable, Optional[List[str]]]] = []

    def subscribe(self, fn: Callable[[TState], None], keys: Optional[List[str]] = None):
        """Subscribe to state changes. If keys provided, only triggers when those keys change."""
        self._listeners.append((fn, keys))

    def set(self, **changes):
        """Update state and selectively fire listeners based on shallow diff."""
        changed_keys = set()
        for k, v in changes.items():
            if getattr(self.state, k) != v:
                setattr(self.state, k, v)
                changed_keys.add(k)
        
        if changed_keys:
            for fn, keys in self._listeners:
                if keys is None or any(k in changed_keys for k in keys):
                    try:
                        fn(self.state)
                    except Exception as e:
                        log_exception(e, "Store subscriber error")

class UndoStack:
    """Manages push/undo/redo actions."""
    def __init__(self):
        self.stack: List[Tuple[Callable, Callable]] = []
        self.ptr: int = -1

    def push(self, do_fn: Callable, undo_fn: Callable):
        """Executes do_fn immediately and pushes to stack."""
        try:
            do_fn()
            self.ptr += 1
            self.stack = self.stack[:self.ptr]
            self.stack.append((do_fn, undo_fn))
        except Exception as e:
            log_exception(e, "UndoStack push (do) error")

    def undo(self, _event=None):
        if self.ptr >= 0:
            try:
                _, undo_fn = self.stack[self.ptr]
                undo_fn()
                self.ptr -= 1
            except Exception as e:
                log_exception(e, "UndoStack undo error")

    def redo(self, _event=None):
        if self.ptr < len(self.stack) - 1:
            self.ptr += 1
            try:
                do_fn, _ = self.stack[self.ptr]
                do_fn()
            except Exception as e:
                log_exception(e, "UndoStack redo error")

_MAIN_LOOP_QUEUE = queue.Queue()

def _process_thread_queue(root: ctk.CTk):
    """Processes callbacks from background threads on the main GUI thread."""
    try:
        while True:
            fn = _MAIN_LOOP_QUEUE.get_nowait()
            fn()
    except queue.Empty:
        pass
    root.after(50, lambda: _process_thread_queue(root))

def run_in_thread(fn: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Runs fn in a background thread, marshalling results safely to Tk mainloop."""
    def worker():
        try:
            res = fn()
            if on_done:
                _MAIN_LOOP_QUEUE.put(lambda: on_done(res))
        except Exception as e:
            log_exception(e, "Threaded execution failed")
            if on_error:
                _MAIN_LOOP_QUEUE.put(lambda: on_error(e))
    threading.Thread(target=worker, daemon=True).start()

class FormBuilder:
    """Turns a dataclass into a customtkinter form, two-way bound to a Store."""
    @staticmethod
    def build(parent: ctk.CTkFrame, store: Store) -> ctk.CTkFrame:
        form_frame = ctk.CTkFrame(parent, fg_color="transparent")
        
        row = 0
        for field_name, field_def in store.state.__dataclass_fields__.items():
            lbl = ctk.CTkLabel(form_frame, text=field_name.replace("_", " ").title())
            lbl.grid(row=row, column=0, padx=5, pady=5, sticky="e")
            
            val = getattr(store.state, field_name)
            
            if isinstance(val, bool):
                var = ctk.BooleanVar(value=val)
                widget = ctk.CTkCheckBox(form_frame, text="", variable=var)
                def make_cmd(f_name, v_var):
                    return lambda: store.set(**{f_name: v_var.get()})
                widget.configure(command=make_cmd(field_name, var))
            else:
                var = ctk.StringVar(value=str(val))
                widget = ctk.CTkEntry(form_frame, textvariable=var)
                def make_trace(f_name, v_var):
                    def trace_cb(*args):
                        store.set(**{f_name: v_var.get()})
                    return trace_cb
                var.trace_add("write", make_trace(field_name, var))
            
            widget.grid(row=row, column=1, padx=5, pady=5, sticky="ew")
            
            # Sub to store changes to update UI if changed elsewhere
            def make_sub(v_var, f_name):
                def update_ui(state):
                    new_val = getattr(state, f_name)
                    if v_var.get() != (new_val if isinstance(new_val, bool) else str(new_val)):
                        v_var.set(new_val)
                return update_ui
            store.subscribe(make_sub(var, field_name), keys=[field_name])
            
            row += 1
            
        form_frame.columnconfigure(1, weight=1)
        return form_frame


# =============================================================================
# SUBSYSTEM 3: VISUAL POLISH
# =============================================================================

@dataclass
class ThemeColors:
    bg: str
    fg: str
    accent: str
    muted: str
    success: str
    warn: str
    error: str

@dataclass
class ThemeDef:
    mode: str
    colors: ThemeColors
    spacing: Dict[str, int]
    fonts: Dict[str, Tuple[str, int]]

LIGHT_THEME = ThemeDef(
    mode="light",
    colors=ThemeColors(bg="#f4f4f5", fg="#18181b", accent="#3b82f6", muted="#a1a1aa", success="#22c55e", warn="#eab308", error="#ef4444"),
    spacing={"xs": 4, "sm": 8, "md": 16, "lg": 24},
    fonts={"ui": ("Roboto", 13), "mono": ("Consolas", 12), "heading": ("Roboto", 18, "bold")}
)

DARK_THEME = ThemeDef(
    mode="dark",
    colors=ThemeColors(bg="#18181b", fg="#f4f4f5", accent="#3b82f6", muted="#52525b", success="#22c55e", warn="#eab308", error="#ef4444"),
    spacing={"xs": 4, "sm": 8, "md": 16, "lg": 24},
    fonts={"ui": ("Roboto", 13), "mono": ("Consolas", 12), "heading": ("Roboto", 18, "bold")}
)

ThemeStore = Store(DARK_THEME)

class StyledButton(ctk.CTkButton):
    def __init__(self, master, variant="accent", **kwargs):
        super().__init__(master, **kwargs)
        self.variant = variant
        ThemeStore.subscribe(self._apply_theme)
        self._apply_theme(ThemeStore.state)

    def _apply_theme(self, theme: ThemeDef):
        color = getattr(theme.colors, self.variant, theme.colors.accent)
        self.configure(fg_color=color, font=theme.fonts["ui"])

class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=8, **kwargs)
        ThemeStore.subscribe(self._apply_theme)
        self._apply_theme(ThemeStore.state)

    def _apply_theme(self, theme: ThemeDef):
        self.configure(fg_color=theme.colors.muted if theme.mode == "dark" else "#ffffff")

class Divider(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, height=2, **kwargs)
        ThemeStore.subscribe(self._apply_theme)
        self._apply_theme(ThemeStore.state)

    def _apply_theme(self, theme: ThemeDef):
        self.configure(fg_color=theme.colors.muted)

class StatusDot(ctk.CTkFrame):
    def __init__(self, master, status="success", **kwargs):
        super().__init__(master, width=12, height=12, corner_radius=6, **kwargs)
        self.status = status
        ThemeStore.subscribe(self._apply_theme)
        self._apply_theme(ThemeStore.state)

    def _apply_theme(self, theme: ThemeDef):
        color = getattr(theme.colors, self.status, theme.colors.success)
        self.configure(fg_color=color)

class Toast:
    """Smooth fade-in notification (emulated via slide up since CTk alpha is OS-dependent)."""
    @staticmethod
    def show(root: ctk.CTk, message: str, variant: str = "success"):
        theme = ThemeStore.state
        bg_color = getattr(theme.colors, variant, theme.colors.accent)
        
        toast_frame = ctk.CTkFrame(root, fg_color=bg_color, corner_radius=8)
        lbl = ctk.CTkLabel(toast_frame, text=message, text_color="#ffffff", font=theme.fonts["ui"])
        lbl.pack(padx=20, pady=10)
        
        # Slide animation
        start_y = 1.2
        target_y = 0.9
        
        def animate_in(current_y):
            if current_y > target_y:
                toast_frame.place(relx=0.5, rely=current_y, anchor="center")
                root.after(16, lambda: animate_in(current_y - 0.02))
            else:
                root.after(3000, lambda: toast_frame.destroy())
                
        animate_in(start_y)

class EmptyState(ctk.CTkFrame):
    """Component for 'no data' panels."""
    def __init__(self, master, icon: str, message: str, action_text: str, action_cmd: Callable, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        lbl_icon = ctk.CTkLabel(self, text=icon, font=("Roboto", 48))
        lbl_icon.pack(pady=(20, 10))
        
        lbl_msg = ctk.CTkLabel(self, text=message, font=ThemeStore.state.fonts["ui"])
        lbl_msg.pack(pady=(0, 20))
        
        btn = StyledButton(self, text=action_text, command=action_cmd)
        btn.pack(pady=(0, 20))


# =============================================================================
# SUBSYSTEM 4: TUTORIAL MODULE
# =============================================================================

@dataclass
class TutorialStep:
    target_widget: ctk.CTkBaseClass
    title: str
    body: str

class Walkthrough:
    """Interactive overlay tutorial."""
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.steps: List[TutorialStep] = []
        self.current_idx = 0
        self.overlay_frames: List[ctk.CTkFrame] = []
        self.tooltip: Optional[ctk.CTkFrame] = None
        self._load_state()

    def _load_state(self):
        self.completed = False
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.completed = data.get("tutorial_completed", False)
            except:
                pass

    def _save_state(self, completed: bool):
        self.completed = completed
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"tutorial_completed": completed}, f)
        except Exception as e:
            log_exception(e, "Failed to save tutorial state")

    def reset(self):
        self._save_state(False)
        Toast.show(self.root, "Tutorials reset! Restart app to view.", "success")

    def start(self, steps: List[TutorialStep]):
        if self.completed or not steps:
            return
        self.steps = steps
        self.current_idx = 0
        # Give UI time to render actual coordinates
        self.root.after(500, self._render_step)

    def _clear(self):
        for f in self.overlay_frames:
            f.destroy()
        self.overlay_frames.clear()
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    def _render_step(self):
        self._clear()
        if self.current_idx >= len(self.steps):
            self._save_state(True)
            return

        step = self.steps[self.current_idx]
        target = step.target_widget
        
        # Calculate absolute geometry safely
        target.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        tx, ty = target.winfo_rootx() - rx, target.winfo_rooty() - ry
        tw, th = target.winfo_width(), target.winfo_height()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()

        # Create 4 dark frames to simulate a cutout (dimmed overlay)
        dim_color = "#000000"
        opacity_hack = True # CTk doesn't natively support transparent overlays well, just opaque dimming is fallback
        
        f_top = ctk.CTkFrame(self.root, fg_color=dim_color, corner_radius=0)
        f_top.place(x=0, y=0, width=rw, height=ty)
        
        f_bottom = ctk.CTkFrame(self.root, fg_color=dim_color, corner_radius=0)
        f_bottom.place(x=0, y=ty+th, width=rw, height=rh - (ty+th))
        
        f_left = ctk.CTkFrame(self.root, fg_color=dim_color, corner_radius=0)
        f_left.place(x=0, y=ty, width=tx, height=th)
        
        f_right = ctk.CTkFrame(self.root, fg_color=dim_color, corner_radius=0)
        f_right.place(x=tx+tw, y=ty, width=rw - (tx+tw), height=th)
        
        self.overlay_frames = [f_top, f_bottom, f_left, f_right]

        # Tooltip
        self.tooltip = Card(self.root, border_width=2, border_color=ThemeStore.state.colors.accent)
        lbl_title = ctk.CTkLabel(self.tooltip, text=step.title, font=ThemeStore.state.fonts["heading"])
        lbl_title.pack(padx=15, pady=(10, 5), anchor="w")
        
        lbl_body = ctk.CTkLabel(self.tooltip, text=step.body, font=ThemeStore.state.fonts["ui"], justify="left", wraplength=250)
        lbl_body.pack(padx=15, pady=(0, 15), anchor="w")
        
        btn_frame = ctk.CTkFrame(self.tooltip, fg_color="transparent")
        btn_frame.pack(fill="x", padx=15, pady=(0, 10))
        
        if self.current_idx > 0:
            btn_back = StyledButton(btn_frame, text="Back", variant="muted", width=60, command=self._prev)
            btn_back.pack(side="left", padx=(0, 5))
            
        btn_skip = StyledButton(btn_frame, text="Skip", variant="muted", width=60, command=self._skip)
        btn_skip.pack(side="left")
        
        next_text = "Finish" if self.current_idx == len(self.steps) - 1 else "Next"
        btn_next = StyledButton(btn_frame, text=next_text, width=60, command=self._next)
        btn_next.pack(side="right")
        
        # Position tooltip below target
        self.tooltip.place(x=tx, y=ty + th + 10)

    def _next(self):
        self.current_idx += 1
        self._render_step()

    def _prev(self):
        self.current_idx -= 1
        self._render_step()
        
    def _skip(self):
        self._clear()
        self._save_state(True)


# =============================================================================
# SUBSYSTEM 5: COHESIVE DEMO APP
# =============================================================================

@dataclass
class UserProfile:
    username: str = "DevUser"
    email: str = "dev@example.com"
    notifications: bool = True

class DemoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Coding Essentials Toolkit")
        self.geometry("900x600")
        
        # Global UI initialization hooks
        _process_thread_queue(self)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # Apply initial theme
        self._apply_theme(ThemeStore.state)
        ThemeStore.subscribe(self._apply_theme, keys=["mode", "colors"])
        
        # Setup UI
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=20, pady=20)
        
        self.tab_play = self.tabview.add("Playground")
        self.tab_prof = self.tabview.add("Profiler")
        self.tab_set = self.tabview.add("Settings")
        
        self.undo_stack = UndoStack()
        
        self._build_playground()
        self._build_profiler()
        self._build_settings()
        
        # Bind undo/redo
        self.bind("<Control-z>", self.undo_stack.undo)
        self.bind("<Control-y>", self.undo_stack.redo)
        
        # Setup tutorial
        self.walkthrough = Walkthrough(self)
        self.after(500, self._init_tutorial)

    def _apply_theme(self, theme: ThemeDef):
        ctk.set_appearance_mode(theme.mode)
        self.configure(fg_color=theme.colors.bg)

    @timed
    def _build_playground(self):
        # 1. FormBuilder Demo
        self.profile_store = Store(UserProfile())
        form_card = Card(self.tab_play)
        form_card.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        
        lbl = ctk.CTkLabel(form_card, text="Profile Form (Store-Bound)", font=ThemeStore.state.fonts["heading"])
        lbl.pack(pady=10)
        
        form_ui = FormBuilder.build(form_card, self.profile_store)
        form_ui.pack(fill="x", padx=20, pady=10)
        self.tutorial_target_form = form_card
        
        # 2. Tasks / Efficiency Demo
        task_card = Card(self.tab_play)
        task_card.pack(side="right", fill="both", expand=True, padx=10, pady=10)
        
        lbl_t = ctk.CTkLabel(task_card, text="Async & Undo", font=ThemeStore.state.fonts["heading"])
        lbl_t.pack(pady=10)
        
        self.lbl_task_status = ctk.CTkLabel(task_card, text="Ready")
        self.lbl_task_status.pack(pady=5)
        
        btn_slow = StyledButton(task_card, text="Run Slow Task (Threaded)", command=self._run_slow_task)
        btn_slow.pack(pady=10)
        self.tutorial_target_async = btn_slow
        
        # Undo Demo
        self.counter = 0
        self.lbl_counter = ctk.CTkLabel(task_card, text=f"Counter: {self.counter}")
        self.lbl_counter.pack(pady=5)
        
        def do_inc():
            self.counter += 1
            self.lbl_counter.configure(text=f"Counter: {self.counter}")
        def undo_inc():
            self.counter -= 1
            self.lbl_counter.configure(text=f"Counter: {self.counter}")
            
        btn_inc = StyledButton(task_card, text="Increment (+Undo)", variant="success", command=lambda: self.undo_stack.push(do_inc, undo_inc))
        btn_inc.pack(pady=5)

    def _run_slow_task(self):
        self.lbl_task_status.configure(text="Running...")
        
        @timed
        def heavy_work():
            time.sleep(2) # Simulate work
            return "Task Complete!"
            
        def on_done(res):
            self.lbl_task_status.configure(text="Ready")
            Toast.show(self, res, "success")
            self._refresh_profiler() # Update profiler tab
            
        run_in_thread(heavy_work, on_done=on_done)

    @timed
    def _build_profiler(self):
        self.prof_frame = ctk.CTkScrollableFrame(self.tab_prof)
        self.prof_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.tutorial_target_prof = self.tab_prof
        
        btn_frame = ctk.CTkFrame(self.tab_prof, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        StyledButton(btn_frame, text="Refresh", command=self._refresh_profiler).pack(side="left", padx=5)
        StyledButton(btn_frame, text="Export CSV", variant="warn", command=self._export_prof).pack(side="right", padx=5)
        
        self._refresh_profiler()

    def _refresh_profiler(self):
        for widget in self.prof_frame.winfo_children():
            widget.destroy()
            
        if not Profiler.stats:
            EmptyState(self.prof_frame, icon="📊", message="No profiling data yet. Run some tasks!", action_text="Refresh", action_cmd=self._refresh_profiler).pack(pady=50)
            return
            
        headers = ["Function", "Calls", "Total ms", "Avg ms"]
        for i, h in enumerate(headers):
            ctk.CTkLabel(self.prof_frame, text=h, font=ThemeStore.state.fonts["heading"]).grid(row=0, column=i, padx=20, pady=5)
            
        row = 1
        for name, stat in Profiler.stats.items():
            ctk.CTkLabel(self.prof_frame, text=name).grid(row=row, column=0, padx=20, pady=5)
            ctk.CTkLabel(self.prof_frame, text=str(stat.count)).grid(row=row, column=1, padx=20, pady=5)
            ctk.CTkLabel(self.prof_frame, text=f"{stat.total_ms:.2f}").grid(row=row, column=2, padx=20, pady=5)
            ctk.CTkLabel(self.prof_frame, text=f"{(stat.total_ms/stat.count):.2f}").grid(row=row, column=3, padx=20, pady=5)
            row += 1

    def _export_prof(self):
        path = os.path.join(APP_DIR, "profile_export.csv")
        Profiler.flush_to_csv(path)
        Toast.show(self, f"Exported to {path}", "success")

    @timed
    def _build_settings(self):
        card = Card(self.tab_set)
        card.pack(fill="x", padx=10, pady=10)
        
        lbl = ctk.CTkLabel(card, text="Appearance", font=ThemeStore.state.fonts["heading"])
        lbl.pack(pady=(10, 0))
        
        def toggle_theme():
            new_theme = DARK_THEME if ThemeStore.state.mode == "light" else LIGHT_THEME
            ThemeStore.set(**asdict(new_theme))
            
        StyledButton(card, text="Toggle Light/Dark Mode", command=toggle_theme).pack(pady=20)
        
        card2 = Card(self.tab_set)
        card2.pack(fill="x", padx=10, pady=10)
        StyledButton(card2, text="Reset Tutorial", variant="warn", command=self.walkthrough.reset).pack(pady=20)

    def _init_tutorial(self):
        steps = [
            TutorialStep(self.tutorial_target_form, "Reactive Forms", "This form uses a Store dataclass. Changes automatically sync without boilerplate."),
            TutorialStep(self.tutorial_target_async, "Thread Safety", "Clicking this runs a heavy task safely in the background, showing a Toast when done."),
            TutorialStep(self.tab_play, "Undo/Redo Stack", "Try clicking the increment button, then press Ctrl+Z to undo."),
            TutorialStep(self.tabview._segmented_button, "Built-in Profiler", "Every major function is timed. Check the Profiler tab to see the execution stats.")
        ]
        self.walkthrough.start(steps)

    def _on_closing(self):
        # Graceful shutdown: export profile data on close
        Profiler.flush_to_csv(os.path.join(APP_DIR, "last_session_profile.csv"))
        self.destroy()

# =============================================================================
# SELF-TEST HEADLESS RUNNER
# =============================================================================

def run_selftests():
    print("Running coding_essentials selftests...")
    
    # Test Memoize
    @memoize(ttl=1)
    def add(a, b): return a + b
    assert add(1, 2) == 3
    assert len(add.cache) == 1 # type: ignore
    
    # Test Store
    @dataclass
    class Tmp:
        x: int = 1
    s = Store(Tmp())
    calls = []
    s.subscribe(lambda state: calls.append(state.x))
    s.set(x=2)
    assert calls == [2]
    
    # Test UndoStack
    u = UndoStack()
    val = [0]
    def inc(): val[0] += 1
    def dec(): val[0] -= 1
    u.push(inc, dec)
    assert val[0] == 1
    u.undo()
    assert val[0] == 0
    u.redo()
    assert val[0] == 1
    
    print("PASS: All headless selftests executed successfully.")
    sys.exit(0)

# =============================================================================
# MAIN ENTRY
# =============================================================================

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftests()
    else:
        app = DemoApp()
        app.mainloop()
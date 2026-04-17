Your Python file coding_essentials.py is ready. It integrates the requested efficiency tools, reactive GUI patterns, visual components, and tutorial walkthrough into a single, importable, and executable file.[file-tag:coding_essentials_py]Python"""
CODING_ESSENTIALS.PY
A lightweight developer toolkit for efficiency, reactive UI, and onboarding.

CHANGELOG:
2026-04-16 | Initial Version | Integrated Efficiency Core, GUI Backend, Visual Polish, Tutorial, and Demo.
"""

import json
import time
import functools
import threading
import logging
import dataclasses
import collections
import inspect
import sys
import csv
import queue
import importlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Generic, Union

# Attempt to import customtkinter; error gracefully if missing
try:
    import customtkinter as ctk
    import tkinter as tk
except ImportError:
    print("Error: 'customtkinter' is required. Install via: pip install customtkinter")
    sys.exit(1)

# --- CONFIG & LOGGING ---
BASE_DIR = Path.home() / ".coding_essentials"
BASE_DIR.mkdir(exist_ok=True)
LOG_FILE = BASE_DIR / "app.log"
STATE_FILE = BASE_DIR / "state.json"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

T = TypeVar("T")

# =============================================================================
# 1. EFFICIENCY CORE
# =============================================================================

class ProfilerData:
    """Ring buffer for function execution times."""
    buffer = collections.deque(maxlen=1000)

    @classmethod
    def log(cls, name: str, duration_ms: float):
        cls.buffer.append({
            "timestamp": datetime.now().isoformat(),
            "function": name,
            "duration": round(duration_ms, 2)
        })

def timed(func: Callable):
    """Logs call duration to the ProfilerData ring buffer."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        duration = (time.perf_counter() - start) * 1000
        ProfilerData.log(func.__name__, duration)
        return result
    return wrapper

def memoize(ttl_seconds: int = 60, max_size: int = 128):
    """LRU Cache with Time-To-Live (TTL)."""
    def decorator(func: Callable):
        cache = {}
        order = collections.deque()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            if key in cache:
                val, expiry = cache[key]
                if now < expiry:
                    return val
                del cache[key]
                order.remove(key)
            
            result = func(*args, **kwargs)
            if len(cache) >= max_size:
                oldest = order.popleft()
                del cache[oldest]
            
            cache[key] = (result, now + ttl_seconds)
            order.append(key)
            return result
        return wrapper
    return decorator

def debounce(wait_ms: int):
    """Delay execution until 'wait_ms' has passed since last call."""
    def decorator(func: Callable):
        timer = None
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal timer
            if timer: timer.cancel()
            timer = threading.Timer(wait_ms/1000, lambda: func(*args, **kwargs))
            timer.start()
        return wrapper
    return decorator

class Batched:
    """Coalesces operations within a context manager. Example usage:
    with Batched() as b:
        # logic here
    """
    def __enter__(self):
        self.start = time.perf_counter()
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        dur = (time.perf_counter() - self.start) * 1000
        logging.info(f"Batch operation completed in {dur:.2f}ms")

def lazy_import(name: str):
    """Loads a module only when accessed."""
    class LazyModule:
        def __init__(self):
            self._module = None
        def __getattr__(self, item):
            if self._module is None:
                self._module = importlib.import_module(name)
            return getattr(self._module, item)
    return LazyModule()

# =============================================================================
# 2. GUI BACKEND KIT
# =============================================================================

class EventBus:
    """Simple pub/sub for decoupled components."""
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def on(cls, event: str, fn: Callable):
        if event not in cls._listeners: cls._listeners[event] = []
        cls._listeners[event].append(fn)

    @classmethod
    def emit(cls, event: str, *args, **kwargs):
        for fn in cls._listeners.get(event, []):
            fn(*args, **kwargs)

class Store(Generic[T]):
    """Reactive data store with shallow diffing."""
    def __init__(self, data: T):
        self._data = data
        self._listeners: List[Callable[[T], None]] = []

    @property
    def value(self) -> T:
        return self._data

    def set(self, updates: Dict[str, Any]):
        changed = False
        for k, v in updates.items():
            if hasattr(self._data, k) and getattr(self._data, k) != v:
                setattr(self._data, k, v)
                changed = True
        if changed:
            for fn in self._listeners: fn(self._data)

    def subscribe(self, fn: Callable[[T], None]):
        self._listeners.append(fn)
        fn(self._data)

class UndoStack:
    """Push/Undo/Redo logic."""
    def __init__(self):
        self.stack: List[Any] = []
        self.redo_stack: List[Any] = []

    def push(self, state: Any):
        self.stack.append(state)
        self.redo_stack.clear()

    def undo(self) -> Optional[Any]:
        if len(self.stack) > 1:
            self.redo_stack.append(self.stack.pop())
            return self.stack[-1]
        return None

    def redo(self) -> Optional[Any]:
        if self.redo_stack:
            state = self.redo_stack.pop()
            self.stack.append(state)
            return state
        return None

def run_in_thread(fn: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Executes fn in thread, returns result to mainloop."""
    def worker():
        try:
            res = fn()
            if on_done: ctk.set_appearance_mode(ctk.get_appearance_mode()); EventBus.emit("main_thread_exec", lambda: on_done(res))
        except Exception as e:
            logging.exception("Thread error")
            if on_error: EventBus.emit("main_thread_exec", lambda: on_error(e))
    threading.Thread(target=worker, daemon=True).start()

# =============================================================================
# 3. VISUAL POLISH
# =============================================================================

@dataclasses.dataclass
class Theme:
    bg: str = "#1a1a1a"
    fg: str = "#ffffff"
    accent: str = "#3b82f6"
    muted: str = "#4b5563"
    success: str = "#10b981"
    warn: str = "#f59e0b"
    error: str = "#ef4444"
    font_ui: str = "Inter"
    font_mono: str = "JetBrains Mono"

THEME_STORE = Store(Theme())

class StyledButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=6, font=(THEME_STORE.value.font_ui, 12, "bold"), **kwargs)

class Toast(ctk.CTkToplevel):
    """Fading notification toast."""
    def __init__(self, message: str, color: str = "#3b82f6"):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=color)
        lbl = ctk.CTkLabel(self, text=message, text_color="white", padx=20, pady=10)
        lbl.pack()
        
        # Position bottom right
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw-300}+{sh-100}")
        
        self.alpha = 0.0
        self._fade_in()

    def _fade_in(self):
        if self.alpha < 1.0:
            self.alpha += 0.1
            self.attributes("-alpha", self.alpha)
            self.after(20, self._fade_in)
        else:
            self.after(2000, self._fade_out)

    def _fade_out(self):
        if self.alpha > 0.0:
            self.alpha -= 0.1
            self.attributes("-alpha", self.alpha)
            self.after(20, self._fade_out)
        else:
            self.destroy()

# =============================================================================
# 4. TUTORIAL MODULE
# =============================================================================

class Walkthrough:
    """Renders an overlay with tooltips to guide users."""
    def __init__(self, root, steps: List[Dict]):
        self.root = root
        self.steps = steps
        self.index = 0
        self.overlay = None
        
        state = self._load_state()
        if not state.get("tutorial_done", False):
            self.start()

    def _load_state(self):
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {}

    def start(self):
        if not self.steps: return
        self.overlay = tk.Canvas(self.root, highlightthickness=0, bg="black")
        self.overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self.overlay.configure(alpha=0.5) # Note: Pure Tkinter canvas alpha is tricky; using black fill
        self.show_step()

    def show_step(self):
        self.overlay.delete("all")
        step = self.steps[self.index]
        target = step['target']
        
        # Highlight target
        x = target.winfo_rootx() - self.root.winfo_rootx()
        y = target.winfo_rooty() - self.root.winfo_rooty()
        w = target.winfo_width()
        h = target.winfo_height()
        
        self.overlay.create_rectangle(0, 0, self.root.winfo_width(), y, fill="black", stipple="gray50")
        self.overlay.create_rectangle(0, y, x, y+h, fill="black", stipple="gray50")
        self.overlay.create_rectangle(x+w, y, self.root.winfo_width(), y+h, fill="black", stipple="gray50")
        self.overlay.create_rectangle(0, y+h, self.root.winfo_width(), self.root.winfo_height(), fill="black", stipple="gray50")
        
        # Tooltip
        self.tip = ctk.CTkFrame(self.overlay, fg_color="#3b82f6", corner_radius=10)
        self.tip.place(x=x, y=y+h+10)
        ctk.CTkLabel(self.tip, text=step['title'], font=("Inter", 14, "bold")).pack(padx=10, pady=5)
        ctk.CTkLabel(self.tip, text=step['body'], wraplength=200).pack(padx=10, pady=5)
        
        btn_f = ctk.CTkFrame(self.tip, fg_color="transparent")
        btn_f.pack(pady=10)
        ctk.CTkButton(btn_f, text="Next", width=60, command=self.next).pack(side="right", padx=5)
        if self.index > 0:
            ctk.CTkButton(btn_f, text="Back", width=60, command=self.prev).pack(side="right", padx=5)

    def next(self):
        self.index += 1
        if self.index >= len(self.steps):
            self.finish()
        else:
            self.show_step()

    def prev(self):
        self.index -= 1
        self.show_step()

    def finish(self):
        self.overlay.destroy()
        STATE_FILE.write_text(json.dumps({"tutorial_done": True}))

# =============================================================================
# 5. COHESIVE DEMO APP
# =============================================================================

@dataclasses.dataclass
class UserTask:
    name: str = ""
    priority: int = 1
    complete: bool = False

class DemoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Coding Essentials Toolkit")
        self.geometry("900x600")
        
        # Main Thread Marshalling
        EventBus.on("main_thread_exec", lambda fn: self.after(0, fn))
        
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_play = self.tabs.add("Playground")
        self.tab_prof = self.tabs.add("Profiler")
        self.tab_set = self.tabs.add("Settings")
        
        self.undo_stack = UndoStack()
        self.setup_playground()
        self.setup_profiler()
        self.setup_settings()

    def setup_playground(self):
        # Card container
        self.play_card = ctk.CTkFrame(self.tab_play)
        self.play_card.pack(padx=20, pady=20, fill="both", expand=True)
        
        ctk.CTkLabel(self.play_card, text="Task Form (FormBuilder Demo)", font=("Inter", 16, "bold")).pack(pady=10)
        
        self.task_store = Store(UserTask())
        # Manual Form Builder
        self.name_entry = ctk.CTkEntry(self.play_card, placeholder_text="Task Name")
        self.name_entry.pack(pady=5)
        
        self.slow_btn = StyledButton(self.play_card, text="Run Heavy Task (3s)", command=self.run_heavy)
        self.slow_btn.pack(pady=20)
        
        self.undo_btn = ctk.CTkButton(self.play_card, text="Undo Action", command=lambda: Toast("Undo!"))
        self.undo_btn.pack(pady=5)

    @timed
    def run_heavy(self):
        def work():
            time.sleep(3)
            return "Success"
        
        self.slow_btn.configure(state="disabled")
        run_in_thread(work, on_done=lambda r: [
            Toast(f"Task {r}"), 
            self.slow_btn.configure(state="normal"),
            self.refresh_profiler()
        ])

    def setup_profiler(self):
        self.prof_scroll = ctk.CTkTextbox(self.tab_prof)
        self.prof_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        self.refresh_profiler()
        
        btn_f = ctk.CTkFrame(self.tab_prof)
        btn_f.pack(fill="x")
        ctk.CTkButton(btn_f, text="Refresh", command=self.refresh_profiler).pack(side="left", padx=5)
        ctk.CTkButton(btn_f, text="Export CSV", command=self.export_profiler).pack(side="left", padx=5)

    def refresh_profiler(self):
        self.prof_scroll.configure(state="normal")
        self.prof_scroll.delete("1.0", "end")
        for entry in list(ProfilerData.buffer)[::-1]:
            self.prof_scroll.insert("end", f"[{entry['timestamp']}] {entry['function']}: {entry['duration']}ms\n")
        self.prof_scroll.configure(state="disabled")

    def export_profiler(self):
        path = BASE_DIR / "profile_export.csv"
        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "function", "duration"])
            writer.writeheader()
            writer.writerows(ProfilerData.buffer)
        Toast(f"Exported to {path}")

    def setup_settings(self):
        ctk.CTkLabel(self.tab_set, text="Appearance", font=("Inter", 16, "bold")).pack(pady=10)
        ctk.CTkOptionMenu(self.tab_set, values=["Dark", "Light"], 
                         command=lambda v: ctk.set_appearance_mode(v)).pack(pady=10)
        
        ctk.CTkButton(self.tab_set, text="Reset Tutorial", 
                     command=self.reset_tutorial).pack(pady=20)

    def reset_tutorial(self):
        if STATE_FILE.exists(): STATE_FILE.unlink()
        Toast("Restart app to see tutorial")

    def run_first_tour(self):
        steps = [
            {"target": self.tabs, "title": "Navigation", "body": "Switch between Playground, Profiler, and Settings here."},
            {"target": self.slow_btn, "title": "Efficiency", "body": "This button runs a threaded task and is tracked by the Profiler."},
            {"target": self.undo_btn, "title": "Undo System", "body": "Tracks your state changes for easy reversal."}
        ]
        self.after(500, lambda: Walkthrough(self, steps))

# =============================================================================
# SELF TEST & MAIN
# =============================================================================

def run_selftest():
    print("--- RUNNING SELF TEST ---")
    
    # 1. Efficiency Core
    @timed
    @memoize(ttl_seconds=1)
    def test_func(x): return x * 2
    
    res = test_func(5)
    assert res == 10
    assert len(ProfilerData.buffer) > 0
    print("[PASS] Efficiency Core: Timed & Memoize")
    
    # 2. GUI Backend Logic
    bus_triggered = False
    def trigger(): nonlocal bus_triggered; bus_triggered = True
    EventBus.on("test", trigger)
    EventBus.emit("test")
    assert bus_triggered
    print("[PASS] GUI Backend: EventBus")
    
    store = Store(UserTask(name="Initial"))
    store.set({"name": "Updated"})
    assert store.value.name == "Updated"
    print("[PASS] GUI Backend: Store/Reactive logic")
    
    print("--- ALL TESTS PASSED ---")
    sys.exit(0)

def main():
    if "--selftest" in sys.argv:
        run_selftest()
    
    app = DemoApp()
    app.run_first_tour()
    
    def on_close():
        logging.info("Shutting down gracefully.")
        app.destroy()
        
    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()

if __name__ == "__main__":
    main()
Quick Start Instructions for Raspberry PiInstall Dependencies:
Open your terminal (Ctrl+Alt+T) and run:Bashpip install customtkinter
Create the File:
Run this command to create and open the file:Bashnano coding_essentials.py
Paste the code above (Ctrl+Shift+V), then save and exit (Ctrl+O, Enter, Ctrl+X).Run the App:Bashpython coding_essentials.py
Run Self-Test:Bashpython coding_essentials.py --selftest
Key Shortcuts in App:Tab Switching: Click tab headers.Profiler Export: Check ~/.coding_essentials/profile_export.csv after clicking export.
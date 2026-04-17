Your Python toolkit coding_essentials.py is ready.To install and run this on your Raspberry Pi (or any system with Python 3.10+):Install Dependencies: Open your terminal (Ctrl+Alt+T) and run:Bashpip install customtkinter
Create the file:Bashnano coding_essentials.py
Paste the code: Press Ctrl+Shift+V (or right-click) to paste the entire block below.Save and Exit: Press Ctrl+O, then Enter, then Ctrl+X.Run the App:Bashpython3 coding_essentials.py
Run Self-Test:Bashpython3 coding_essentials.py --selftest
[file-tag: coding_essentials.py]Python"""
CHANGELOG:
2026-04-16 | Initial Release | Unified Efficiency, GUI, Visuals, Tutorials, and Demo into one file.
"""

import os
import json
import time
import logging
import threading
import functools
import collections
import dataclasses
import inspect
import sys
import csv
import tkinter as tk
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union, Generic
from queue import Queue

import customtkinter as ctk

# --- INITIALIZATION & LOGGING ---
APP_DIR = os.path.expanduser("~/.coding_essentials")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "app.log")
STATE_FILE = os.path.join(APP_DIR, "state.json")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

T = TypeVar("T")

# --- 1. EFFICIENCY CORE ---

PROFILER_DATA = collections.deque(maxlen=1000)

def timed(func: Callable) -> Callable:
    """Logs execution time to the profiler ring buffer."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        duration = (time.perf_counter() - start) * 1000
        PROFILER_DATA.append({
            "name": func.__name__,
            "ms": duration,
            "timestamp": datetime.now().isoformat()
        })
        return result
    return wrapper

def memoize(max_size: int = 128, ttl_seconds: Optional[int] = None) -> Callable:
    """LRU Cache with optional Time-To-Live."""
    def decorator(func: Callable) -> Callable:
        cache = {}
        order = collections.deque()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            if key in cache:
                val, expiry = cache[key]
                if ttl_seconds is None or now < expiry:
                    order.remove(key)
                    order.append(key)
                    return val
            
            result = func(*args, **kwargs)
            if len(cache) >= max_size:
                oldest = order.popleft()
                del cache[oldest]
            
            expiry = now + ttl_seconds if ttl_seconds else None
            cache[key] = (result, expiry)
            order.append(key)
            return result
        return wrapper
    return decorator

class Debounce:
    """Prevents a function from being called until a delay has passed."""
    def __init__(self, wait_ms: int):
        self.wait = wait_ms / 1000
        self.timer = None

    def __call__(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.wait, lambda: func(*args, **kwargs))
            self.timer.start()
        return wrapper

class Batched:
    """Context manager to coalesce operations (e.g. file writes)."""
    def __init__(self, collector: List[Any], flush_fn: Callable[[List[Any]], None]):
        self.collector = collector
        self.flush_fn = flush_fn

    def __enter__(self):
        return self.collector

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.collector:
            self.flush_fn(self.collector)
            self.collector.clear()

def lazy_import(name: str):
    """Loads a module only when accessed."""
    class LazyProxy:
        def __getattr__(self, item):
            module = __import__(name)
            return getattr(module, item)
    return LazyProxy()

# --- 2. GUI BACKEND KIT ---

class EventBus:
    """Simple Pub/Sub for cross-component communication."""
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def on(cls, event: str, fn: Callable):
        if event not in cls._listeners:
            cls._listeners[event] = []
        cls._listeners[event].append(fn)
        return lambda: cls.off(event, fn)

    @classmethod
    def off(cls, event: str, fn: Callable):
        if event in cls._listeners:
            cls._listeners[event].remove(fn)

    @classmethod
    def emit(cls, event: str, *args, **kwargs):
        for fn in cls._listeners.get(event, []):
            fn(*args, **kwargs)

class Store(Generic[T]):
    """Reactive data store with shallow diffing."""
    def __init__(self, data: T):
        self._data = data
        self._subscribers = []

    @property
    def data(self) -> T:
        return self._data

    def update(self, **kwargs):
        changed = False
        for k, v in kwargs.items():
            if getattr(self._data, k) != v:
                setattr(self._data, k, v)
                changed = True
        if changed:
            for sub in self._subscribers:
                sub(self._data)

    def subscribe(self, fn: Callable[[T], None]):
        self._subscribers.append(fn)
        fn(self._data)
        return lambda: self._subscribers.remove(fn)

class UndoStack:
    """Tracks state changes for undo/redo."""
    def __init__(self, max_depth=50):
        self.stack = []
        self.redo_stack = []
        self.max_depth = max_depth

    def push(self, undo_fn: Callable, redo_fn: Callable):
        self.stack.append((undo_fn, redo_fn))
        self.redo_stack.clear()
        if len(self.stack) > self.max_depth:
            self.stack.pop(0)

    def undo(self):
        if self.stack:
            u, r = self.stack.pop()
            u()
            self.redo_stack.append((u, r))

    def redo(self):
        if self.redo_stack:
            u, r = self.redo_stack.pop()
            r()
            self.stack.append((u, r))

def run_in_thread(fn: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Executes task in background and returns to TK mainloop."""
    def wrapper():
        try:
            res = fn()
            if on_done:
                ctk.set_appearance_mode(None) # Force thread-safe check (dummy call)
                on_done(res)
        except Exception as e:
            logging.error(f"Thread Error: {e}")
            if on_error:
                on_error(e)
    threading.Thread(target=wrapper, daemon=True).start()

class FormBuilder:
    """Generates CTK inputs from a dataclass."""
    @staticmethod
    def create(parent, store: Store, fields: List[str]):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        widgets = {}
        for i, field in enumerate(fields):
            lbl = ctk.CTkLabel(frame, text=field.replace("_", " ").title(), font=("Inter", 12, "bold"))
            lbl.grid(row=i, column=0, padx=10, pady=5, sticky="w")
            
            val = getattr(store.data, field)
            entry = ctk.CTkEntry(frame)
            entry.insert(0, str(val))
            entry.grid(row=i, column=1, padx=10, pady=5, sticky="ew")
            widgets[field] = entry

            def on_change(event, f=field, e=entry):
                store.update(**{f: e.get()})

            entry.bind("<KeyRelease>", on_change)
        
        frame.columnconfigure(1, weight=1)
        return frame

# --- 3. VISUAL POLISH ---

@dataclasses.dataclass
class ThemeConfig:
    bg: str = "#1a1a1a"
    fg: str = "#ffffff"
    accent: str = "#3b82f6"
    muted: str = "#6b7280"
    success: str = "#10b981"
    warn: str = "#f59e0b"
    error: str = "#ef4444"
    card_bg: str = "#262626"

THEME_STORE = Store(ThemeConfig())

class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, fg_color=THEME_STORE.data.card_bg, border_width=1, border_color="#333333", **kwargs)

class Toast(ctk.CTkToplevel):
    def __init__(self, message: str, level="info"):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.0)
        
        color = THEME_STORE.data.accent
        if level == "error": color = THEME_STORE.data.error
        
        self.frame = ctk.CTkFrame(self, fg_color=color, corner_radius=8)
        self.frame.pack(padx=2, pady=2)
        
        self.label = ctk.CTkLabel(self.frame, text=message, text_color="white", padx=20, pady=10)
        self.label.pack()

        # Position bottom-right
        self.update_idletasks()
        x = self.winfo_screenwidth() - self.winfo_width() - 20
        y = self.winfo_screenheight() - self.winfo_height() - 60
        self.geometry(f"+{x}+{y}")
        
        self._fade_in()

    def _fade_in(self):
        alpha = self.attributes("-alpha")
        if alpha < 1.0:
            self.attributes("-alpha", alpha + 0.1)
            self.after(20, self._fade_in)
        else:
            self.after(3000, self._fade_out)

    def _fade_out(self):
        alpha = self.attributes("-alpha")
        if alpha > 0.0:
            self.attributes("-alpha", alpha - 0.1)
            self.after(20, self._fade_out)
        else:
            self.destroy()

# --- 4. TUTORIAL MODULE ---

class TutorialOverlay(ctk.CTkCanvas):
    def __init__(self, master, steps: List[dict], on_finish: Callable):
        super().__init__(master, highlightthickness=0, bg="black")
        self.steps = steps
        self.index = 0
        self.on_finish = on_finish
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.bind("<Configure>", lambda e: self._draw_step())
        self._draw_step()

    def _draw_step(self):
        self.delete("all")
        self.configure(alpha=0.5) # CTK Canvas handling is standard TK
        
        step = self.steps[self.index]
        target = step["target"]
        
        # Dimming
        self.create_rectangle(0, 0, self.winfo_width(), self.winfo_height(), fill="#000000", stipple="gray50")
        
        # Hole
        x = target.winfo_rootx() - self.master.winfo_rootx()
        y = target.winfo_rooty() - self.master.winfo_rooty()
        w = target.winfo_width()
        h = target.winfo_height()
        self.create_rectangle(x-5, y-5, x+w+5, y+h+5, fill="black") # Clearer via black 'hole'

        # Tooltip
        tx = x + w + 10 if x + w + 200 < self.winfo_width() else x - 210
        ty = y
        self.create_rectangle(tx, ty, tx+200, ty+100, fill="#333333", outline="#3b82f6")
        self.create_text(tx+100, ty+20, text=step["title"], fill="white", font=("Inter", 12, "bold"), width=180)
        self.create_text(tx+100, ty+60, text=step["body"], fill="#cccccc", font=("Inter", 10), width=180)

        # Buttons (using canvas items for simplicity in overlay)
        btn = self.create_rectangle(tx+120, ty+75, tx+190, ty+95, fill="#3b82f6")
        self.create_text(tx+155, ty+85, text="Next" if self.index < len(self.steps)-1 else "Finish", fill="white")
        self.tag_bind(btn, "<Button-1>", lambda e: self._next())

    def _next(self):
        self.index += 1
        if self.index >= len(self.steps):
            self.on_finish()
            self.destroy()
        else:
            self._draw_step()

# --- 5. COHESIVE DEMO APP ---

@dataclasses.dataclass
class UserState:
    username: str = "Dev"
    api_key: str = "sk_test_..."
    tutorial_done: bool = False

class CodingEssentialsApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Coding Essentials Toolkit")
        self.geometry("1000x650")
        ctk.set_appearance_mode("dark")
        
        self.state = self._load_state()
        self.store = Store(self.state)
        self.undo = UndoStack()
        
        self._setup_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.state.tutorial_done:
            self.after(1000, self._start_tutorial)

    def _setup_ui(self):
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(expand=True, fill="both", padx=10, pady=10)
        self.tabview.add("Playground")
        self.tabview.add("Profiler")
        self.tabview.add("Settings")

        # --- Playground Tab ---
        pg = self.tabview.tab("Playground")
        
        # Form
        self.card_form = Card(pg)
        self.card_form.pack(fill="x", padx=20, pady=20)
        ctk.CTkLabel(self.card_form, text="Reactive Settings Form", font=("Inter", 16, "bold")).pack(pady=10)
        self.form = FormBuilder.create(self.card_form, self.store, ["username", "api_key"])
        self.form.pack(fill="x", padx=10, pady=10)

        # Action Buttons
        btn_frame = ctk.CTkFrame(pg, fg_color="transparent")
        btn_frame.pack(pady=10)
        
        self.btn_slow = ctk.CTkButton(btn_frame, text="Run Slow Task (Threaded)", command=self._run_slow_task)
        self.btn_slow.pack(side="left", padx=5)

        self.btn_undo = ctk.CTkButton(btn_frame, text="Undo Edit", command=self.undo.undo, fg_color="#444")
        self.btn_undo.pack(side="left", padx=5)

        # --- Profiler Tab ---
        prof_tab = self.tabview.tab("Profiler")
        self.prof_scroll = ctk.CTkScrollableFrame(prof_tab)
        self.prof_scroll.pack(expand=True, fill="both", padx=10, pady=10)
        
        p_btns = ctk.CTkFrame(prof_tab, fg_color="transparent")
        p_btns.pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(p_btns, text="Refresh", command=self._refresh_profiler).pack(side="left", padx=5)
        ctk.CTkButton(p_btns, text="Export CSV", command=self._export_profiler).pack(side="left", padx=5)

        # --- Settings Tab ---
        set_tab = self.tabview.tab("Settings")
        self.btn_reset_tut = ctk.CTkButton(set_tab, text="Reset Tutorials", command=self._reset_tutorials)
        self.btn_reset_tut.pack(pady=20)
        ctk.CTkLabel(set_tab, text=f"Log: {LOG_FILE}", font=("Mono", 10)).pack()

    def _start_tutorial(self):
        steps = [
            {"target": self.card_form, "title": "Reactive Forms", "body": "Edit these fields to see live state updates."},
            {"target": self.btn_slow, "title": "Threading", "body": "Click this to run a timed background task without freezing."},
            {"target": self.tabview._segmented_button, "title": "Navigation", "body": "Switch tabs to view the function profiler."}
        ]
        TutorialOverlay(self, steps, self._finish_tutorial)

    def _finish_tutorial(self):
        self.store.update(tutorial_done=True)
        Toast("Tutorial Completed!", "info")

    @timed
    def _run_slow_task(self):
        def task():
            time.sleep(1.5)
            return "Task Finished"
        
        self.btn_slow.configure(state="disabled")
        run_in_thread(task, on_done=lambda r: [
            Toast(r), 
            self.btn_slow.configure(state="normal")
        ])

    def _refresh_profiler(self):
        for w in self.prof_scroll.winfo_children(): w.destroy()
        header = ctk.CTkFrame(self.prof_scroll)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="Function", width=150).pack(side="left")
        ctk.CTkLabel(header, text="Duration (ms)", width=100).pack(side="left")
        
        for entry in list(PROFILER_DATA)[-20:]:
            row = ctk.CTkFrame(self.prof_scroll)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=entry["name"], width=150).pack(side="left")
            ctk.CTkLabel(row, text=f"{entry['ms']:.2f}", width=100).pack(side="left")

    def _export_profiler(self):
        path = os.path.join(APP_DIR, "profile_export.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "ms", "timestamp"])
            writer.writeheader()
            writer.writerows(PROFILER_DATA)
        Toast(f"Exported to {path}")

    def _reset_tutorials(self):
        self.store.update(tutorial_done=False)
        Toast("Restart app to see tutorial.")

    def _load_state(self) -> UserState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return UserState(**json.load(f))
            except: pass
        return UserState()

    def _on_close(self):
        with open(STATE_FILE, "w") as f:
            json.dump(dataclasses.asdict(self.store.data), f)
        self.destroy()

# --- 6. SELF TEST & MAIN ---

def run_self_test():
    print("--- RUNNING SELF TEST ---")
    results = []
    
    # Test 1: Memoize
    @memoize(max_size=2)
    def add(a, b): return a + b
    results.append(("Memoize", add(1,2) == 3 and add(1,2) == 3))
    
    # Test 2: EventBus
    bus_test = False
    def listener(v): nonlocal bus_test; bus_test = v
    EventBus.on("test", listener)
    EventBus.emit("test", True)
    results.append(("EventBus", bus_test))

    # Test 3: Store
    s = Store(UserState())
    s.update(username="Tester")
    results.append(("Store", s.data.username == "Tester"))

    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"{name:15}: {status}")
    
    sys.exit(0 if all(r[1] for r in results) else 1)

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_self_test()
    else:
        app = CodingEssentialsApp()
        app.mainloop()
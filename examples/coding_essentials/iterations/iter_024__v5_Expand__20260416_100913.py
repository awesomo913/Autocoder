1. Create and integrate the plugin
Use the following commands in your Raspberry Pi terminal to create the new plugin file.

Bash
# Create and open the new plugin file
nano system_telemetry_plugin.py

# (Use Ctrl+Shift+V or Right-Click to paste the code below)
# Save and exit: Ctrl+O, Enter, Ctrl+X

# Make the script executable
chmod +x system_telemetry_plugin.py

# To run and attach it to the existing toolkit:
python3 system_telemetry_plugin.py


2. Raw Code Replacement
Below is the complete system_telemetry_plugin.py. It integrates seamlessly with coding_essentials.py by dynamically injecting a new "Telemetry" tab into the existing DemoApp, adding a hardware-aware metrics dashboard and a thread-safe task queueing system.

Python
#!/usr/bin/env python3
"""
system_telemetry_plugin.py - Hardware Monitoring and Job Queue Extension

This module extends coding_essentials.py without modifying it.
It adds:
1. A robust hardware poller (optimized for Raspberry Pi thermals/memory).
2. A thread-safe, strict-turn-taking Job Queue to prevent hardware overload.
3. A reactive Telemetry Dashboard UI.
"""

import os
import sys
import time
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import customtkinter as ctk # type: ignore
except ImportError:
    print("FATAL: customtkinter not found.")
    sys.exit(1)

# Dynamically import the existing codebase
try:
    import coding_essentials as ce
except ImportError:
    print("FATAL: coding_essentials.py must be in the same directory.")
    sys.exit(1)


# =============================================================================
# HARDWARE ABSTRACTION & LIMITS
# =============================================================================

@dataclass
class SystemMetrics:
    cpu_usage: float = 0.0
    ram_usage_mb: float = 0.0
    ram_total_mb: float = 16384.0  # Defaulting to 16GB limit
    temperature_c: float = 0.0
    thermal_throttling: bool = False

MetricsStore = ce.Store(SystemMetrics())

class HardwarePoller:
    """Reads raw hardware stats gracefully, failing over to mocks if not on Linux/Pi."""
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_idle = 0
        self._last_total = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        ce.EventBus.emit("sys_log", "HardwarePoller initialized.")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _poll_loop(self):
        while self._running:
            metrics = self._read_metrics()
            # Push updates to the main loop via ce.Store
            ce._MAIN_LOOP_QUEUE.put(lambda: MetricsStore.set(**metrics))
            time.sleep(1.0)

    def _read_metrics(self) -> Dict[str, Any]:
        """Reads system files directly to avoid external psutil dependencies."""
        data = {
            "cpu_usage": 0.0,
            "ram_usage_mb": 0.0,
            "ram_total_mb": 16384.0,
            "temperature_c": 0.0,
            "thermal_throttling": False
        }

        # 1. Read CPU
        try:
            with open('/proc/stat', 'r') as f:
                lines = f.readlines()
                cpu_line = lines[0].split()
                if cpu_line[0] == 'cpu':
                    idle = float(cpu_line[4]) + float(cpu_line[5])
                    total = sum(float(x) for x in cpu_line[1:8])
                    idle_delta = idle - self._last_idle
                    total_delta = total - self._last_total
                    self._last_idle = idle
                    self._last_total = total
                    if total_delta > 0:
                        data["cpu_usage"] = 100.0 * (1.0 - idle_delta / total_delta)
        except Exception:
            data["cpu_usage"] = -1.0 # Unsupported

        # 2. Read RAM
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = {}
                for line in f:
                    parts = line.split(':')
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].split()[0])
                total_kb = meminfo.get('MemTotal', 16777216)
                free_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
                data["ram_total_mb"] = total_kb / 1024.0
                data["ram_usage_mb"] = (total_kb - free_kb) / 1024.0
        except Exception:
            data["ram_usage_mb"] = -1.0

        # 3. Read Temp (Raspberry Pi specific)
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp_millicigrades = int(f.read().strip())
                data["temperature_c"] = temp_millicigrades / 1000.0
                if data["temperature_c"] > 80.0:
                    data["thermal_throttling"] = True
                    ce.EventBus.emit("sys_warn", f"Thermal Throttle limit reached: {data['temperature_c']:.1f}°C")
        except Exception:
            data["temperature_c"] = -1.0

        return data


# =============================================================================
# QUEUEING & TURN-TAKING ENGINE
# =============================================================================

@dataclass
class JobState:
    pending_count: int = 0
    active_job: str = "None"
    completed_count: int = 0

JobStore = ce.Store(JobState())

class JobQueue:
    """Strict serial execution queue preventing hardware saturation from parallel tasks."""
    def __init__(self):
        self._q = queue.Queue()
        self._running = False
        self._worker_thread = None
        self._completed = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()

    def stop(self):
        self._running = False
        self._q.put(None) # Poison pill

    def enqueue(self, job_name: str, fn: Callable[[], Any], on_done: Optional[Callable] = None):
        """Adds a job to the strict execution queue."""
        self._q.put((job_name, fn, on_done))
        self._update_store()
        ce.EventBus.emit("job_enqueued", job_name)

    def _process_queue(self):
        while self._running:
            item = self._q.get()
            if item is None:
                break
            
            job_name, fn, on_done = item
            
            # Sync state to UI safely
            def set_active(): JobStore.set(active_job=job_name)
            ce._MAIN_LOOP_QUEUE.put(set_active)
            ce.EventBus.emit("sys_log", f"Executing job: {job_name}")
            
            try:
                res = fn()
                if on_done:
                    ce._MAIN_LOOP_QUEUE.put(lambda: on_done(res))
            except Exception as e:
                ce.log_exception(e, f"JobQueue Error in {job_name}")
                ce.EventBus.emit("sys_warn", f"Job Failed: {job_name}")
            finally:
                self._completed += 1
                self._q.task_done()
                ce._MAIN_LOOP_QUEUE.put(self._update_store)

    def _update_store(self):
        JobStore.set(
            pending_count=self._q.qsize(),
            active_job="None" if self._q.empty() else JobStore.state.active_job,
            completed_count=self._completed
        )


# =============================================================================
# PLUGIN USER INTERFACE
# =============================================================================

class ProgressBarCard(ce.Card):
    """Custom UI component for rendering telemetry limits safely."""
    def __init__(self, master, title: str, unit: str, max_val: float, **kwargs):
        super().__init__(master, **kwargs)
        self.max_val = max_val
        self.unit = unit
        
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(10, 0))
        
        ctk.CTkLabel(header, text=title, font=ce.ThemeStore.state.fonts_bold["ui"]).pack(side="left")
        self.lbl_val = ctk.CTkLabel(header, text=f"0 {unit}", font=ce.ThemeStore.state.fonts["mono"])
        self.lbl_val.pack(side="right")
        
        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=10, pady=10)
        self.progress.set(0)

    def update_value(self, current: float):
        if current < 0:
            self.lbl_val.configure(text="N/A")
            self.progress.set(0)
            return

        clamped = min(max(current, 0.0), self.max_val)
        ratio = clamped / self.max_val
        self.progress.set(ratio)
        self.lbl_val.configure(text=f"{current:.1f} {self.unit}")
        
        # Color shift for stability warnings
        if ratio > 0.85:
            self.progress.configure(progress_color=ce.ThemeStore.state.colors.error)
        elif ratio > 0.70:
            self.progress.configure(progress_color=ce.ThemeStore.state.colors.warn)
        else:
            self.progress.configure(progress_color=ce.ThemeStore.state.colors.accent)


class TelemetryDashboard(ctk.CTkFrame):
    def __init__(self, master, job_queue: JobQueue, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.job_queue = job_queue
        
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        
        # Hardware Metrics Column
        hw_frame = ctk.CTkFrame(self, fg_color="transparent")
        hw_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        
        ctk.CTkLabel(hw_frame, text="Hardware Telemetry", font=ce.ThemeStore.state.fonts_bold["heading"]).pack(anchor="w", pady=(0, 10))
        
        self.cpu_card = ProgressBarCard(hw_frame, "CPU Utilization", "%", 100.0)
        self.cpu_card.pack(fill="x", pady=5)
        
        self.ram_card = ProgressBarCard(hw_frame, "Memory Usage", "MB", 16384.0)
        self.ram_card.pack(fill="x", pady=5)
        
        self.temp_card = ProgressBarCard(hw_frame, "Core Temp", "°C", 85.0)
        self.temp_card.pack(fill="x", pady=5)
        
        # Binding Store to UI
        MetricsStore.subscribe(self._on_metrics_update)

        # Job Queue Column
        q_frame = ce.Card(self)
        q_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        
        ctk.CTkLabel(q_frame, text="Task Queue Controller", font=ce.ThemeStore.state.fonts_bold["heading"]).pack(anchor="w", padx=10, pady=10)
        ce.Divider(q_frame).pack(fill="x", padx=10)
        
        self.lbl_active = ctk.CTkLabel(q_frame, text="Active: None", font=ce.ThemeStore.state.fonts["mono"])
        self.lbl_active.pack(anchor="w", padx=10, pady=(10, 0))
        
        self.lbl_pending = ctk.CTkLabel(q_frame, text="Pending: 0")
        self.lbl_pending.pack(anchor="w", padx=10)
        
        self.lbl_completed = ctk.CTkLabel(q_frame, text="Completed: 0")
        self.lbl_completed.pack(anchor="w", padx=10, pady=(0, 10))
        
        JobStore.subscribe(self._on_job_update)

        ce.Divider(q_frame).pack(fill="x", padx=10, pady=10)
        
        # Test controls for strict queueing
        ctrl_frame = ctk.CTkFrame(q_frame, fg_color="transparent")
        ctrl_frame.pack(fill="x", padx=10, pady=10)
        
        ce.StyledButton(ctrl_frame, text="Enqueue Sleep Task", command=self._add_dummy_job).pack(side="left", padx=5)
        ce.StyledButton(ctrl_frame, text="Enqueue Math Task", variant="success", command=self._add_math_job).pack(side="left", padx=5)

    def _on_metrics_update(self, state: SystemMetrics):
        self.cpu_card.update_value(state.cpu_usage)
        self.ram_card.max_val = state.ram_total_mb
        self.ram_card.update_value(state.ram_usage_mb)
        self.temp_card.update_value(state.temperature_c)

    def _on_job_update(self, state: JobState):
        self.lbl_active.configure(text=f"Active: {state.active_job}")
        self.lbl_pending.configure(text=f"Pending: {state.pending_count}")
        self.lbl_completed.configure(text=f"Completed: {state.completed_count}")

    def _add_dummy_job(self):
        job_id = f"SleepJob-{JobStore.state.pending_count + JobStore.state.completed_count}"
        def work():
            time.sleep(2.0)
            return "Woke up."
        self.job_queue.enqueue(job_id, work, lambda res: ce.Toast.show(self.winfo_toplevel(), f"{job_id}: {res}"))

    @ce.timed
    def _add_math_job(self):
        job_id = f"MathJob-{JobStore.state.pending_count + JobStore.state.completed_count}"
        def work():
            # Simulate heavy block
            return sum(i * i for i in range(5_000_000))
        self.job_queue.enqueue(job_id, work, lambda res: ce.EventBus.emit("sys_log", f"{job_id} calculated {res}"))


# =============================================================================
# PLUGIN INTEGRATION LAYER
# =============================================================================

class TelemetryPlugin:
    """Bootstrapper that mounts the plugin into the existing DemoApp running instance."""
    def __init__(self, target_app: ce.DemoApp):
        self.app = target_app
        self.poller = HardwarePoller()
        self.job_queue = JobQueue()
        
        # Inject new tab into existing UI
        self.tab_telemetry = self.app.tabview.add("Telemetry (Pi)")
        self.dashboard = TelemetryDashboard(self.tab_telemetry, self.job_queue)
        self.dashboard.pack(fill="both", expand=True)

        # Event Bus Hook for Toasts
        ce.EventBus.on("sys_warn", lambda msg: ce._MAIN_LOOP_QUEUE.put(lambda: ce.Toast.show(self.app, msg, variant="error")))

        self.start()

    def start(self):
        self.poller.start()
        self.job_queue.start()
        ce.Toast.show(self.app, "Telemetry Module Loaded & Polling Started")

    def stop(self):
        self.poller.stop()
        self.job_queue.stop()

# =============================================================================
# CLI WRAPPER & EXECUTION
# =============================================================================

if __name__ == "__main__":
    print("Initializing Coding Essentials App with Telemetry Plugin...")
    
    # 1. Instantiate the master app (from coding_essentials)
    app = ce.DemoApp()
    
    # 2. Attach the new capabilities seamlessly
    plugin = TelemetryPlugin(app)
    
    # 3. Handle graceful shutdown
    original_closing = app._on_closing
    def new_closing():
        plugin.stop()
        original_closing()
        
    app.protocol("WM_DELETE_WINDOW", new_closing)
    
    # 4. Run loop
    app.mainloop()

Explanation

This extension addresses the structural need for hardware awareness and safe background processing.

Anticipates Hardware Limits: The HardwarePoller queries /proc/stat, /proc/meminfo, and Raspberry Pi's specific /sys/class/thermal/thermal_zone0/temp to keep an eye on hardware throttling without requiring heavy third-party packages. If those files don't exist (e.g., if you run it on a non-Pi machine), it catches the exception and outputs neutral fallback states so it won't crash.

System Queueing/Turn-taking: JobQueue creates a strict serialized queue.Queue background thread. It guarantees that heavy operations process one by one. You queue jobs through enqueue(job_name, function, on_done_callback), which naturally stops Pi architectures from freezing under multi-threaded parallel computation loads.

Integration Layer: The TelemetryPlugin class hooks directly into the existing DemoApp instance. By passing app = ce.DemoApp() into it, it patches into the existing customtkinter Tabview, appends a new tab, and wires into the global exception loop and EventBus logic without modifying the parent file's logic.
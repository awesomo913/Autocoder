To set up and run this test suite on your Raspberry Pi, follow these instructions.

1. Create the Test File

First, create the test script in the same directory as your coding_essentials.py file.

Bash
# Install pytest if you haven't already
pip install pytest

# Create and open the test file
nano test_coding_essentials.py

# (Use Ctrl+Shift+V or Right-Click to paste the code below)
# Save and exit: Ctrl+O, Enter, Ctrl+X

# Run the test suite
pytest test_coding_essentials.py -v

2. Test Suite Raw Code Replacement

Paste the following complete code into test_coding_essentials.py.

Python
#!/usr/bin/env python3
"""
test_coding_essentials.py - Comprehensive pytest suite for coding_essentials.py
"""

import os
import time
import pytest
import csv
from dataclasses import dataclass
from typing import Any

# Import the target module
import coding_essentials as ce

# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture(autouse=True)
def reset_globals():
    """Resets global states (Profiler, EventBus) before each test."""
    ce.Profiler.clear()
    ce.EventBus._listeners.clear()
    yield

@pytest.fixture
def sample_dataclass():
    @dataclass
    class AppState:
        user: str = "guest"
        count: int = 0
        is_active: bool = False
    return AppState

# =============================================================================
# UNIT & EDGE CASE TESTS: EFFICIENCY CORE
# =============================================================================

def test_profiler_record_and_clear():
    """Test standard recording and clearing of the profiler."""
    ce.Profiler.record("func_a", 15.5)
    ce.Profiler.record("func_a", 10.0)
    ce.Profiler.record("func_b", 5.0)

    assert "func_a" in ce.Profiler.stats
    assert ce.Profiler.stats["func_a"].count == 2
    assert ce.Profiler.stats["func_a"].total_ms == 25.5
    assert len(ce.Profiler.ring_buffer) == 3

    ce.Profiler.clear()
    assert len(ce.Profiler.stats) == 0
    assert len(ce.Profiler.ring_buffer) == 0

def test_profiler_csv_export(tmp_path):
    """Test exporting profiler data to a CSV file."""
    csv_file = tmp_path / "test_profile.csv"
    ce.Profiler.record("test_op", 42.0)
    ce.Profiler.flush_to_csv(str(csv_file))

    assert csv_file.exists()
    with open(csv_file, 'r') as f:
        reader = csv.reader(f)
        rows = list(reader)
        assert len(rows) == 2  # Header + 1 record
        assert rows[0] == ["Timestamp", "Function", "Duration_ms"]
        assert rows[1][1] == "test_op"
        assert rows[1][2] == "42.0"

def test_timed_decorator():
    """Test that the @timed decorator logs execution correctly."""
    @ce.timed
    def slow_func():
        time.sleep(0.01)
        return "done"

    result = slow_func()
    assert result == "done"
    assert "slow_func" in ce.Profiler.stats
    assert ce.Profiler.stats["slow_func"].count == 1
    assert ce.Profiler.stats["slow_func"].total_ms >= 10.0

@pytest.mark.parametrize("a, b, expected", [
    (1, 2, 3),
    ("foo", "bar", "foobar"),
    (0, 0, 0),
])
def test_memoize_standard_and_edge_types(a, b, expected):
    """Test memoization caching with various input types."""
    call_count = 0

    @ce.memoize(ttl=60)
    def compute(x, y):
        nonlocal call_count
        call_count += 1
        return x + y

    assert compute(a, b) == expected
    assert compute(a, b) == expected
    assert call_count == 1  # Should only be called once

def test_memoize_ttl_expiration():
    """Test that memoized cache expires based on TTL."""
    call_count = 0

    @ce.memoize(ttl=0.05)
    def fetch_data():
        nonlocal call_count
        call_count += 1
        return call_count

    assert fetch_data() == 1
    assert fetch_data() == 1  # Cache hit
    time.sleep(0.06)          # Wait for TTL to expire
    assert fetch_data() == 2  # Cache miss, recalcs

def test_debounce_decorator():
    """Test that debounce prevents execution until cooldown finishes."""
    calls = []

    @ce.debounce(0.05)
    def trigger(val):
        calls.append(val)

    trigger(1)
    trigger(2)
    trigger(3)
    
    assert len(calls) == 0  # Shouldn't run immediately
    time.sleep(0.1)         # Wait for debounce timer
    assert calls == [3]     # Only the last call should execute

def test_throttle_decorator():
    """Test that throttle limits execution frequency."""
    calls = 0

    @ce.throttle(0.05)
    def increment():
        nonlocal calls
        calls += 1

    increment() # Executes
    increment() # Blocked
    increment() # Blocked
    assert calls == 1
    
    time.sleep(0.06)
    increment() # Executes again
    assert calls == 2

def test_batched_context_manager_success():
    """Test that Batched coalesces items and calls the action function."""
    processed = []
    
    def bulk_action(items):
        processed.extend(items)

    with ce.Batched(bulk_action) as b:
        b.add("A")
        b.add("B")
        
    assert processed == ["A", "B"]

def test_batched_context_manager_exception_bypass():
    """Test that Batched aborts execution if an exception occurs inside the block."""
    processed = []
    
    with pytest.raises(ValueError):
        with ce.Batched(lambda items: processed.extend(items)) as b:
            b.add(1)
            raise ValueError("Something broke")
            
    assert processed == [] # Action should not have been called

def test_lazy_import():
    """Test the lazy_import wrapper."""
    json_mod = ce.lazy_import("json")
    assert hasattr(json_mod, "dumps")
    
    with pytest.raises(ImportError):
        ce.lazy_import("non_existent_module_12345")

# =============================================================================
# UNIT & EDGE CASE TESTS: GUI BACKEND KIT
# =============================================================================

def test_eventbus_lifecycle():
    """Test standard publish/subscribe mechanics of EventBus."""
    payloads = []
    
    def listener(data):
        payloads.append(data)

    ce.EventBus.on("update", listener)
    ce.EventBus.emit("update", "version_1")
    ce.EventBus.emit("update", "version_2")
    
    assert payloads == ["version_1", "version_2"]
    
    ce.EventBus.off("update", listener)
    ce.EventBus.emit("update", "version_3")
    
    assert payloads == ["version_1", "version_2"] # Shouldn't receive v3

def test_eventbus_error_isolation():
    """Test that one failing listener doesn't crash the EventBus or other listeners."""
    results = []
    
    def bad_listener(x):
        raise RuntimeError("Crash")
        
    def good_listener(x):
        results.append(x)
        
    ce.EventBus.on("safe_event", bad_listener)
    ce.EventBus.on("safe_event", good_listener)
    
    # Emit should log the exception internally but continue processing
    ce.EventBus.emit("safe_event", 100)
    assert results == [100]

def test_store_requires_dataclass():
    """Test that Store rejects non-dataclass initialization."""
    with pytest.raises(ValueError, match="must be a dataclass instance"):
        ce.Store({"dict": "is_invalid"})

def test_store_state_updates_and_subscriptions(sample_dataclass):
    """Test Store updates values and fires wildcard subscriptions."""
    store = ce.Store(sample_dataclass())
    notified_states = []
    
    store.subscribe(lambda s: notified_states.append(s.count))
    
    store.set(count=1)
    store.set(user="admin")
    
    assert store.state.count == 1
    assert store.state.user == "admin"
    assert notified_states == [1, 1]

def test_store_selective_subscription(sample_dataclass):
    """Test Store only fires target subscriptions when specific keys change."""
    store = ce.Store(sample_dataclass())
    calls = 0
    
    def on_count_change(state):
        nonlocal calls
        calls += 1
        
    store.subscribe(on_count_change, keys=["count"])
    
    store.set(user="admin") # Shouldn't fire
    store.set(is_active=True) # Shouldn't fire
    store.set(count=5) # Should fire
    store.set(count=5) # Value unchanged, shouldn't fire
    
    assert calls == 1

def test_undostack_push_undo_redo():
    """Test core undo stack operations."""
    history = []
    stack = ce.UndoStack()
    
    def do_action(val): history.append(val)
    def undo_action(): history.pop()
    
    stack.push(lambda: do_action("A"), undo_action)
    stack.push(lambda: do_action("B"), undo_action)
    
    assert history == ["A", "B"]
    
    stack.undo()
    assert history == ["A"]
    
    stack.undo()
    assert history == []
    
    stack.redo()
    assert history == ["A"]

def test_undostack_edge_cases():
    """Test UndoStack boundary safety (undoing when empty, redoing when full)."""
    stack = ce.UndoStack()
    val = {"x": 0}
    
    # Undoing an empty stack shouldn't crash
    stack.undo()
    stack.redo()
    
    stack.push(lambda: val.update(x=1), lambda: val.update(x=0))
    stack.undo()
    assert val["x"] == 0
    
    # Redoing past the top shouldn't crash
    stack.redo()
    stack.redo()
    stack.redo()
    assert val["x"] == 1

# =============================================================================
# INTEGRATION TESTS
# =============================================================================

def test_workflow_store_eventbus_integration(sample_dataclass):
    """Integration: Data arriving via EventBus updates Store, which triggers side effects."""
    store = ce.Store(sample_dataclass())
    history = []
    
    # 1. Store is listening to Store updates
    store.subscribe(lambda s: history.append(s.count), keys=["count"])
    
    # 2. EventBus maps network/system events to Store sets
    ce.EventBus.on("network_count_sync", lambda new_count: store.set(count=new_count))
    
    # 3. Simulate workflow
    ce.EventBus.emit("network_count_sync", 10)
    ce.EventBus.emit("network_count_sync", 20)
    ce.EventBus.emit("unrelated_event", 999)
    
    assert history == [10, 20]
    assert store.state.count == 20
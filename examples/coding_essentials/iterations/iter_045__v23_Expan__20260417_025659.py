Create a new utility file to house these functions. This keeps the data layer isolated from your controller and prevents bloat.Open your terminal on the Pi:nano palette_utils.py(Shortcut to paste: Right-Click in PuTTY or Ctrl+Shift+V)Pythonimport json
import logging
import os
import tempfile
import threading
from typing import Callable, Dict, Any, Tuple, Optional

# =============================================================================
# DATA I/O & VALIDATION UTILITIES
# =============================================================================

def atomic_json_save(filepath: str, data: Dict[str, int]) -> bool:
    """
    Writes data to a JSON file atomically. 
    Critical for Raspberry Pi deployments to prevent SD card corruption 
    if power is lost mid-write.
    
    Args:
        filepath: Target destination for the JSON file.
        data: The usage frequency dictionary to serialize.
        
    Returns:
        bool: True if write was successful, False otherwise.
    """
    directory = os.path.dirname(filepath) or "."
    try:
        # Create a temporary file in the same directory to ensure they are on the same filesystem
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix="freq_tmp_", suffix=".json")
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno()) # Force write to physical disk
            
        os.replace(temp_path, filepath) # Atomic swap
        return True
    except (IOError, OSError, TypeError) as e:
        logging.error(f"Atomic save failed for {filepath}: {e}")
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        return False


def safe_json_load(filepath: str, fallback_type: Callable[[], Any] = dict) -> Dict[str, Any]:
    """
    Safely loads and parses a JSON file with automatic fallback.
    
    Args:
        filepath: Path to the JSON file.
        fallback_type: Type constructor to return if loading fails (default: dict).
        
    Returns:
        Dict containing the parsed data, or an empty dictionary on failure.
    """
    if not os.path.exists(filepath):
        logging.warning(f"File missing, returning fallback: {filepath}")
        return fallback_type()
        
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON root is not a dictionary.")
            return data
    except (json.JSONDecodeError, IOError, ValueError) as e:
        logging.error(f"Failed to parse {filepath}. File may be corrupted. Error: {e}")
        return fallback_type()


# =============================================================================
# EXECUTION & PERFORMANCE UTILITIES
# =============================================================================

def debounce(wait_time_ms: int) -> Callable:
    """
    Decorator that delays the execution of a function until after 'wait_time_ms' 
    milliseconds have elapsed since the last time it was invoked.
    Essential for limiting search filtering cycles on low-power Pi CPUs.
    
    Args:
        wait_time_ms: Time to wait in milliseconds before executing.
    """
    def decorator(fn: Callable) -> Callable:
        timer: Optional[threading.Timer] = None

        def debounced(*args: Any, **kwargs: Any) -> None:
            nonlocal timer
            if timer is not None:
                timer.cancel()
            
            # Convert ms to seconds for threading.Timer
            timer = threading.Timer(wait_time_ms / 1000.0, fn, args, kwargs)
            timer.start()

        return debounced
    return decorator


def execute_with_timeout(timeout_seconds: float, func: Callable, *args: Any, **kwargs: Any) -> Tuple[bool, str]:
    """
    Wraps command execution in a bounded thread to prevent misbehaving plugins 
    from causing an infinite loop and locking the main Tkinter UI thread.
    
    Args:
        timeout_seconds: Maximum allowed execution time.
        func: The target callable.
        
    Returns:
        Tuple[bool, str]: Success status and corresponding message/error.
    """
    result: Dict[str, Any] = {"success": False, "message": "Execution timed out."}
    
    def worker() -> None:
        try:
            func(*args, **kwargs)
            result["success"] = True
            result["message"] = "Success"
        except Exception as e:
            result["success"] = False
            result["message"] = str(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    
    if thread.is_alive():
        logging.error(f"Command {func.__name__} timed out after {timeout_seconds}s.")
        return False, f"Timeout: exceeded {timeout_seconds}s limit."
        
    return result["success"], result["message"]
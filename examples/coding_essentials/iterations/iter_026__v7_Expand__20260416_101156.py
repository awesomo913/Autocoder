You are shifting the architecture from a zero-dependency, single-file script to a distributed, installable package. This breaks your original "just curl and run" constraint, but it is the correct move for scaling.

I am using argparse and json instead of click and yaml to minimize external dependencies. Every new dependency you add is a potential failure point or version-conflict risk on edge hardware. Furthermore, I have implemented a RotatingFileHandler in the logging module. Standard logging appends forever; if you don't rotate logs, you will eventually crash the filesystem of your Pi when the storage fills up.

Below are the exact commands and full file contents to build this extension layer.

1. Terminal Creation Steps

Run these commands in your Pi terminal to create the modular package structure. Use Ctrl+Shift+V or Right-Click to paste into the nano editor.

Bash
# 1. Create the package configuration
nano pyproject.toml
# (Paste pyproject.toml code, save: Ctrl+O, Enter, Ctrl+X)

# 2. Create the advanced logger
nano logger_setup.py
# (Paste logger_setup.py code, save: Ctrl+O, Enter, Ctrl+X)

# 3. Create the configuration manager
nano config_manager.py
# (Paste config_manager.py code, save: Ctrl+O, Enter, Ctrl+X)

# 4. Create the CLI wrapper
nano cli.py
# (Paste cli.py code, save: Ctrl+O, Enter, Ctrl+X)

# 5. Install the package in editable mode for testing
pip install -e .

# 6. Test the new CLI command globally
ce-cli --help

2. Module Replacements
pyproject.toml

Modern Python packaging relies on pyproject.toml instead of setup.py. This defines your metadata, dependencies, and creates a global terminal command (ce-cli) that maps directly to your new CLI module.

Ini, TOML
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "coding_essentials"
version = "1.0.0"
description = "A lightweight toolkit and GUI for Python developers"
readme = "README.md"
requires-python = ">=3.9"
authors = [
    { name = "DevUser", email = "dev@example.com" }
]
dependencies = [
    "customtkinter>=5.2.0"
]

[project.scripts]
ce-cli = "cli:main"

[tool.setuptools]
py-modules = ["coding_essentials", "cli", "config_manager", "logger_setup"]

logger_setup.py

This replaces the basic logging.basicConfig from the main file. It enforces a 5MB size limit on log files with a maximum of 3 backups to prevent storage exhaustion on edge hardware.

Python
#!/usr/bin/env python3
"""
logger_setup.py - Advanced logging configuration with log rotation.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

def configure_logger(app_dir: str, log_level: int = logging.INFO) -> logging.Logger:
    """
    Configures a robust logger with console output and rotating file handlers.
    Prevents infinite file growth which causes system crashes on edge devices.
    """
    os.makedirs(app_dir, exist_ok=True)
    log_file = os.path.join(app_dir, "system_operations.log")

    logger = logging.getLogger("CodingEssentials")
    logger.setLevel(log_level)
    
    # Clear existing handlers to prevent duplicates if called multiple times
    if logger.hasHandlers():
        logger.handlers.clear()

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)s:%(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 2. Rotating File Handler (Max 5MB per file, keep 3 backups)
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG) 
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except PermissionError as e:
        console_handler.setLevel(logging.DEBUG)
        logger.error(f"Failed to create file handler: {e}. Falling back to console only.")

    # Global Exception Hook Override
    def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_unhandled_exception

    return logger

config_manager.py

A pure-Python JSON configuration loader. By strict design, if a configuration file is corrupted, it moves the corrupted file to a .bak extension and rebuilds a clean default state rather than crashing the application.

Python
#!/usr/bin/env python3
"""
config_manager.py - Resilient JSON configuration system.
"""

import os
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict

@dataclass
class AppConfig:
    theme_mode: str = "dark"
    enable_notifications: bool = True
    max_profiler_history: int = 2000
    debug_mode: bool = False
    log_level: str = "INFO"

class ConfigManager:
    """Handles loading, validating, and saving application configuration."""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = AppConfig()
        self._load()

    def _load(self) -> None:
        """Loads JSON config. Recovers gracefully from missing or corrupted files."""
        if not os.path.exists(self.config_path):
            self.save()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            # Merge loaded data with defaults (ignores unknown keys)
            valid_keys = self.config.__dataclass_fields__.keys()
            filtered_data = {k: v for k, v in data.items() if k in valid_keys}
            
            # Type enforcement
            if "max_profiler_history" in filtered_data:
                filtered_data["max_profiler_history"] = int(filtered_data["max_profiler_history"])
            if "enable_notifications" in filtered_data:
                filtered_data["enable_notifications"] = bool(filtered_data["enable_notifications"])
                
            for key, value in filtered_data.items():
                setattr(self.config, key, value)
                
        except (json.JSONDecodeError, ValueError) as e:
            # Handle corruption by backing up the bad file and writing fresh defaults
            backup_path = f"{self.config_path}.bak"
            os.replace(self.config_path, backup_path)
            self.save()
            print(f"WARNING: Config corrupted. Backed up to {backup_path} and reset to defaults. Error: {e}")
        except Exception as e:
            print(f"CRITICAL: Unexpected error loading config: {e}")

    def save(self) -> None:
        """Serializes current dataclass state to JSON."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self.config), f, indent=4)
        except Exception as e:
            print(f"CRITICAL: Failed to save configuration to {self.config_path}: {e}")

    def get(self, key: str) -> Any:
        return getattr(self.config, key)

    def update(self, **kwargs) -> None:
        """Updates specific keys and saves immediately."""
        changed = False
        valid_keys = self.config.__dataclass_fields__.keys()
        for k, v in kwargs.items():
            if k in valid_keys and getattr(self.config, k) != v:
                setattr(self.config, k, v)
                changed = True
        
        if changed:
            self.save()

cli.py

This acts as the command-line orchestrator. It imports the main module, hooks in the new logger and config manager, and exposes discrete run states.

Python
#!/usr/bin/env python3
"""
cli.py - Command Line Interface for Coding Essentials.
"""

import os
import sys
import argparse
import logging

# Ensure the local directory is in path for module resolution
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import coding_essentials as ce
from config_manager import ConfigManager
from logger_setup import configure_logger

def setup_environment() -> tuple[ConfigManager, logging.Logger]:
    """Bootstraps the config and logging subsystems."""
    config_path = os.path.join(ce.APP_DIR, "config.json")
    config_mgr = ConfigManager(config_path)
    
    log_level = logging.DEBUG if config_mgr.get("debug_mode") else getattr(logging, config_mgr.get("log_level").upper(), logging.INFO)
    logger = configure_logger(ce.APP_DIR, log_level=log_level)
    
    return config_mgr, logger

def command_run(args: argparse.Namespace, config_mgr: ConfigManager, logger: logging.Logger) -> None:
    """Executes the main GUI application."""
    logger.info("Initializing Coding Essentials GUI...")
    
    # Apply initial config state to the GUI's ThemeStore
    mode = config_mgr.get("theme_mode")
    if mode == "light":
        ce.ThemeStore.set(**ce.asdict(ce.LIGHT_THEME))
    else:
        ce.ThemeStore.set(**ce.asdict(ce.DARK_THEME))
        
    app = ce.DemoApp()
    
    # Attach a listener to save theme changes back to config
    def on_theme_change(state):
        config_mgr.update(theme_mode=state.mode)
        logger.debug(f"Theme mode updated to: {state.mode}")
        
    ce.ThemeStore.subscribe(on_theme_change, keys=["mode"])
    
    try:
        app.mainloop()
    except Exception as e:
        logger.critical(f"Application crashed during mainloop: {e}", exc_info=True)
        sys.exit(1)

def command_test(args: argparse.Namespace, logger: logging.Logger) -> None:
    """Triggers the built-in selftests."""
    logger.info("Initiating headless self-tests...")
    ce.run_selftests()

def command_config(args: argparse.Namespace, config_mgr: ConfigManager, logger: logging.Logger) -> None:
    """CLI utility to view or set configuration variables."""
    if args.set:
        try:
            key, value = args.set.split("=", 1)
            # Basic type casting for CLI inputs
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            elif value.isdigit():
                value = int(value)
                
            config_mgr.update(**{key: value})
            logger.info(f"Updated config: {key} = {value}")
            print(f"SUCCESS: Set {key} to {value}")
        except ValueError:
            logger.error("Invalid format for --set. Use key=value.")
            print("ERROR: Invalid format. Use --set key=value")
            sys.exit(1)
    else:
        print("\n--- Current Configuration ---")
        for k, v in ce.asdict(config_mgr.config).items():
            print(f"{k}: {v}")
        print("-----------------------------\n")

def main() -> None:
    """Main CLI entrypoint parser."""
    config_mgr, logger = setup_environment()
    
    parser = argparse.ArgumentParser(
        description="Coding Essentials Toolkit CLI",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    subparsers.required = True
    
    # 1. Run Command
    parser_run = subparsers.add_parser("run", help="Launch the primary GUI application")
    parser_run.set_defaults(func=lambda args: command_run(args, config_mgr, logger))
    
    # 2. Test Command
    parser_test = subparsers.add_parser("test", help="Execute headless system selftests")
    parser_test.set_defaults(func=lambda args: command_test(args, logger))
    
    # 3. Config Command
    parser_config = subparsers.add_parser("config", help="View or modify application configuration")
    parser_config.add_argument("--set", type=str, help="Set a config value (format: key=value)", metavar="KEY=VALUE")
    parser_config.set_defaults(func=lambda args: command_config(args, config_mgr, logger))
    
    args = parser.parse_args()
    
    # Execute routed function
    args.func(args)

if __name__ == "__main__":
    main()


Now that you have refactored this into an installable Python package rather than a standalone script, how are you planning to manage the deployment updates across your Pi cluster?
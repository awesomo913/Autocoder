# Autocoder

AI-powered browser automation tool that sends coding tasks to Gemini (or any AI chat) via Chrome DevTools Protocol, then endlessly improves the generated code through iterative feedback loops.

## Features

- **CDP Browser Automation** — Direct DOM manipulation via WebSocket, no blind clicking
- **Endless Improvement Loop** — Sends task, extracts code, feeds it back with rotating improvement focuses
- **8 Selectable Improvement Focuses** — Deep Code Dive, Extra Features, Pressure Test, Explore & Expand, Beautiful GUI, Solid & Functional, Reference Images, Review & Grade
- **Perfection Loop** — Cycles all selected focuses, auto-stops when code stops improving
- **Smart Recovery** — Detects when AI returns chat instead of code, resets conversation and asks AI to self-diagnose
- **File Attachments** — Attach .py, .json, .c, .md, or any code file as context for every iteration
- **Stagnation Detection** — MD5 hash comparison catches when code stops evolving
- **Expansion Mode** — When code plateaus, switches to generating new companion modules
- **Auto-Save** — Every iteration saves to Downloads with clean naming
- **State Persistence** — Stop and resume without losing progress

## Quick Start

```bash
# Install dependencies
pip install customtkinter pyautogui pyperclip websocket-client

# Run
python -m gemini_coder_web
```

1. Click **Launch CDP Browser** — opens a dedicated Chrome instance
2. Log into Gemini (or any AI chat) in the browser
3. Click **Grab** on a session card to connect
4. Type a task, select improvement focuses, click **Start Autocoding**

## How It Works

```
Task → Engineered Prompt → Gemini builds code → Extract code from response
  ↓                                                        ↓
  ← ← ← ← ← Feed code back with next improvement focus ← ←
```

Each iteration:
1. Takes the current codebase
2. Applies the next improvement focus (e.g., "Pressure Test")
3. Sends the full codebase + directive to Gemini
4. Extracts the improved code from the response
5. Saves to Downloads, repeats with next focus

## Requirements

- Python 3.11+
- Chrome/Chromium browser
- A Gemini (or other AI) account

## Build Executable

```bash
python build_autocoder.py
```

Creates a standalone `.exe` in `dist/Autocoder/`.

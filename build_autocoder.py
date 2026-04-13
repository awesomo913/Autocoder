"""Build Autocoder into a distributable zip with exe + instructions.

Run:  python build_autocoder.py
Output: ~/Desktop/Autocoder_v3.1.0.zip
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "Autocoder"
VERSION = "3.2.0"
ZIP_NAME = f"{APP_NAME}_v{VERSION}"
DESKTOP = Path.home() / "Desktop"

ENTRY_SCRIPT = ROOT / "_autocoder_entry.py"


def write_entry_script():
    """Write a self-contained entry script for PyInstaller."""
    ENTRY_SCRIPT.write_text(
        '"""Autocoder entry point for PyInstaller."""\n'
        "import io\n"
        "import logging\n"
        "import sys\n"
        "import os\n"
        "\n"
        "# Fix encoding for frozen exe\n"
        "if getattr(sys, 'frozen', False):\n"
        "    if sys.stdout is None:\n"
        "        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')\n"
        "    if sys.stderr is None:\n"
        "        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')\n"
        "elif sys.stdout and hasattr(sys.stdout, 'reconfigure'):\n"
        "    sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "    sys.stderr.reconfigure(encoding='utf-8', errors='replace')\n"
        "\n"
        "# Ensure the bundled packages are importable\n"
        "if getattr(sys, 'frozen', False):\n"
        "    base = sys._MEIPASS\n"
        "else:\n"
        "    base = os.path.dirname(os.path.abspath(__file__))\n"
        "if base not in sys.path:\n"
        "    sys.path.insert(0, base)\n"
        "\n"
        "logging.basicConfig(\n"
        "    level=logging.INFO,\n"
        "    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',\n"
        ")\n"
        "\n"
        "from gemini_coder_web.ui.app_web import GeminiCoderWebApp\n"
        "app = GeminiCoderWebApp()\n"
        "app.mainloop()\n",
        encoding="utf-8",
    )
    print(f"  Entry script: {ENTRY_SCRIPT}")


def run_pyinstaller():
    """Run PyInstaller to build the exe."""
    selectors_src = ROOT / "gemini_coder_web" / "default_selectors.json"
    selectors_dst = "gemini_coder_web"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--windowed",                      # No console window
        "--onedir",                        # Faster startup than --onefile
        f"--add-data={selectors_src};{selectors_dst}",
        "--hidden-import=gemini_coder",
        "--hidden-import=gemini_coder.ui",
        "--hidden-import=gemini_coder.ui.app",
        "--hidden-import=gemini_coder.ui.theme",
        "--hidden-import=gemini_coder.config",
        "--hidden-import=gemini_coder.task_manager",
        "--hidden-import=gemini_coder.expander",
        "--hidden-import=gemini_coder.history",
        "--hidden-import=gemini_coder.platform_utils",
        "--hidden-import=gemini_coder_web",
        "--hidden-import=gemini_coder_web.ui",
        "--hidden-import=gemini_coder_web.ui.app_web",
        "--hidden-import=gemini_coder_web.ai_profiles",
        "--hidden-import=gemini_coder_web.auto_save",
        "--hidden-import=gemini_coder_web.broadcast",
        "--hidden-import=gemini_coder_web.browser_actions",
        "--hidden-import=gemini_coder_web.browser_client",
        "--hidden-import=gemini_coder_web.cdp_client",
        "--hidden-import=gemini_coder_web.session_manager",
        "--hidden-import=gemini_coder_web.universal_client",
        "--hidden-import=gemini_coder_web.window_manager",
        "--hidden-import=customtkinter",
        "--hidden-import=pyautogui",
        "--hidden-import=pyperclip",
        "--hidden-import=pystray",
        "--hidden-import=PIL",
        "--hidden-import=psutil",
        "--hidden-import=websocket",
        "--collect-all=customtkinter",
        str(ENTRY_SCRIPT),
    ]

    # Optional: prompt_engine and mousetraffic
    prompt_engine = ROOT / "prompt_engine.py"
    if prompt_engine.exists():
        cmd.extend([
            "--hidden-import=prompt_engine",
            f"--add-data={prompt_engine};.",
        ])

    mousetraffic = ROOT / "mousetraffic"
    if mousetraffic.is_dir():
        cmd.extend([
            "--hidden-import=mousetraffic",
            "--hidden-import=mousetraffic.client",
            f"--add-data={mousetraffic};mousetraffic",
        ])

    print(f"\n  Running PyInstaller...")
    print(f"  CMD: {' '.join(cmd[:8])}...\n")

    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=False)
    if result.returncode != 0:
        print(f"\n  ERROR: PyInstaller failed (exit {result.returncode})")
        sys.exit(1)

    print(f"\n  Build complete: {DIST / APP_NAME}")


def create_readme():
    """Create README with install/usage instructions."""
    readme = DIST / APP_NAME / "README.txt"
    readme.write_text(
        f"{'='*60}\n"
        f"  AUTOCODER v{VERSION}\n"
        f"  AI Browser Automation — Endless Code Improvement\n"
        f"{'='*60}\n"
        f"\n"
        f"QUICK START\n"
        f"-----------\n"
        f"1. Double-click  Autocoder.exe\n"
        f"2. Click 'Launch Gemini (CDP)' — opens a Chrome window\n"
        f"3. Sign into Gemini in the browser if needed\n"
        f"4. Type your task (e.g. 'Build a calculator with themes')\n"
        f"5. Click 'Start Autocoding'\n"
        f"6. Watch it build and endlessly improve your code!\n"
        f"7. Output .txt files saved to your Downloads folder\n"
        f"\n"
        f"REQUIREMENTS\n"
        f"------------\n"
        f"- Windows 10/11\n"
        f"- Google Chrome installed (for CDP browser automation)\n"
        f"- A Google account with Gemini access (free tier works)\n"
        f"\n"
        f"FEATURES\n"
        f"--------\n"
        f"- Sends your task to Gemini and extracts the code response\n"
        f"- Feeds the code back with rotating improvement focuses:\n"
        f"    Add Features -> Polish -> Robustness -> Performance -> Review\n"
        f"- Saves every iteration as a .txt file in Downloads\n"
        f"- 'Expand on stagnation' toggle: when code stops growing,\n"
        f"  automatically switches to creating NEW utility functions,\n"
        f"  plugins, test suites, and companion modules\n"
        f"- Uses Chrome DevTools Protocol (CDP) — no mouse/keyboard\n"
        f"  stealing, works in the background\n"
        f"\n"
        f"OPTIONS\n"
        f"-------\n"
        f"- Build Target: Choose PC Desktop App, Web App, Game, etc.\n"
        f"- Expand on Stagnation: Toggle ON to auto-create new functions\n"
        f"  when the AI starts repeating itself\n"
        f"- Ctrl+K or Stop button to halt at any time\n"
        f"\n"
        f"FILES\n"
        f"-----\n"
        f"- Autocoder.exe      Main application\n"
        f"- _internal/         Runtime libraries (do not delete)\n"
        f"- README.txt         This file\n"
        f"\n"
        f"TROUBLESHOOTING\n"
        f"---------------\n"
        f"- 'No configured session': Click 'Launch Gemini (CDP)' first\n"
        f"- Chrome won't open: Make sure Chrome is installed at the\n"
        f"  default location, or close all Chrome windows and retry\n"
        f"- Antivirus blocks exe: Add an exception for the Autocoder\n"
        f"  folder — it's a PyInstaller bundle, not malware\n"
        f"- Code extraction fails: Gemini may have changed its UI.\n"
        f"  Check ~/.autocoder/selectors.json for CSS selectors\n"
        f"\n"
        f"{'='*60}\n",
        encoding="utf-8",
    )
    print(f"  README: {readme}")


def create_zip():
    """Zip the dist folder and place it on the Desktop."""
    app_dir = DIST / APP_NAME
    if not app_dir.is_dir():
        print(f"  ERROR: {app_dir} not found")
        sys.exit(1)

    zip_path = DESKTOP / f"{ZIP_NAME}.zip"

    print(f"\n  Creating zip: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in app_dir.rglob("*"):
            if file.is_file():
                arcname = f"{APP_NAME}/{file.relative_to(app_dir)}"
                zf.write(file, arcname)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  Done: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def cleanup():
    """Remove temp entry script."""
    if ENTRY_SCRIPT.exists():
        ENTRY_SCRIPT.unlink()


def main():
    print(f"\n{'='*60}")
    print(f"  Building {APP_NAME} v{VERSION}")
    print(f"{'='*60}\n")

    write_entry_script()
    run_pyinstaller()
    create_readme()
    zip_path = create_zip()
    cleanup()

    print(f"\n{'='*60}")
    print(f"  BUILD COMPLETE")
    print(f"  Zip: {zip_path}")
    print(f"  Extract and run Autocoder.exe")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

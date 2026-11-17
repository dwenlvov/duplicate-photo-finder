"""Start point of the installed app (Mac .app and the Windows folder).

Keeps the app's data (fingerprints, choices, history) in the user's folder, because the app's own folder may be
read-only (macOS runs a downloaded app from a temporary read-only copy). Then starts the page and opens the browser.
"""
import os
import sys
from pathlib import Path

APP = "Duplicate Photo Finder"


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP


def main():
    os.environ.setdefault("DEDUP_DATA", str(data_dir()))
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:  # the Windows folder keeps the dedup package next to this file
        sys.path.insert(0, str(here))
    if sys.platform == "win32":
        # the page has a language switch, this window does not: Russian, then English
        print("Duplicate Photo Finder запускается, страница откроется в браузере.\n"
              "Не закрывайте это окно, пока работаете: закрыть окно значит выключить программу.\n\n"
              "Duplicate Photo Finder is starting, the page will open in your browser.\n"
              "Keep this window open while you work: closing it shuts the program down.\n", flush=True)
    from dedup.__main__ import main as dedup_main
    dedup_main(["ui", "--open", "--port", os.environ.get("DEDUP_PORT", "8777")])


if __name__ == "__main__":
    main()

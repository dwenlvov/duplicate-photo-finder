"""Start point of the installed app (Mac .app and the Windows folder).

Keeps the app's data (fingerprints, choices, history) in the user's folder, because the app's own folder may be
read-only (macOS runs a downloaded app from a temporary read-only copy). Then starts the page, opens the browser
and puts an icon next to the clock (the Mac menu bar, the Windows notification area): it shows the program is
running, and its menu opens the page or shuts the program down.
"""
import os
import sys
import threading
import webbrowser
from pathlib import Path

APP = "Duplicate Photo Finder"


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP


def russian() -> bool:
    """The menu of the icon follows the language of the system: Russian, otherwise English."""
    try:
        if sys.platform == "darwin":
            import Foundation
            return str(Foundation.NSLocale.preferredLanguages()[0]).startswith("ru")
        if sys.platform == "win32":
            import ctypes
            return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x19
    except Exception:
        pass
    return (os.environ.get("LANG") or "").startswith("ru")


def icon_image(mac: bool):
    """Two photos, one over the other. Mac: black on transparent, macOS recolours it for a light or dark menu bar."""
    from PIL import Image, ImageDraw
    n = 44 if mac else 64
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (0, 0, 0, 255) if mac else (45, 99, 163, 255)
    fill = (0, 0, 0, 0) if mac else (255, 255, 255, 255)
    w = max(3, n // 14)
    u = n / 44
    d.rounded_rectangle([2 * u, 2 * u, 30 * u, 26 * u], radius=4 * u, outline=ink, width=w)
    d.rounded_rectangle([12 * u, 14 * u, 42 * u, 40 * u], radius=4 * u, fill=fill if not mac else None, outline=ink, width=w)
    if mac:  # the back photo must not show through the front one
        d.rounded_rectangle([12 * u + w, 14 * u + w, 42 * u - w, 40 * u - w], radius=2 * u, fill=(0, 0, 0, 0))
    d.polygon([(16 * u, 36 * u), (24 * u, 26 * u), (30 * u, 32 * u), (33 * u, 29 * u), (38 * u, 36 * u)], fill=ink)
    d.ellipse([31 * u, 18 * u, 36 * u, 23 * u], fill=ink)
    return img


def tray(url: str, quit_program):
    """The icon and its menu. Runs on the main thread (macOS requires it) until stop() is called."""
    import pystray
    ru = russian()
    menu = pystray.Menu(
        pystray.MenuItem("Открыть страницу" if ru else "Open the page", lambda: webbrowser.open(url), default=True),
        pystray.MenuItem("Выключить" if ru else "Shut down", lambda icon: quit_program()),
    )
    cls = pystray.Icon
    if sys.platform == "darwin":
        import AppKit
        import Foundation
        import io

        class MacIcon(pystray.Icon):
            def _assert_image(self):  # sharp on Retina, and a template image follows the menu bar colour
                if self._icon_image:
                    return
                b = io.BytesIO()
                self._icon.save(b, "png")
                img = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(b.getvalue()))
                side = self._status_bar.thickness() * 0.8
                img.setSize_((side, side))
                img.setTemplate_(True)
                self._icon_image = img
                self._status_item.button().setImage_(img)
        cls = MacIcon
    return cls(APP, icon_image(sys.platform == "darwin"), APP, menu)


def main():
    os.environ.setdefault("DEDUP_DATA", str(data_dir()))
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:  # the Windows folder keeps the dedup package next to this file
        sys.path.insert(0, str(here))
    if sys.platform == "win32":
        # the page has a language switch, this window does not: Russian, then English
        print("Duplicate Photo Finder запускается, страница откроется в браузере.\n"
              "Не закрывайте это окно, пока работаете: закрыть окно значит выключить программу.\n"
              "Выключить программу можно и через значок у часов, внизу справа.\n\n"
              "Duplicate Photo Finder is starting, the page will open in your browser.\n"
              "Keep this window open while you work: closing it shuts the program down.\n"
              "You can also shut the program down from its icon by the clock, bottom right.\n", flush=True)
    from dedup.ui import server
    srv, url = server.bind(int(os.environ.get("DEDUP_PORT", "8777")))
    if srv is None:  # this version already runs: show its page, do not start a second copy
        print(f"Программа уже запущена: {url}\nThe program is already running: {url}", flush=True)
        webbrowser.open(url)
        return
    print(f"Открыть: {url}\nOpen: {url}", flush=True)
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        icon = tray(url, lambda: threading.Thread(target=srv.shutdown, daemon=True).start())
    except Exception as e:  # no icon on this system: the page alone still works, the button on it shuts down
        print(f"Значок у часов недоступен: {e}\nThe icon by the clock is not available: {e}", flush=True)
        srv.serve_forever()
        return
    web = threading.Thread(target=srv.serve_forever, daemon=True)
    web.start()

    def watch():  # shut down from the page or from the menu: the icon goes too
        web.join()
        icon.stop()
    threading.Thread(target=watch, daemon=True).start()
    icon.run()
    print("Программа выключена\nThe program is shut down", flush=True)


if __name__ == "__main__":
    main()

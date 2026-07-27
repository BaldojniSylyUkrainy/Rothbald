#!/usr/bin/env python3
from __future__ import annotations

import threading
import time
import urllib.request
import webbrowser
import sys
from pathlib import Path

import server
import transcribe_video


def set_macos_dock_icon() -> None:
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage

        icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
        if icon_path.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            )
    except Exception:
        # The packaged .app still gets its icon from Rothbald.spec.
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--transcribe":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        transcribe_video.main()
        return
    url = f"http://{server.HOST}:{server.PORT}"
    threading.Thread(target=server.main, daemon=True, name="rothbald-server").start()
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"{url}/api/bootstrap", timeout=.5):
                break
        except Exception:
            time.sleep(.1)
    else:
        raise RuntimeError("Rothbald не зміг запустити локальний двигун")

    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        while True:
            time.sleep(60)

    set_macos_dock_icon()
    webview.create_window(
        "Rothbald",
        url,
        width=1280,
        height=800,
        min_size=(960, 640),
        resizable=True,
        background_color="#151216",
        text_select=False,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()

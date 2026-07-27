#!/usr/bin/env python3
from __future__ import annotations

import os
import threading
import time
import traceback
import urllib.request
import sys
import uuid
from pathlib import Path

import server
import transcribe_video


def application_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def configure_bundled_tools() -> None:
    if not getattr(sys, "frozen", False):
        return
    bundled_root = str(application_root())
    existing = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(part for part in (bundled_root, existing) if part)


def main() -> None:
    configure_bundled_tools()
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

    from PySide6.QtCore import QObject, QUrl, Signal, Slot
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow

    class FolderPicker(QObject):
        requested = Signal(str, str)

        def __init__(self):
            super().__init__()
            self._pending: dict[str, tuple[threading.Event, dict]] = {}
            self._lock = threading.Lock()
            self.requested.connect(self._show)

        def choose(self, prompt: str) -> Path | None:
            token = uuid.uuid4().hex
            event, result = threading.Event(), {}
            with self._lock:
                self._pending[token] = (event, result)
            self.requested.emit(token, prompt)
            event.wait()
            value = result.get("path")
            return Path(value).resolve() if value else None

        @Slot(str, str)
        def _show(self, token: str, prompt: str) -> None:
            selection = QFileDialog.getExistingDirectory(None, prompt)
            with self._lock:
                event, result = self._pending.pop(token)
            result["path"] = selection
            event.set()

    class RothbaldPage(QWebEnginePage):
        def acceptNavigationRequest(self, target: QUrl, navigation_type, is_main_frame: bool) -> bool:
            if target.scheme() == "mailto":
                QDesktopServices.openUrl(target)
                return False
            if target.scheme() in {"http", "https"} and target.host() not in {"127.0.0.1", "localhost"}:
                QDesktopServices.openUrl(target)
                return False
            return super().acceptNavigationRequest(target, navigation_type, is_main_frame)

    class RothbaldWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Rothbald")
            self.resize(1280, 800)
            self.setMinimumSize(960, 640)
            icon_path = application_root() / "assets" / "app-icon.png"
            if icon_path.is_file():
                self.setWindowIcon(QIcon(str(icon_path)))
            view = QWebEngineView(self)
            view.setPage(RothbaldPage(view))
            view.setUrl(QUrl(url))
            self.setCentralWidget(view)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Rothbald")
    qt_app.setOrganizationName("Baldojni Syly Ukrainy")
    icon_path = application_root() / "assets" / "app-icon.png"
    if icon_path.is_file():
        qt_app.setWindowIcon(QIcon(str(icon_path)))
    picker = FolderPicker()
    server.set_folder_picker(picker.choose)
    window = RothbaldWindow()
    window.show()
    raise SystemExit(qt_app.exec())


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (server.DATA_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise

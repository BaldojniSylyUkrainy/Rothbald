#!/usr/bin/env python3
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import traceback
import uuid
from pathlib import Path

import server
import transcribe_video
from native_update import macos_app_bundle, macos_helper_arguments, windows_installer_arguments


def application_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def configure_bundled_tools() -> None:
    if not getattr(sys, "frozen", False):
        return
    bundled_root = str(application_root())
    existing = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(part for part in (bundled_root, existing) if part)


def duplicate_instance_should_focus(platform_name: str) -> bool:
    """Let macOS activation semantics preserve the user's current workspace."""
    return platform_name == "win32"


def runtime_smoke() -> None:
    """Fail fast when a packaged build lost a runtime dependency."""
    from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401
    from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: F401
    from PySide6.QtWebChannel import QWebChannel  # noqa: F401

    transcribe_video.verify_runtime_dependencies()
    for name in ("ffmpeg", "ffprobe"):
        executable = server.bundled_tool(name)
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"{name} не запускається у цій збірці Rothbald")


def close_confirmation_text(activity: dict) -> tuple[str, str, str]:
    """Build native close-dialog copy from a runtime activity snapshot."""
    lines: list[str] = []
    model_status = activity.get("model_status")
    models = activity.get("models") or []
    if model_status == "checking":
        lines.append("• Перевірка моделей")
    for model in models:
        if model.get("status") == "downloading":
            lines.append(f"• Завантаження: {model.get('name', 'модель')} — {int(model.get('percent') or 0)}%")
    update_status = activity.get("update_status")
    if update_status == "checking":
        lines.append("• Перевірка оновлення Rothbald")
    elif update_status == "downloading":
        lines.append(f"• Завантаження оновлення — {int(activity.get('update_percent') or 0)}%")
    processing = int(activity.get("processing") or 0)
    queued = int(activity.get("queued") or 0)
    indexing = int(activity.get("indexing") or 0)
    media_checks = int(activity.get("media_checks") or 0)
    if processing:
        lines.append(f"• Розпізнавання відео: {processing}")
    if queued:
        lines.append(f"• Відео в черзі: {queued}")
    if indexing:
        lines.append(f"• Індексація для пошуку: {indexing}")
    if media_checks:
        lines.append(f"• Перевірка відеофайлів: {media_checks}")
    if lines:
        return (
            "Закрити Rothbald?",
            "Зараз ще тривають процеси:",
            "\n".join(lines) + "\n\nЯкщо закрити застосунок, активна робота зупиниться. "
            "Незавершену чергу відео можна буде продовжити після наступного запуску.",
        )
    return (
        "Закрити Rothbald?",
        "Точно хочеш закрити застосунок?",
        "Зараз активних процесів немає.",
    )


def main() -> None:
    configure_bundled_tools()
    if len(sys.argv) > 1 and sys.argv[1] == "--runtime-smoke":
        runtime_smoke()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--transcribe":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        transcribe_video.main()
        return
    from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSlider,
        QStyle,
        QVBoxLayout,
        QWidget,
    )

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Rothbald")
    qt_app.setOrganizationName("Baldojni Syly Ukrainy")

    instance_name = "ua.rothbald.app.instance"
    instance_server = QLocalServer()
    if not instance_server.listen(instance_name):
        existing = QLocalSocket()
        existing.connectToServer(instance_name)
        if existing.waitForConnected(1000):
            existing.write(b"focus")
            existing.flush()
            existing.waitForBytesWritten(1000)
            return
        QLocalServer.removeServer(instance_name)
        if not instance_server.listen(instance_name):
            QMessageBox.critical(None, "Rothbald", "Не вдалося відкрити Rothbald.")
            return

    url = f"http://{server.HOST}:{server.PORT}"
    try:
        http_server = server.create_http_server()
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 10048}:
            QMessageBox.critical(
                None,
                "Rothbald",
                f"Локальний порт {server.PORT} зайнятий іншою програмою.",
            )
            return
        raise
    server_thread = threading.Thread(
        target=http_server.serve_forever,
        daemon=True,
        name="rothbald-server",
    )
    server_thread.start()

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

    class UpdateInstaller(QObject):
        requested = Signal(str, str)

        def __init__(self):
            super().__init__()
            self._pending: dict[str, tuple[threading.Event, dict]] = {}
            self._lock = threading.Lock()
            self.requested.connect(self._open)

        def install(self, path: Path) -> bool:
            token = uuid.uuid4().hex
            event, result = threading.Event(), {}
            with self._lock:
                self._pending[token] = (event, result)
            self.requested.emit(token, str(path))
            event.wait()
            return bool(result.get("launched"))

        @Slot(str, str)
        def _open(self, token: str, raw_path: str) -> None:
            path = Path(raw_path)
            if sys.platform == "win32":
                started = QProcess.startDetached(
                    str(path),
                    windows_installer_arguments(),
                    str(path.parent),
                )
                launched = bool(started[0] if isinstance(started, tuple) else started)
                if launched:
                    qt_app.setProperty("rothbaldInstallerLaunched", True)
                    QTimer.singleShot(750, qt_app.quit)
            elif sys.platform == "darwin":
                try:
                    target_app = macos_app_bundle()
                    _, arguments = macos_helper_arguments(
                        path,
                        target_app,
                        process_id=os.getpid(),
                    )
                    started = QProcess.startDetached("/bin/sh", arguments, str(path.parent))
                    launched = bool(started[0] if isinstance(started, tuple) else started)
                except (OSError, ValueError):
                    launched = False
                if launched:
                    qt_app.setProperty("rothbaldInstallerLaunched", True)
                    QTimer.singleShot(750, qt_app.quit)
            else:
                launched = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            with self._lock:
                event, result = self._pending.pop(token)
            result["launched"] = launched
            event.set()

    class NativePlayer(QWidget):
        clearRequested = Signal()
        errorRaised = Signal(str)

        def __init__(self, parent: QWidget):
            super().__init__(parent)
            self.setObjectName("nativePlayer")
            self.setStyleSheet(
                """
                QWidget#nativePlayer { background: #030303; border: 1px solid #202024; }
                QWidget#nativePlayer QWidget { background: #080809; color: #f1f1f2; }
                QWidget#nativePlayer QPushButton {
                    min-width: 30px; border: 0; padding: 5px 8px;
                    background: transparent; color: #f1f1f2; font-weight: 700;
                }
                QWidget#nativePlayer QPushButton:hover { background: #202024; }
                QWidget#nativePlayer QSlider::groove:horizontal { height: 4px; background: #36363b; }
                QWidget#nativePlayer QSlider::handle:horizontal {
                    width: 12px; margin: -4px 0; border-radius: 6px; background: #aaa1b5;
                }
                QWidget#nativePlayer QSlider::sub-page:horizontal { background: #aaa1b5; }
                """
            )
            self.pending_position_ms: int | None = None
            self.dragging = False

            self.video = QVideoWidget(self)
            self.audio = QAudioOutput(self)
            self.audio.setVolume(0.8)
            self.media = QMediaPlayer(self)
            self.media.setAudioOutput(self.audio)
            self.media.setVideoOutput(self.video)

            self.play_button = QPushButton(self)
            self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            self.play_button.setAccessibleName("Відтворити або призупинити")
            self.position = QSlider(Qt.Horizontal, self)
            self.position.setRange(0, 0)
            self.time_label = QLabel("0:00 / 0:00", self)
            self.fullscreen_button = QPushButton("⛶", self)
            self.fullscreen_button.setAccessibleName("Повноекранний режим")

            controls = QHBoxLayout()
            controls.setContentsMargins(6, 2, 6, 2)
            controls.setSpacing(8)
            controls.addWidget(self.play_button)
            controls.addWidget(self.position, 1)
            controls.addWidget(self.time_label)
            controls.addWidget(self.fullscreen_button)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(self.video, 1)
            layout.addLayout(controls)

            self.close_button = QPushButton("×", self)
            self.close_button.setFixedSize(34, 34)
            self.close_button.setAccessibleName("Зняти вибір відео")
            self.close_button.setStyleSheet(
                "border-radius: 17px; background: rgba(8,8,7,210); font-size: 20px;"
            )
            self.close_button.clicked.connect(self.clearRequested.emit)
            self.play_button.clicked.connect(self.toggle_playback)
            self.fullscreen_button.clicked.connect(
                lambda: self.video.setFullScreen(not self.video.isFullScreen())
            )
            self.position.sliderPressed.connect(lambda: setattr(self, "dragging", True))
            self.position.sliderReleased.connect(self.seek_from_slider)
            self.media.positionChanged.connect(self.update_position)
            self.media.durationChanged.connect(self.update_duration)
            self.media.playbackStateChanged.connect(self.update_play_icon)
            self.media.mediaStatusChanged.connect(self.apply_pending_seek)
            self.media.errorOccurred.connect(
                lambda _error, message: self.errorRaised.emit(
                    message or "Не вдалося відтворити це відео"
                )
            )
            self.hide()

        @staticmethod
        def format_time(milliseconds: int) -> str:
            seconds = max(0, milliseconds // 1000)
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            return (
                f"{hours}:{minutes:02d}:{seconds:02d}"
                if hours
                else f"{minutes}:{seconds:02d}"
            )

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            self.close_button.move(max(0, self.width() - 44), 10)
            self.close_button.raise_()

        def set_media(self, path: Path, seconds: float | None) -> None:
            seek_seconds = max(0.0, float(seconds or 0) - (1.5 if seconds is not None else 0))
            requested_ms = round(seek_seconds * 1000)
            source = QUrl.fromLocalFile(str(path))
            if self.media.source() == source:
                self.media.setPosition(requested_ms)
                self.media.play()
                return
            self.pending_position_ms = requested_ms
            self.media.setSource(source)
            self.media.play()

        def clear(self) -> None:
            self.pending_position_ms = None
            self.media.stop()
            self.media.setSource(QUrl())
            self.hide()

        def apply_pending_seek(self, status) -> None:
            if status not in {
                QMediaPlayer.LoadedMedia,
                QMediaPlayer.BufferedMedia,
                QMediaPlayer.BufferingMedia,
            }:
                return
            if self.pending_position_ms is not None:
                self.media.setPosition(self.pending_position_ms)
                self.pending_position_ms = None
            self.media.play()

        def toggle_playback(self) -> None:
            if self.media.playbackState() == QMediaPlayer.PlayingState:
                self.media.pause()
            else:
                self.media.play()

        def seek_from_slider(self) -> None:
            self.dragging = False
            self.media.setPosition(self.position.value())

        def update_position(self, value: int) -> None:
            if not self.dragging:
                self.position.setValue(value)
            self.time_label.setText(
                f"{self.format_time(value)} / {self.format_time(self.media.duration())}"
            )

        def update_duration(self, value: int) -> None:
            self.position.setRange(0, max(0, value))
            self.update_position(self.media.position())

        def update_play_icon(self, state) -> None:
            icon = (
                QStyle.SP_MediaPause
                if state == QMediaPlayer.PlayingState
                else QStyle.SP_MediaPlay
            )
            self.play_button.setIcon(self.style().standardIcon(icon))

    class NativePlayerBridge(QObject):
        errorOccurred = Signal(str)

        def __init__(self, player_widget: NativePlayer):
            super().__init__()
            self.player_widget = player_widget

        @Slot(str, float)
        def select(self, video_id: str, seconds: float) -> None:
            video = server.get_video(video_id)
            path = Path(video["source_path"]) if video else None
            if not path or not path.is_file():
                self.errorOccurred.emit(
                    "Текст збережений, але самого відеофайлу немає. Скористайся Locate."
                )
                return
            at = None if seconds < 0 else seconds
            self.player_widget.set_media(path, at)

        @Slot()
        def clear(self) -> None:
            self.player_widget.clear()

        @Slot(int, int, int, int, bool)
        def setPlayerGeometry(
            self,
            x: int,
            y: int,
            width: int,
            height: int,
            visible: bool,
        ) -> None:
            if not visible or width < 80 or height < 60:
                self.player_widget.hide()
                return
            self.player_widget.setGeometry(x, y, width, height)
            self.player_widget.show()
            self.player_widget.raise_()

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
            native_player = NativePlayer(view)
            player_bridge = NativePlayerBridge(native_player)
            channel = QWebChannel(view.page())
            channel.registerObject("nativePlayer", player_bridge)
            view.page().setWebChannel(channel)
            native_player.clearRequested.connect(
                lambda: view.page().runJavaScript("clearVideoSelection()")
            )
            native_player.errorRaised.connect(player_bridge.errorOccurred.emit)
            view.setUrl(QUrl(url))
            self.setCentralWidget(view)
            self.view = view
            self.native_player = native_player
            self.player_bridge = player_bridge
            self.web_channel = channel

        def closeEvent(self, event) -> None:
            # Installing a verified update intentionally hands control to the
            # platform installer and must not be stopped by a confirmation.
            if qt_app.property("rothbaldInstallerLaunched"):
                event.accept()
                return
            activity = server.runtime_activity_summary()
            title, prompt, details = close_confirmation_text(activity)
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Warning if activity["active"] else QMessageBox.Question)
            dialog.setWindowTitle(title)
            dialog.setText(prompt)
            dialog.setInformativeText(details)
            stay_button = dialog.addButton("Не закривати", QMessageBox.RejectRole)
            close_button = dialog.addButton("Закрити", QMessageBox.DestructiveRole)
            dialog.setDefaultButton(stay_button)
            dialog.setEscapeButton(stay_button)
            dialog.exec()
            if dialog.clickedButton() is close_button:
                event.accept()
            else:
                event.ignore()

    class PowerAwake:
        def __init__(self):
            self.active = False
            self.caffeinate: subprocess.Popen | None = None

        def set_active(self, active: bool) -> None:
            if active == self.active:
                return
            self.active = active
            if sys.platform == "darwin":
                if active:
                    self.caffeinate = subprocess.Popen(
                        ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                elif self.caffeinate and self.caffeinate.poll() is None:
                    self.caffeinate.terminate()
                    try:
                        self.caffeinate.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.caffeinate.kill()
                        self.caffeinate.wait(timeout=2)
                    self.caffeinate = None
            elif sys.platform == "win32":
                import ctypes

                continuous = 0x80000000
                system_required = 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    continuous | (system_required if active else 0)
                )

        def close(self) -> None:
            self.set_active(False)

    icon_path = application_root() / "assets" / "app-icon.png"
    if icon_path.is_file():
        qt_app.setWindowIcon(QIcon(str(icon_path)))
    picker = FolderPicker()
    update_installer = UpdateInstaller()
    server.set_folder_picker(picker.choose)
    server.update_manager.set_installer_callback(update_installer.install)
    window = RothbaldWindow()
    power_awake = PowerAwake()
    power_timer = QTimer()
    power_timer.setInterval(2000)
    power_timer.timeout.connect(lambda: power_awake.set_active(server.runtime_has_active_work()))
    power_timer.start()

    def focus_window() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def accept_instance_message() -> None:
        while instance_server.hasPendingConnections():
            connection = instance_server.nextPendingConnection()
            connection.waitForReadyRead(250)
            connection.readAll()
            connection.disconnectFromServer()
        if duplicate_instance_should_focus(sys.platform):
            focus_window()

    instance_server.newConnection.connect(accept_instance_message)
    window.show()
    try:
        exit_code = qt_app.exec()
    finally:
        power_timer.stop()
        power_awake.close()
        instance_server.close()
        server.shutdown_runtime()
        http_server.shutdown()
        http_server.server_close()
        server_thread.join(timeout=2)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (server.DATA_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise

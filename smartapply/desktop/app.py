"""Élan's native macOS Qt Quick application."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from smartapply.desktop.bootstrap import prepare_runtime_environment

RUNTIME = prepare_runtime_environment()

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QFont, QGuiApplication, QIcon  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402

from smartapply.desktop.bridge import DesktopBridge  # noqa: E402
from smartapply.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def resource_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "resources" / name


def qml_path(name: str = "Main.qml") -> Path:
    return Path(__file__).resolve().parent / "qml" / name


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Open the desktop shell briefly and exit with a status code.",
    )
    return parser


class ShutdownGuard(QObject):
    """Catch native Quit before Qt starts destroying windows and workers."""

    def __init__(self, application, bridge):
        super().__init__(application)
        self.bridge = bridge
        application.installEventFilter(self)
        bridge.shutdownReadyChanged.connect(
            lambda: application.quit() if bridge.shutdownReady else None
        )

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Quit and not self.bridge.shutdownReady:
            self.bridge.requestShutdown()
            return True
        return super().eventFilter(watched, event)


def create_engine(application: QGuiApplication) -> tuple[QQmlApplicationEngine, DesktopBridge]:
    bridge = DesktopBridge()
    bridge._shutdown_guard = ShutdownGuard(application, bridge)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("AppBridge", bridge)
    engine.load(QUrl.fromLocalFile(str(qml_path())))
    if not engine.rootObjects():
        raise RuntimeError(f"Impossible de charger l’interface {qml_path()}")
    application.aboutToQuit.connect(engine.deleteLater)
    return engine, bridge


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    os.environ.setdefault("QSG_RENDER_LOOP", "threaded")
    # Keep the native macOS GPU path for smooth Qt Quick compositing.
    # The software backend remains selected explicitly for smoke tests below.
    if not args.smoke_test:
        os.environ.setdefault("QSG_RHI_BACKEND", "metal")
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
        os.environ.setdefault("QSG_RHI_BACKEND", "software")
    setup_logging()
    application = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    application.setApplicationName("Elan")
    application.setApplicationDisplayName("Élan")
    application.setOrganizationName("Elan")
    application.setFont(QFont(".AppleSystemUIFont", 13))
    application.setWindowIcon(QIcon(str(resource_path("app_icon.svg"))))
    try:
        engine, _bridge = create_engine(application)
        if args.smoke_test:
            QTimer.singleShot(1400, _bridge.requestShutdown)
        return application.exec()
    except Exception:
        logger.exception("Desktop startup failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

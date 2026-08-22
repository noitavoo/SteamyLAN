from __future__ import annotations

import json
import sys

from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .constants import APP_DEVELOPER, APP_NAME, APP_VERSION
from .launch_args import connect_lobby_id_from_argv
from .logging_setup import configure_logging
from .resources import application_icon, set_windows_app_user_model_id
from .services import DetectionService, SessionManager, SteamService
from .settings import AccessStore, PreferenceStore
from .steam_api import SteamClient, start_steam_client, steam_process_running
from .ui.main_window import MainWindow


INSTANCE_NAME = "SteamyLAN.SingleInstance.v1"


class SingleInstance(QObject):
    raiseRequested = Signal()
    commandReceived = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: QLocalServer | None = None

    def become_primary(self, argv=None) -> bool:
        probe = QLocalSocket(self)
        probe.connectToServer(INSTANCE_NAME, QIODevice.OpenModeFlag.WriteOnly)
        if probe.waitForConnected(200):
            payload = json.dumps({"argv": [str(value) for value in (argv or [])[1:]]}, separators=(",", ":")).encode("utf-8")
            probe.write(payload)
            probe.flush()
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return False

        QLocalServer.removeServer(INSTANCE_NAME)
        self.server = QLocalServer(self)
        if not self.server.listen(INSTANCE_NAME):
            return True
        self.server.newConnection.connect(self._incoming)
        return True

    def _incoming(self) -> None:
        if not self.server:
            return
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if not sock:
                continue
            payload = b""
            if sock.waitForReadyRead(250):
                payload = bytes(sock.readAll())
            self.raiseRequested.emit()
            if payload:
                try:
                    decoded = json.loads(payload.decode("utf-8"))
                    argv = decoded.get("argv", []) if isinstance(decoded, dict) else []
                    if isinstance(argv, list):
                        self.commandReceived.emit([str(value) for value in argv])
                except Exception:
                    pass
            sock.disconnectFromServer()
            sock.deleteLater()


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    launch_argv = tuple(argv[1:])
    set_windows_app_user_model_id()
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_DEVELOPER)
    app.setApplicationVersion(APP_VERSION)
    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setQuitOnLastWindowClosed(False)

    single = SingleInstance(app)
    if not single.become_primary([argv[0] if argv else APP_NAME, *launch_argv]):
        return 0

    logger = configure_logging()





    try:
        if not steam_process_running():
            start_steam_client(show_window=True)
    except Exception:
        logger.debug("Could not proactively start Steam", exc_info=True)

    prefs = PreferenceStore()
    access = AccessStore()
    steam_client = SteamClient(
        logger,
        app_id=prefs.effective_app_id(),
    )
    steam_service = SteamService(steam_client, logger)
    detection = DetectionService(logger)
    session = SessionManager(steam_service, prefs, access, logger)
    window = MainWindow(detection, steam_service, session, prefs, logger)

    pending_launch_lobby = {"id": connect_lobby_id_from_argv(launch_argv)}

    def request_launch_lobby(lobby_id: int) -> None:
        lobby_id = int(lobby_id or 0)
        if lobby_id <= 0:
            return
        window._show_from_tray()
        snapshot = session.snapshot
        if snapshot.mode == "joining" and int(snapshot.lobby_id or 0) == lobby_id:
            pending_launch_lobby["id"] = 0
            return
        if snapshot.mode != "idle":
            pending_launch_lobby["id"] = 0
            window._show_notice("Steam requested a lobby join, but SteamyLAN already has an active session.")
            return
        if not steam_service.initialized:
            pending_launch_lobby["id"] = lobby_id
            return
        pending_launch_lobby["id"] = 0
        session.join_lobby_id(lobby_id, "Steam invite")

    def handle_forwarded_args(forwarded_argv) -> None:
        lobby_id = connect_lobby_id_from_argv(forwarded_argv or ())
        if lobby_id:
            request_launch_lobby(lobby_id)

    def handle_steam_ready(*_args) -> None:
        lobby_id = int(pending_launch_lobby.get("id") or 0)
        if lobby_id:
            QTimer.singleShot(0, lambda lid=lobby_id: request_launch_lobby(lid))

    single.raiseRequested.connect(window._show_from_tray)
    single.commandReceived.connect(handle_forwarded_args)
    steam_service.ready.connect(handle_steam_ready)

    def cleanup() -> None:
        try:
            session.stop()
        except Exception:
            logger.exception("Session cleanup failed")
        try:
            steam_service.shutdown()
        except Exception:
            logger.exception("Steam cleanup failed")

    app.aboutToQuit.connect(cleanup)
    window.show()



    QTimer.singleShot(0, steam_service.initialize)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

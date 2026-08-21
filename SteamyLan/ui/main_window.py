from __future__ import annotations

import base64
import os
import sys
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QFileInfo, QSize, QThreadPool, QTimer, Qt, QUrl, Slot
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon, QImage, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFileIconProvider,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QComboBox,
)

from ..constants import (
    APP_DEVELOPER,
    APP_NAME,
    APP_VERSION,
    DEFAULT_APP_ID,
    SERVICE_SCAN_INTERVAL_MS,
    VISIBILITY_FRIENDS,
    VISIBILITY_INVITE,
    VISIBILITY_PUBLIC,
)
from ..logging_setup import log_directory
from ..models import AppSnapshot, ChatMessage, DetectedService, Endpoint, SharingHost
from ..detector import shared_process_is_alive
from ..resources import application_icon
from ..services import DetectionService, SessionManager, SteamService
from ..settings import PreferenceStore
from ..updater import (
    UpdateInfo,
    check_for_update,
    download_update,
    launch_update_helper,
    update_cache_directory,
)
from ..workers import FunctionWorker
from ..util import elapsed_label, short_path
from .theme import STYLE


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget:
            widget.deleteLater()
        elif child:
            clear_layout(child)
            child.deleteLater()


def loading_indicator(text: str = "Loading…") -> QWidget:
    """Create a compact indeterminate progress indicator for async UI work."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.setSpacing(8)
    bar = QProgressBar()
    bar.setObjectName("LoadingBar")
    bar.setRange(0, 0)
    bar.setTextVisible(False)
    bar.setFixedSize(84, 7)
    layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignVCenter)
    label = QLabel(text)
    label.setObjectName("Muted")
    layout.addWidget(label)
    layout.addStretch(1)
    return row




def parse_port_ranges(text: str, *, max_ports: int = 32) -> tuple[int, ...]:
    raw = str(text or "").replace(";", ",").replace(" ", ",")
    ports: list[int] = []
    seen: set[int] = set()
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if "-" in token:
            if token.count("-") != 1:
                raise ValueError(f"Invalid port range: {token}")
            start_text, end_text = (part.strip() for part in token.split("-", 1))
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"Invalid port range: {token}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"Port range must go from low to high: {token}")
            values = range(start, end + 1)
        else:
            if not token.isdigit():
                raise ValueError(f"Invalid port: {token}")
            values = (int(token),)
        for port in values:
            if not 1 <= int(port) <= 65535:
                raise ValueError(f"Port {port} must be between 1 and 65535.")
            if int(port) not in seen:
                seen.add(int(port))
                ports.append(int(port))
            if len(ports) > max_ports:
                raise ValueError(f"You can enter at most {max_ports} ports at a time.")
    if not ports:
        raise ValueError("Enter at least one port.")
    return tuple(ports)


def compact_port_ranges(ports) -> str:
    values = sorted({int(port) for port in ports if 1 <= int(port) <= 65535})
    if not values:
        return ""
    result: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    result.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(result)

def plain_label(text: object = "") -> QLabel:
    """QLabel for user/network supplied text; never interpret it as rich text."""
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class PlayerLimitEdit(QLineEdit):
    """Compact player-limit editor with normal multi-digit typing semantics.

    The first digit typed after focusing/clicking replaces the old value, while
    following digits append normally.  Intermediate values such as ``1`` are
    allowed while the user is in the middle of typing ``100``; the final value
    is clamped to the configured range when editing finishes.
    """

    def __init__(self, minimum: int = 2, maximum: int = 250, parent=None):
        super().__init__(parent)
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._replace_on_next_digit = True



        self.setValidator(QIntValidator(0, self._maximum, self))

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def value(self) -> int:
        try:
            return int(self.text())
        except (TypeError, ValueError):
            return self._minimum

    def setValue(self, value: int) -> None:
        value = max(self._minimum, min(self._maximum, int(value)))
        self.setText(str(value))
        self.setCursorPosition(len(self.text()))
        self.deselect()

    def arm_replace(self) -> None:
        self._replace_on_next_digit = True
        self.deselect()
        self.setCursorPosition(len(self.text()))

    def finish_editing(self) -> int:
        value = self.value()
        value = max(self._minimum, min(self._maximum, value))
        self.setValue(value)
        self.arm_replace()
        return value

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.arm_replace()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.arm_replace()

    def keyPressEvent(self, event) -> None:
        modifiers = event.modifiers()
        plain_digit = bool(event.text() and event.text().isdigit()) and not (
            modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        )
        if plain_digit and self._replace_on_next_digit:



            self.setText(event.text())
            self.setCursorPosition(len(self.text()))
            self._replace_on_next_digit = False
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._replace_on_next_digit:
            self.clear()
            self._replace_on_next_digit = False
            event.accept()
            return
        if event.key() not in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            self._replace_on_next_digit = False
        super().keyPressEvent(event)


class SettingsDialog(QDialog):
    def __init__(
        self,
        prefs: PreferenceStore,
        update_check_callback=None,
        steam_client=None,
        parent=None,
    ):
        super().__init__(parent)
        self.prefs = prefs
        self._update_check_callback = update_check_callback
        self._steam_client = steam_client
        self.setWindowTitle("SteamyLAN - Settings")
        self.setMinimumSize(560, 620)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(8)

        title = QLabel("Settings")
        title.setObjectName("Title")
        root.addWidget(title)
        intro = QLabel("Normal choices are safe to leave alone. Network and AppID overrides are under Advanced settings.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        content = QVBoxLayout(body)
        content.setContentsMargins(2, 4, 8, 4)
        content.setSpacing(8)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        def add_setting_group(checkbox: QCheckBox, hint_text: str | None = None) -> None:
            group = QWidget()
            group.setObjectName("SettingsGroup")
            group_lay = QVBoxLayout(group)
            group_lay.setContentsMargins(0, 0, 0, 0)
            group_lay.setSpacing(1)
            group_lay.addWidget(checkbox)
            if hint_text:
                hint = QLabel(hint_text)
                hint.setObjectName("Subtle")
                hint.setWordWrap(True)
                hint.setContentsMargins(27, 0, 0, 0)
                group_lay.addWidget(hint)
            content.addWidget(group)

        self.startup = QCheckBox(f"Start {APP_NAME} when I start my computer")
        self.startup.setChecked(prefs.prefs.start_with_computer)
        add_setting_group(self.startup, "Starts SteamyLAN with Windows.")

        self.tray = QCheckBox(f"Keep {APP_NAME} running in the tray")
        self.tray.setChecked(prefs.prefs.keep_in_tray)
        add_setting_group(self.tray, "Closing the window hides it instead of ending the active connection.")

        self.auto = QCheckBox("Let friends join without asking me")
        self.auto.setChecked(prefs.prefs.auto_allow_friends)
        add_setting_group(self.auto, "When disabled, new members require host approval.")

        self.auto_ports = QCheckBox("Open every shared port automatically when I join")
        self.auto_ports.setChecked(prefs.prefs.auto_accept_ports)
        add_setting_group(self.auto_ports, "When disabled, choose each shared connection from the Server panel.")

        self.notifications = QCheckBox("Show small Windows notifications")
        self.notifications.setChecked(prefs.prefs.notifications)
        add_setting_group(self.notifications)

        self.steam_status = QCheckBox("Show my current SteamyLAN session in Steam Rich Presence")
        self.steam_status.setChecked(prefs.prefs.show_steam_status)
        add_setting_group(self.steam_status, "Shows status such as ‘SteamyLAN - Sharing Minecraft’ or ‘SteamyLAN - Joined Minecraft’ in Steam's View Game Info panel while active.")

        updates_card = QFrame()
        updates_card.setObjectName("Card")
        updates_lay = QVBoxLayout(updates_card)
        updates_lay.setContentsMargins(16, 12, 16, 12)
        updates_lay.setSpacing(6)
        updates_title = QLabel("Updates")
        updates_title.setObjectName("Heading")
        updates_lay.addWidget(updates_title)
        self.update_start = QCheckBox("Check GitHub for updates when SteamyLAN starts")
        self.update_start.setChecked(prefs.prefs.check_updates_on_start)
        updates_lay.addWidget(self.update_start)
        update_mode_label = QLabel("Update behavior")
        update_mode_label.setObjectName("Section")
        updates_lay.addWidget(update_mode_label)
        self.update_mode = QComboBox()
        self.update_mode.addItem("Download and install automatically", "automatic")
        self.update_mode.addItem("Notify me, but do not install", "notify")
        self.update_mode.addItem("Do not check automatically", "disabled")
        self.update_mode.setCurrentIndex(max(0, self.update_mode.findData(prefs.prefs.update_mode)))
        updates_lay.addWidget(self.update_mode)
        update_hint = QLabel("Automatic updates download in the background and restart SteamyLAN when safe. Active lobbies are never interrupted; the update waits until you disconnect.")
        update_hint.setObjectName("Subtle")
        update_hint.setWordWrap(True)
        updates_lay.addWidget(update_hint)
        check_now = QPushButton("Check for Updates Now")
        check_now.setObjectName("Small")
        check_now.setEnabled(update_check_callback is not None)
        if update_check_callback is not None:
            check_now.clicked.connect(lambda: update_check_callback(self, automatic=False))
        updates_lay.addWidget(check_now, 0, Qt.AlignmentFlag.AlignLeft)
        content.addWidget(updates_card)

        adv_button = QToolButton()
        adv_button.setText("Advanced settings")
        adv_button.setCheckable(True)
        adv_button.setArrowType(Qt.ArrowType.RightArrow)
        adv_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        content.addWidget(adv_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced = QFrame()
        self.advanced.setObjectName("Card")
        advanced_lay = QVBoxLayout(self.advanced)
        advanced_lay.setContentsMargins(16, 12, 16, 12)
        advanced_lay.setSpacing(8)

        appid_title = QLabel("Steam AppID")
        appid_title.setObjectName("Section")
        advanced_lay.addWidget(appid_title)
        self.custom_app_id = QLineEdit(prefs.prefs.custom_app_id)
        self.custom_app_id.setPlaceholderText("Default: 480")
        self.custom_app_id.setMaxLength(10)
        advanced_lay.addWidget(self.custom_app_id)
        appid_hint = QLabel("Enter any valid unsigned 32-bit Steam AppID, or leave it blank to use the default. AppID changes require a restart.")
        appid_hint.setObjectName("Subtle")
        appid_hint.setWordWrap(True)
        advanced_lay.addWidget(appid_hint)
        self.effective_app_id = QLabel()
        self.effective_app_id.setObjectName("Muted")
        advanced_lay.addWidget(self.effective_app_id)

        route_title = QLabel("Steam network route")
        route_title.setObjectName("Section")
        advanced_lay.addWidget(route_title)
        self.relay_mode = QComboBox()
        self.relay_mode.addItem("Automatic (Steam decides)", "automatic")
        self.relay_mode.addItem("Prefer direct connection", "prefer_direct")
        self.relay_mode.addItem("Force direct connection", "force_direct")
        self.relay_mode.addItem("Prefer Steam Relay", "prefer_relay")
        self.relay_mode.addItem("Force Steam Relay", "force_relay")
        idx = self.relay_mode.findData(prefs.prefs.relay_mode)
        self.relay_mode.setCurrentIndex(max(0, idx))
        advanced_lay.addWidget(self.relay_mode)
        route_hint = QLabel("Prefer modes bias Steam toward that route while keeping a fallback. Force direct strongly penalizes relay routes; Force Steam Relay disables direct ICE candidates.")
        route_hint.setObjectName("Subtle")
        route_hint.setWordWrap(True)
        advanced_lay.addWidget(route_hint)

        relay_location_title = QLabel("Steam relay location")
        relay_location_title.setObjectName("Section")
        advanced_lay.addWidget(relay_location_title)
        relay_row = QHBoxLayout()
        self.relay_location = QComboBox()
        relay_row.addWidget(self.relay_location, 1)
        refresh_relays = QPushButton("Refresh pings")
        refresh_relays.setObjectName("Small")
        refresh_relays.clicked.connect(self._refresh_relay_locations)
        relay_row.addWidget(refresh_relays)
        advanced_lay.addLayout(relay_row)
        relay_hint = QLabel("Automatic lets Steam choose. Available Steam Datagram Relay locations are ordered from lowest measured ping to highest.")
        relay_hint.setObjectName("Subtle")
        relay_hint.setWordWrap(True)
        advanced_lay.addWidget(relay_hint)
        self._refresh_relay_locations()

        bind_title = QLabel("Local tunnel bind address")
        bind_title.setObjectName("Section")
        advanced_lay.addWidget(bind_title)
        self.bind_address = QLineEdit(prefs.prefs.bind_address)
        self.bind_address.setPlaceholderText("127.0.0.1")
        self.bind_address.setToolTip("Numeric IPv4 or IPv6 address used for local forwarded ports")
        advanced_lay.addWidget(self.bind_address)
        bind_hint = QLabel(
            "127.0.0.1 keeps forwarded ports local to this computer. "
            "0.0.0.0 listens on every IPv4 interface; :: listens on every IPv6 interface. "
            "The change applies when a shared port is next opened or after reconnecting."
        )
        bind_hint.setObjectName("Subtle")
        bind_hint.setWordWrap(True)
        advanced_lay.addWidget(bind_hint)

        bandwidth_title = QLabel("Bandwidth limits")
        bandwidth_title.setObjectName("Section")
        advanced_lay.addWidget(bandwidth_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.upload_limit = QSpinBox()
        self.upload_limit.setRange(0, 1_000_000)
        self.upload_limit.setValue(prefs.prefs.upload_limit_kbps)
        self.upload_limit.setSuffix(" Kbit/s")
        self.upload_limit.setSpecialValueText("Unlimited")
        self.download_limit = QSpinBox()
        self.download_limit.setRange(0, 1_000_000)
        self.download_limit.setValue(prefs.prefs.download_limit_kbps)
        self.download_limit.setSuffix(" Kbit/s")
        self.download_limit.setSpecialValueText("Unlimited")
        grid.addWidget(QLabel("Upload"), 0, 0)
        grid.addWidget(self.upload_limit, 0, 1)
        grid.addWidget(QLabel("Download"), 1, 0)
        grid.addWidget(self.download_limit, 1, 1)
        grid.setColumnStretch(1, 1)
        advanced_lay.addLayout(grid)
        bandwidth_hint = QLabel("0 means unlimited. The upload cap is also passed to Steam's native send-rate controller; both directions are enforced by SteamyLAN's transport layer.")
        bandwidth_hint.setObjectName("Subtle")
        bandwidth_hint.setWordWrap(True)
        advanced_lay.addWidget(bandwidth_hint)
        self.advanced.hide()
        content.addWidget(self.advanced)
        adv_button.toggled.connect(
            lambda checked: (
                self.advanced.setVisible(checked),
                adv_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow),
            )
        )

        about_button = QToolButton()
        about_button.setText("About and logs")
        about_button.setCheckable(True)
        about_button.setArrowType(Qt.ArrowType.RightArrow)
        about_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        content.addWidget(about_button, 0, Qt.AlignmentFlag.AlignLeft)
        about_card = QFrame()
        about_card.setObjectName("Card")
        about_lay = QVBoxLayout(about_card)
        about = QLabel(f"{APP_NAME} {APP_VERSION}")
        about.setObjectName("Heading")
        about_lay.addWidget(about)
        developer = QLabel(f"Developer: {APP_DEVELOPER}")
        developer.setObjectName("Muted")
        about_lay.addWidget(developer)
        log_label = QLabel(f"Logs are saved here: {log_directory()}")
        log_label.setWordWrap(True)
        log_label.setObjectName("Muted")
        about_lay.addWidget(log_label)
        open_logs = QPushButton("Open Logs")
        open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_directory()))))
        about_lay.addWidget(open_logs, 0, Qt.AlignmentFlag.AlignLeft)
        about_card.hide()
        content.addWidget(about_card)
        about_button.toggled.connect(
            lambda checked: (
                about_card.setVisible(checked),
                about_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow),
            )
        )
        content.addStretch(1)

        self.custom_app_id.textChanged.connect(self._sync_app_id_display)
        self._sync_app_id_display()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


    def _refresh_relay_locations(self) -> None:
        current = str(getattr(self.prefs.prefs, "relay_location", "automatic") or "automatic")
        if hasattr(self, "relay_location") and self.relay_location.currentData():
            current = str(self.relay_location.currentData())
        self.relay_location.clear()
        self.relay_location.addItem("Automatic (Steam chooses)", "automatic")
        rows = []
        if self._steam_client is not None and getattr(self._steam_client, "initialized", False):
            try:
                rows = list(self._steam_client.relay_locations())
            except Exception:
                rows = []
        for code, ping in rows:
            code = str(code).strip().casefold()
            if not code:
                continue
            label = f"{code.upper()} · {ping} ms" if int(ping) >= 0 else f"{code.upper()} · ping unavailable"
            self.relay_location.addItem(label, code)
        if current != "automatic" and self.relay_location.findData(current) < 0:
            self.relay_location.addItem(f"{current.upper()} · saved selection", current)
        index = self.relay_location.findData(current)
        self.relay_location.setCurrentIndex(max(0, index))

    def _parsed_custom_app_id(self) -> int | None:
        text = self.custom_app_id.text().strip()
        if not text:
            return None
        try:
            app_id = int(text, 10)
        except (TypeError, ValueError, OverflowError):
            return -1
        return app_id if 0 < app_id <= 0xFFFFFFFF else -1

    def _sync_app_id_display(self, *_args) -> None:
        parsed = self._parsed_custom_app_id()
        if parsed and parsed > 0:
            value = parsed
            source = "custom override"
        else:
            value = DEFAULT_APP_ID
            source = "default"
        self.effective_app_id.setText(f"Effective AppID after restart: {value} ({source})")

    def accept(self) -> None:
        parsed_app_id = self._parsed_custom_app_id()
        if parsed_app_id == -1:
            QMessageBox.warning(self, APP_NAME, "Custom AppID must be a number from 1 through 4294967295, or left blank.")
            return
        bind_address = self.prefs.normalize_bind_address(self.bind_address.text())
        if bind_address is None:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Bind address must be a numeric IPv4 or IPv6 address, such as 127.0.0.1, 0.0.0.0, ::1, or ::.",
            )
            return

        previous = replace(self.prefs.prefs)
        startup_enabled = self.startup.isChecked()
        startup_changed = startup_enabled != previous.start_with_computer
        if startup_changed:
            ok, error = self.prefs.set_startup(startup_enabled)
            if not ok:
                QMessageBox.warning(self, APP_NAME, f"Couldn't change the startup setting.\n\n{error}")
                self.startup.setChecked(previous.start_with_computer)
                return

        self.prefs.prefs.start_with_computer = startup_enabled
        self.prefs.prefs.keep_in_tray = self.tray.isChecked()
        self.prefs.prefs.auto_allow_friends = self.auto.isChecked()
        self.prefs.prefs.auto_accept_ports = self.auto_ports.isChecked()
        self.prefs.prefs.notifications = self.notifications.isChecked()
        self.prefs.prefs.show_steam_status = self.steam_status.isChecked()
        self.prefs.prefs.check_updates_on_start = self.update_start.isChecked()
        self.prefs.prefs.update_mode = str(self.update_mode.currentData() or "automatic")
        self.prefs.prefs.custom_app_id = "" if parsed_app_id is None else str(parsed_app_id)
        self.prefs.prefs.relay_mode = str(self.relay_mode.currentData() or "automatic")
        self.prefs.prefs.relay_location = str(self.relay_location.currentData() or "automatic")
        self.prefs.prefs.bind_address = bind_address
        self.prefs.prefs.upload_limit_kbps = int(self.upload_limit.value())
        self.prefs.prefs.download_limit_kbps = int(self.download_limit.value())
        try:
            self.prefs.save()
        except Exception as exc:
            self.prefs.prefs = previous
            rollback_error = ""
            if startup_changed:
                _rolled_back, rollback_error = self.prefs.set_startup(previous.start_with_computer)
            detail = str(exc)
            if rollback_error:
                detail += f"\n\nThe Windows startup entry also could not be restored: {rollback_error}"
            QMessageBox.warning(self, APP_NAME, f"Couldn't save the settings.\n\n{detail}")
            return
        super().accept()


class DonationDialog(QDialog):
    BITCOIN_ADDRESS = "bc1qsp9tuke9ftw7xlr9nsndhhyl5fhu9q8a5mfvzt"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Support {APP_NAME}")
        self.setMinimumWidth(500)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(9)

        title = QLabel("Donate ❤️")
        title.setObjectName("Heading")
        root.addWidget(title)

        text = QLabel("If SteamyLAN is useful to you, you can support its development with a Bitcoin donation.")
        text.setObjectName("Muted")
        text.setWordWrap(True)
        root.addWidget(text)

        network = QLabel("Bitcoin address")
        network.setObjectName("Section")
        root.addWidget(network)

        address_row = QHBoxLayout()
        address_row.setSpacing(8)
        address = QLineEdit(self.BITCOIN_ADDRESS)
        address.setReadOnly(True)
        address.setObjectName("DonationAddress")
        address.setToolTip("Bitcoin mainnet address")
        address_row.addWidget(address, 1)
        copy_button = QPushButton("Copy")
        copy_button.setObjectName("Small")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.BITCOIN_ADDRESS))
        address_row.addWidget(copy_button)
        root.addLayout(address_row)

        caution = QLabel("Verify the address before sending. Cryptocurrency transfers cannot normally be reversed.")
        caution.setObjectName("Subtle")
        caution.setWordWrap(True)
        root.addWidget(caution)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


class ManualServiceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a server manually")
        self._service: DetectedService | None = None
        root = QVBoxLayout(self)
        hint = QLabel("Use this only if SteamyLAN cannot find the server automatically. Port ranges such as 25565-25570 are supported.")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        root.addWidget(hint)
        self.name = QLineEdit("Game Server")
        root.addWidget(QLabel("Server name"))
        root.addWidget(self.name)
        row = QHBoxLayout()
        self.protocol = QComboBox()
        self.protocol.addItems(["Any", "TCP", "UDP"])
        self.protocol.setToolTip("Any shares every entered port over both TCP and UDP.")
        self.port = QLineEdit("25565")
        self.port.setPlaceholderText("25565 or 25565-25570")
        row.addWidget(QLabel("Connection type"))
        row.addWidget(self.protocol)
        row.addWidget(QLabel("Ports"))
        row.addWidget(self.port, 1)
        root.addLayout(row)
        protocol_hint = QLabel("Separate entries with commas. Any creates both TCP and UDP entries, up to 32 shared endpoints total.")
        protocol_hint.setObjectName("Subtle")
        protocol_hint.setWordWrap(True)
        root.addWidget(protocol_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:
        try:
            ports = parse_port_ranges(self.port.text(), max_ports=32)
            proto = self.protocol.currentText()
            endpoint_count = len(ports) * (2 if proto == "Any" else 1)
            if endpoint_count > 32:
                raise ValueError("This selection creates more than 32 TCP/UDP endpoints. Use a smaller range.")
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        name = self.name.text().strip() or "Game Server"
        protocols = ("TCP", "UDP") if proto == "Any" else (proto,)
        endpoints = tuple(Endpoint(protocol, port, "127.0.0.1") for port in ports for protocol in protocols)
        self._service = DetectedService(
            key=f"manual:{proto}:{','.join(map(str, ports))}",
            name=name,
            process_name="Manual",
            pid=0,
            endpoints=endpoints,
            confidence=100,
            known_game=False,
        )
        super().accept()

    def service(self) -> DetectedService:
        if self._service is None:
            raise RuntimeError("Manual service has not been accepted.")
        return self._service


class ServerSettingsDialog(QDialog):
    def __init__(self, session: SessionManager, services: list[DetectedService], parent=None):
        super().__init__(parent)
        self.session = session
        self.config = session.session_config
        self.current_service = session.shared_service
        self._all_services = list(services or [])
        self._services: list[DetectedService] = []
        self._result_service: DetectedService | None = None
        self._base_service: DetectedService | None = None
        self._show_background_services = False
        self._endpoint_names: dict[tuple[str, int], str] = {}
        self._endpoint_ips: dict[tuple[str, int], str] = {}
        if self.config is None or self.current_service is None:
            raise RuntimeError("No hosted server is active.")
        self._base_service = self.current_service
        self._endpoint_names = {
            (spec.protocol.upper(), int(spec.port)): spec.name for spec in self.config.services
        }
        self._endpoint_ips = {
            (endpoint.protocol.upper(), int(endpoint.port)): endpoint.local_ip
            for endpoint in self.current_service.endpoints
        }
        self.setWindowTitle("Server Settings")
        self.setMinimumWidth(640)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(9)

        title = QLabel("Server settings")
        title.setObjectName("Heading")
        root.addWidget(title)
        hint = QLabel("Changes are applied to the active Steam lobby. Existing members stay connected while their shared ports update.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.name = QLineEdit(self.config.lobby_name)
        self.name.setMaxLength(80)
        form.addWidget(QLabel("Server name"), 0, 0)
        form.addWidget(self.name, 0, 1)
        self.visibility = QComboBox()
        self.visibility.addItem("Friends Only", VISIBILITY_FRIENDS)
        self.visibility.addItem("Public", VISIBILITY_PUBLIC)
        self.visibility.addItem("Invite Only", VISIBILITY_INVITE)
        self.visibility.setCurrentIndex(max(0, self.visibility.findData(self.config.visibility)))
        form.addWidget(QLabel("Visibility"), 1, 0)
        form.addWidget(self.visibility, 1, 1)
        self.max_members = QSpinBox()
        self.max_members.setRange(2, 250)
        self.max_members.setValue(self.config.max_members)
        form.addWidget(QLabel("Player limit"), 2, 0)
        form.addWidget(self.max_members, 2, 1)
        root.addLayout(form)

        self.password_enabled = QCheckBox("Password protected")
        self.password_enabled.setChecked(bool(self.config.password_salt))
        root.addWidget(self.password_enabled)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(128)
        self.password.setPlaceholderText("Leave blank to keep the current password" if self.config.password_salt else "Password (4+ characters)")
        self.password.setEnabled(self.password_enabled.isChecked())
        self.password_enabled.toggled.connect(self.password.setEnabled)
        root.addWidget(self.password)

        self.static_code = QCheckBox("Use a static share code for this server")
        self.static_code.setChecked(session.static_share_code_enabled)
        self.static_code.setToolTip("A static code remains the same when this server is hosted again from this computer.")
        root.addWidget(self.static_code)

        program_title = QLabel("Program and shared ports")
        program_title.setObjectName("Section")
        root.addWidget(program_title)

        selector_row = QHBoxLayout()
        self.program = QComboBox()
        self.program.currentIndexChanged.connect(self._update_program_summary)
        selector_row.addWidget(self.program, 1)
        self.show_background = QCheckBox("Show background services")
        self.show_background.setChecked(False)
        self.show_background.setToolTip("Background processes without a visible window are hidden by default.")
        self.show_background.toggled.connect(self._background_toggled)
        selector_row.addWidget(self.show_background)
        root.addLayout(selector_row)

        self.program_summary = QLabel()
        self.program_summary.setObjectName("Subtle")
        self.program_summary.setWordWrap(True)
        root.addWidget(self.program_summary)

        program_actions = QHBoxLayout()
        self.add_program_ports = QPushButton("Add these ports to the server")
        self.add_program_ports.setObjectName("PortAction")
        self.add_program_ports.setToolTip("Keep every currently shared port and add any new ports detected for the selected program.")
        self.add_program_ports.clicked.connect(self._append_program)
        program_actions.addWidget(self.add_program_ports, 1)
        self.use_program_ports = QPushButton("Use only these ports")
        self.use_program_ports.setObjectName("PortAction")
        self.use_program_ports.setToolTip("Remove the current port selection and use only ports detected for the selected program.")
        self.use_program_ports.clicked.connect(self._replace_program)
        program_actions.addWidget(self.use_program_ports, 1)
        root.addLayout(program_actions)

        # These are commands, not a persistent two-option selection.  Keep
        # their idle styling identical and only show a short confirmation on
        # the action that was actually applied.
        self._program_feedback_timer = QTimer(self)
        self._program_feedback_timer.setSingleShot(True)
        self._program_feedback_timer.setInterval(1400)
        self._program_feedback_timer.timeout.connect(self._clear_program_action_feedback)

        self.program_action_status = QLabel("Choose how the selected program should change the port list.")
        self.program_action_status.setObjectName("ActionStatus")
        self.program_action_status.setWordWrap(True)
        root.addWidget(self.program_action_status)

        ports = QGridLayout()
        self.tcp_ports = QLineEdit(compact_port_ranges(spec.port for spec in self.config.services if spec.protocol.upper() == "TCP"))
        self.tcp_ports.setPlaceholderText("e.g. 25565-25570")
        self.udp_ports = QLineEdit(compact_port_ranges(spec.port for spec in self.config.services if spec.protocol.upper() == "UDP"))
        self.udp_ports.setPlaceholderText("e.g. 27015, 27020-27022")
        ports.addWidget(QLabel("TCP ports"), 0, 0)
        ports.addWidget(self.tcp_ports, 0, 1)
        ports.addWidget(QLabel("UDP ports"), 1, 0)
        ports.addWidget(self.udp_ports, 1, 1)
        root.addLayout(ports)
        range_hint = QLabel("You can also edit these fields directly. Use commas and hyphen ranges; one server can share up to 32 TCP/UDP ports.")
        range_hint.setObjectName("Subtle")
        range_hint.setWordWrap(True)
        root.addWidget(range_hint)

        self._populate_programs()
        self._update_program_summary()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _background_service(service: DetectedService) -> bool:
        return bool(service.pid > 0 and not service.known_game and not (service.window_title or "").strip())

    def _background_toggled(self, checked: bool) -> None:
        self._show_background_services = bool(checked)
        self._populate_programs()

    def _populate_programs(self) -> None:
        selected_key = None
        current = self._selected_program()
        if current is not None:
            selected_key = MainWindow._service_ui_key(current)
        self.program.blockSignals(True)
        self.program.clear()
        self._services.clear()
        candidates = [self.current_service, *self._all_services]
        seen: set[str] = set()
        selected_index = 0
        for service in candidates:
            if service is None:
                continue
            key = MainWindow._service_ui_key(service)
            if key in seen:
                continue
            seen.add(key)
            if service is not self.current_service and not self._show_background_services and self._background_service(service):
                continue
            self._services.append(service)
            label = f"{service.name} — {service.process_name}" if service.process_name else service.name
            self.program.addItem(label, len(self._services) - 1)
            if selected_key and key == selected_key:
                selected_index = self.program.count() - 1
        self.program.setCurrentIndex(selected_index if self.program.count() else -1)
        self.program.blockSignals(False)
        self._update_program_summary()

    def _update_program_summary(self, _index: int = -1) -> None:
        if not hasattr(self, "program_summary"):
            return
        service = self._selected_program()
        enabled = bool(service and service.endpoints)
        self.add_program_ports.setEnabled(enabled)
        self.use_program_ports.setEnabled(enabled)
        if not enabled:
            self.program_summary.setText("No usable TCP or UDP ports were detected for this program.")
            return
        tcp = sum(1 for endpoint in service.endpoints if endpoint.protocol.upper() == "TCP")
        udp = sum(1 for endpoint in service.endpoints if endpoint.protocol.upper() == "UDP")
        parts = []
        if tcp:
            parts.append(f"{tcp} TCP")
        if udp:
            parts.append(f"{udp} UDP")
        self.program_summary.setText(
            f"Detected for {service.name}: {' + '.join(parts)} port{'s' if tcp + udp != 1 else ''}."
        )

    def _selected_program(self) -> DetectedService | None:
        if not hasattr(self, "program"):
            return None
        data = self.program.currentData()
        if data is None:
            return None
        index = int(data)
        return self._services[index] if 0 <= index < len(self._services) else None

    def _parse_current_ports(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        tcp = parse_port_ranges(self.tcp_ports.text(), max_ports=32) if self.tcp_ports.text().strip() else ()
        udp = parse_port_ranges(self.udp_ports.text(), max_ports=32) if self.udp_ports.text().strip() else ()
        return tcp, udp

    @staticmethod
    def _refresh_dynamic_style(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _show_program_action_feedback(self, selected: QPushButton) -> None:
        for button in (self.add_program_ports, self.use_program_ports):
            button.setProperty("applied", button is selected)
            self._refresh_dynamic_style(button)
        self._program_feedback_timer.start()

    def _clear_program_action_feedback(self) -> None:
        for button in (self.add_program_ports, self.use_program_ports):
            if button.property("applied"):
                button.setProperty("applied", False)
                self._refresh_dynamic_style(button)

    def _append_program(self) -> None:
        service = self._selected_program()
        if service is None:
            return
        try:
            tcp, udp = self._parse_current_ports()
            tcp_values = set(tcp)
            udp_values = set(udp)
            before = len(tcp_values) + len(udp_values)
            for endpoint in service.endpoints:
                key = (endpoint.protocol.upper(), int(endpoint.port))
                already_selected = (
                    key[1] in tcp_values if key[0] == "TCP" else key[1] in udp_values
                )
                if key[0] == "TCP":
                    tcp_values.add(key[1])
                elif key[0] == "UDP":
                    udp_values.add(key[1])
                if not already_selected:
                    self._endpoint_names[key] = service.name
                    self._endpoint_ips[key] = endpoint.local_ip
            if len(tcp_values) + len(udp_values) > 32:
                raise ValueError("Adding this program would exceed the 32-port server limit.")
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.tcp_ports.setText(compact_port_ranges(tcp_values))
        self.udp_ports.setText(compact_port_ranges(udp_values))
        added = len(tcp_values) + len(udp_values) - before
        self.program_action_status.setText(
            f"Added {added} new port{'s' if added != 1 else ''} from {service.name}. "
            f"The server will share {len(tcp_values) + len(udp_values)} ports after you save."
            if added
            else f"All ports detected for {service.name} were already selected."
        )
        self._show_program_action_feedback(self.add_program_ports)
        if MainWindow._service_ui_key(service) != MainWindow._service_ui_key(self.current_service):
            self._base_service = replace(self.current_service, pid=0, process_name="Multiple programs", exe_path="", window_title="")

    def _replace_program(self) -> None:
        service = self._selected_program()
        if service is None:
            return
        self.tcp_ports.setText(compact_port_ranges(endpoint.port for endpoint in service.endpoints if endpoint.protocol.upper() == "TCP"))
        self.udp_ports.setText(compact_port_ranges(endpoint.port for endpoint in service.endpoints if endpoint.protocol.upper() == "UDP"))
        self._endpoint_names = {
            (endpoint.protocol.upper(), int(endpoint.port)): service.name for endpoint in service.endpoints
        }
        self._endpoint_ips = {
            (endpoint.protocol.upper(), int(endpoint.port)): endpoint.local_ip for endpoint in service.endpoints
        }
        self._base_service = service
        total = len(service.endpoints)
        self.program_action_status.setText(
            f"Selected only {service.name}'s {total} detected port{'s' if total != 1 else ''}. "
            "The previous port selection will be removed when you save."
        )
        self._show_program_action_feedback(self.use_program_ports)

    def accept(self) -> None:
        try:
            tcp, udp = self._parse_current_ports()
            if not tcp and not udp:
                raise ValueError("Select at least one TCP or UDP port.")
            if len(tcp) + len(udp) > 32:
                raise ValueError("A server can share at most 32 TCP/UDP endpoints.")
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        keys = [("TCP", port) for port in tcp] + [("UDP", port) for port in udp]
        fallback_name = (self._selected_program() or self.current_service).name
        endpoint_names = {key: self._endpoint_names.get(key, fallback_name) for key in keys}
        endpoints = tuple(Endpoint(proto, port, self._endpoint_ips.get((proto, port), "127.0.0.1")) for proto, port in keys)
        unique_names = []
        for key in keys:
            value = endpoint_names[key]
            if value not in unique_names:
                unique_names.append(value)
        display_name = unique_names[0] if len(unique_names) == 1 else " + ".join(unique_names[:3])
        base = self._base_service or self.current_service
        if len(unique_names) > 1:
            base = replace(base, pid=0, process_name="Multiple programs", exe_path="", window_title="")
        self._result_service = replace(base, name=display_name[:120], endpoints=endpoints)
        if not self.password_enabled.isChecked():
            password: str | None = ""
        elif self.config.password_salt and not self.password.text():
            password = None
        else:
            password = self.password.text()
            if len(password) < 4:
                QMessageBox.warning(self, APP_NAME, "Lobby passwords must be at least 4 characters.")
                return
        if self.session.reconfigure_server(
            self._result_service,
            lobby_name=self.name.text(),
            visibility=str(self.visibility.currentData()),
            max_members=self.max_members.value(),
            password=password,
            static_code=self.static_code.isChecked(),
            endpoint_names=endpoint_names,
        ):
            super().accept()


class MainWindow(QMainWindow):
    def __init__(
        self,
        detection: DetectionService,
        steam: SteamService,
        session: SessionManager,
        prefs: PreferenceStore,
        logger,
    ):
        super().__init__()
        self.detection = detection
        self.steam = steam
        self.session = session
        self.prefs = prefs
        self.log = logger
        self.services: list[DetectedService] = []
        self.hosts: list[SharingHost] = []
        self.public_lobbies: list[SharingHost] = []
        self.friends = []
        self.steam_name = ""
        self.steam_ready = False
        self.steam_error = "Starting Steam and signing in…"
        self._really_quit = False
        self._tray_notice_shown = False
        self._create_query = ""
        self._join_query = ""
        self._friend_query = ""
        self._friend_list_collapsed = False
        self._server_details_collapsed = True
        self._friend_category_collapsed = {"online": False, "away": True, "offline": True}
        self._selected_friend_ids: set[int] = set()
        self._friend_invite_button = None
        self._friend_card_body = None
        self._friend_list_toggle = None
        self._friend_total_count_label = None
        self._friend_rows_layout = None
        self._friend_selected_count_label = None
        self._friend_select_visible_button = None
        self._friend_clear_selection_button = None
        self._friend_visible_ids: tuple[int, ...] = ()
        self._member_metric_labels: dict[int, tuple[QLabel, QLabel]] = {}
        self._chat_messages_layout = None
        self._chat_scroll_area = None
        self._chat_editor = None
        self._chat_latest_button = None
        self._chat_state_label = None
        self._chat_send_button = None
        self._chat_rendered_keys: tuple[tuple, ...] = ()
        self._chat_following = True
        self._chat_unread = 0
        self._restoring_chat_scroll = False
        self._rendered_server_structure_key = None
        self._join_tab_index = 0
        self._lobby_visibility_filter = "Friends"
        self._lobby_protocol_filter = "Any"
        self._lobby_open_only = True
        self._invite_code_draft = ""
        self._chat_draft = ""
        self._create_lobby_name = ""
        self._create_visibility = VISIBILITY_FRIENDS
        self._create_max_members = 8
        self._create_password_enabled = False
        self._create_password = ""
        self._create_static_code = False
        self._show_background_services = False
        self._server_dirty = True
        self._share_dirty = True
        self._join_dirty = True
        self._icon_cache: dict[tuple[str, int], QIcon] = {}
        self._avatar_pixmap_cache: dict[tuple[int, int, int, int], QPixmap] = {}
        self.friend_search_input = None
        self._update_check_running = False


        self._automatic_update_check_attempted = False
        self._show_current_update_result = False
        self._update_check_was_automatic = False
        self._update_check_notice = None
        self._update_worker = None
        self._update_install_running = False
        self._update_download_notice = None
        self._update_download_worker = None
        self._downloading_update_info = None
        self._pending_update_archive = None
        self._pending_update_info = None
        self._runtime_labels: list[tuple[QLabel, DetectedService]] = []
        self._share_names: dict[str, str] = {}
        self._share_port_choices: dict[str, set[tuple[str, int]]] = {}
        self._share_known_ports: dict[str, set[tuple[str, int]]] = {}



        self.program_card_holder = None
        self.program_count_label = None
        self.host_card_holder = None
        self.host_port_holder = None
        self._lobby_card_holder = None
        self._lobby_count_label = None
        self.icon_provider = QFileIconProvider()

        self._create_search_timer = QTimer(self)
        self._create_search_timer.setSingleShot(True)
        self._create_search_timer.setInterval(90)
        self._create_search_timer.timeout.connect(lambda: self._render_program_cards(self._create_query))
        self._join_search_timer = QTimer(self)
        self._join_search_timer.setSingleShot(True)
        self._join_search_timer.setInterval(80)
        self._join_search_timer.timeout.connect(self._render_lobby_cards)
        self._friend_search_timer = QTimer(self)
        self._friend_search_timer.setSingleShot(True)
        self._friend_search_timer.setInterval(80)
        self._friend_search_timer.timeout.connect(self._render_friend_rows)
        self._detection_loading = False
        self._lobbies_loading = False
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(60_000)
        self._runtime_timer.timeout.connect(self._update_elapsed_labels)
        self._runtime_timer.start()

        self.setWindowTitle("SteamyLAN")
        self.resize(980, 760)
        self.setMinimumSize(680, 560)
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:


            app.setStyleSheet(STYLE)
        else:
            self.setStyleSheet(STYLE)

        self._build_ui()
        self._build_tray()
        self._connect()
        self._restore_geometry()

        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(SERVICE_SCAN_INTERVAL_MS)
        self.scan_timer.timeout.connect(self.detection.refresh)
        self._sync_detection_timer(refresh_now=True)
        self._sync_lobby_discovery()
        self._render_visible(force=True)
        if self.prefs.prefs.check_updates_on_start and self.prefs.prefs.update_mode != "disabled":
            QTimer.singleShot(1500, lambda: self._check_for_updates(self, automatic=True))

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)



        top = QFrame()
        top.setObjectName("TopBar")
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(20, 9, 20, 8)
        top_lay.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(9)
        brand = QLabel(APP_NAME)
        brand.setObjectName("Brand")
        brand_row.addWidget(brand)
        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("Subtle")
        brand_row.addWidget(version)
        brand_row.addStretch(1)

        self.identity = QLabel("Starting Steam…")
        self.identity.setObjectName("Identity")
        self.identity.setMaximumWidth(230)
        self.identity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        brand_row.addWidget(self.identity)

        top_lay.addLayout(brand_row)

        self.nav_row = QWidget()
        self.nav_row.setObjectName("NavRow")
        nav_lay = QHBoxLayout(self.nav_row)
        nav_lay.setContentsMargins(0, 0, 0, 0)
        nav_lay.setSpacing(8)

        self.join_nav = QPushButton("Lobbies")
        self.join_nav.setObjectName("Nav")
        self.join_nav.setCheckable(True)
        nav_lay.addWidget(self.join_nav)

        self.create_nav = QPushButton("Create Server")
        self.create_nav.setObjectName("Nav")
        self.create_nav.setCheckable(True)
        nav_lay.addWidget(self.create_nav)

        self.server_nav = QPushButton("Server")
        self.server_nav.setObjectName("Nav")
        self.server_nav.setCheckable(True)
        nav_lay.addWidget(self.server_nav)




        nav_lay.addStretch(1)
        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("SettingsButton")
        nav_lay.addWidget(self.settings_button)
        top_lay.addWidget(self.nav_row)
        outer.addWidget(top)

        self.stack = QStackedWidget()
        self.stack.setObjectName("ContentStack")
        outer.addWidget(self.stack, 1)
        self.join_page, self.join_content = self._make_page()
        self.share_page, self.share_content = self._make_page()
        self.server_page, self.server_content = self._make_page()
        self.stack.addWidget(self.join_page)
        self.stack.addWidget(self.share_page)
        self.stack.addWidget(self.server_page)



        year = max(2026, datetime.now().year)
        year_text = "2026" if year == 2026 else f"2026-{year}"
        footer = QFrame()
        footer.setObjectName("Footer")
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(22, 7, 22, 8)
        donate_label = QLabel('<a href="donate" style="color:#aebcff;text-decoration:none;">Donate ❤️</a>')
        donate_label.setObjectName("DonateLink")
        donate_label.setToolTip("Support SteamyLAN development")
        donate_label.linkActivated.connect(lambda _href: DonationDialog(self).exec())
        footer_lay.addWidget(donate_label)
        footer_lay.addStretch(1)
        copyright_label = QLabel(
            f'<a style="color:#8492a8;text-decoration:none;" '
            f'href="https://github.com/noitavoo/SteamyLAN">© {year_text} noitavoo. All rights reserved.</a>'
        )
        copyright_label.setObjectName("FooterLink")
        copyright_label.setOpenExternalLinks(True)
        copyright_label.setToolTip("Open noitavoo on GitHub")
        footer_lay.addWidget(copyright_label)
        outer.addWidget(footer)

        self.join_nav.clicked.connect(lambda: self._switch(0))
        self.create_nav.clicked.connect(lambda: self._switch(1))
        self.server_nav.clicked.connect(lambda: self._switch(2))
        self._sync_create_visibility()
        initial_page = 1 if self.prefs.prefs.last_page == "create" and self._create_available() else 0
        self._switch(initial_page)

    def _make_page(self):
        scroll = QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)



        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("PageBody")
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content = QVBoxLayout(body)
        content.setContentsMargins(22, 20, 22, 24)
        content.setSpacing(11)
        content.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(body)
        return scroll, content

    def _build_tray(self) -> None:
        icon = application_icon()
        if icon.isNull():
            icon = QApplication.windowIcon()
        self.tray = QSystemTrayIcon(self)
        if not icon.isNull():
            self.tray.setIcon(icon)
        self.tray.setToolTip(APP_NAME)
        self.tray_menu = QMenu(self)
        show_action = QAction(f"Open {APP_NAME}", self)
        show_action.triggered.connect(self._show_from_tray)
        self.tray_server = QAction("Open Server", self)
        self.tray_server.triggered.connect(lambda: (self._show_from_tray(), self._switch(2)))
        self.tray_copy_code = QAction("Copy Share Code", self)
        self.tray_copy_code.triggered.connect(lambda: QApplication.clipboard().setText(self.session.snapshot.join_code))
        self.tray_stop = QAction("Stop Sharing / Disconnect", self)
        self.tray_stop.triggered.connect(self.session.stop)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self._quit_from_tray)
        self.tray_menu.addAction(show_action)
        self.tray_menu.addAction(self.tray_server)
        self.tray_menu.addAction(self.tray_copy_code)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.tray_stop)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(quit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: self._show_from_tray()
            if reason in {QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger}
            else None
        )
        self._ensure_tray()
        self._update_tray(self.session.snapshot)

    def _ensure_tray(self) -> None:




        if not self.tray.isVisible():
            self.tray.show()

    def _connect(self) -> None:
        self.settings_button.clicked.connect(self._settings)
        self.detection.updated.connect(self._services_updated)
        self.detection.failed.connect(self._detection_failed)
        self.steam.ready.connect(self._steam_ready)
        self.steam.failed.connect(self._steam_failed)
        self.steam.friendsUpdated.connect(self._friends_updated)
        self.steam.hostsUpdated.connect(self._hosts_updated)
        self.steam.lobbiesUpdated.connect(self._public_lobbies_updated)
        self.steam.lobbyInviteReceived.connect(self._steam_lobby_invite_received)
        self.steam.joinRequested.connect(self._steam_lobby_join_requested)
        self.session.changed.connect(self._session_changed)
        self.session.chatChanged.connect(self._chat_changed)
        self.session.chatStateChanged.connect(self._chat_state_changed)
        self.session.error.connect(self._show_error)
        self.session.notice.connect(self._show_notice)
        self.session.approvalRequested.connect(self._approval_requested)
        self.session.passwordRequested.connect(self._password_requested)

    def _create_available(self) -> bool:
        return True

    def _sync_create_visibility(self) -> None:
        available = self._create_available()
        active = self.session.snapshot.mode != "idle"
        self.nav_row.setVisible(True)
        self.join_nav.setVisible(True)
        self.create_nav.setVisible(available)
        self.server_nav.setVisible(active)
        if not available and self.stack.currentIndex() == 1:
            self.stack.setCurrentIndex(0)
        if not active and self.stack.currentIndex() == 2:
            self.stack.setCurrentIndex(0)
        current = self.stack.currentIndex()
        self.join_nav.setChecked(current == 0)
        self.create_nav.setChecked(current == 1)
        self.server_nav.setChecked(current == 2)
        self._sync_detection_timer()

    def _sync_detection_timer(self, refresh_now: bool = False) -> None:



        if not hasattr(self, "scan_timer"):
            return
        should_run = self._create_available()
        if should_run:
            if not self.scan_timer.isActive():
                self.scan_timer.start()
                refresh_now = True
            if refresh_now:
                self._detection_loading = True
                self.detection.refresh()
        elif self.scan_timer.isActive():
            self.scan_timer.stop()

    def _sync_lobby_discovery(self) -> None:


        active = self.stack.currentIndex() == 0 and self.session.snapshot.mode == "idle"
        self.steam.set_lobby_discovery_active(active)

    def _switch(self, index: int) -> None:
        index = int(index)
        if index == 1 and not self._create_available():
            index = 0
        if index == 2 and self.session.snapshot.mode == "idle":
            index = 0
        index = max(0, min(2, index))
        self.stack.setCurrentIndex(index)
        self.join_nav.setChecked(index == 0)
        self.create_nav.setChecked(index == 1)
        self.server_nav.setChecked(index == 2)
        if index != 1:
            self._create_search_timer.stop()
        if index != 0:
            self._join_search_timer.stop()
        page = {0: "join", 1: "create", 2: "server"}[index]
        if self.prefs.prefs.last_page != page:
            self.prefs.prefs.last_page = page
            try:
                self.prefs.save()
            except Exception:
                self.log.debug("Could not save last page", exc_info=True)
        self._sync_lobby_discovery()
        self._render_visible()

    def _render_visible(self, force: bool = False) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            if force or self._join_dirty:
                self._render_join()
        elif index == 1:
            if force or self._share_dirty:
                self._render_share()
        else:
            if force or self._server_dirty:
                self._render_server()

    def _steam_ready(self, name: str, _steam_id) -> None:
        self.steam_ready = True
        self.steam_name = name
        self.identity.setText(f"Signed in as {name}")
        self.identity.setToolTip(f"Steam account: {name}")
        self._share_dirty = True
        self._join_dirty = True
        self._server_dirty = True
        self._sync_lobby_discovery()
        self._render_visible()

    def _steam_failed(self, text: str) -> None:
        self.steam_ready = False
        self.steam_error = text
        self.identity.setText("Steam needs attention")
        self._share_dirty = True
        self._join_dirty = True
        self._server_dirty = True
        self._render_visible()

    def _services_updated(self, services) -> None:
        self._detection_loading = False
        self.services = list(services or [])
        shared = self.session.shared_service
        if shared and self.session.snapshot.mode == "sharing" and shared.pid > 0:




            if not shared_process_is_alive(shared):
                self.session.stop()
                self._show_notice(f"{shared.name} stopped, so sharing stopped.")
                return

        self._share_dirty = True
        if (
            self.stack.currentIndex() == 1
            and self.session.snapshot.mode == "idle"
            and self.steam_ready
            and self._create_available()
            and self.program_card_holder is not None
        ):


            self._render_program_cards(self._create_query)
            self._share_dirty = False

    def _friends_updated(self, friends) -> None:
        self.friends = list(friends or [])
        invitables = {int(friend.steam_id) for friend in self.friends if int(friend.steam_id) > 0}
        self._selected_friend_ids.intersection_update(invitables)
        self._server_dirty = True
        if self.stack.currentIndex() == 2 and self.session.snapshot.mode == "sharing" and self._friend_card_body is not None:
            if self._friend_total_count_label is not None:
                self._friend_total_count_label.setText(f"{len(self.friends)} friends")
            self._render_friend_rows()
            self._server_dirty = False

    def _hosts_updated(self, hosts) -> None:
        self._lobbies_loading = False
        self.hosts = list(hosts or [])
        self._join_dirty = True
        if self.stack.currentIndex() == 0 and self.steam_ready and self._lobby_card_holder is not None:
            self._render_lobby_cards()
            self._join_dirty = False

    def _public_lobbies_updated(self, lobbies) -> None:
        self._lobbies_loading = False
        self.public_lobbies = list(lobbies or [])
        self._join_dirty = True
        if self.stack.currentIndex() == 0 and self.steam_ready and self._lobby_card_holder is not None:
            self._render_lobby_cards()
            self._join_dirty = False

    def _steam_lobby_invite_received(self, lobby_id, friend_name: str) -> None:
        # LobbyInvite_t only means an invite arrived. Steam Chat already presents
        # the accept/join action, so do not show a second SteamyLAN confirmation
        # dialog here. The actual acceptance is delivered separately through
        # GameLobbyJoinRequested_t and handled below.
        joining = self.session.snapshot
        if joining.mode == "joining" and int(joining.lobby_id or 0) == int(lobby_id):
            return
        if joining.mode != "idle":
            self._show_notice(f"{friend_name} sent a Steam lobby invite. Finish your current SteamyLAN session before joining it.")
            return
        self._show_notice(f"{friend_name} sent a Steam lobby invite. Accept it in Steam Chat to join.")

    def _steam_lobby_join_requested(self, lobby_id, friend_name: str) -> None:
        # GameLobbyJoinRequested_t is emitted after the user has already chosen
        # Join in Steam. Obey it directly instead of asking for confirmation a
        # second time (which can be hidden behind Steam/the overlay).
        lobby_id = int(lobby_id or 0)
        if lobby_id <= 0:
            self._show_error("Steam returned an invalid lobby invite.")
            return
        self._show_from_tray()
        joining = self.session.snapshot
        if joining.mode == "joining" and int(joining.lobby_id or 0) == lobby_id:
            return
        if joining.mode != "idle":
            self._show_notice(f"Steam requested joining {friend_name}'s lobby, but you already have an active SteamyLAN session.")
            return
        self.session.join_lobby_id(lobby_id, friend_name or "Steam friend")

    def _chat_changed(self, _messages) -> None:
        self._server_dirty = True
        if self.stack.currentIndex() == 2 and self._chat_messages_layout is not None:
            self._render_chat_messages()
            self._server_dirty = False
        elif self.stack.currentIndex() == 2:
            self._render_server()

    def _chat_state_changed(self, ready: bool) -> None:
        if self._chat_state_label is not None:
            self._chat_state_label.setText("Encrypted and ready" if ready else "Securing chat…")
            self._chat_state_label.setObjectName("SecureChip" if ready else "WaitingChip")
            self._chat_state_label.style().unpolish(self._chat_state_label)
            self._chat_state_label.style().polish(self._chat_state_label)
        if self._chat_send_button is not None:
            self._chat_send_button.setToolTip(
                "Send an encrypted message" if ready else "Your draft will stay here until secure chat is ready"
            )

    def _session_changed(self, snapshot: AppSnapshot) -> None:




        if (
            self._rendered_server_structure_key is not None
            and self._server_structure_key(snapshot) == self._rendered_server_structure_key
        ):
            self._update_server_telemetry(snapshot)
            return

        self._sync_create_visibility()
        self._share_dirty = True
        self._join_dirty = True
        self._server_dirty = True
        if snapshot.mode in {"joining", "connected", "sharing"} and self.stack.currentIndex() != 2:
            self.stack.setCurrentIndex(2)
            self.join_nav.setChecked(False)
            self.create_nav.setChecked(False)
            self.server_nav.setChecked(True)
        elif snapshot.mode == "idle" and self.stack.currentIndex() == 2:
            self.stack.setCurrentIndex(0)
            self.join_nav.setChecked(True)
            self.create_nav.setChecked(False)
            self.server_nav.setChecked(False)

        self._sync_lobby_discovery()
        self._render_visible(force=True)
        self._update_tray(snapshot)
        if snapshot.mode == "idle":
            self._install_pending_update()

    def _render_all(self) -> None:


        self._share_dirty = True
        self._join_dirty = True
        self._render_visible()

    def _render_steam_problem(self, layout) -> bool:
        if self.steam_ready:
            return False
        lowered = (self.steam_error or "").casefold()
        starting = self.steam_error == "Starting Steam and signing in…"
        if starting:
            heading = "Starting Steam"
        elif "steam_api64.dll" in lowered:
            heading = "Steam support file needs attention."
        elif "running" in lowered and "mismatch" in lowered:
            heading = "SteamyLAN found Steam."
        else:
            heading = "Steam needs attention"
        title = QLabel(heading)
        title.setObjectName("Heading")
        layout.addWidget(title)
        if starting:
            layout.addWidget(loading_indicator("Waiting for Steam to sign in…"))
        text = QLabel(self.steam_error)
        text.setWordWrap(True)
        text.setObjectName("Muted")
        layout.addWidget(text)
        if not starting:
            open_button = QPushButton("Open Steam")
            open_button.setObjectName("Primary")
            open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("steam://open/main")))
            layout.addWidget(open_button, 0, Qt.AlignmentFlag.AlignLeft)
            retry_button = QPushButton("Retry connection")
            retry_button.clicked.connect(self._retry_steam)
            layout.addWidget(retry_button, 0, Qt.AlignmentFlag.AlignLeft)
        return True

    def _retry_steam(self) -> None:
        self.steam.initialize()

    def _copy_connection_details(self) -> None:
        snap = self.session.snapshot
        if not snap.mappings:
            self._show_notice("Open a shared port first, then its connection address can be copied.")
            return
        lines = [f"{snap.service_name or snap.lobby_name}: {mapping.address}" for mapping in snap.mappings]
        QApplication.clipboard().setText("\n".join(lines))
        self._show_notice("Connection details copied to the clipboard.")

    def _render_share(self) -> None:
        self._share_dirty = False
        self._create_search_timer.stop()
        self._runtime_labels = []
        self.program_card_holder = None
        self.program_count_label = None
        clear_layout(self.share_content)

        title = QLabel("Create Server")
        title.setObjectName("Title")
        self.share_content.addWidget(title)
        sub = QLabel("Choose the program, lobby access, and player limit. Only the selected ports are exposed through Steam.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        self.share_content.addWidget(sub)

        if self._render_steam_problem(self.share_content):
            return

        snap = self.session.snapshot
        if snap.mode == "idle":
            setup = QFrame()
            setup.setObjectName("Card")
            sl = QVBoxLayout(setup)
            sl.setContentsMargins(16, 14, 16, 14)
            sl.setSpacing(8)
            heading = QLabel("Lobby setup")
            heading.setObjectName("Heading")
            sl.addWidget(heading)
            setup_row = QHBoxLayout()
            setup_row.setSpacing(8)
            lobby_name = QLineEdit(self._create_lobby_name)
            lobby_name.setPlaceholderText("Lobby name (defaults to server name)")
            lobby_name.setMaximumWidth(330)
            lobby_name.textChanged.connect(lambda text: setattr(self, "_create_lobby_name", text[:80]))
            setup_row.addWidget(lobby_name, 2)
            visibility = QComboBox()
            visibility.addItem("Friends Only", VISIBILITY_FRIENDS)
            visibility.addItem("Public", VISIBILITY_PUBLIC)
            visibility.addItem("Invite Only", VISIBILITY_INVITE)
            vi = visibility.findData(self._create_visibility)
            visibility.setCurrentIndex(max(0, vi))
            visibility.currentIndexChanged.connect(
                lambda _i, box=visibility: setattr(self, "_create_visibility", str(box.currentData()))
            )
            setup_row.addWidget(visibility, 1)
            player_stepper = QFrame()
            player_stepper.setObjectName("Stepper")
            player_stepper.setToolTip("Maximum users in this lobby, including the host")
            player_lay = QHBoxLayout(player_stepper)
            player_lay.setContentsMargins(3, 2, 3, 2)
            player_lay.setSpacing(0)

            decrease_users = QToolButton()
            decrease_users.setObjectName("StepperButton")
            decrease_users.setText("−")
            decrease_users.setToolTip("Decrease player limit")
            decrease_users.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            player_lay.addWidget(decrease_users)

            users = PlayerLimitEdit(2, 250)
            users.setObjectName("StepperValue")
            users.setValue(self._create_max_members)
            users.setAlignment(Qt.AlignmentFlag.AlignCenter)
            users.setFixedWidth(50)
            users.setToolTip("Type a value from 2 to 250, including the host")

            def sync_users_from_text(text: str) -> None:
                if not text.isdigit():
                    return
                value = int(text)
                if users.minimum() <= value <= users.maximum():
                    self._create_max_members = value

            def finish_users_edit() -> None:
                self._create_max_members = users.finish_editing()

            users.textChanged.connect(sync_users_from_text)
            users.editingFinished.connect(finish_users_edit)
            player_lay.addWidget(users)

            users_word = QLabel("users")
            users_word.setObjectName("StepperSuffix")
            users_word.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            users_word.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            users_word.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            player_lay.addWidget(users_word)

            increase_users = QToolButton()
            increase_users.setObjectName("StepperButton")
            increase_users.setText("+")
            increase_users.setToolTip("Increase player limit")
            increase_users.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            player_lay.addWidget(increase_users)

            def adjust_users(delta: int) -> None:



                current = self._create_max_members
                value = max(users.minimum(), min(users.maximum(), current + delta))
                self._create_max_members = value
                users.setValue(value)
                QTimer.singleShot(0, users.arm_replace)

            decrease_users.clicked.connect(lambda: adjust_users(-1))
            increase_users.clicked.connect(lambda: adjust_users(1))
            setup_row.addWidget(player_stepper)
            setup_row.addStretch(1)
            sl.addLayout(setup_row)

            security_row = QHBoxLayout()
            security_row.setSpacing(8)
            password_toggle = QCheckBox("Password protected")
            password_toggle.setChecked(self._create_password_enabled)
            security_row.addWidget(password_toggle)
            password_editor = QLineEdit(self._create_password)
            password_editor.setEchoMode(QLineEdit.EchoMode.Password)
            password_editor.setPlaceholderText("Password (4+ characters)")
            password_editor.setMaxLength(128)
            password_editor.setMinimumWidth(250)
            password_editor.setMaximumWidth(360)
            password_editor.setEnabled(self._create_password_enabled)
            password_editor.textChanged.connect(lambda text: setattr(self, "_create_password", text))
            security_row.addWidget(password_editor, 1)

            reveal_password = QToolButton()
            reveal_password.setObjectName("PasswordReveal")
            reveal_password.setText("Show")
            reveal_password.setCheckable(True)
            reveal_password.setChecked(False)
            reveal_password.setEnabled(self._create_password_enabled)
            reveal_password.setToolTip("Show password")
            reveal_password.setFocusPolicy(Qt.FocusPolicy.NoFocus)

            def reveal_toggled(checked: bool) -> None:
                password_editor.setEchoMode(
                    QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                )
                reveal_password.setText("Hide" if checked else "Show")
                reveal_password.setToolTip("Hide password" if checked else "Show password")

            reveal_password.toggled.connect(reveal_toggled)
            security_row.addWidget(reveal_password)
            security_row.addStretch(1)

            def password_toggled(checked: bool) -> None:
                self._create_password_enabled = bool(checked)
                password_editor.setEnabled(bool(checked))
                reveal_password.setEnabled(bool(checked))
                if not checked and reveal_password.isChecked():
                    reveal_password.setChecked(False)
                if checked:
                    password_editor.setFocus()

            password_toggle.toggled.connect(password_toggled)
            sl.addLayout(security_row)

            static_code = QCheckBox("Use a static share code for this server")
            static_code.setChecked(self._create_static_code)
            static_code.setToolTip("Keeps the same share code when this server is hosted again from this computer.")
            static_code.toggled.connect(lambda checked: setattr(self, "_create_static_code", bool(checked)))
            sl.addWidget(static_code)

            hint = QLabel("User limit includes the host. Public appears in the browser; Friends Only uses Steam friends; Invite Only uses a private Steam lobby. Every hosted server gets a share code. The password itself is never advertised in lobby metadata.")
            hint.setObjectName("Subtle")
            hint.setWordWrap(True)
            sl.addWidget(hint)
            self.share_content.addWidget(setup)
        if snap.mode == "starting":
            card = self._card()
            lay = card.layout()
            h = QLabel("Starting your share…")
            h.setObjectName("Heading")
            lay.addWidget(h)
            lay.addWidget(loading_indicator("Creating the Steam lobby…"))
            msg = QLabel(f"Preparing {snap.service_name} for your lobby.")
            msg.setObjectName("Muted")
            lay.addWidget(msg)
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.session.stop)
            lay.addWidget(cancel, 0, Qt.AlignmentFlag.AlignLeft)
            self.share_content.addWidget(card)
            return

        if snap.mode == "sharing":
            card = self._card()
            lay = card.layout()
            h = QLabel("Sharing now")
            h.setObjectName("Heading")
            lay.addWidget(h)
            name = QLabel(snap.service_name)
            name.setObjectName("Service")
            lay.addWidget(name)
            status = QLabel("Lobby members can connect while SteamyLAN and this program remain running.")
            status.setWordWrap(True)
            status.setObjectName("Muted")
            lay.addWidget(status)
            if snap.peers:
                lay.addSpacing(8)
                people = QLabel("FRIENDS")
                people.setObjectName("Section")
                lay.addWidget(people)
                for peer in snap.peers:
                    row = QFrame()
                    row.setObjectName("Banner")
                    rl = QVBoxLayout(row)
                    rl.setContentsMargins(12, 10, 12, 10)
                    rl.setSpacing(6)
                    nm = plain_label(peer.name)
                    nm.setObjectName("Status")
                    nm.setWordWrap(True)
                    rl.addWidget(nm)
                    st = QLabel(peer.status)
                    st.setObjectName("Muted")
                    st.setWordWrap(True)
                    rl.addWidget(st)
                    action_row = QHBoxLayout()
                    action_row.setSpacing(8)
                    if peer.status == "Waiting for approval":
                        allow = QPushButton("Allow")
                        allow.setObjectName("Primary")
                        allow.clicked.connect(lambda _=False, sid=peer.steam_id: self.session.allow_peer(sid, True))
                        deny = QPushButton("Don't Allow")
                        deny.clicked.connect(lambda _=False, sid=peer.steam_id: self.session.deny_peer(sid))
                        action_row.addWidget(allow)
                        action_row.addWidget(deny)
                    else:
                        menu_button = QPushButton("Manage")
                        menu = QMenu(menu_button)
                        disconnect = menu.addAction("Disconnect")
                        remove = menu.addAction("Remove Access")
                        disconnect.triggered.connect(lambda _=False, sid=peer.steam_id: self.session.disconnect_peer(sid))
                        remove.triggered.connect(lambda _=False, sid=peer.steam_id, nm=peer.name: self._confirm_remove(sid, nm))
                        menu_button.setMenu(menu)
                        action_row.addWidget(menu_button)
                    action_row.addStretch(1)
                    rl.addLayout(action_row)
                    lay.addWidget(row)
            else:
                waiting = QLabel("Waiting for lobby members to connect…")
                waiting.setObjectName("Muted")
                lay.addWidget(waiting)
            stop = QPushButton("Stop Sharing")
            stop.setObjectName("Danger")
            stop.clicked.connect(self._confirm_stop_sharing)
            lay.addWidget(stop, 0, Qt.AlignmentFlag.AlignLeft)
            self.share_content.addWidget(card)
            return

        if snap.mode in {"joining", "connected"}:
            card = self._card()
            lay = card.layout()
            h = QLabel("You're connected to a lobby")
            h.setObjectName("Heading")
            lay.addWidget(h)
            info = QLabel("Disconnect from the Server panel before creating a new lobby.")
            info.setObjectName("Muted")
            lay.addWidget(info)
            self.share_content.addWidget(card)
            return

        hero = QFrame()
        hero.setObjectName("Hero")
        hero_lay = QVBoxLayout(hero)
        hero_lay.setContentsMargins(22, 20, 22, 20)
        hero_lay.setSpacing(7)
        hero_title = QLabel("Select a running program")
        hero_title.setObjectName("Heading")
        hero_lay.addWidget(hero_title)
        hero_text = QLabel(
            "SteamyLAN only lists processes that currently own a listening network port. "
            "Nothing is selected automatically."
        )
        hero_text.setWordWrap(True)
        hero_text.setObjectName("Muted")
        hero_lay.addWidget(hero_text)
        self.share_content.addWidget(hero)

        controls = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Search running programs")
        search.setClearButtonEnabled(True)
        search.setText(self._create_query)
        search.textChanged.connect(self._program_search_changed)
        controls.addWidget(search, 1)
        background = QCheckBox("Show background services")
        background.setChecked(self._show_background_services)
        background.setToolTip("Background processes without a visible window are hidden by default.")
        background.toggled.connect(self._background_services_toggled)
        controls.addWidget(background)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.detection.refresh)
        controls.addWidget(refresh)
        manual = QPushButton("Add Manually")
        manual.setToolTip("Share a port over TCP, UDP, or both when automatic detection did not find it.")
        manual.clicked.connect(self._manual_service)
        controls.addWidget(manual)
        self.share_content.addLayout(controls)

        count = QLabel()
        count.setObjectName("Muted")
        self.program_count_label = count
        self.share_content.addWidget(count)

        self.program_card_holder = QVBoxLayout()
        self.program_card_holder.setSpacing(12)
        self.share_content.addLayout(self.program_card_holder)
        self._render_program_cards(self._create_query)

    def _program_search_changed(self, text: str) -> None:
        self._create_query = text or ""
        self._create_search_timer.start()

    def _background_services_toggled(self, checked: bool) -> None:
        self._show_background_services = bool(checked)
        self._render_program_cards(self._create_query)

    @staticmethod
    def _is_background_service(service: DetectedService) -> bool:
        return bool(service.pid > 0 and not service.known_game and not (service.window_title or "").strip())

    def _render_program_cards(self, query: str) -> None:
        if (
            self.stack.currentIndex() != 1
            or self.session.snapshot.mode != "idle"
            or self.program_card_holder is None
        ):
            return
        self._runtime_labels = []
        clear_layout(self.program_card_holder)
        if self._detection_loading and not self.services:
            self.program_card_holder.addWidget(loading_indicator("Scanning listening ports…"))
            return
        folded = (query or "").strip().casefold()

        def matches(service: DetectedService) -> bool:
            if not folded:
                return True
            port_text = " ".join(f"{e.protocol} {e.port}" for e in service.endpoints)
            haystack = " ".join(
                [
                    service.name, service.process_name, service.description,
                    service.exe_path, service.window_title, port_text,
                ]
            ).casefold()
            return folded in haystack



        services = sorted(
            (s for s in self.services if matches(s) and (self._show_background_services or not self._is_background_service(s))),
            key=lambda s: (
                -float(s.started_at or 0.0),
                s.name.casefold(),
                s.process_name.casefold(),
                s.pid,
            ),
        )
        if self.program_count_label is not None:
            total = len(self.services)
            hidden_background = sum(1 for item in self.services if self._is_background_service(item)) if not self._show_background_services else 0
            shown = len(services)
            suffix = f" · {hidden_background} background service{'s' if hidden_background != 1 else ''} hidden" if hidden_background else ""
            if folded:
                self.program_count_label.setText(f"Showing {shown} of {total} programs with listening ports{suffix}")
            else:
                visible_total = total - hidden_background
                self.program_count_label.setText(f"{visible_total} program{'s' if visible_total != 1 else ''} with listening ports{suffix}")

        if not services:
            card = self._card()
            lay = card.layout()
            if self.services and not self._show_background_services and all(self._is_background_service(item) for item in self.services):
                h = QLabel("Only background services found")
                msg = QLabel("Enable Show background services above to include them in this list.")
            elif self.services and folded:
                h = QLabel("No matching programs")
                msg = QLabel("Try another name, executable, or port number.")
            else:
                h = QLabel("No programs with listening ports found")
                msg = QLabel("Start the game or server you want to share, then click Refresh.")
            h.setObjectName("Heading")
            lay.addWidget(h)
            msg.setWordWrap(True)
            msg.setObjectName("Muted")
            lay.addWidget(msg)
            self.program_card_holder.addWidget(card)
            return

        for service in services:
            self.program_card_holder.addWidget(self._service_card(service))

    def _service_icon(self, service: DetectedService) -> QIcon:
        icon_path = (service.icon_path or "").strip()
        exe = (service.exe_path or "").strip()

        candidate = ""
        direct_image = False
        if icon_path and os.path.isfile(icon_path):
            suffix = Path(icon_path).suffix.casefold()
            if suffix in {".png", ".jpg", ".jpeg", ".ico", ".bmp", ".webp"}:
                candidate = icon_path
                direct_image = True
        if not candidate and exe and os.path.exists(exe):
            candidate = exe

        if not candidate:
            return self.windowIcon()

        key = (os.path.normcase(candidate), 1 if direct_image else 0)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached

        icon = QIcon(candidate) if direct_image else self.icon_provider.icon(QFileInfo(candidate))
        if icon.isNull():
            icon = self.windowIcon()



        if len(self._icon_cache) >= 192:
            self._icon_cache.pop(next(iter(self._icon_cache)))
        self._icon_cache[key] = icon
        return icon

    @staticmethod
    def _service_ui_key(service: DetectedService) -> str:


        identity = os.path.normcase((service.exe_path or service.process_name).strip())
        started = int(max(0.0, float(service.started_at or 0.0)) * 1000)
        return f"{service.pid}:{started}:{identity}"

    def _sync_service_port_choices(self, service: DetectedService) -> set[tuple[str, int]]:
        ui_key = self._service_ui_key(service)
        current = {endpoint.key for endpoint in service.endpoints}
        seen = self._share_known_ports.get(ui_key, set())
        selected = set(self._share_port_choices.get(ui_key, set()))
        if not seen:
            selected.update(current)
        else:


            selected.update(current - seen)
        self._share_known_ports[ui_key] = seen | current
        self._share_port_choices[ui_key] = selected
        return selected

    def _service_card(self, service: DetectedService) -> QFrame:
        card = QFrame()
        card.setObjectName("ProcessCard")
        card.setMinimumWidth(0)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 15, 16, 15)
        lay.setSpacing(9)
        ui_key = self._service_ui_key(service)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_label = QLabel()
        icon_label.setObjectName("IconTile")
        icon_label.setFixedSize(50, 50)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(self._service_icon(service).pixmap(QSize(36, 36)))
        top.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        names_widget = QWidget()
        names_widget.setMinimumWidth(0)
        names_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        names = QVBoxLayout(names_widget)
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(3)

        name = QLabel(service.name)
        name.setObjectName("Service")
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        names.addWidget(name)

        description = QLabel(service.description or "Running application")
        description.setObjectName("Muted")
        description.setWordWrap(True)
        description.setMinimumWidth(0)
        description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        names.addWidget(description)

        process = QLabel(f"{service.process_name}  •  PID {service.pid}  •  {elapsed_label(service.started_at)}")
        process.setObjectName("Subtle")
        process.setWordWrap(True)
        process.setMinimumWidth(0)
        process.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        names.addWidget(process)
        self._runtime_labels.append((process, service))
        top.addWidget(names_widget, 1)
        lay.addLayout(top)

        if service.window_title and service.window_title.casefold() not in {service.name.casefold(), service.description.casefold()}:
            window = QLabel(f"Window: {service.window_title}")
            window.setObjectName("Muted")
            window.setWordWrap(True)
            window.setMinimumWidth(0)
            window.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lay.addWidget(window)

        if service.exe_path:
            path = QLabel(f"Executable: {short_path(service.exe_path, 105)}")
            path.setToolTip(service.exe_path)
            path.setObjectName("Subtle")
            path.setWordWrap(True)
            path.setMinimumWidth(0)
            path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lay.addWidget(path)

        ports_label = QLabel("CHOOSE WHICH PORTS LOBBY MEMBERS CAN USE")
        ports_label.setObjectName("Section")
        lay.addWidget(ports_label)
        ports_hint = QLabel("Click a port to switch it ON or OFF. Only ON ports are shared with lobby members.")
        ports_hint.setObjectName("Subtle")
        ports_hint.setWordWrap(True)
        lay.addWidget(ports_hint)

        selected = self._sync_service_port_choices(service)
        port_buttons: list[tuple[QPushButton, Endpoint]] = []
        ports = QGridLayout()
        ports.setHorizontalSpacing(7)
        ports.setVerticalSpacing(7)
        max_columns = 3
        for index, endpoint in enumerate(service.endpoints):
            button = QPushButton()
            button.setObjectName("PortToggle")
            button.setCheckable(True)
            button.setChecked(endpoint.key in selected)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row, column = divmod(index, max_columns)
            ports.addWidget(button, row, column)
            port_buttons.append((button, endpoint))
        for column in range(max_columns):
            ports.setColumnStretch(column, 1)
        port_controls = QHBoxLayout()
        toggle_all_ports = QPushButton()
        toggle_all_ports.setObjectName("Small")
        port_controls.addWidget(toggle_all_ports, 0, Qt.AlignmentFlag.AlignLeft)
        port_controls.addStretch(1)
        lay.addLayout(port_controls)
        lay.addLayout(ports)

        port_state = QLabel()
        port_state.setObjectName("Muted")
        port_state.setWordWrap(True)
        lay.addWidget(port_state)

        details_button = QToolButton()
        details_button.setText("Technical details")
        details_button.setCheckable(True)
        details_button.setArrowType(Qt.ArrowType.RightArrow)
        details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        details = QFrame()
        details.setObjectName("Banner")
        details.setMinimumWidth(0)
        details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        details_lay = QVBoxLayout(details)
        details_lay.setContentsMargins(12, 10, 12, 10)
        details_lay.setSpacing(5)
        if service.steam_appid:
            steam_meta = QLabel(f"Steam App ID: {service.steam_appid}")
            steam_meta.setObjectName("Subtle")
            details_lay.addWidget(steam_meta)
        bindings = ", ".join(f"{e.protocol} {e.local_ip or '0.0.0.0'}:{e.port}" for e in service.endpoints)
        binding_label = QLabel(f"Detected bindings: {bindings}")
        binding_label.setObjectName("Subtle")
        binding_label.setWordWrap(True)
        binding_label.setMinimumWidth(0)
        binding_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details_lay.addWidget(binding_label)
        if service.cmdline:
            command = " ".join(service.cmdline)
            cmd_label = QLabel(f"Command: {short_path(command, 150)}")
            cmd_label.setToolTip(command)
            cmd_label.setObjectName("Subtle")
            cmd_label.setWordWrap(True)
            cmd_label.setMinimumWidth(0)
            cmd_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            details_lay.addWidget(cmd_label)
        details.hide()




        lay.addWidget(details_button, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(details)
        details_button.toggled.connect(
            lambda checked: (
                details.setVisible(checked),
                details_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow),
            )
        )

        if service.warning:
            warning = QFrame()
            warning.setObjectName("WarningBanner")
            warning.setMinimumWidth(0)
            warning_lay = QVBoxLayout(warning)
            warning_lay.setContentsMargins(11, 8, 11, 8)
            warning_text = QLabel(service.warning)
            warning_text.setWordWrap(True)
            warning_text.setMinimumWidth(0)
            warning_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            warning_text.setObjectName("Muted")
            warning_lay.addWidget(warning_text)
            lay.addWidget(warning)

        share_name_label = QLabel("LOBBY NAME MEMBERS WILL SEE")
        share_name_label.setObjectName("Section")
        lay.addWidget(share_name_label)

        share_name = QLineEdit()
        share_name.setMaxLength(120)
        share_name.setPlaceholderText("Name your server")
        share_name.setText(self._share_names.get(ui_key, service.name))
        share_name.setToolTip("This is only the lobby name members see. It does not rename the running program.")
        share_name.textChanged.connect(lambda text, key=ui_key: self._share_names.__setitem__(key, text))
        lay.addWidget(share_name)

        name_hint = QLabel("Call it anything you want. This only changes the name shown to lobby members.")
        name_hint.setObjectName("Subtle")
        name_hint.setWordWrap(True)
        lay.addWidget(name_hint)

        share = QPushButton("Start Sharing")
        share.setObjectName("Primary")
        share.setMinimumWidth(132)
        share.setMaximumWidth(190)
        share.clicked.connect(lambda _=False, s=service, editor=share_name: self._start_service_share(s, editor))

        def refresh_port_buttons() -> None:
            chosen = self._share_port_choices.setdefault(ui_key, set())
            for button, endpoint in port_buttons:
                is_on = endpoint.key in chosen
                button.blockSignals(True)
                button.setChecked(is_on)
                button.setText(f"{endpoint.protocol} {endpoint.port}  •  {'ON' if is_on else 'OFF'}")
                button.blockSignals(False)
            current_keys = {endpoint.key for endpoint in service.endpoints}
            count = len(chosen & current_keys)
            total = len(service.endpoints)
            toggle_all_ports.setText("Turn All Off" if total and count == total else "Turn All On")
            if count:
                port_state.setText(f"{count} of {total} port{'s' if total != 1 else ''} will be shared.")
                share.setEnabled(True)
                share.setToolTip("Share this server using only the ports marked ON.")
            else:
                port_state.setText("No ports are ON. Turn on at least one port before sharing.")
                share.setEnabled(False)
                share.setToolTip("Turn on at least one port first.")

        def port_toggled(endpoint_key: tuple[str, int], checked: bool) -> None:
            chosen = self._share_port_choices.setdefault(ui_key, set())
            if checked:
                chosen.add(endpoint_key)
            else:
                chosen.discard(endpoint_key)
            refresh_port_buttons()

        def toggle_all() -> None:
            chosen = self._share_port_choices.setdefault(ui_key, set())
            current_keys = {endpoint.key for endpoint in service.endpoints}
            if current_keys and current_keys.issubset(chosen):
                chosen.difference_update(current_keys)
            else:
                chosen.update(current_keys)
            refresh_port_buttons()

        for button, endpoint in port_buttons:
            button.toggled.connect(lambda checked, key=endpoint.key: port_toggled(key, checked))
        toggle_all_ports.clicked.connect(toggle_all)
        refresh_port_buttons()

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        actions.addWidget(share, 0, Qt.AlignmentFlag.AlignRight)
        lay.addLayout(actions)

        return card

    def _start_service_share(self, service: DetectedService, editor: QLineEdit) -> None:
        name = editor.text().strip()
        if not name:
            QMessageBox.warning(self, APP_NAME, "Enter a server name before sharing.")
            editor.setFocus()
            return
        ui_key = self._service_ui_key(service)
        selected = self._share_port_choices.get(ui_key, {endpoint.key for endpoint in service.endpoints})
        endpoints = tuple(endpoint for endpoint in service.endpoints if endpoint.key in selected)
        if not endpoints:
            QMessageBox.warning(self, APP_NAME, "Turn on at least one port before sharing.")
            return
        self._share_names[ui_key] = name


        self.session.share(
            replace(service, name=name, endpoints=endpoints),
            lobby_name=(self._create_lobby_name.strip() or f"{name} Lobby")[:80],
            visibility=self._create_visibility,
            max_members=self._create_max_members,
            password=self._create_password if self._create_password_enabled else "",
            static_code=self._create_static_code,
        )
        if self.session.snapshot.mode == "starting":
            self._create_password = ""

    def _update_elapsed_labels(self) -> None:
        for label, service in tuple(self._runtime_labels):
            try:
                label.setText(
                    f"{service.process_name}  •  PID {service.pid}  •  {elapsed_label(service.started_at)}"
                )
            except RuntimeError:

                continue

    def _render_join(self) -> None:
        self._join_dirty = False
        self._join_search_timer.stop()
        self.host_card_holder = None
        self.host_port_holder = None
        self._lobby_card_holder = None
        self._lobby_count_label = None
        clear_layout(self.join_content)

        title = QLabel("Lobby Browser")
        title.setObjectName("Title")
        self.join_content.addWidget(title)
        sub = QLabel("Browse public SteamyLAN lobbies, find friends who are hosting, or enter a share code.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        self.join_content.addWidget(sub)

        if self._render_steam_problem(self.join_content):
            return

        snap = self.session.snapshot
        if snap.mode != "idle":
            active = QFrame()
            active.setObjectName("Banner")
            al = QHBoxLayout(active)
            al.setContentsMargins(12, 10, 12, 10)
            al.setSpacing(10)
            text = QLabel(f"Active: {snap.lobby_name or snap.service_name or 'SteamyLAN server'}")
            text.setObjectName("Status")
            al.addWidget(text, 1)
            open_server = QPushButton("Open Server")
            open_server.setObjectName("Primary")
            open_server.clicked.connect(lambda: self._switch(2))
            al.addWidget(open_server)
            self.join_content.addWidget(active)

        code_card = QFrame()
        code_card.setObjectName("Card")
        cl = QVBoxLayout(code_card)
        cl.setContentsMargins(16, 13, 16, 13)
        cl.setSpacing(7)
        code_title = QLabel("Join with code")
        code_title.setObjectName("Heading")
        cl.addWidget(code_title)
        code_row = QHBoxLayout()
        code_row.setSpacing(8)
        code = QLineEdit(self._invite_code_draft)
        code.setPlaceholderText("STLN-XXXX-XXXX-…")
        code.textChanged.connect(lambda text: setattr(self, "_invite_code_draft", text))
        code_row.addWidget(code, 1)
        join_code = QPushButton("Join")
        join_code.setObjectName("Primary")
        join_code.setEnabled(snap.mode == "idle")
        join_code.clicked.connect(lambda _=False, editor=code: self.session.join_code(editor.text()))
        code.returnPressed.connect(lambda editor=code: self.session.join_code(editor.text()) if self.session.snapshot.mode == "idle" else None)
        code_row.addWidget(join_code)
        cl.addLayout(code_row)
        code_hint = QLabel("Share codes resolve the host and current lobby. Public, Friends Only, Invite Only, password, and approval rules still apply.")
        code_hint.setObjectName("Subtle")
        code_hint.setWordWrap(True)
        cl.addWidget(code_hint)
        self.join_content.addWidget(code_card)

        controls = QFrame()
        controls.setObjectName("FilterBar")
        fl = QGridLayout(controls)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setHorizontalSpacing(8)
        fl.setVerticalSpacing(7)
        search = QLineEdit(self._join_query)
        search.setPlaceholderText("Search lobby, host, game, or port")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._lobby_search_changed)
        fl.addWidget(search, 0, 0, 1, 4)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_lobbies)
        fl.addWidget(refresh, 0, 4)

        visibility = QComboBox()
        visibility.addItems(["All visibility", "Public", "Friends only"])
        visibility_map = {"All": "All visibility", "Friends": "Friends only"}
        vi = visibility.findText(visibility_map.get(self._lobby_visibility_filter, self._lobby_visibility_filter))
        visibility.setCurrentIndex(max(0, vi))
        visibility.currentTextChanged.connect(
            lambda text: self._lobby_visibility_changed({"All visibility": "All", "Friends only": "Friends"}.get(text, text))
        )
        fl.addWidget(visibility, 1, 0)

        protocol = QComboBox()
        protocol.addItems(["Any protocol", "TCP", "UDP"])
        protocol_text = "Any protocol" if self._lobby_protocol_filter == "Any" else self._lobby_protocol_filter
        pi = protocol.findText(protocol_text)
        protocol.setCurrentIndex(max(0, pi))
        protocol.currentTextChanged.connect(
            lambda text: self._lobby_protocol_changed("Any" if text == "Any protocol" else text)
        )
        fl.addWidget(protocol, 1, 1)

        open_only = QCheckBox("Open spots only")
        open_only.setChecked(self._lobby_open_only)
        open_only.toggled.connect(self._lobby_open_changed)
        fl.addWidget(open_only, 1, 2, 1, 2)
        fl.setColumnStretch(3, 1)
        self.join_content.addWidget(controls)

        count = QLabel()
        count.setObjectName("Subtle")
        self._lobby_count_label = count
        self.join_content.addWidget(count)

        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(8)
        self._lobby_card_holder = results_layout
        self.join_content.addWidget(results_widget)
        self._render_lobby_cards()

    def _render_lobby_cards(self) -> None:
        holder = self._lobby_card_holder
        if holder is None:
            return
        clear_layout(holder)
        if self._lobbies_loading:
            holder.addWidget(loading_indicator("Refreshing lobbies…"))
            return
        lobbies = self._filtered_lobbies()
        if self._lobby_count_label is not None:
            self._lobby_count_label.setText(f"{len(lobbies)} lobby{'ies' if len(lobbies) != 1 else ''} shown")

        if not lobbies:
            empty = self._card()
            el = empty.layout()
            eh = QLabel("No matching lobbies")
            eh.setObjectName("Heading")
            el.addWidget(eh)
            et = QLabel("Public servers appear here through Steam search. Friends-only servers appear when a Steam friend is hosting.")
            et.setObjectName("Muted")
            et.setWordWrap(True)
            el.addWidget(et)
            holder.addWidget(empty)
            return

        snap = self.session.snapshot
        for host in lobbies:
            card = QFrame()
            card.setObjectName("ProcessCard")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(15, 13, 15, 13)
            lay.setSpacing(7)

            top = QHBoxLayout()
            names = QVBoxLayout()
            names.setSpacing(2)
            name = plain_label(host.lobby_name or self._display_host_services(host))
            name.setObjectName("Service")
            name.setWordWrap(True)
            names.addWidget(name)
            visibility_text = "Public" if host.visibility == VISIBILITY_PUBLIC else "Friends only"
            protection = "  •  Password protected" if host.password_protected else ""
            meta = plain_label(f"Hosted by {host.host_name}  •  {host.member_count}/{host.max_members} users  •  {visibility_text}{protection}")
            meta.setObjectName("Muted")
            meta.setWordWrap(True)
            names.addWidget(meta)
            top.addLayout(names, 1)
            slots = QLabel("Full" if host.open_slots <= 0 else f"{host.open_slots} open")
            slots.setObjectName("CountChip")
            top.addWidget(slots, 0, Qt.AlignmentFlag.AlignTop)
            lay.addLayout(top)

            services = QHBoxLayout()
            services.setSpacing(6)
            shown = 0
            for spec in host.services:
                chip = plain_label(f"{spec.name} · {spec.protocol} {spec.port}")
                chip.setObjectName("PortChip")
                services.addWidget(chip)
                shown += 1
                if shown >= 4:
                    break
            services.addStretch(1)
            lay.addLayout(services)

            action = QHBoxLayout()
            join = QPushButton("Join Lobby")
            join.setObjectName("Primary")
            join.setMaximumWidth(170)
            join.setEnabled(snap.mode == "idle" and host.open_slots > 0)
            join.clicked.connect(lambda _=False, h=host: self._join_host(h))
            action.addWidget(join)
            action.addStretch(1)
            lay.addLayout(action)
            holder.addWidget(card)

    def _display_host_services(self, host: SharingHost) -> str:
        names: list[str] = []
        for spec in host.services:
            if spec.name not in names:
                names.append(spec.name)
        return ", ".join(names[:3]) or "SteamyLAN Server"

    def _combined_lobbies(self) -> list[SharingHost]:
        merged: dict[int, SharingHost] = {}
        for host in self.public_lobbies:
            merged[int(host.lobby_id)] = host
        for host in self.hosts:
            existing = merged.get(int(host.lobby_id))
            if existing is None or host.visibility != VISIBILITY_PUBLIC:
                merged[int(host.lobby_id)] = host
        return list(merged.values())

    def _filtered_lobbies(self) -> list[SharingHost]:
        query = (self._join_query or "").strip().casefold()
        result: list[SharingHost] = []
        for host in self._combined_lobbies():
            if self._lobby_visibility_filter == "Public" and host.visibility != VISIBILITY_PUBLIC:
                continue
            if self._lobby_visibility_filter == "Friends" and host.visibility != VISIBILITY_FRIENDS:
                continue
            if self._lobby_open_only and host.open_slots <= 0:
                continue
            if self._lobby_protocol_filter in {"TCP", "UDP"} and not any(
                spec.protocol == self._lobby_protocol_filter for spec in host.services
            ):
                continue
            if query:
                parts = [host.lobby_name, host.host_name, host.visibility]
                for spec in host.services:
                    parts.extend([spec.name, spec.protocol, str(spec.port)])
                if query not in " ".join(parts).casefold():
                    continue
            result.append(host)
        result.sort(key=lambda h: (h.open_slots <= 0, h.visibility != VISIBILITY_PUBLIC, h.lobby_name.casefold(), h.host_name.casefold()))
        return result

    def _lobby_search_changed(self, text: str) -> None:
        self._join_query = text or ""
        self._join_search_timer.start()

    def _refresh_lobbies(self) -> None:
        self._lobbies_loading = True
        self._render_lobby_cards()
        self.steam.refresh_lobbies()
        # Steam's discovery callbacks may omit an update when the result is
        # unchanged; keep the indicator from remaining visible in that case.
        QTimer.singleShot(7000, self._finish_lobby_loading)

    def _finish_lobby_loading(self) -> None:
        if not self._lobbies_loading:
            return
        self._lobbies_loading = False
        self._render_lobby_cards()

    def _lobby_visibility_changed(self, text: str) -> None:
        self._lobby_visibility_filter = text or "All"
        self._join_dirty = True
        self._render_lobby_cards()
        self._join_dirty = False

    def _lobby_protocol_changed(self, text: str) -> None:
        self._lobby_protocol_filter = text or "Any"
        self._join_dirty = True
        self._render_lobby_cards()
        self._join_dirty = False

    def _lobby_open_changed(self, checked: bool) -> None:
        self._lobby_open_only = bool(checked)
        self._join_dirty = True
        self._render_lobby_cards()
        self._join_dirty = False

    def _friend_search_changed(self, text: str) -> None:
        self._friend_query = str(text or "")
        self._friend_search_timer.start()

    def _set_friend_list_collapsed(self, collapsed: bool) -> None:
        self._friend_list_collapsed = bool(collapsed)
        if self._friend_list_toggle is not None:
            self._friend_list_toggle.setArrowType(
                Qt.ArrowType.RightArrow if self._friend_list_collapsed else Qt.ArrowType.DownArrow
            )
            self._friend_list_toggle.setToolTip(
                "Expand friend list" if self._friend_list_collapsed else "Collapse friend list"
            )
        self._render_friend_card_body()

    def _set_friend_category_collapsed(self, category: str, collapsed: bool) -> None:
        if category not in self._friend_category_collapsed:
            return
        self._friend_category_collapsed[category] = bool(collapsed)
        self._render_friend_rows()

    def _friend_selection_changed(self, steam_id: int, checked: bool) -> None:
        sid = int(steam_id)
        if checked:
            self._selected_friend_ids.add(sid)
        else:
            self._selected_friend_ids.discard(sid)
        self._update_friend_selection_controls()

    def _update_friend_invite_button(self) -> None:
        button = self._friend_invite_button
        if button is None:
            return
        count = len(self._selected_friend_ids)
        button.setText(f"Invite selected ({count})" if count else "Invite selected")
        button.setEnabled(count > 0)

    def _update_friend_selection_controls(self) -> None:
        self._update_friend_invite_button()
        count = len(self._selected_friend_ids)
        if self._friend_selected_count_label is not None:
            self._friend_selected_count_label.setText(f"{count} selected")
        if self._friend_clear_selection_button is not None:
            self._friend_clear_selection_button.setEnabled(count > 0)

    def _filtered_friends(self):
        query = self._friend_query.strip().casefold()
        if not query:
            return list(self.friends)
        return [
            friend for friend in self.friends
            if query in f"{friend.name} {friend.steam_id} {friend.state}".casefold()
        ]

    def _select_visible_friends(self, steam_ids) -> None:
        for sid in steam_ids:
            self._selected_friend_ids.add(int(sid))
        self._render_friend_rows()

    def _clear_friend_selection(self) -> None:
        if not self._selected_friend_ids:
            return
        self._selected_friend_ids.clear()
        self._render_friend_rows()

    def _invite_selected_friends(self) -> None:
        selected = tuple(sorted(self._selected_friend_ids))
        if not selected:
            return
        sent, failed = self.session.invite_friends(selected)
        for sid in sent:
            self._selected_friend_ids.discard(int(sid))
        if sent:
            suffix = "friend" if len(sent) == 1 else "friends"
            self._show_notice(f"Sent Steam lobby invite to {len(sent)} {suffix}.")
        if not failed:
            self._selected_friend_ids.clear()
        self._render_friend_rows()

    @staticmethod
    def _peer_presentation_key(peer) -> tuple:
        return (
            int(peer.steam_id),
            str(peer.name),
            str(peer.status),
            peer.avatar_rgba,
            int(peer.avatar_width),
            int(peer.avatar_height),
            str(peer.network_state),
        )

    def _server_structure_key(self, snapshot: AppSnapshot) -> tuple:
        return (
            snapshot.mode,
            snapshot.status,
            int(snapshot.lobby_id),
            snapshot.lobby_name,
            snapshot.visibility,
            int(snapshot.max_members),
            int(snapshot.member_count),
            snapshot.host_name,
            snapshot.service_name,
            snapshot.mappings,
            tuple(self._peer_presentation_key(peer) for peer in snapshot.peers),
            tuple(self._peer_presentation_key(member) for member in snapshot.members),
            snapshot.join_code,
        )

    def _update_server_telemetry(self, snapshot: AppSnapshot) -> None:
        own_id = int(self.steam.steam.steam_id()) if self.steam.initialized else 0
        for member in snapshot.members:
            labels = self._member_metric_labels.get(int(member.steam_id))
            if not labels:
                continue
            ping_label, rate_label = labels
            ping = self._ping_label(member, own_id)
            ping_text = f"Ping {ping}"
            rate_text = f"↑ {self._rate_label(member.upload_bps)}   ↓ {self._rate_label(member.download_bps)}"
            if ping_label.text() != ping_text:
                ping_label.setText(ping_text)
            if rate_label.text() != rate_text:
                rate_label.setText(rate_text)

    @staticmethod
    def _ping_label(member, own_id: int) -> str:
        if int(member.steam_id) == int(own_id):
            return "Local"
        if member.ping_ms >= 0:
            return f"{member.ping_ms} ms"
        if member.network_state == "unresponsive":
            return "No response"
        if member.network_state == "connecting":
            return "Connecting…"
        return "Not measured"

    @staticmethod
    def _connection_summary(snapshot: AppSnapshot, own_id: int) -> str:
        remote = [member for member in snapshot.members if int(member.steam_id) != int(own_id)]
        if not remote:
            return "Ready · waiting for peers"
        states = {member.network_state for member in remote}
        if "unresponsive" in states:
            return "P2P degraded"
        if states <= {"connected", "local"}:
            return "P2P healthy"
        if "connected" in states:
            return "P2P partially ready"
        return "P2P connecting"

    @classmethod
    def _connection_chip_name(cls, snapshot: AppSnapshot, own_id: int) -> str:
        summary = cls._connection_summary(snapshot, own_id)
        if summary == "P2P healthy":
            return "HealthGood"
        if summary == "P2P degraded":
            return "HealthBad"
        if summary.startswith("Ready"):
            return "HealthLocal"
        return "HealthWaiting"

    def _render_friend_card_body(self) -> None:
        body = self._friend_card_body
        if body is None:
            return
        clear_layout(body)
        self._friend_rows_layout = None
        self._friend_selected_count_label = None
        self._friend_select_visible_button = None
        self._friend_clear_selection_button = None
        self.friend_search_input = None

        if self._friend_list_collapsed:
            return

        friend_editor = QLineEdit(self._friend_query)
        friend_editor.setObjectName("FriendSearch")
        friend_editor.setPlaceholderText("Search friends by name, Steam ID, or status")
        friend_editor.setClearButtonEnabled(True)
        friend_editor.textChanged.connect(self._friend_search_changed)
        self.friend_search_input = friend_editor
        body.addWidget(friend_editor)

        selection_row = QHBoxLayout()
        select_visible = QPushButton("Select visible")
        select_visible.setObjectName("Small")
        self._friend_select_visible_button = select_visible
        select_visible.clicked.connect(lambda: self._select_visible_friends(self._friend_visible_ids))
        selection_row.addWidget(select_visible)

        clear_selected = QPushButton("Clear selection")
        clear_selected.setObjectName("Small")
        clear_selected.clicked.connect(self._clear_friend_selection)
        self._friend_clear_selection_button = clear_selected
        selection_row.addWidget(clear_selected)
        selection_row.addStretch(1)

        selected_count = QLabel()
        selected_count.setObjectName("Subtle")
        self._friend_selected_count_label = selected_count
        selection_row.addWidget(selected_count)
        body.addLayout(selection_row)

        rows_widget = QWidget()
        rows_layout = QVBoxLayout(rows_widget)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(7)
        self._friend_rows_layout = rows_layout
        body.addWidget(rows_widget)
        self._render_friend_rows()

    def _render_friend_rows(self) -> None:
        rows_layout = self._friend_rows_layout
        if rows_layout is None or self._friend_list_collapsed:
            return

        focused = QApplication.focusWidget()
        search_had_focus = focused is self.friend_search_input
        cursor_pos = self.friend_search_input.cursorPosition() if search_had_focus and self.friend_search_input is not None else -1
        clear_layout(rows_layout)

        filtered = self._filtered_friends()
        self._friend_visible_ids = tuple(int(friend.steam_id) for friend in filtered if int(friend.steam_id) > 0)
        if self._friend_select_visible_button is not None:
            self._friend_select_visible_button.setEnabled(bool(self._friend_visible_ids))
        self._update_friend_selection_controls()

        category_rows = (("online", "Online"), ("away", "Away"), ("offline", "Offline"))
        shown = 0
        missing_avatar_ids: list[int] = []
        for category, title_text in category_rows:
            rows = [friend for friend in filtered if getattr(friend, "category", "offline") == category]
            category_header = QHBoxLayout()
            category_toggle = QToolButton()
            collapsed = self._friend_category_collapsed.get(category, False)
            category_toggle.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow)
            category_toggle.setToolTip(
                f"Expand {title_text.lower()} friends" if collapsed else f"Collapse {title_text.lower()} friends"
            )
            category_toggle.clicked.connect(
                lambda _=False, cat=category: self._set_friend_category_collapsed(
                    cat, not self._friend_category_collapsed.get(cat, False)
                )
            )
            category_header.addWidget(category_toggle)
            label = QLabel(title_text)
            label.setObjectName("Section")
            category_header.addWidget(label)
            category_header.addStretch(1)
            count = QLabel(str(len(rows)))
            count.setObjectName("CountChip")
            category_header.addWidget(count)
            rows_layout.addLayout(category_header)

            if collapsed:
                continue

            for friend in rows:
                shown += 1
                if not friend.avatar_rgba:
                    missing_avatar_ids.append(int(friend.steam_id))
                row = QFrame()
                row.setObjectName("CompactRow")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(10, 7, 10, 7)
                rl.setSpacing(9)

                selectable = int(friend.steam_id) > 0
                selected = int(friend.steam_id) in self._selected_friend_ids and selectable
                select_friend = QCheckBox()
                select_friend.setChecked(selected)
                select_friend.setEnabled(selectable)
                select_friend.setToolTip(f"Select {friend.name} for a group invite")
                select_friend.toggled.connect(
                    lambda checked, sid=int(friend.steam_id): self._friend_selection_changed(sid, checked)
                )
                rl.addWidget(select_friend)
                rl.addWidget(
                    self._avatar_label(
                        friend.steam_id, friend.name, friend.avatar_rgba, friend.avatar_width, friend.avatar_height, 36
                    )
                )
                names = QVBoxLayout()
                names.setSpacing(0)
                name = plain_label(friend.name)
                name.setObjectName("Status")
                names.addWidget(name)
                steam_status = plain_label(f"{friend.state} · {friend.steam_id}")
                steam_status.setObjectName("Subtle")
                names.addWidget(steam_status)
                rl.addLayout(names, 1)
                invite = QPushButton("Invite")
                invite.setObjectName("SmallPrimary")
                invite.setEnabled(selectable)
                invite.setToolTip(
                    "Send a Steam lobby invite"
                    if int(friend.state_num) != 0
                    else "Send a Steam lobby invite to this offline friend"
                )
                invite.clicked.connect(lambda _=False, sid=int(friend.steam_id): self.session.invite_friend(sid))
                rl.addWidget(invite)
                rows_layout.addWidget(row)

        if not filtered:
            empty_friends = QLabel(
                "No Steam friends match this search." if self.friends else "Your Steam friend list is empty or still loading."
            )
            empty_friends.setObjectName("Muted")
            empty_friends.setWordWrap(True)
            rows_layout.addWidget(empty_friends)
        elif shown == 0:
            collapsed_note = QLabel("All matching friend categories are collapsed.")
            collapsed_note.setObjectName("Muted")
            collapsed_note.setWordWrap(True)
            rows_layout.addWidget(collapsed_note)

        if missing_avatar_ids:
            self.steam.hydrate_friend_avatars(tuple(missing_avatar_ids))

        if search_had_focus and self.friend_search_input is not None:
            self.friend_search_input.setFocus()
            self.friend_search_input.setCursorPosition(max(0, min(cursor_pos, len(self.friend_search_input.text()))))

    def _render_chat_messages(self) -> None:
        layout = self._chat_messages_layout
        scroll = self._chat_scroll_area
        if layout is None or scroll is None:
            return
        bar = scroll.verticalScrollBar()
        old_max = int(bar.maximum())
        old_value = int(bar.value())
        distance_from_bottom = max(0, old_max - old_value)
        follow_latest = self._chat_following
        messages = tuple(self.session.chat_messages[-200:])
        keys = tuple(
            (int(message.sender_id), float(message.created_at), str(message.text))
            for message in messages
        )
        previous = self._chat_rendered_keys
        incremental = bool(previous) and len(keys) >= len(previous) and keys[:len(previous)] == previous
        start = len(previous) if incremental else 0
        if not incremental:
            clear_layout(layout)
        new_count = max(0, len(keys) - start)
        if messages:
            own_id = int(self.steam.steam.steam_id()) if self.steam.initialized else 0
            for message in messages[start:]:
                layout.addWidget(self._chat_message_widget(message, own_id))
        elif not incremental:
            empty_chat = QLabel("No messages yet. Say hello when another player joins.")
            empty_chat.setObjectName("ChatEmpty")
            empty_chat.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty_chat)

        if new_count and not follow_latest:
            self._chat_unread += new_count
        self._chat_rendered_keys = keys
        self._update_chat_latest_button()

        def restore_chat_scroll() -> None:
            self._restoring_chat_scroll = True
            try:
                if follow_latest:
                    bar.setValue(bar.maximum())
                    self._chat_unread = 0
                    self._chat_following = True
                elif incremental:
                    # New messages were added below the viewport. Keep the
                    # exact history position instead of pulling the reader.
                    bar.setValue(min(old_value, bar.maximum()))
                else:
                    # The bounded 200-message history rolled over and widgets
                    # were rebuilt. Preserve the reader's distance from latest.
                    bar.setValue(max(0, bar.maximum() - distance_from_bottom))
            finally:
                self._restoring_chat_scroll = False
            self._update_chat_latest_button()

        QTimer.singleShot(0, restore_chat_scroll)

    def _chat_message_widget(self, message: ChatMessage, own_id: int) -> QFrame:
        bubble = QFrame()
        own = int(message.sender_id) == int(own_id)
        bubble.setObjectName("ChatBubbleOwn" if own else "ChatBubble")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(11, 8, 11, 8)
        bl.setSpacing(3)
        stamp = datetime.fromtimestamp(message.created_at).strftime("%H:%M")
        sender = "You" if own else message.sender_name
        who = plain_label(f"{sender}  ·  {stamp}")
        who.setObjectName("ChatMeta")
        bl.addWidget(who)
        body = plain_label(message.text)
        body.setObjectName("ChatBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(body)
        return bubble

    def _chat_scroll_changed(self, value: int) -> None:
        if self._restoring_chat_scroll or self._chat_scroll_area is None:
            return
        bar = self._chat_scroll_area.verticalScrollBar()
        following = int(value) >= max(0, int(bar.maximum()) - 24)
        self._chat_following = following
        if following:
            self._chat_unread = 0
        self._update_chat_latest_button()

    def _jump_to_latest_chat(self) -> None:
        if self._chat_scroll_area is None:
            return
        self._chat_following = True
        self._chat_unread = 0
        bar = self._chat_scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._update_chat_latest_button()

    def _update_chat_latest_button(self) -> None:
        button = self._chat_latest_button
        if button is None:
            return
        button.setText(f"{self._chat_unread} new · Latest" if self._chat_unread else "Latest")
        button.setEnabled(not self._chat_following or self._chat_unread > 0)

    @staticmethod
    def _rate_label(bytes_per_second: float) -> str:
        value = max(0.0, float(bytes_per_second or 0.0))
        if value <= 0:
            return "—"
        if value < 1024:
            return f"{value:.0f} B/s"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KiB/s"
        return f"{value / (1024 * 1024):.2f} MiB/s"

    def _avatar_label(
        self,
        steam_id: int,
        name: str,
        rgba: bytes = b"",
        width: int = 0,
        height: int = 0,
        size: int = 32,
    ) -> QLabel:
        label = QLabel((str(name)[:1] or "?").upper())
        label.setObjectName("MiniAvatar")
        label.setFixedSize(size, size)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        data = bytes(rgba or b"")
        w, h = int(width or 0), int(height or 0)
        if data and w > 0 and h > 0 and len(data) == w * h * 4:
            key = (int(steam_id), w, h, hash(data))
            pixmap = self._avatar_pixmap_cache.get(key)
            if pixmap is None:
                image = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image).scaled(
                        size,
                        size,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self._avatar_pixmap_cache[key] = pixmap
            if pixmap is not None and not pixmap.isNull():
                label.setText("")
                label.setPixmap(pixmap)
        return label

    def _update_message_parent(self):
        """Return the best live window to own updater message boxes."""
        app = QApplication.instance()
        if app is not None:
            active_modal = app.activeModalWidget()
            if active_modal is not None:
                return active_modal
            active_window = app.activeWindow()
            if active_window is not None:
                return active_window
        return self

    def _show_update_check_notice(self, parent=None) -> None:
        """Show a visible, non-blocking popup while a manual check runs."""
        self._close_update_check_notice()
        box = QMessageBox(parent or self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("SteamyLAN updates")
        box.setText("Checking GitHub for updates…")
        box.setInformativeText("SteamyLAN will show another popup when the check finishes.")
        box.setStandardButtons(QMessageBox.StandardButton.NoButton)
        box.setModal(False)
        box.show()
        box.raise_()
        box.activateWindow()
        self._update_check_notice = box

    def _close_update_check_notice(self) -> None:
        box = self._update_check_notice
        self._update_check_notice = None
        if box is None:
            return
        try:
            box.close()
            box.deleteLater()
        except RuntimeError:

            pass

    def _show_update_download_notice(self, version: str) -> None:
        self._close_update_download_notice()
        box = QMessageBox(self._update_message_parent())
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Updating SteamyLAN")
        box.setText(f"Downloading SteamyLAN {version}…")
        box.setInformativeText("The update will install automatically and SteamyLAN will restart when it is ready.")
        box.setStandardButtons(QMessageBox.StandardButton.NoButton)
        box.setModal(False)
        box.layout().addWidget(loading_indicator("Preparing the update…"), 1, 0, 1, 1)
        box.show()
        box.raise_()
        box.activateWindow()
        self._update_download_notice = box

    def _close_update_download_notice(self) -> None:
        box = self._update_download_notice
        self._update_download_notice = None
        if box is None:
            return
        try:
            box.close()
            box.deleteLater()
        except RuntimeError:
            pass

    def _begin_update_download(self, info: UpdateInfo) -> None:
        if self._update_install_running:
            return
        self._update_install_running = True
        self._downloading_update_info = info
        self._show_update_download_notice(info.latest_version)
        worker = FunctionWorker(download_update, info, update_cache_directory())
        worker.signals.result.connect(self._update_download_result)
        worker.signals.error.connect(self._update_download_error)
        worker.signals.finished.connect(self._update_download_finished)
        self._update_download_worker = worker
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _update_download_result(self, archive: object) -> None:
        self._close_update_download_notice()
        info = self._downloading_update_info
        if self.session.snapshot.mode != "idle":
            self._pending_update_archive = Path(str(archive))
            self._pending_update_info = info
            self._update_install_running = False
            self._show_notice("An update is ready and will install after the current lobby session ends.")
            return
        self._launch_downloaded_update(Path(str(archive)))

    def _launch_downloaded_update(self, archive: Path) -> None:
        try:
            launch_update_helper(archive, os.getpid())
        except Exception as exc:
            Path(str(archive)).unlink(missing_ok=True)
            self._update_install_running = False
            QMessageBox.warning(
                self._update_message_parent(),
                "SteamyLAN update failed",
                f"The update was downloaded but could not be installed.\n\n{exc}",
            )
            return
        self._really_quit = True
        self.session.stop()
        QApplication.quit()

    def _install_pending_update(self) -> None:
        archive = self._pending_update_archive
        if self._update_install_running:
            return
        if archive is None and self._pending_update_info is not None:
            info = self._pending_update_info
            self._pending_update_info = None
            self._begin_update_download(info)
            return
        if archive is None:
            return
        if not archive.is_file():
            self._pending_update_archive = None
            self._pending_update_info = None
            return
        self._pending_update_archive = None
        self._pending_update_info = None
        self._update_install_running = True
        self._launch_downloaded_update(archive)

    @Slot(str)
    def _update_download_error(self, text: str) -> None:
        self._close_update_download_notice()
        self._update_install_running = False
        self._downloading_update_info = None
        self._update_error(text, automatic=False)

    @Slot()
    def _update_download_finished(self) -> None:
        self._update_download_worker = None

    def _check_for_updates(self, parent=None, automatic: bool = False) -> None:



        if automatic:
            if self._automatic_update_check_attempted:
                return
            self._automatic_update_check_attempted = True

        if self._update_check_running:
            if not automatic:


                self._show_current_update_result = True
                QMessageBox.information(
                    parent or self._update_message_parent(),
                    "SteamyLAN updates",
                    "An update check is already in progress. SteamyLAN will show the result when it finishes.",
                )
            return

        self._update_check_running = True
        self._show_current_update_result = not automatic
        self._update_check_was_automatic = bool(automatic)

        if not automatic:
            self._show_update_check_notice(parent or self._update_message_parent())

        worker = FunctionWorker(check_for_update)



        worker.signals.result.connect(self._update_worker_result)
        worker.signals.error.connect(self._update_worker_error)
        worker.signals.finished.connect(self._update_check_finished)
        self._update_worker = worker
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _update_worker_result(self, info: object) -> None:
        self._close_update_check_notice()
        self._update_result(info, automatic=self._update_check_was_automatic)

    @Slot(str)
    def _update_worker_error(self, text: str) -> None:
        self._close_update_check_notice()
        self._update_error(text, automatic=self._update_check_was_automatic)

    @Slot()
    def _update_check_finished(self) -> None:
        self._close_update_check_notice()
        self._update_check_running = False
        self._show_current_update_result = False
        self._update_check_was_automatic = False
        self._update_worker = None

    def _update_result(self, info: object, automatic: bool = False) -> None:
        target = self._update_message_parent()
        show_result = (not automatic) or self._show_current_update_result

        if not isinstance(info, UpdateInfo):
            if show_result:
                QMessageBox.warning(
                    target,
                    "SteamyLAN updates",
                    "GitHub returned an unexpected update response. No update was installed.",
                )
            return

        if info.newer:
            notes = info.release_notes.strip()
            if automatic and self.prefs.prefs.update_mode == "notify":
                summary = f"SteamyLAN {info.latest_version} is available."
                if notes:
                    summary += f"\n\n{notes[:800]}"
                self._show_notice(summary)
                return
            if info.installable:
                if not getattr(sys, "frozen", False):
                    if show_result:
                        QMessageBox.information(
                            target,
                            "SteamyLAN update available",
                            "A packaged Windows build is available, but source checkouts must be updated manually.",
                        )
                    return
                if self.session.snapshot.mode != "idle":
                    self._pending_update_info = info
                    self._show_notice(
                        f"SteamyLAN {info.latest_version} is available. It will install automatically after this lobby session ends."
                    )
                    return
                self._begin_update_download(info)
                return
            answer = QMessageBox.question(
                target,
                "SteamyLAN update available",
                (
                    f"SteamyLAN {info.latest_version} is available, but this release does not contain "
                    "a complete automatic-update package. Open the download page?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(info.release_url))
            return



        if not show_result:
            return

        if info.same_version:
            QMessageBox.information(
                target,
                "SteamyLAN is up to date",
                (
                    "You already have the latest SteamyLAN release.\n\n"
                    f"Installed: {info.current_version}\n"
                    f"Latest on GitHub: {info.latest_version}"
                ),
            )
            return

        QMessageBox.information(
            target,
            "SteamyLAN updates",
            (
                "Your installed SteamyLAN version is newer than the latest published GitHub release.\n\n"
                f"Installed: {info.current_version}\n"
                f"Latest on GitHub: {info.latest_version}"
            ),
        )

    def _update_error(self, text: str, automatic: bool = False) -> None:
        self.log.debug("Update check failed\n%s", text)
        show_result = (not automatic) or self._show_current_update_result
        if not show_result:
            return

        detail = ""
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if lines:
            detail = lines[-1]
        message = "SteamyLAN could not check GitHub for updates."
        if detail:
            message += f"\n\nDetails: {detail}"
        message += "\n\nCheck your internet connection and try again."
        QMessageBox.warning(self._update_message_parent(), "SteamyLAN update check failed", message)

    def _remap_client_service(self, service_id: str, mapping) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Change local port")
        dialog.setMinimumWidth(360)
        layout = QVBoxLayout(dialog)
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        title = QLabel(f"{mapping.protocol.lower()}/{mapping.remote_port} {mapping.name}")
        title.setObjectName("Muted")
        title.setWordWrap(True)
        form.addWidget(title, 0, 0, 1, 2)
        port = QLineEdit(str(mapping.local_port))
        port.setPlaceholderText("local port (blank = automatic)")
        port.setMaxLength(5)
        form.addWidget(QLabel("Local port"), 1, 0)
        form.addWidget(port, 1, 1)
        bind = QComboBox()
        bind_options = (
            ("Default", self.prefs.prefs.bind_address),
            ("Localhost only", "127.0.0.1"),
            ("All IPv4 interfaces", "0.0.0.0"),
            ("All IPv6 interfaces", "::"),
        )
        for label, value in bind_options:
            bind.addItem(label, value)
        current_index = bind.findData(mapping.bind_host)
        bind.setCurrentIndex(max(0, current_index))
        form.addWidget(QLabel("Bind to"), 2, 0)
        form.addWidget(bind, 2, 1)
        layout.addLayout(form)
        hint = QLabel("Leave the port blank to use the next available local port. The Steam connection stays active.")
        hint.setObjectName("Subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text = port.text().strip()
        try:
            local_port = int(text) if text else 0
        except ValueError:
            QMessageBox.warning(self, APP_NAME, "Enter a valid local port number, or leave it blank.")
            return
        self.session.remap_client_service(service_id, local_port, str(bind.currentData()))

    def _accept_all_remote_services(self) -> None:
        for spec in self.session.remote_services:
            if spec.service_id not in {item.service_id for item in self.session.snapshot.mappings}:
                self.session.accept_client_service(spec.service_id)

    def _revoke_all_remote_services(self) -> None:
        for mapping in tuple(self.session.snapshot.mappings):
            self.session.revoke_client_service(mapping.service_id)

    def _render_server(self) -> None:
        self._server_dirty = False
        self._member_metric_labels = {}
        self._friend_card_body = None
        self._friend_list_toggle = None
        self._friend_total_count_label = None
        self._friend_rows_layout = None
        self._friend_selected_count_label = None
        self._friend_select_visible_button = None
        self._friend_clear_selection_button = None
        self._chat_messages_layout = None
        self._chat_scroll_area = None
        self._chat_editor = None
        self._chat_latest_button = None
        self._chat_state_label = None
        self._chat_send_button = None
        self._chat_rendered_keys = ()
        scroll_bar = self.server_page.verticalScrollBar()
        old_scroll = int(scroll_bar.value())
        old_max = int(scroll_bar.maximum())
        was_near_bottom = old_max > 0 and old_scroll >= max(0, old_max - 72)
        focused = QApplication.focusWidget()
        restore_chat_focus = bool(focused is not None and focused.objectName() == "ChatEditor")
        restore_friend_focus = bool(focused is not None and focused.objectName() == "FriendSearch")
        self.friend_search_input = None
        friend_editor = None
        clear_layout(self.server_content)
        snap = self.session.snapshot
        own_id = int(self.steam.steam.steam_id()) if self.steam.initialized else 0
        self._rendered_server_structure_key = self._server_structure_key(snap)

        title = QLabel("Server")
        title.setObjectName("Title")
        self.server_content.addWidget(title)
        if snap.mode == "idle":
            empty = self._card()
            lay = empty.layout()
            h = QLabel("No active lobby")
            h.setObjectName("Heading")
            lay.addWidget(h)
            t = QLabel("Join a lobby from the browser or create a server on this computer.")
            t.setObjectName("Muted")
            t.setWordWrap(True)
            lay.addWidget(t)
            browse = QPushButton("Browse Lobbies")
            browse.setObjectName("Primary")
            browse.clicked.connect(lambda: self._switch(0))
            lay.addWidget(browse, 0, Qt.AlignmentFlag.AlignLeft)
            self.server_content.addWidget(empty)
            return

        if snap.mode == "starting":
            card = self._card()
            lay = card.layout()
            h = QLabel(snap.lobby_name or "Starting lobby…")
            h.setObjectName("Heading")
            lay.addWidget(h)
            lay.addWidget(loading_indicator("Creating the Steam lobby…"))
            st = QLabel(snap.status or "Creating the Steam lobby…")
            st.setObjectName("Muted")
            lay.addWidget(st)
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.session.stop)
            lay.addWidget(cancel, 0, Qt.AlignmentFlag.AlignLeft)
            self.server_content.addWidget(card)
            return

        config = self.session.session_config
        if config is None:
            card = self._card()
            lay = card.layout()
            h = QLabel(snap.status or "Connecting…")
            h.setObjectName("Heading")
            lay.addWidget(h)
            lay.addWidget(loading_indicator("Connecting to the lobby…"))
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.session.stop)
            lay.addWidget(cancel, 0, Qt.AlignmentFlag.AlignLeft)
            self.server_content.addWidget(card)
            return

        hero = QFrame()
        hero.setObjectName("Hero")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(18, 15, 18, 15)
        hl.setSpacing(6)
        top = QHBoxLayout()
        names = QVBoxLayout()
        names.setSpacing(2)
        lobby_name = plain_label(config.lobby_name)
        lobby_name.setObjectName("Heading")
        lobby_name.setWordWrap(True)
        names.addWidget(lobby_name)
        status = plain_label(snap.status)
        status.setObjectName("Muted")
        status.setWordWrap(True)
        names.addWidget(status)
        top.addLayout(names, 1)
        badge = QLabel(self._visibility_label(config.visibility))
        badge.setObjectName("CountChip")
        top.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        if config.password_salt:
            password_badge = QLabel("Password")
            password_badge.setObjectName("SecureChip")
            top.addWidget(password_badge, 0, Qt.AlignmentFlag.AlignTop)
        users = QLabel(f"{snap.member_count or 1}/{config.max_members} users")
        users.setObjectName("CountChip")
        top.addWidget(users, 0, Qt.AlignmentFlag.AlignTop)
        hl.addLayout(top)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        if snap.join_code:
            copy_code = QPushButton("Copy Share Code")
            copy_code.setObjectName("Primary")
            copy_code.clicked.connect(lambda _=False, value=snap.join_code, button=copy_code: self._copy(value, button))
            actions.addWidget(copy_code)
        if snap.mode == "connected" and snap.mappings:
            copy_details = QPushButton("Copy connection details")
            copy_details.setObjectName("Primary")
            copy_details.clicked.connect(self._copy_connection_details)
            actions.addWidget(copy_details)
        if snap.mode == "sharing":
            settings_button = QPushButton("Server Settings")
            settings_button.clicked.connect(self._server_settings)
            actions.addWidget(settings_button)
        copy_id = QPushButton("Copy Lobby ID")
        copy_id.clicked.connect(lambda _=False, value=str(snap.lobby_id), button=copy_id: self._copy(value, button))
        actions.addWidget(copy_id)
        actions.addStretch(1)
        stop = QPushButton("Stop Server" if snap.mode == "sharing" else "Disconnect")
        stop.setObjectName("Danger")
        stop.clicked.connect(self._confirm_stop_sharing if snap.mode == "sharing" else self.session.stop)
        actions.addWidget(stop)
        hl.addLayout(actions)
        self.server_content.addWidget(hero)

        details = QFrame()
        details.setObjectName("Card")
        dl = QVBoxLayout(details)
        dl.setContentsMargins(16, 13, 16, 13)
        dl.setSpacing(7)
        details_header = QHBoxLayout()
        details_toggle = QToolButton()
        details_toggle.setArrowType(
            Qt.ArrowType.RightArrow if self._server_details_collapsed else Qt.ArrowType.DownArrow
        )
        details_toggle.setToolTip(
            "Expand server details" if self._server_details_collapsed else "Collapse server details"
        )
        details_header.addWidget(details_toggle)
        dh = QLabel("Server details")
        dh.setObjectName("Heading")
        details_header.addWidget(dh)
        details_header.addStretch(1)
        dl.addLayout(details_header)
        details_body = QWidget()
        grid = QGridLayout(details_body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        detail_rows = [
            ("Host", config.host_name),
            ("Role", "Host" if snap.mode == "sharing" else "Member"),
            ("Visibility", self._visibility_label(config.visibility)),
            ("Capacity", f"{snap.member_count or len(snap.members) or 1} of {config.max_members} users"),
            ("Password", "Required" if config.password_salt else "Not required"),
            ("Steam lobby", str(snap.lobby_id)),
            ("Session", config.session_id),
            *(([("Share code", snap.join_code)] if snap.join_code else [])),
            ("P2P health", self._connection_summary(snap, own_id)),
            ("Secure chat", "X25519 + ChaCha20-Poly1305 over Steam Networking Messages"),
        ]
        for row, (label_text, value_text) in enumerate(detail_rows):
            label = QLabel(label_text)
            label.setObjectName("Subtle")
            value = plain_label(value_text)
            value.setObjectName("Status")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        details_body.setVisible(not self._server_details_collapsed)
        dl.addWidget(details_body)

        def toggle_server_details() -> None:
            self._server_details_collapsed = not self._server_details_collapsed
            details_body.setVisible(not self._server_details_collapsed)
            details_toggle.setArrowType(
                Qt.ArrowType.RightArrow if self._server_details_collapsed else Qt.ArrowType.DownArrow
            )
            details_toggle.setToolTip(
                "Expand server details" if self._server_details_collapsed else "Collapse server details"
            )

        details_toggle.clicked.connect(toggle_server_details)
        self.server_content.addWidget(details)

        ports = QFrame()
        ports.setObjectName("Card")
        pl = QVBoxLayout(ports)
        pl.setContentsMargins(16, 13, 16, 13)
        pl.setSpacing(7)
        ports_header = QHBoxLayout()
        ph = QLabel("Shared connections")
        ph.setObjectName("Heading")
        ports_header.addWidget(ph)
        ports_header.addStretch(1)
        if snap.mode == "connected":
            accept_all = QPushButton("Open every port locally")
            accept_all.setObjectName("SmallPrimary")
            accept_all.setToolTip("Start a local listener for every port shared by the host")
            accept_all.clicked.connect(self._accept_all_remote_services)
            ports_header.addWidget(accept_all)
            close_all = QPushButton("Close local ports")
            close_all.setObjectName("Small")
            close_all.setToolTip("Stop all local listeners without leaving the lobby")
            close_all.clicked.connect(self._revoke_all_remote_services)
            ports_header.addWidget(close_all)
        pl.addLayout(ports_header)
        mappings = {m.service_id: m for m in snap.mappings}
        for spec in config.services:
            row = QFrame()
            row.setObjectName("CompactRow")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 7, 10, 7)
            rl.setSpacing(8)
            name = plain_label(spec.name)
            name.setObjectName("Status")
            name.setWordWrap(True)
            rl.addWidget(name, 1)
            endpoint = QLabel(f"{spec.protocol} {spec.port}")
            endpoint.setObjectName("PortChip")
            rl.addWidget(endpoint)
            if snap.mode == "connected":
                mapping = mappings.get(spec.service_id)
                if mapping:
                    local = QLabel(mapping.address)
                    local.setObjectName("Muted")
                    local.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    if mapping.bind_host in {"0.0.0.0", "::", "::0"}:
                        bound = (
                            f"[{mapping.bind_host}]:{mapping.local_port}"
                            if ":" in mapping.bind_host
                            else f"{mapping.bind_host}:{mapping.local_port}"
                        )
                        local.setToolTip(f"Tunnel listens on {bound}; this is the local address to paste into the game.")
                    rl.addWidget(local)
                    copy_ip = QPushButton("Copy address")
                    copy_ip.setObjectName("SmallPrimary")
                    copy_ip.clicked.connect(
                        lambda _=False, value=mapping.address, button=copy_ip: self._copy(value, button)
                    )
                    rl.addWidget(copy_ip)
                    remap = QPushButton("Change local port")
                    remap.setObjectName("Small")
                    remap.clicked.connect(
                        lambda _=False, sid=spec.service_id, current=mapping: self._remap_client_service(sid, current)
                    )
                    rl.addWidget(remap)
                    revoke = QPushButton("Close")
                    revoke.setObjectName("Small")
                    revoke.clicked.connect(lambda _=False, sid=spec.service_id: self.session.revoke_client_service(sid))
                    rl.addWidget(revoke)
                else:
                    accept = QPushButton("Open locally")
                    accept.setObjectName("SmallPrimary")
                    accept.clicked.connect(lambda _=False, sid=spec.service_id: self.session.accept_client_service(sid))
                    rl.addWidget(accept)
            pl.addWidget(row)
        if snap.mode == "connected" and snap.mappings:
            address_hint = QLabel("Copy the local address beside the port your game expects, then paste it into the game's direct-connect field.")
            address_hint.setObjectName("Subtle")
            address_hint.setWordWrap(True)
            pl.addWidget(address_hint)

        self.server_content.addWidget(ports)

        members_card = QFrame()
        members_card.setObjectName("Card")
        ml = QVBoxLayout(members_card)
        ml.setContentsMargins(16, 13, 16, 13)
        ml.setSpacing(7)
        member_header = QHBoxLayout()
        mh = QLabel("Members")
        mh.setObjectName("Heading")
        member_header.addWidget(mh)
        member_header.addStretch(1)
        health = QLabel(self._connection_summary(snap, own_id))
        health.setObjectName(self._connection_chip_name(snap, own_id))
        member_header.addWidget(health)
        member_header.addWidget(QLabel(f"{snap.member_count or len(snap.members)}/{config.max_members}"))
        ml.addLayout(member_header)

        peer_map = {p.steam_id: p for p in snap.peers}
        pending_statuses = {"Waiting for approval", "Awaiting approval"}
        pending_ids = {p.steam_id for p in snap.peers if p.status in pending_statuses}
        own_id = int(self.steam.steam.steam_id()) if self.steam.initialized else 0
        for member in snap.members:
            state = member
            row = QFrame()
            row.setObjectName("CompactRow")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 7, 10, 7)
            rl.setSpacing(9)
            avatar_source = state if state.avatar_rgba else member
            rl.addWidget(
                self._avatar_label(
                    member.steam_id,
                    member.name,
                    avatar_source.avatar_rgba,
                    avatar_source.avatar_width,
                    avatar_source.avatar_height,
                    32,
                )
            )

            names = QVBoxLayout()
            names.setSpacing(0)
            nm = plain_label(member.name)
            nm.setObjectName("Status")
            names.addWidget(nm)
            st = plain_label(state.status)
            st.setObjectName("Subtle")
            names.addWidget(st)
            steam_id = QLabel(f"Steam ID {member.steam_id}")
            steam_id.setObjectName("PeerDetail")
            steam_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            names.addWidget(steam_id)
            rl.addLayout(names, 1)

            state_text = {
                "connected": "P2P online",
                "connecting": "P2P connecting",
                "unresponsive": "P2P no response",
                "local": "This computer",
            }.get(state.network_state, "P2P not measured")
            state_chip = QLabel(state_text)
            state_chip.setObjectName({
                "connected": "HealthGood",
                "local": "HealthLocal",
                "unresponsive": "HealthBad",
            }.get(state.network_state, "HealthWaiting"))
            rl.addWidget(state_chip)

            metrics = QVBoxLayout()
            metrics.setSpacing(0)
            ping = self._ping_label(state, own_id)
            ping_label = QLabel(f"Ping {ping}")
            ping_label.setObjectName("Subtle")
            ping_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            ping_label.setToolTip("Round-trip latency measured through the active Steam P2P session")
            metrics.addWidget(ping_label)
            rate_label = QLabel(
                f"↑ {self._rate_label(state.upload_bps)}   ↓ {self._rate_label(state.download_bps)}"
            )
            rate_label.setObjectName("Subtle")
            rate_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            rate_label.setToolTip("Upload and download rates reported by Steam for this peer connection")
            metrics.addWidget(rate_label)
            rl.addLayout(metrics)
            self._member_metric_labels[int(member.steam_id)] = (ping_label, rate_label)

            if snap.mode == "sharing" and member.steam_id != config.host_id:
                if state.status in pending_statuses or member.steam_id in pending_ids:
                    allow = QPushButton("Allow")
                    allow.setObjectName("SmallPrimary")
                    allow.clicked.connect(lambda _=False, sid=member.steam_id: self.session.allow_peer(sid, True))
                    deny = QPushButton("Deny")
                    deny.setObjectName("Small")
                    deny.clicked.connect(lambda _=False, sid=member.steam_id: self.session.deny_peer(sid))
                    rl.addWidget(allow)
                    rl.addWidget(deny)
                elif state.status != "Kicked":
                    kick = QPushButton("Kick")
                    kick.setObjectName("Small")
                    kick.clicked.connect(
                        lambda _=False, sid=member.steam_id, name=member.name: self._confirm_kick(sid, name)
                    )
                    rl.addWidget(kick)
            ml.addWidget(row)
        self.server_content.addWidget(members_card)

        if snap.mode == "sharing":
            friends_card = QFrame()
            friends_card.setObjectName("Card")
            fl = QVBoxLayout(friends_card)
            fl.setContentsMargins(16, 13, 16, 13)
            fl.setSpacing(7)

            friend_header = QHBoxLayout()
            list_toggle = QToolButton()
            list_toggle.setArrowType(
                Qt.ArrowType.RightArrow if self._friend_list_collapsed else Qt.ArrowType.DownArrow
            )
            list_toggle.setToolTip(
                "Expand friend list" if self._friend_list_collapsed else "Collapse friend list"
            )
            list_toggle.clicked.connect(
                lambda _=False: self._set_friend_list_collapsed(not self._friend_list_collapsed)
            )
            self._friend_list_toggle = list_toggle
            friend_header.addWidget(list_toggle)
            fh = QLabel("Invite Steam friends")
            fh.setObjectName("Heading")
            friend_header.addWidget(fh)
            friend_header.addStretch(1)
            friend_count = QLabel(f"{len(self.friends)} friends")
            self._friend_total_count_label = friend_count
            friend_header.addWidget(friend_count)
            invite_selected = QPushButton()
            invite_selected.setObjectName("SmallPrimary")
            invite_selected.clicked.connect(self._invite_selected_friends)
            self._friend_invite_button = invite_selected
            friend_header.addWidget(invite_selected)
            fl.addLayout(friend_header)
            self._update_friend_invite_button()

            body_widget = QWidget()
            body_layout = QVBoxLayout(body_widget)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(7)
            self._friend_card_body = body_layout
            fl.addWidget(body_widget)
            self._render_friend_card_body()
            friend_editor = self.friend_search_input
            self.server_content.addWidget(friends_card)

        chat = QFrame()
        chat.setObjectName("Card")
        cl = QVBoxLayout(chat)
        cl.setContentsMargins(16, 13, 16, 13)
        cl.setSpacing(7)
        chat_header = QHBoxLayout()
        ch = QLabel("Encrypted chat")
        ch.setObjectName("Heading")
        chat_header.addWidget(ch)
        chat_header.addStretch(1)
        latest = QPushButton("Latest")
        latest.setObjectName("Small")
        latest.setToolTip("Jump to the newest chat message")
        latest.clicked.connect(self._jump_to_latest_chat)
        self._chat_latest_button = latest
        chat_header.addWidget(latest)
        lock = QLabel("Encrypted and ready" if self.session.chat_ready else "Securing chat…")
        lock.setObjectName("SecureChip" if self.session.chat_ready else "WaitingChip")
        self._chat_state_label = lock
        chat_header.addWidget(lock)
        cl.addLayout(chat_header)
        chat_note = QLabel("Messages use pairwise encryption over Steam networking. The host relays group messages and can therefore read lobby chat.")
        chat_note.setObjectName("Subtle")
        chat_note.setWordWrap(True)
        cl.addWidget(chat_note)

        message_holder = QWidget()
        message_layout = QVBoxLayout(message_holder)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.setSpacing(7)
        message_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_messages_layout = message_layout

        message_scroll = QScrollArea()
        message_scroll.setObjectName("ChatMessagesScroll")
        message_scroll.setWidgetResizable(True)
        message_scroll.setFrameShape(QFrame.Shape.NoFrame)
        message_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        message_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        message_scroll.setMinimumHeight(180)
        message_scroll.setMaximumHeight(280)
        message_scroll.setWidget(message_holder)
        self._chat_scroll_area = message_scroll
        message_scroll.verticalScrollBar().valueChanged.connect(self._chat_scroll_changed)
        cl.addWidget(message_scroll)
        self._render_chat_messages()

        compose = QHBoxLayout()
        editor = QLineEdit(self._chat_draft)
        editor.setObjectName("ChatEditor")
        editor.setPlaceholderText("Message the lobby")
        editor.setMaxLength(1800)
        editor.textChanged.connect(lambda text: setattr(self, "_chat_draft", text))
        self._chat_editor = editor
        compose.addWidget(editor, 1)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        self._chat_send_button = send
        self._chat_state_changed(self.session.chat_ready)

        def submit_message() -> None:
            text = editor.text().strip()
            if not text:
                return
            if self.session.send_chat(text):
                self._chat_draft = ""
                editor.clear()
                self._chat_following = True
                QTimer.singleShot(0, self._jump_to_latest_chat)
            else:
                editor.setFocus()
                editor.selectAll()

        send.clicked.connect(submit_message)
        editor.returnPressed.connect(submit_message)
        compose.addWidget(send)
        cl.addLayout(compose)
        self.server_content.addWidget(chat)




        def restore_view() -> None:
            bar = self.server_page.verticalScrollBar()
            bar.setValue(bar.maximum() if was_near_bottom else min(old_scroll, bar.maximum()))
            if restore_chat_focus:
                editor.setFocus()
                editor.setCursorPosition(len(editor.text()))
            elif restore_friend_focus and friend_editor is not None:
                friend_editor.setFocus()
                friend_editor.setCursorPosition(len(friend_editor.text()))

        QTimer.singleShot(0, restore_view)

    @staticmethod
    def _visibility_label(value: str) -> str:
        return {
            VISIBILITY_PUBLIC: "Public",
            VISIBILITY_FRIENDS: "Friends only",
            VISIBILITY_INVITE: "Invite only",
        }.get(value, value.title() if value else "Unknown")

    def _card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(7)
        return card

    def _server_settings(self) -> None:
        if self.session.snapshot.mode != "sharing":
            return
        try:
            dlg = ServerSettingsDialog(self.session, self.services, self)
        except RuntimeError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._server_dirty = True
            self._share_dirty = True
            self._render_visible(force=True)

    def _manual_service(self) -> None:
        dlg = ManualServiceDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            service = dlg.service()
            self.session.share(
                service,
                lobby_name=self._create_lobby_name or service.name,
                visibility=self._create_visibility,
                max_members=self._create_max_members,
                password=self._create_password if self._create_password_enabled else "",
                static_code=self._create_static_code,
            )
            if self.session.snapshot.mode == "starting":
                self._create_password = ""

    def _join_host(self, host: SharingHost) -> None:
        password = ""
        if host.password_protected:
            password, accepted = QInputDialog.getText(
                self,
                "Lobby password",
                f"Enter the password for {host.lobby_name or 'this lobby'}:",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return
            if not password:
                QMessageBox.warning(self, APP_NAME, "Enter the lobby password.")
                return
        self.session.join(host, password=password)

    def _password_requested(self, lobby_name: str) -> None:
        password, accepted = QInputDialog.getText(
            self,
            "Lobby password",
            f"Enter the password for {lobby_name or 'this lobby'}:",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            self.session.stop()
            return
        self.session.provide_password(password)

    def _settings(self) -> None:
        active_app_id = int(self.steam.steam.app_id)
        dlg = SettingsDialog(
            self.prefs,
            update_check_callback=self._check_for_updates,
            steam_client=self.steam.steam,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                self.steam.steam.configure_network(
                    relay_mode=self.prefs.prefs.relay_mode,
                    relay_location=self.prefs.prefs.relay_location,
                    upload_limit_kbps=self.prefs.prefs.upload_limit_kbps,
                    download_limit_kbps=self.prefs.prefs.download_limit_kbps,
                )
            except Exception:
                self.log.exception("Could not apply networking settings")
            self.session.refresh_steam_status()
            self._sync_create_visibility()
            self._share_dirty = True
            self._join_dirty = True
            self._server_dirty = True
            self._sync_detection_timer(refresh_now=self._create_available())
            self._render_visible(force=True)
            new_app_id = self.prefs.effective_app_id()
            if new_app_id != active_app_id:
                QMessageBox.information(
                    self,
                    "Restart required",
                    f"Steam AppID will change from {active_app_id} to {new_app_id}. Restart SteamyLAN to apply it.",
                )

    def _approval_requested(self, steam_id, name: str) -> None:
        if self.prefs.prefs.notifications and self.tray.isVisible():
            self.tray.showMessage("SteamyLAN", f"{name} wants to connect to your server.", QSystemTrayIcon.MessageIcon.Information, 5000)
        self._switch(2)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_error(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("SteamyLAN")
        box.setText(str(text or "Something went wrong."))
        copy_button = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy_button:
            QApplication.clipboard().setText(str(text or ""))

    def _detection_failed(self, text: str) -> None:
        self._detection_loading = False
        self._show_error(text)

    def _show_notice(self, text: str) -> None:
        if self.prefs.prefs.notifications and self.tray.isVisible():
            self.tray.showMessage("SteamyLAN", text, QSystemTrayIcon.MessageIcon.Information, 3500)

    def _copy(self, text: str, button: QPushButton) -> None:
        QApplication.clipboard().setText(text)
        old = button.text()
        button.setText("Copied!")
        QTimer.singleShot(1200, lambda: button.setText(old))

    def _confirm_stop_sharing(self) -> None:
        count = len([p for p in self.session.snapshot.peers if p.status != "Waiting for approval"])
        if count:
            answer = QMessageBox.question(
                self,
                "Stop sharing?",
                f"{count} member{'s are' if count != 1 else ' is'} connected. Stopping will disconnect them.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.session.stop()

    def _confirm_kick(self, steam_id: int, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "Kick player?",
            f"Kick {name} from this lobby? They cannot reconnect until this hosted session ends.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session.kick_peer(steam_id)

    def _confirm_remove(self, steam_id: int, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "Remove access?",
            f"Remove {name}'s access? They can ask again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session.remove_access(steam_id)

    def _update_tray(self, snap: AppSnapshot) -> None:
        if snap.mode == "sharing":
            self.tray.setToolTip(f"SteamyLAN — Sharing {snap.service_name}")
        elif snap.mode == "connected":
            self.tray.setToolTip(f"SteamyLAN — Connected to {snap.host_name}")
        else:
            self.tray.setToolTip("SteamyLAN — Not connected")
        active = snap.mode != "idle"
        self.tray_server.setVisible(active)
        self.tray_server.setEnabled(active)
        self.tray_copy_code.setVisible(bool(snap.join_code))
        self.tray_copy_code.setEnabled(bool(snap.join_code))
        self.tray_stop.setEnabled(active)

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _active_connection_count(self) -> int:
        snap = self.session.snapshot
        if snap.mode == "sharing":
            return len([p for p in snap.peers if p.status != "Waiting for approval"])
        return 1 if snap.mode == "connected" else 0

    def _quit_from_tray(self) -> None:
        active = self._active_connection_count()
        if active:
            answer = QMessageBox.question(
                self,
                "Exit SteamyLAN?",
                f"{active} connection{'s' if active != 1 else ''} will be disconnected.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._really_quit = True
        self.session.stop()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_geometry()
        if not self._really_quit and self.prefs.prefs.keep_in_tray and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self._tray_notice_shown = True
                if self.prefs.prefs.notifications:
                    self.tray.showMessage("SteamyLAN", "SteamyLAN is still running here.", QSystemTrayIcon.MessageIcon.Information, 3000)
            return

        if not self._really_quit:
            active = self._active_connection_count()
            if active:
                answer = QMessageBox.question(
                    self,
                    "Exit SteamyLAN?",
                    f"{active} connection{'s' if active != 1 else ''} will be disconnected.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        self._really_quit = True
        self.session.stop()
        event.accept()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _save_geometry(self) -> None:
        try:
            self.prefs.prefs.window_geometry = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
            self.prefs.save()
        except Exception:
            pass

    def _restore_geometry(self) -> None:
        try:
            text = self.prefs.prefs.window_geometry
            if text:
                self.restoreGeometry(base64.b64decode(text))
        except Exception:
            pass

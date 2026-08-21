from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from SteamyLan.models import ChatMessage, DetectedService, Endpoint, SessionConfig, SharedServiceSpec
from SteamyLan.ui.main_window import MainWindow, ServerSettingsDialog


class FakeHostedSession:
    def __init__(self):
        self.session_config = SessionConfig(
            session_id="ui-test",
            host_id=1,
            host_name="Host",
            control_channel=30_100,
            chat_channel=40_100,
            services=(SharedServiceSpec("existing", "Current game", "TCP", 1234, 12_000),),
        )
        self.shared_service = DetectedService(
            key="current",
            name="Current game",
            process_name="current.exe",
            pid=10,
            endpoints=(Endpoint("TCP", 1234, "10.0.0.1"),),
        )
        self.static_share_code_enabled = False

    def reconfigure_server(self, *_args, **_kwargs):
        return True


class FakeChatSession:
    def __init__(self, messages):
        self.chat_messages = tuple(messages)


class FakeSteamService:
    initialized = False


class QtBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_server_port_actions_explain_keep_vs_replace_and_preserve_duplicates(self):
        session = FakeHostedSession()
        second = DetectedService(
            key="second",
            name="Second game",
            process_name="second.exe",
            pid=20,
            window_title="Second game server",
            endpoints=(
                Endpoint("TCP", 1234, "192.0.2.10"),
                Endpoint("UDP", 5678, "127.0.0.1"),
            ),
        )
        dialog = ServerSettingsDialog(session, [second])
        try:
            self.assertEqual(dialog.windowTitle(), "Edit Live Server")
            self.assertEqual(dialog.add_program_ports.text(), "Add to current port list")
            self.assertEqual(dialog.use_program_ports.text(), "Replace list with detected ports")
            self.assertEqual(dialog.add_program_ports.objectName(), "PortAction")
            self.assertEqual(dialog.use_program_ports.objectName(), "PortAction")
            self.assertFalse(bool(dialog.add_program_ports.property("applied")))
            self.assertFalse(bool(dialog.use_program_ports.property("applied")))
            self.assertIn("Only your Steam friends", dialog.visibility_help.text())
            self.assertIn("1 of 32 ports selected", dialog.port_count_status.text())
            dialog.program.setCurrentIndex(1)
            self.assertIn("TCP 1234", dialog.program_summary.text())
            self.assertIn("UDP 5678", dialog.program_summary.text())
            dialog._append_program()
            self.assertEqual(dialog.tcp_ports.text(), "1234")
            self.assertEqual(dialog.udp_ports.text(), "5678")
            self.assertEqual(dialog._endpoint_ips[("TCP", 1234)], "10.0.0.1")
            self.assertIn("Added 1 new port", dialog.program_action_status.text())
            self.assertIn("2 of 32 ports selected", dialog.port_count_status.text())
            self.assertTrue(bool(dialog.add_program_ports.property("applied")))
            self.assertFalse(bool(dialog.use_program_ports.property("applied")))

            dialog._replace_program()
            self.assertFalse(bool(dialog.add_program_ports.property("applied")))
            self.assertTrue(bool(dialog.use_program_ports.property("applied")))
        finally:
            dialog.close()

    def test_server_settings_explains_access_and_validates_ports_live(self):
        dialog = ServerSettingsDialog(FakeHostedSession(), [])
        try:
            dialog.visibility.setCurrentIndex(dialog.visibility.findData("public"))
            self.assertIn("public list", dialog.visibility_help.text())

            dialog.password_enabled.setChecked(True)
            self.assertTrue(dialog.password.isEnabled())
            self.assertIn("at least 4 characters", dialog.password.placeholderText())

            dialog.tcp_ports.setText("not-a-port")
            self.assertEqual(dialog.port_count_status.objectName(), "PortCountError")
            self.assertIn("Check the port list", dialog.port_count_status.text())

            dialog.tcp_ports.setText("27015")
            dialog.udp_ports.setText("27015-27016")
            self.assertEqual(dialog.port_count_status.objectName(), "PortCountGood")
            self.assertIn("3 of 32 ports selected", dialog.port_count_status.text())

            buttons = dialog.findChild(QDialogButtonBox)
            self.assertIsNotNone(buttons)
            self.assertEqual(
                buttons.button(QDialogButtonBox.StandardButton.Save).text(),
                "Apply changes",
            )
        finally:
            dialog.close()

    def test_new_chat_message_does_not_move_reader_who_scrolled_up(self):
        now = time.time()
        messages = [
            ChatMessage(index + 1, f"Player {index + 1}", f"Message {index + 1} " * 8, now + index)
            for index in range(40)
        ]
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.session = FakeChatSession(messages)
        window.steam = FakeSteamService()
        window._chat_following = True
        window._chat_unread = 0
        window._restoring_chat_scroll = False
        window._chat_rendered_keys = ()
        window._chat_latest_button = QPushButton()

        holder = QWidget()
        window._chat_messages_layout = QVBoxLayout(holder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.resize(420, 180)
        scroll.setWidget(holder)
        scroll.show()
        window._chat_scroll_area = scroll
        scroll.verticalScrollBar().valueChanged.connect(window._chat_scroll_changed)
        try:
            window._render_chat_messages()
            self.app.processEvents()
            self.app.processEvents()
            bar = scroll.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0)
            bar.setValue(bar.maximum() // 3)
            self.app.processEvents()
            old_value = bar.value()
            self.assertFalse(window._chat_following)

            window.session.chat_messages += (
                ChatMessage(99, "New player", "A newly arrived message", now + 100),
            )
            window._render_chat_messages()
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(bar.value(), old_value)
            self.assertEqual(window._chat_unread, 1)
        finally:
            scroll.close()
            window.hide()
            window.deleteLater()

    def test_new_chat_message_follows_when_reader_is_at_bottom(self):
        now = time.time()
        messages = [
            ChatMessage(index + 1, f"Player {index + 1}", f"Message {index + 1} " * 8, now + index)
            for index in range(40)
        ]
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.session = FakeChatSession(messages)
        window.steam = FakeSteamService()
        window._chat_following = True
        window._chat_unread = 0
        window._restoring_chat_scroll = False
        window._chat_scroll_generation = 0
        window._chat_rendered_keys = ()
        window._chat_latest_button = QPushButton()

        holder = QWidget()
        window._chat_messages_layout = QVBoxLayout(holder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.resize(420, 180)
        scroll.setWidget(holder)
        scroll.show()
        window._chat_scroll_area = scroll
        scroll.verticalScrollBar().valueChanged.connect(window._chat_scroll_changed)
        try:
            window._render_chat_messages()
            for _ in range(4):
                self.app.processEvents()
            bar = scroll.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0)
            self.assertEqual(bar.value(), bar.maximum())

            old_max = bar.maximum()
            window.session.chat_messages += (
                ChatMessage(99, "New player", "A newly arrived message " * 8, now + 100),
            )
            window._render_chat_messages()
            for _ in range(4):
                self.app.processEvents()
            self.assertGreater(bar.maximum(), old_max)
            self.assertEqual(bar.value(), bar.maximum())
            self.assertTrue(window._chat_following)
            self.assertEqual(window._chat_unread, 0)
        finally:
            scroll.close()
            window.hide()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from SteamyLan.updater import (
    begin_update_attempt,
    clear_update_state,
    record_update_failure,
    update_attempt_path,
    update_version_is_blocked,
)


class UpdateStateTests(unittest.TestCase):
    def test_interrupted_attempt_blocks_only_the_same_version(self):
        with tempfile.TemporaryDirectory() as temp:
            install = Path(temp) / "SteamyLAN"
            install.mkdir()
            archive = Path(temp) / "update.zip"
            archive.write_bytes(b"update")

            begin_update_attempt("1.2.3", archive, install)

            self.assertTrue(update_attempt_path(install).is_file())
            self.assertTrue(update_version_is_blocked("v1.2.3", install))
            self.assertFalse(update_version_is_blocked("1.2.4", install))

    def test_failed_update_is_blocked_until_a_success_clears_state(self):
        with tempfile.TemporaryDirectory() as temp:
            install = Path(temp) / "SteamyLAN"
            install.mkdir()
            archive = Path(temp) / "update.zip"
            archive.write_bytes(b"update")

            begin_update_attempt("1.2.3", archive, install)
            record_update_failure("1.2.3", "replacement failed", install)

            self.assertTrue(update_version_is_blocked("1.2.3", install))
            self.assertFalse(update_attempt_path(install).exists())
            clear_update_state(install)
            self.assertFalse(update_version_is_blocked("1.2.3", install))


if __name__ == "__main__":
    unittest.main()

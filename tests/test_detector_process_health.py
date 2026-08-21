from __future__ import annotations

import os
import unittest

import psutil

from SteamyLan.detector import shared_process_is_alive
from SteamyLan.models import DetectedService, Endpoint


class SharedProcessHealthTests(unittest.TestCase):
    def _service(self, *, pid=None, started_at=None, exe_path=None):
        proc = psutil.Process(os.getpid())
        return DetectedService(
            key="test",
            name="Test server",
            process_name=proc.name(),
            pid=int(proc.pid if pid is None else pid),
            endpoints=(Endpoint("TCP", 27015, "127.0.0.1"),),
            started_at=float(proc.create_time() if started_at is None else started_at),
            exe_path=str(proc.exe() if exe_path is None else exe_path),
        )

    def test_live_original_process_keeps_share_alive_even_without_listener_scan(self):


        self.assertTrue(shared_process_is_alive(self._service()))

    def test_pid_reuse_guard_rejects_different_process_start_time(self):
        service = self._service(started_at=1.0)
        self.assertFalse(shared_process_is_alive(service))

    def test_missing_process_is_dead(self):
        pid = max(999_999, os.getpid() + 10_000_000)
        self.assertFalse(shared_process_is_alive(self._service(pid=pid)))

    def test_manual_share_has_no_process_lifetime_dependency(self):
        service = DetectedService(
            key="manual", name="Manual", process_name="Manual", pid=0,
            endpoints=(Endpoint("UDP", 27015, "127.0.0.1"),),
        )
        self.assertTrue(shared_process_is_alive(service))


if __name__ == "__main__":
    unittest.main()

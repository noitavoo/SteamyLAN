from __future__ import annotations

import os
import socket
from collections import defaultdict

import psutil

from .models import DetectedService, Endpoint, ProgramInfo, unique_endpoints
from .program_catalog import ProgramCatalog, friendly_program_name





HIDDEN_CORE_PROCESSES = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "lsass.exe",
    "services.exe",
    "svchost.exe",
    "wininit.exe",
    "winlogon.exe",
    "csrss.exe",
    "smss.exe",
    "fontdrvhost.exe",
    "dwm.exe",
    "spoolsv.exe",
}
STEAMYLAN_PROCESSES = {"steamylan.exe", "steamylan", "python.exe", "pythonw.exe"}

KNOWN_EXE_NAMES = {
    "terrariaserver.exe": "Terraria",
    "valheim_server.exe": "Valheim",
    "projectzomboid64.exe": "Project Zomboid",
    "projectzomboid32.exe": "Project Zomboid",
    "factorio.exe": "Factorio",
    "srcds.exe": "Source Game Server",
    "srcds_linux": "Source Game Server",
    "7daystodie_server.exe": "7 Days to Die",
    "palserver-win64-test-cmd.exe": "Palworld",
    "enshrouded_server.exe": "Enshrouded",
    "vrisingserver.exe": "V Rising",
}





SENSITIVE_PORTS = {
    21, 22, 23, 25, 53, 110, 135, 139, 143, 389, 445, 465, 587, 636,
    993, 995, 1433, 1521, 2375, 2376, 3306, 3389, 5432, 5900, 5985,
    5986, 6379, 9200, 11211, 27017,
}


def shared_process_is_alive(service: DetectedService) -> bool:
    """Return whether a detected shared service still belongs to the same live process.

    Listener discovery is intentionally *not* part of this check. A game/server can
    briefly close or rebind one of its sockets while remaining perfectly healthy;
    treating one such scan as a shutdown tears down an otherwise valid Steam lobby.
    PID reuse is guarded by process creation time and, when available, executable path.
    Access-denied metadata is treated conservatively as alive rather than destroying
    the user's active session.
    """
    pid = int(getattr(service, "pid", 0) or 0)
    if pid <= 0:

        return True
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, ValueError, OSError):
        return False

    try:
        if not proc.is_running():
            return False
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
        except (psutil.AccessDenied, OSError):
            pass

        expected_started = float(getattr(service, "started_at", 0.0) or 0.0)
        if expected_started:
            try:
                actual_started = float(proc.create_time() or 0.0)
            except (psutil.AccessDenied, OSError):
                actual_started = 0.0

            if actual_started and abs(actual_started - expected_started) > 1.0:
                return False

        expected_exe = str(getattr(service, "exe_path", "") or "")
        if expected_exe:
            try:
                actual_exe = str(proc.exe() or "")
            except (psutil.AccessDenied, OSError):
                actual_exe = ""
            if actual_exe and os.path.normcase(os.path.abspath(actual_exe)) != os.path.normcase(os.path.abspath(expected_exe)):
                return False
        return True
    except psutil.NoSuchProcess:
        return False


class ServiceDetector:
    def __init__(self, logger):
        self.log = logger
        self.catalog = ProgramCatalog()

    def _programs_by_pid(self, pids: set[int]) -> dict[int, ProgramInfo]:
        try:
            return {int(p.pid): p for p in self.catalog.scan(pids)}
        except Exception as exc:
            self.log.debug("Program metadata discovery failed: %s", exc)
            return {}

    @staticmethod
    def _warning_for(endpoints: tuple[Endpoint, ...]) -> str:
        if any(e.port in SENSITIVE_PORTS for e in endpoints):
            return "This program uses a port commonly associated with administration or databases. Share it only if you recognize the program."
        return ""

    def scan(self) -> list[DetectedService]:
        by_pid: dict[int, list[Endpoint]] = defaultdict(list)
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception as exc:
            self.log.warning("Global socket discovery failed: %s", exc)



            raise RuntimeError("Could not enumerate local network listeners.") from exc

        for conn in conns:
            try:
                pid = int(conn.pid or 0)
                if pid <= 0 or not conn.laddr:
                    continue
                port = int(conn.laddr.port)
                if not 1 <= port <= 65535:
                    continue
                ip = str(conn.laddr.ip)
                if conn.type == socket.SOCK_STREAM:
                    if conn.status != psutil.CONN_LISTEN:
                        continue
                    proto = "TCP"
                elif conn.type == socket.SOCK_DGRAM:


                    if conn.raddr:
                        continue
                    proto = "UDP"
                else:
                    continue
                by_pid[pid].append(Endpoint(proto, port, ip))
            except Exception:
                continue

        metadata = self._programs_by_pid(set(by_pid))
        found: list[DetectedService] = []
        own_pid = os.getpid()

        for pid, endpoints in by_pid.items():
            if pid == own_pid:
                continue

            program = metadata.get(pid)
            if program is not None:
                pname = program.process_name
                pname_fold = pname.casefold()
                exe = program.exe
                cmdline = program.cmdline
                started_at = program.started_at
                parents = list(program.parent_names)
            else:

                try:
                    proc = psutil.Process(pid)
                    with proc.oneshot():
                        pname = proc.name() or "Application"
                        pname_fold = pname.casefold()
                        cmdline = tuple(proc.cmdline() or ())
                        exe = proc.exe() or pname
                        started_at = float(proc.create_time() or 0.0)
                    parents = []
                    cur = proc.parent()
                    for _ in range(4):
                        if cur is None:
                            break
                        try:
                            parents.append(cur.name())
                            cur = cur.parent()
                        except Exception:
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue

            if pname_fold in HIDDEN_CORE_PROCESSES:
                continue


            if pname_fold in STEAMYLAN_PROCESSES and any(tag in " ".join(cmdline).casefold() for tag in ("steamylan",)):
                continue

            clean = unique_endpoints(endpoints)
            if not clean:
                continue

            if program is not None:
                name = program.display_name or pname
                description = program.subtitle or "Running application"
                window_title = program.window_title
                icon_path = program.icon_path or exe
                steam_appid = program.steam_appid
            else:
                name, description = friendly_program_name(
                    exe=exe,
                    process_name=pname,
                    cmdline=cmdline,
                    parent_names=parents,
                )
                window_title = ""
                icon_path = exe
                steam_appid = None

            exe_base = os.path.basename(exe).casefold()
            known = exe_base in KNOWN_EXE_NAMES
            if known:
                name = KNOWN_EXE_NAMES[exe_base]
            text = " ".join(cmdline).casefold()
            if "minecraft_server" in text or "net.minecraft.server.main" in text or ("server.jar" in text and "java" in exe_base):
                name = "Minecraft Server"
                description = "Minecraft Java server"
                known = True

            ports = ",".join(f"{e.protocol}{e.port}" for e in clean)
            key = f"{pid}:{exe_base}:{ports}"
            found.append(
                DetectedService(
                    key=key,
                    name=name or pname,
                    process_name=pname,
                    pid=pid,
                    endpoints=clean,
                    confidence=100 if known else 50,
                    known_game=known,
                    exe_path=exe,
                    description=description,
                    window_title=window_title,
                    icon_path=icon_path,
                    started_at=started_at,
                    cmdline=cmdline,
                    steam_appid=steam_appid,
                    warning=self._warning_for(clean),
                )
            )

        found.sort(
            key=lambda s: (
                -float(s.started_at or 0.0),
                s.name.casefold(),
                s.process_name.casefold(),
                s.pid,
            )
        )
        return found

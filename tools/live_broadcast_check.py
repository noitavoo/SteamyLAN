from __future__ import annotations

import logging
import socket
import threading
import time

from SteamyLan.broadcast_redirect import BroadcastRedirectorController


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("steamylan.broadcast.live-check")
    ready = threading.Event()
    failed = threading.Event()
    result = {"message": ""}

    def status(kind: str, message: str) -> None:
        logger.info("%s: %s", kind, message)
        result["message"] = message
        if kind == "ready":
            ready.set()
        elif kind == "error":
            failed.set()

    proxy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    proxy.bind(("127.0.0.1", 0))
    proxy.settimeout(4.0)
    port = int(proxy.getsockname()[1])
    controller = BroadcastRedirectorController(logger, status)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sender.settimeout(4.0)
    payload = b"SteamyLAN safe LAN discovery check"
    try:
        controller.update([port])
        deadline = time.monotonic() + 45.0
        while not ready.wait(0.1):
            if failed.is_set():
                raise RuntimeError(result["message"] or "LAN discovery helper failed.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows did not start the LAN discovery helper in time.")
        sender.sendto(payload, ("255.255.255.255", port))
        received, game_address = proxy.recvfrom(1024)
        if received != payload or game_address[0] != "127.0.0.1":
            raise RuntimeError("The redirected discovery request did not arrive safely on loopback.")
        proxy.sendto(b"SteamyLAN discovery reply", game_address)
        reply, proxy_address = sender.recvfrom(1024)
        if reply != b"SteamyLAN discovery reply" or proxy_address[0] != "127.0.0.1":
            raise RuntimeError("The discovery reply did not return to the local game socket.")
        print(f"Live LAN discovery redirect passed on loopback UDP port {port}.")
        return 0
    except Exception as exc:
        print(f"Live LAN discovery redirect failed: {exc}")
        return 1
    finally:
        controller.detach()
        controller.stop()
        sender.close()
        proxy.close()


if __name__ == "__main__":
    raise SystemExit(main())

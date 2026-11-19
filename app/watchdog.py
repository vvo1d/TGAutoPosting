"""Сторож systemd: приложение раз в 30 секунд сообщает «живо». Если цикл событий завис
(например, бесконечный цикл в коде), сигналы прекращаются, и systemd перезапускает службу
через WatchdogSec. Без systemd (локальный запуск) ничего не делает."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger(__name__)


def _notify(msg: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError as e:
        log.warning("sd_notify: %s", e)
        return False


async def run(interval: float = 30) -> None:
    if not os.environ.get("WATCHDOG_USEC"):
        return  # служба запущена без WatchdogSec — сторож не нужен
    _notify("READY=1")
    log.info("Сторож systemd включён")
    while True:
        _notify("WATCHDOG=1")
        await asyncio.sleep(interval)

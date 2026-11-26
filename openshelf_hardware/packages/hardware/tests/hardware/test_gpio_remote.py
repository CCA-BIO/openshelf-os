from __future__ import annotations

import json
import socket
import threading

import pytest

from hardware.exceptions import GpioReadError
from hardware.gpio.manager import GpioManager
from hardware.gpio.types import GpioPinSpec


class OnePinServer:
    """Answers each input setup with the pin's level, then lets the test cut the link."""

    def __init__(self, level: bool) -> None:
        self.level = level
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.client: socket.socket | None = None
        self.answered = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.client, _ = self.listener.accept()
        for line in self.client.makefile("r", encoding="utf-8"):
            message = json.loads(line)
            if message["op"] == "setup" and message["direction"] == "input":
                reply = {"op": "input", "name": message["name"], "value": self.level}
                self.client.sendall((json.dumps(reply) + "\n").encode())
                self.answered.set()

    def cut(self) -> None:
        self.listener.close()
        assert self.client is not None
        self.client.shutdown(socket.SHUT_RDWR)
        self.client.close()


def test_a_lost_remote_link_fails_reads_instead_of_reporting_the_last_level(monkeypatch: pytest.MonkeyPatch) -> None:
    # The e-stop and cabinet-door monitors treat a read error as unsafe. A backend that kept
    # answering its last level after the link dropped would report a released e-stop forever.
    server = OnePinServer(level=False)
    monkeypatch.setenv("OPENSHELF_GPIO_REMOTE", f"127.0.0.1:{server.port}")
    estop = GpioPinSpec(name="estop", board="pca9535", expander="pca9535_1", pin=8, direction="input")
    manager = GpioManager({"estop": estop})
    try:
        estop_input = manager.input("estop")
        assert server.answered.wait(2.0)
        assert estop_input.read() is False

        server.cut()
        with pytest.raises(GpioReadError):
            for _ in range(200):
                estop_input.read()
                threading.Event().wait(0.01)
    finally:
        manager.close()

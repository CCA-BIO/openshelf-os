"""GPIO pins served by another process over TCP, for hardware-in-the-loop rigs and simulators.

Selected when OPENSHELF_GPIO_REMOTE is set to `host:port` (see GpioManager). Every pin in the config
then goes to this backend instead of the Pi or an I2C expander, so the same config runs against a
simulator that models the curtain, doors, limit switches and the rest.

Protocol: UTF-8 JSON, one object per line, raw electrical levels (inversion stays in DigitalInput /
DigitalOutput, as with every other backend).

    client -> server  {"op": "hello", "proto": 1}
    client -> server  {"op": "setup", "name": "light_curtain", "direction": "input",
                       "board": "pca9535", "expander": "pca9535_1", "pin": 1, "inverted": false}
    client -> server  {"op": "set", "name": "cabinet_top_unlock", "value": true}
    server -> client  {"op": "input", "name": "light_curtain", "value": false}

The server answers each input setup with that pin's level and pushes every later change. Reads come
from the last level received, so they stay synchronous like the other backends. While the link is
down a read raises GpioReadError: the OS's fail-safe paths (e-stop active, doors open) then apply,
as they do when an expander stops answering.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections import defaultdict

from hardware.exceptions import GpioConnectionError, GpioReadError, GpioWriteError
from hardware.gpio.types import GpioEdge, GpioPinSpec, GpioSubscription, RawGpioCallback

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CONNECT_TIMEOUT_S = 10.0
FIRST_VALUE_TIMEOUT_S = 1.0
RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 8.0


def parse_address(url: str) -> tuple[str, int]:
    address = url.removeprefix("tcp://")
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise GpioConnectionError("Remote GPIO address must be host:port")
    return host, int(port)


class RemoteGpioBackend:
    def __init__(self, url: str) -> None:
        self._address = parse_address(url)
        self._lock = threading.RLock()
        self._specs: dict[str, GpioPinSpec] = {}
        self._values: dict[str, bool] = {}
        self._outputs: dict[str, bool] = {}
        self._callbacks: defaultdict[str, list[RawGpioCallback]] = defaultdict(list)
        self._value_arrived = threading.Condition(self._lock)
        self._sock: socket.socket | None = None
        self._closed = False
        self._connect_first()
        self._reader = threading.Thread(target=self._run, name="remote-gpio", daemon=True)
        self._reader.start()

    # -- connection -----------------------------------------------------------------------------

    def _connect_first(self) -> None:
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while True:
            try:
                self._open()
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise GpioConnectionError("Remote GPIO server did not answer") from exc
                time.sleep(RECONNECT_MIN_S)

    def _open(self) -> None:
        sock = socket.create_connection(self._address, timeout=CONNECT_TIMEOUT_S)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._sock = sock
            self._send_locked({"op": "hello", "proto": PROTOCOL_VERSION})
            for spec in self._specs.values():
                self._send_setup_locked(spec)
            for name, value in self._outputs.items():
                self._send_locked({"op": "set", "name": name, "value": value})
        logger.info("Remote GPIO connected", extra={"host": self._address[0], "port": self._address[1]})

    def _drop(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = RECONNECT_MIN_S
        while not self._closed:
            sock = self._sock
            if sock is None:
                try:
                    self._open()
                except OSError:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue
            received = False
            try:
                for line in sock.makefile("r", encoding="utf-8"):
                    self._receive(line)
                    received = True
            except (OSError, ValueError) as exc:
                if not self._closed:
                    logger.warning("Remote GPIO link failed", exc_info=exc)
            self._drop()
            if self._closed:
                return
            # A server that accepts and then drops the link at once must not be hammered.
            backoff = RECONNECT_MIN_S if received else min(backoff * 2, RECONNECT_MAX_S)
            logger.warning("Remote GPIO disconnected; reconnecting", extra={"retry_s": backoff})
            time.sleep(backoff)

    def _send_locked(self, message: dict) -> None:
        if self._sock is None:
            raise OSError("not connected")
        self._sock.sendall((json.dumps(message) + "\n").encode())

    def _send_setup_locked(self, spec: GpioPinSpec) -> None:
        self._send_locked({
            "op": "setup",
            "name": spec.name,
            "direction": spec.direction,
            "board": spec.board,
            "expander": spec.expander,
            "pin": spec.pin,
            "inverted": spec.inverted,
        })

    def _receive(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        message = json.loads(line)
        if message.get("op") != "input":
            return
        name = str(message["name"])
        value = bool(message["value"])
        with self._lock:
            previous = self._values.get(name)
            self._values[name] = value
            self._value_arrived.notify_all()
            callbacks = list(self._callbacks.get(name, [])) if previous is not None and previous != value else []
        for callback in callbacks:
            callback(value)

    # -- GpioBackend ----------------------------------------------------------------------------

    def setup_input(self, spec: GpioPinSpec) -> None:
        self._setup(spec)

    def setup_output(self, spec: GpioPinSpec) -> None:
        with self._lock:
            if spec.initial is not None:
                self._outputs[spec.name] = spec.to_raw(spec.initial)
        self._setup(spec)
        with self._lock:
            if spec.name in self._outputs:
                try:
                    self._send_locked({"op": "set", "name": spec.name, "value": self._outputs[spec.name]})
                except OSError:
                    pass  # replayed on reconnect

    def _setup(self, spec: GpioPinSpec) -> None:
        with self._lock:
            self._specs[spec.name] = spec
            try:
                self._send_setup_locked(spec)
            except OSError:
                pass  # replayed on reconnect

    def read(self, spec: GpioPinSpec) -> bool:
        with self._lock:
            if spec.direction == "output" and spec.name in self._outputs:
                return self._outputs[spec.name]
            if self._sock is None:
                raise GpioReadError(f"Remote GPIO is disconnected; '{spec.name}' cannot be read")
            if spec.name not in self._values:
                self._value_arrived.wait_for(lambda: spec.name in self._values, FIRST_VALUE_TIMEOUT_S)
            if spec.name not in self._values:
                raise GpioReadError(f"Remote GPIO has no value for '{spec.name}'")
            return self._values[spec.name]

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        with self._lock:
            self._outputs[spec.name] = bool(raw_value)
            try:
                self._send_locked({"op": "set", "name": spec.name, "value": bool(raw_value)})
            except OSError as exc:
                raise GpioWriteError(f"Remote GPIO write to '{spec.name}' failed") from exc

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription:
        del edge, debounce_ms  # DigitalInput filters edges and debounces

        with self._lock:
            self._callbacks[spec.name].append(callback)

        def did_unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(spec.name)
                if callbacks is not None and callback in callbacks:
                    callbacks.remove(callback)

        return GpioSubscription(did_unsubscribe)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._callbacks.clear()
        self._drop()

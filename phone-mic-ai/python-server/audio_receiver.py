"""Receive the phone's microphone stream over Bluetooth RFCOMM or USB.

Both receivers run a blocking ``run(sink, stop)`` loop that keeps going until
``stop`` is set: they wait for the phone, read the header, push PCM to the
sink, and on any disconnect / stall / error report it and wait again.

Bluetooth (no network needed)
    The laptop is an RFCOMM *server*: it advertises SERVICE_UUID over SDP with
    Windows' own Bluetooth stack (WinRT ``RfcommServiceProvider``). The phone,
    already paired, connects to that UUID. No drivers, no Wi-Fi.

USB (no network needed)
    The phone app listens on its own 127.0.0.1:5000. ``adb forward tcp:5000
    tcp:5000`` tunnels that over the USB cable, so the laptop connects to its
    own 127.0.0.1:5000. The same TCP receiver also works for testing with
    ``tools/fake_phone.py``.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from typing import Protocol

from protocol import SERVICE_NAME, SERVICE_UUID, AudioFormat, ProtocolError, StreamParser

STALL_TIMEOUT = 3.0     # seconds without audio = connection lost
RETRY_DELAY = 2.0       # seconds between reconnect attempts
HEADER_TIMEOUT = 5.0    # a connection must send its header within this time


class AudioSink(Protocol):
    def on_status(self, text: str) -> None: ...
    def on_connected(self, fmt: AudioFormat, source: str) -> None: ...
    def on_audio(self, pcm: bytes) -> None: ...
    def on_disconnected(self, reason: str) -> None: ...


def _dispatch(parser: StreamParser, data: bytes, sink: AudioSink, source: str) -> None:
    for kind, value in parser.feed(data):
        if kind == "format":
            sink.on_connected(value, source)  # type: ignore[arg-type]
        else:
            sink.on_audio(value)  # type: ignore[arg-type]


# --- USB / TCP --------------------------------------------------------------------

class TcpReceiver:
    """Connects to host:port (adb-forwarded phone, or the fake phone) and
    reconnects forever."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5000, label: str = "USB") -> None:
        self.host, self.port, self.label = host, port, label

    def run(self, sink: AudioSink, stop: threading.Event) -> None:
        waiting_said = False
        while not stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=3)
            except OSError:
                if not waiting_said:
                    sink.on_status(f"Waiting for the phone on {self.label} ({self.host}:{self.port})...")
                    waiting_said = True
                stop.wait(RETRY_DELAY)
                continue
            reason = self._session(sock, sink, stop)
            if reason is not None:          # None = adb accepted but the app isn't streaming
                sink.on_disconnected(reason)
                waiting_said = False
            stop.wait(RETRY_DELAY)

    def _session(self, sock: socket.socket, sink: AudioSink, stop: threading.Event) -> str | None:
        parser = StreamParser()
        started = time.monotonic()
        sock.settimeout(0.5)
        last_data = time.monotonic()
        try:
            with sock:
                while not stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        idle = time.monotonic() - last_data
                        if parser.format is None and time.monotonic() - started > HEADER_TIMEOUT:
                            return None
                        if parser.format is not None and idle > STALL_TIMEOUT:
                            return f"no audio for {STALL_TIMEOUT:.0f} s (stream stalled)"
                        continue
                    if not data:
                        # adb forward accepts the TCP connection even when the
                        # app isn't listening, then closes it straight away.
                        return "phone closed the stream" if parser.format else None
                    last_data = time.monotonic()
                    _dispatch(parser, data, sink, self.label)
                return "stopped"
        except ProtocolError as e:
            return f"protocol error: {e}"
        except OSError as e:
            return f"connection lost: {e}"


# --- Bluetooth RFCOMM (Windows) ----------------------------------------------------

class BluetoothReceiver:
    """RFCOMM server advertised over SDP; the paired phone connects to it."""

    def __init__(self, service_uuid: str = SERVICE_UUID, name: str = SERVICE_NAME) -> None:
        self.service_uuid, self.name = service_uuid, name

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import winrt.windows.devices.bluetooth.rfcomm  # noqa: F401
            import winrt.windows.networking.sockets  # noqa: F401
            import winrt.windows.storage.streams  # noqa: F401
        except ImportError as e:
            return False, f"WinRT Bluetooth packages missing ({e.name}); run: pip install -r requirements.txt"
        return True, ""

    def run(self, sink: AudioSink, stop: threading.Event) -> None:
        ok, why = self.available()
        if not ok:
            sink.on_status(why)
            return
        while not stop.is_set():
            try:
                asyncio.run(self._serve(sink, stop))
            except Exception as e:  # noqa: BLE001 - radio off, adapter missing, ...
                sink.on_status(f"Bluetooth error: {e}. Retrying in 5 s (is Bluetooth on?)")
                stop.wait(5)

    async def _serve(self, sink: AudioSink, stop: threading.Event) -> None:
        from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId, RfcommServiceProvider
        from winrt.windows.networking.sockets import SocketProtectionLevel, StreamSocketListener
        from winrt.windows.storage.streams import DataWriter, UnicodeEncoding

        loop = asyncio.get_running_loop()
        incoming: asyncio.Queue = asyncio.Queue()

        provider = await RfcommServiceProvider.create_async(RfcommServiceId.from_uuid(uuid.UUID(self.service_uuid)))
        listener = StreamSocketListener()

        def on_connection(_sender, args) -> None:   # called on a WinRT thread
            loop.call_soon_threadsafe(incoming.put_nowait, args.socket)

        token = listener.add_connection_received(on_connection)
        # (pywinrt names WinRT overloads separately: BindServiceNameAsync(name, level))
        await listener.bind_service_name_with_protection_level_async(
            provider.service_id.as_string(), SocketProtectionLevel.BLUETOOTH_ENCRYPTION_ALLOW_NULL_AUTHENTICATION)

        # SDP "Service Name" attribute (0x0100) so the service shows a name.
        w = DataWriter()
        w.unicode_encoding = UnicodeEncoding.UTF8
        name = self.name.encode("utf-8")
        w.write_byte((4 << 3) | 5)        # type 4 = text string, size index 5 = 1-byte length
        w.write_byte(len(name))
        w.write_string(self.name)
        provider.sdp_raw_attributes.insert(0x100, w.detach_buffer())
        provider.start_advertising_with_radio_discoverability(listener, True)
        sink.on_status(f"Bluetooth server ready: '{self.name}' ({self.service_uuid}). "
                       "Open the app on the phone and tap Start.")
        try:
            while not stop.is_set():
                try:
                    sock = await asyncio.wait_for(incoming.get(), 0.5)
                except asyncio.TimeoutError:
                    continue
                peer = "Bluetooth"
                try:
                    peer = f"Bluetooth ({sock.information.remote_host_name.display_name})"
                except Exception:  # noqa: BLE001
                    pass
                reason = await self._session(sock, sink, stop, peer)
                sink.on_disconnected(reason)
                sink.on_status("Waiting for the phone to reconnect over Bluetooth...")
        finally:
            try:
                provider.stop_advertising()
                listener.remove_connection_received(token)
                listener.close()
            except Exception:  # noqa: BLE001
                pass

    async def _session(self, sock, sink: AudioSink, stop: threading.Event, peer: str) -> str:
        from winrt.windows.storage.streams import Buffer, InputStreamOptions

        parser = StreamParser()
        size = 8192
        try:
            while not stop.is_set():
                buf = Buffer(size)
                timeout = STALL_TIMEOUT if parser.format else HEADER_TIMEOUT
                try:
                    res = await asyncio.wait_for(
                        sock.input_stream.read_async(buf, size, InputStreamOptions.PARTIAL), timeout)
                except asyncio.TimeoutError:
                    return f"no audio for {timeout:.0f} s (stream stalled)"
                n = res.length
                if n == 0:
                    return "phone closed the stream"
                _dispatch(parser, bytes(memoryview(res))[:n], sink, peer)
            return "stopped"
        except ProtocolError as e:
            return f"protocol error: {e}"
        except OSError as e:
            return f"Bluetooth link lost: {e}"
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

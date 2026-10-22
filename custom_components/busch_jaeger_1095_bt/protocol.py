"""Authenticated ACS protocol for the Busch-Jaeger 1095 UTA-BT.

Transport reconstructed from the free@home Next APK and validated on hardware.
The caller supplies a connected Bleak client; this module never starts a scanner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import math
import os
import struct
import time

from bleak.exc import BleakError
from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VERSION = 9
DATA_IN_UUID = "09c9be03-7b3f-4164-bc97-9c59517440e0"
CONTROL_UUID = "09c9be02-7b3f-4164-bc97-9c59517440e0"
STATUS_UUID = "09c9be01-7b3f-4164-bc97-9c59517440e0"
FLOW_UUID = "09c9be05-7b3f-4164-bc97-9c59517440e0"
TELEGRAM_UUID = "09c9be04-7b3f-4164-bc97-9c59517440e0"


def aes_cmac(key, message):
    c = cmac.CMAC(algorithms.AES(key))
    c.update(message)
    return c.finalize()


def confirmation(client_xy, server_xy, shared, random4, secret):
    # The central-side calcConfirmationCode(true, ...) orders peer before local.
    stage1 = aes_cmac(bytes(16), server_xy + client_xy)
    stage2 = aes_cmac(stage1, shared)
    return aes_cmac(stage2, random4 + secret)


def decrypt_packet(packet, shared, iv_suffix):
    if len(packet) < 20:
        raise ValueError("encrypted packet shorter than header/tag")
    version, resource, counter = struct.unpack_from("<HHQ", packet)
    if version != 1:
        raise ValueError(f"unsupported encrypted packet version {version}")
    iv = packet[4:12] + iv_suffix
    tag, ciphertext = packet[12:20], packet[20:]
    dec = Cipher(algorithms.AES(shared[:16]), modes.GCM(iv, tag, min_tag_length=8)).decryptor()
    plaintext = dec.update(ciphertext) + dec.finalize()
    return resource, counter, plaintext


def encrypt_packet(plaintext, shared, iv_suffix, counter, resource=0xAA02):
    header = struct.pack("<HHQ", 1, resource, counter)
    enc = Cipher(algorithms.AES(shared[:16]), modes.GCM(header[4:12] + iv_suffix)).encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()
    return header + enc.tag[:8] + ciphertext


def input_write(index, value, destination=0x0201):
    body = struct.pack(">HB", 1, index) + value
    apdu = bytes.fromhex("02c700da0081") + bytes([len(body)]) + body
    return bytes.fromhex("11000400fff0") + struct.pack(">HB", destination, len(apdu) - 1) + apdu


def knx_float(data):
    raw = int.from_bytes(data, "big")
    mantissa = raw & 0x7FF
    if raw & 0x8000:
        mantissa -= 2048
    return 0.01 * mantissa * (1 << ((raw >> 11) & 15))


def function_reply(plaintext):
    if not plaintext or plaintext[0] & 3 != 3:
        return None
    offset = 1 + (2 if plaintext[0] & 0x80 else 0) + 3
    frame = plaintext[offset:]
    if len(frame) < 19 or frame[0] not in (0x11, 0x29) or frame[1] != 0:
        return None
    if int.from_bytes(frame[9:11], "big") & 0x3FF != 0x2C9:
        return None
    result = frame[13]
    names = {
        0: "OperationSucceeded",
        1: "AdditionalDataAvailable",
        2: "FurtherIndexesAvailable",
        255: "OperationFailed",
        254: "IndexInvalid",
        253: "InvalidPayloadSize",
        128: "CommandNotSupported",
    }
    return {
        "property": f"0x{frame[12]:02x}",
        "service": f"0x{frame[14]:02x}",
        "result_code": result,
        "result_name": names.get(result, "unknown"),
        "channel": int.from_bytes(frame[16:18], "big"),
        "datapoint_index": frame[18],
        "frame": frame.hex(),
    }


def state_reports(plaintext):
    # TelegramService extended header (bit 7) carries two optional bytes.
    # Then strip the 3-byte Bluetooth envelope, as native onPacketReceived does.
    if not plaintext or plaintext[0] & 3 != 3:
        return []
    offset = 1 + (2 if plaintext[0] & 0x80 else 0) + 3
    frame = plaintext[offset:]
    if len(frame) < 11 or frame[0] not in (0x11, 0x29) or frame[1] != 0 or not frame[3] & 0x80:
        return []
    reports = []
    pos = 6
    while pos + 3 <= len(frame):
        address = int.from_bytes(frame[pos : pos + 2], "big")
        size = frame[pos + 2] + 1
        pos += 3
        if size < 2 or pos + size > len(frame):
            break
        apdu = frame[pos : pos + size]
        pos += size
        value = apdu[2:]
        if address in (0x704B, 0x6467, 0x3F71) and len(value) == 2:
            name = {
                0x704B: "measured_temperature",
                0x6467: "setpoint",
                0x3F71: "temperature_offset",
            }[address]
            reports.append(
                {"name": name, "value": round(knx_float(value), 3), "group": f"{address:04x}"}
            )
        elif address == 0x8FFD and len(value) == 1:
            reports.append(
                {"name": "eco", "value": bool(value[0] & 4), "raw": value[0], "group": "8ffd"}
            )
    return reports


class ProtocolError(RuntimeError):
    pass


class AuthenticationError(ProtocolError):
    """The peer could not authenticate with the configured credential."""


class Thermostat:
    """One persistent, authenticated BLE session; serialize writes internally.

    secret is the 32-byte ACS installation secret, not the label's 16-byte PK.
    on_event is an optional synchronous callback accepting a dictionary.
    """

    def __init__(
        self, address, secret, destination=0x0201, on_event=None, raw=False, connection_factory=None
    ):
        if len(secret) != 32:
            raise ValueError("ACS secret must contain 32 bytes")
        self.address, self.secret, self.destination = address, bytes(secret), destination
        self.on_event, self.raw = on_event, raw
        self._connection_factory = connection_factory
        self.state = {}
        self._client = None
        self._reader = None
        self._started = time.monotonic()
        self._queue = asyncio.Queue()
        self._outer = {}
        self._inner = bytearray()
        self._sequence = 0
        self._tx_counter = 0
        self._rx_counter = -1
        self._pending = None
        self._lock = asyncio.Lock()
        self._failure = None
        self._shared = None
        self._iv_suffix = bytes.fromhex("efbeadde")

    def _emit(self, kind, **fields):
        event = {"seconds": round(time.monotonic() - self._started, 3), "kind": kind, **fields}
        if self.on_event:
            self.on_event(event)

    async def __aenter__(self):
        try:
            await self.connect()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *exc):
        await self.close()

    def _received(self, char, data):
        uuid, data = char.uuid.lower(), bytes(data)
        if self.raw:
            self._emit("rx", uuid=uuid, hex=data.hex())
        if uuid in (STATUS_UUID, FLOW_UUID):
            self._emit("transport_status", uuid=uuid, hex=data.hex())
            return
        if not data:
            return
        flags = data[0] & 3
        if flags & 1:
            if uuid in self._outer:
                self._emit(
                    "fragment_error", uuid=uuid, error="new start before previous packet ended"
                )
            self._outer[uuid] = bytearray()
        if uuid not in self._outer:
            self._emit("fragment_error", uuid=uuid, error="continuation without start")
            return
        self._outer[uuid].extend(data[1:])
        if len(self._outer[uuid]) > 65536:
            del self._outer[uuid]
            self._emit("fragment_error", uuid=uuid, error="packet exceeds size limit")
            return
        if flags & 2:
            self._queue.put_nowait((uuid, bytes(self._outer.pop(uuid))))

    async def _send(self, payload, uuid=CONTROL_UUID):
        # This counter continues across packets and characteristics in a session.
        for offset in range(0, len(payload), 19):
            chunk = payload[offset : offset + 19]
            flags = (1 if offset == 0 else 0) | (2 if offset + len(chunk) == len(payload) else 0)
            fragment = bytes([(self._sequence & 252) | flags]) + chunk
            self._sequence = (self._sequence + 4) & 255
            if self.raw:
                self._emit("tx_fragment", uuid=uuid, hex=fragment.hex())
            if self._client is None:
                raise ProtocolError("Bluetooth session is closed")
            async with asyncio.timeout(10):
                await self._client.write_gatt_char(self._chars[uuid], fragment, response=True)

    async def _opcode(self, opcode):
        async def receive():
            while True:
                uuid, packet = await self._queue.get()
                if uuid == CONTROL_UUID and packet and packet[0] == opcode:
                    return packet
                self._emit("handshake_other_packet", uuid=uuid, hex=packet.hex())

        return await asyncio.wait_for(receive(), 12)

    async def connect(self):
        if self._client is not None:
            raise RuntimeError("Use a new Thermostat instance for each connection")
        if self._connection_factory is None:
            raise RuntimeError(
                "A connection factory is required; Home Assistant owns Bluetooth discovery"
            )
        self._client = await self._connection_factory()
        self._chars = {}
        for uuid, properties in [
            (CONTROL_UUID, ("write", "indicate")),
            (STATUS_UUID, ("read", "indicate")),
            (TELEGRAM_UUID, ("indicate",)),
            (FLOW_UUID, ("notify",)),
            (DATA_IN_UUID, ("write",)),
        ]:
            char = self._client.services.get_characteristic(uuid)
            if char is None or not all(prop in char.properties for prop in properties):
                raise ProtocolError(f"Missing expected characteristic/properties: {uuid}")
            self._chars[uuid] = char
        for uuid in (STATUS_UUID, CONTROL_UUID, TELEGRAM_UUID, FLOW_UUID):
            await self._client.start_notify(self._chars[uuid], self._received)
        initial = bytes(await self._client.read_gatt_char(self._chars[STATUS_UUID]))
        self._emit("initial_status", hex=initial.hex())
        await self._send(b"\x19")
        await self._opcode(0x1A)
        await self._send(b"\x01")
        descriptor = await self._opcode(0x0E)
        if self._iv_suffix not in descriptor:
            raise ProtocolError("Unsupported AES nonce descriptor")
        await self._opcode(0)
        await self._send(bytes.fromhex("1101000103"))
        await self._opcode(0)
        private = ec.generate_private_key(ec.SECP256R1())
        numbers = private.public_key().public_numbers()
        client_xy = numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
        random = os.urandom(4)
        await self._send(bytes.fromhex("1b010020") + client_xy[:32] + b"\x20" + client_xy[32:])
        reply = await self._opcode(0x1C)
        if len(reply) < 69 or reply[1:4] != bytes.fromhex("010020") or reply[36] != 32:
            raise ProtocolError("Malformed ECDH public-key reply")
        server_xy = reply[4:36] + reply[37:69]
        peer = ec.EllipticCurvePublicNumbers(
            int.from_bytes(server_xy[:32], "big"),
            int.from_bytes(server_xy[32:], "big"),
            ec.SECP256R1(),
        ).public_key()
        self._shared = private.exchange(ec.ECDH(), peer)
        await self._send(
            bytes.fromhex("1d0100")
            + confirmation(client_xy, server_xy, self._shared, random, self.secret)
        )
        peer_confirmation = await self._opcode(0x1E)
        if len(peer_confirmation) != 19 or peer_confirmation[1:3] != b"\x01\x00":
            raise ProtocolError("Malformed confirmation reply")
        await self._send(bytes.fromhex("1f0100") + random)
        reply = await self._opcode(0x20)
        if len(reply) != 7 or reply[1:3] != b"\x01\x00":
            raise ProtocolError("Malformed random-number reply")
        expected = confirmation(client_xy, server_xy, self._shared, reply[3:], self.secret)
        if not hmac.compare_digest(expected, peer_confirmation[3:]):
            raise AuthenticationError(
                "Peer authentication failed; verify installation export and password"
            )
        self._emit("connected", version=VERSION, address=self.address)
        self._reader = asyncio.create_task(self._receive_loop())

    async def close(self):
        """Release the proxy slot, cancel the receiver, and fail pending requests."""
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(ProtocolError("Bluetooth session closed"))
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(BleakError, OSError, TimeoutError):
                async with asyncio.timeout(5):
                    await client.disconnect()
        self._shared = None

    def _telegram(self, plaintext):
        if not plaintext:
            raise ProtocolError("Empty inner telegram fragment")
        header, offset = plaintext[0], 1
        if header & 128:
            offset += 2
        if len(plaintext) < offset:
            raise ProtocolError("Truncated extended fragment header")
        if header & 1:
            if self._inner:
                raise ProtocolError("Inner telegram start before previous end")
            self._inner = bytearray()
        self._inner.extend(plaintext[offset:])
        if len(self._inner) > 65536:
            raise ProtocolError("Inner telegram exceeds size limit")
        if not header & 2:
            return
        envelope, self._inner = bytes(self._inner), bytearray()
        if len(envelope) < 3:
            raise ProtocolError("Truncated Bluetooth telegram envelope")
        frame = envelope[3:]
        # Reuse decoders after normalizing the already reassembled envelope.
        normalized = bytes.fromhex("03000000") + frame
        if len(frame) >= 6 and int.from_bytes(frame[4:6], "big") != self.destination:
            return
        reply = function_reply(normalized)
        if reply:
            self._emit("application_reply", **reply)
            if self._pending:
                index, future = self._pending
                if (
                    reply["property"] == "0xda"
                    and reply["service"] == "0x81"
                    and reply["channel"] == 1
                    and reply["datapoint_index"] == index
                    and not future.done()
                ):
                    future.set_result(reply)
        for report in state_reports(normalized):
            self.state[report["name"]] = report["value"]
            self._emit("state", **report)

    async def _receive_loop(self):
        try:
            while True:
                uuid, packet = await self._queue.get()
                if uuid != TELEGRAM_UUID:
                    self._emit("other_packet", uuid=uuid, hex=packet.hex())
                    continue
                resource, counter, plaintext = decrypt_packet(packet, self._shared, self._iv_suffix)
                if counter <= self._rx_counter:
                    raise ProtocolError("Repeated or out-of-order encrypted receive counter")
                self._rx_counter = counter
                if self.raw:
                    self._emit(
                        "decrypted",
                        resource=f"0x{resource:04x}",
                        counter=counter,
                        hex=plaintext.hex(),
                    )
                if resource == 0xAA03:
                    self._telegram(plaintext)
                else:
                    self._emit("other_resource", resource=f"0x{resource:04x}", hex=plaintext.hex())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc
            self._emit("receive_error", error=str(exc))
            if self._pending and not self._pending[1].done():
                self._pending[1].set_exception(exc)

    async def _write_input(self, index, value):
        if not self._reader or self._failure:
            raise ProtocolError(f"Connection is not usable: {self._failure}")
        future = asyncio.get_running_loop().create_future()
        self._pending = (index, future)
        frame = input_write(index, value, self.destination)
        # The APK clears the inner offset AND sequence after a complete telegram
        # (sendNextFragment, 0x7776040). Every small new telegram starts with 03.
        plaintext = bytes.fromhex("03020000") + frame
        counter = self._tx_counter
        self._tx_counter += 1  # Never reuse a nonce, including after failed sends.
        packet = encrypt_packet(plaintext, self._shared, self._iv_suffix, counter)
        self._emit("command", index=index, counter=counter, frame=frame.hex())
        try:
            await self._send(packet, DATA_IN_UUID)
            reply = await asyncio.wait_for(future, 5)
            if reply["result_code"] & 128:
                raise ProtocolError(f"Thermostat rejected input {index}: {reply['result_name']}")
            # Positive status 2 accompanied the hardware-verified setpoint write.
            return reply
        except BaseException:
            # A failed send leaves device-side receive state uncertain. Require
            # a fresh session; do not silently replay or continue with new writes.
            self._failure = ProtocolError(
                "Previous command failed; reconnect before another command"
            )
            raise
        finally:
            if not future.done():
                future.cancel()
            self._pending = None

    async def set_temperature(self, celsius):
        """Set absolute Celsius temperature; hardware accepts 21 and 21.5.

        Values outside the installation's limits may be rejected by firmware.
        This diagnostic client's explicit input range is 5..35 C.
        """
        if not math.isfinite(celsius) or not 5 <= celsius <= 35:
            raise ValueError("Temperature must be finite and between 5 and 35 C")
        # Float32 conversion matches the APK; retain its mantissa truncation.
        value = struct.unpack("<f", struct.pack("<f", celsius))[0]
        mantissa, exponent = value * 100, 0
        while math.floor(mantissa + 0.5) > 2047:
            mantissa /= 2
            exponent += 1
        encoded = struct.pack(">H", (exponent << 11) | int(mantissa))
        async with self._lock:
            return await self._write_input(8, encoded)

    async def set_eco(self, enabled):
        """Follow the hardware-verified on/eco transition in the app's order."""
        async with self._lock:
            operations = [(6, True), (5, True)] if enabled else [(5, False), (6, True)]
            return [await self._write_input(index, bytes([value])) for index, value in operations]

    async def listen(self, seconds):
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._failure:
                raise ProtocolError(str(self._failure))
            if self._client is None or not self._client.is_connected:
                raise ProtocolError("Bluetooth connection lost")
            await asyncio.sleep(min(0.2, max(0, deadline - asyncio.get_running_loop().time())))

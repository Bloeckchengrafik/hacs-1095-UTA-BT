"""Offline transport regression tests. No BLE adapter or thermostat is used."""

import asyncio
import struct

from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.busch_jaeger_1095_bt import protocol as bt

TEST_SECRET = bytes(range(32))


def bt_encode(value):
    # These synthetic device targets need only exponent 1.
    return struct.pack(">H", 0x0800 | round(value * 50))


class Characteristic:
    def __init__(self, uuid, properties):
        self.uuid, self.properties = uuid, properties


class Services:
    def __init__(self):
        self.chars = {
            u: Characteristic(u, props)
            for u, props in (
                (bt.CONTROL_UUID, ["write", "indicate"]),
                (bt.STATUS_UUID, ["read", "indicate"]),
                (bt.DATA_IN_UUID, ["write"]),
                (bt.TELEGRAM_UUID, ["indicate"]),
                (bt.FLOW_UUID, ["notify"]),
            )
        }

    def get_characteristic(self, uuid):
        return self.chars.get(uuid)


class Peripheral:
    instances = []

    def __init__(self, *args, **kwargs):
        self.services = Services()
        self.callbacks, self.fragments = {}, {}
        self.sequence, self.last_client_sequence, self.counter = 0, -1, -1
        self.rx_counter = 2**63 - 1
        self.commands = []
        self.reads = []
        self.read_result = 2
        self.read_value = None
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.is_connected = False
        self.report_preset = True
        self.eco, self.temperature = False, 21.0
        Peripheral.instances.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char, callback):
        self.callbacks[char.uuid] = callback

    async def read_gatt_char(self, char):
        return bytes.fromhex("010100")

    def reply(self, packet, uuid=bt.CONTROL_UUID):
        for offset in range(0, len(packet), 19):
            chunk = packet[offset : offset + 19]
            flags = int(offset == 0) | (2 if offset + len(chunk) == len(packet) else 0)
            data = bytes([(self.sequence & 252) | flags]) + chunk
            self.sequence = (self.sequence + 4) & 255
            self.callbacks[uuid](self.services.get_characteristic(uuid), data)

    def report(self, frame):
        # Captured form: extended fragment header, metadata and Bluetooth envelope.
        plaintext = bytes.fromhex("83010200") + struct.pack("<H", len(frame)) + frame
        packet = bt.encrypt_packet(
            plaintext, self.shared, bytes.fromhex("efbeadde"), self.rx_counter, 0xAA03
        )
        self.rx_counter += 1
        self.reply(packet, bt.TELEGRAM_UUID)

    async def write_gatt_char(self, char, data, response):
        assert response
        sequence = data[0] >> 2
        assert sequence == (self.last_client_sequence + 1) % 64, "outer sequence must continue"
        self.last_client_sequence = sequence
        flags = data[0] & 3
        if flags & 1:
            self.fragments[char.uuid] = bytearray()
        self.fragments[char.uuid].extend(data[1:])
        if not flags & 2:
            return
        packet = bytes(self.fragments.pop(char.uuid))
        if char.uuid == bt.DATA_IN_UUID:
            resource, counter, plaintext = bt.decrypt_packet(
                packet, self.shared, bytes.fromhex("efbeadde")
            )
            assert resource == 0xAA02 and counter == self.counter + 1
            self.counter = counter
            # This is the v7/v8 regression: they emitted 07, 0B, ... here.
            assert plaintext[:4] == bytes.fromhex("03020000"), (
                "inner sequence must reset per complete telegram"
            )
            frame = plaintext[4:]
            assert frame[8] == len(frame) - 10 and frame[15] == len(frame) - 16
            if frame[9:15] == bytes.fromhex("02c700df0001"):
                assert frame.hex() == "11000400fff002010902c700df000103000102"
                self.reads.append(2)
                # A distinct effective Eco target, without a group-value report.
                value = self.read_value
                if value is None:
                    value = bytes.fromhex("0708") if self.eco else bt_encode(self.temperature)
                reply = (
                    bytes.fromhex("110004600201fff0")
                    + bytes([9 + len(value)])
                    + bytes.fromhex("02c900df")
                    + bytes([self.read_result, 1, 3 + len(value), 0, 1, 2])
                    + value
                )
                # A write reply with the same index cannot satisfy the read.
                self.report(bytes.fromhex("110004600201fff00902c900da028103000102"))
                self.report(reply)
                return
            assert frame[9:15] == bytes.fromhex("02c700da0081")
            assert frame[16:18] == bytes.fromhex("0001")
            index, value = frame[18], frame[19:]
            self.commands.append((index, value))
            # Hardware-verified application response format, including status 02.
            self.report(bytes.fromhex("110004600201fff00902c900da0281030001") + bytes([index]))
            if index == 8:
                self.temperature = bt.knx_float(value)
                self.report(bytes.fromhex("11003ce002016467032080") + value)
            elif index == 5:
                self.eco = bool(value[0])
                if self.report_preset:
                    self.report(
                        bytes.fromhex("11003ce002018ffd022080") + bytes([100 if self.eco else 97])
                    )
            return
        opcode = packet[0]
        if opcode == 0x19:
            self.reply(b"\x1a")
        elif opcode == 1:
            self.reply(bytes.fromhex("0eefbeadde"))
            self.reply(b"\x00")
        elif opcode == 0x11:
            self.reply(b"\x00")
        elif opcode == 0x1B:
            self.client_xy = packet[4:36] + packet[37:69]
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(self.client_xy[:32], "big"),
                int.from_bytes(self.client_xy[32:], "big"),
                ec.SECP256R1(),
            ).public_key()
            self.shared = self.private.exchange(ec.ECDH(), peer)
            pub = self.private.public_key().public_numbers()
            self.server_xy = pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
            self.reply(
                bytes.fromhex("1c010020")
                + self.server_xy[:32]
                + b"\x20"
                + self.server_xy[32:]
                + b"\x00"
            )
        elif opcode == 0x1D:
            confirm = bt.confirmation(
                self.client_xy, self.server_xy, self.shared, b"test", TEST_SECRET
            )
            self.reply(bytes.fromhex("1e0100") + confirm)
        elif opcode == 0x1F:
            self.reply(bytes.fromhex("200100") + b"test")
        else:
            raise AssertionError(f"Unexpected opcode {opcode}")


class Scanner:
    @staticmethod
    async def find_device_by_address(*args, **kwargs):
        return object()


async def test_persistent_temperature_and_eco_commands():
    events = []

    async def connector():
        device = Peripheral()
        await device.connect()
        return device

    async with bt.Thermostat(
        "test", TEST_SECRET, on_event=events.append, connection_factory=connector
    ) as device:
        for temp in (21.0, 21.5, 21.0):
            reply = await device.set_temperature(temp)
            await device.listen(0.001)
            assert reply["result_code"] == 2
            assert device.state["setpoint"] == temp
        await device.set_eco(True)
        await device.listen(0.001)
        assert device.state["eco"] is True
        await device.set_eco(False)
        await device.listen(0.001)
        assert device.state["eco"] is False
    peripheral = Peripheral.instances[-1]
    assert peripheral.commands == [
        (8, bytes.fromhex("0c1a")),
        (8, bytes.fromhex("0c33")),
        (8, bytes.fromhex("0c1a")),
        (6, b"\x01"),
        (5, b"\x01"),
        (5, b"\x00"),
        (6, b"\x01"),
    ]
    assert not peripheral.is_connected
    assert len([event for event in events if event["kind"] == "application_reply"]) == 7


async def test_receive_failure_blocks_next_write():
    import pytest

    async def connector():
        device = Peripheral()
        await device.connect()
        return device

    async with bt.Thermostat("test", TEST_SECRET, connection_factory=connector) as device:
        peripheral = Peripheral.instances[-1]
        packet = bytearray(
            bt.encrypt_packet(b"\x03", peripheral.shared, bytes.fromhex("efbeadde"), 2**63, 0xAA03)
        )
        packet[-1] ^= 1
        peripheral.reply(packet, bt.TELEGRAM_UUID)
        await asyncio.sleep(0.001)
        with pytest.raises(bt.ProtocolError):
            await device.set_temperature(21.5)
        assert not peripheral.commands


def test_recorded_packets():
    reply = bt.function_reply(bytes.fromhex("830102001300110004600201fff00902c900da028103000108"))
    assert (reply["result_code"], reply["channel"], reply["datapoint_index"]) == (2, 1, 8)
    assert (
        bt.state_reports(bytes.fromhex("830102000d0011003ce0020164670320800c33"))[0]["value"]
        == 21.5
    )
    assert (
        bt.state_reports(bytes.fromhex("83010200130011003ce00201646703e88007088ffd02ec8064"))[-1][
            "value"
        ]
        is True
    )


async def test_setpoint_readback_and_rejected_read_does_not_break_writes():
    import pytest

    async def connector():
        peripheral = Peripheral()
        await peripheral.connect()
        return peripheral

    async with bt.Thermostat("test", TEST_SECRET, connection_factory=connector) as device:
        peripheral = Peripheral.instances[-1]
        await device.set_eco(True)
        assert await device.read_setpoint() == 18.0
        await device.set_eco(False)
        assert await device.read_setpoint() == 21.0
        peripheral.read_result = 128
        with pytest.raises(bt.ReadbackError, match="rejected"):
            await device.read_setpoint()
        peripheral.read_result = 2
        peripheral.read_value = b""
        with pytest.raises(bt.ReadbackError, match="payload"):
            await device.read_setpoint()
        # Rejected/malformed responses never poison the authenticated session.
        await device.set_temperature(21.5)
        assert peripheral.commands[-1] == (8, bytes.fromhex("0c33"))

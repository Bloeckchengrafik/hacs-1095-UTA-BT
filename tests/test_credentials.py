"""Synthetic export and malformed-input tests; no installation secrets included."""

import hashlib
import struct
import zlib

import pytest

from custom_components.busch_jaeger_1095_bt.credentials import (
    derive_secret,
    installation_material,
    validate_secret,
)


def blob(value):
    return struct.pack("<I", len(value)) + value


def export(material=bytes(range(32))):
    config = struct.pack("<HH", 16, 0) + blob(bytes(16)) + blob(material)
    inner = b"BEAB" + struct.pack("<I", 11) + blob(b"") * 4 + blob(config)
    unpacked = (
        bytes.fromhex("b0ab0200")
        + blob(b"test-uuid")
        + b"\x01"
        + blob(b"installation.bif")
        + blob(inner)
    )
    return b"FAHBTInstallation\x01" + struct.pack(">I", len(unpacked)) + zlib.compress(unpacked)


def test_derive_secret():
    material = bytes(range(32))
    assert installation_material(export()) == material
    assert (
        derive_secret(export(), "synthetic") == hashlib.sha256(material + b"synthetic").hexdigest()
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"garbage",
        export()[:-3],
        export(bytes(16)),
        export()[:18] + b"\xff" * 4 + export()[22:],
    ],
)
def test_invalid_export(data):
    with pytest.raises((ValueError, zlib.error)):
        installation_material(data)


@pytest.mark.parametrize("secret", ["", "aa" * 16, "xx" * 32, "aa" * 33])
def test_invalid_secret(secret):
    with pytest.raises(ValueError):
        validate_secret(secret)

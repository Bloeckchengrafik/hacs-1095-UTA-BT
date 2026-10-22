"""Read the observed export format without retaining the installation password."""

from __future__ import annotations

import hashlib
import struct
import zlib

MAX_EXPORT_SIZE = 4 * 1024 * 1024
MAX_UNPACKED_SIZE = 8 * 1024 * 1024


def _blob(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos < 0 or pos + 4 > len(data):
        raise ValueError("Truncated installation field")
    size = struct.unpack_from("<I", data, pos)[0]
    end = pos + 4 + size
    if end > len(data):
        raise ValueError("Installation field exceeds bounds")
    return data[pos + 4 : end], end


def installation_material(data: bytes) -> bytes:
    """Extract ACS installation material from the version-11 export."""
    if len(data) > MAX_EXPORT_SIZE or not data.startswith(b"FAHBTInstallation\x01"):
        raise ValueError("Unsupported or oversized installation export")
    if len(data) < 23:
        raise ValueError("Truncated export")
    expected = struct.unpack_from(">I", data, 18)[0]
    if expected > MAX_UNPACKED_SIZE:
        raise ValueError("Unpacked installation exceeds size limit")
    inflater = zlib.decompressobj()
    try:
        unpacked = inflater.decompress(data[22:], MAX_UNPACKED_SIZE + 1)
    except zlib.error as exc:
        raise ValueError("Invalid compressed installation") from exc
    if not inflater.eof or inflater.unconsumed_tail or len(unpacked) != expected:
        raise ValueError("Invalid compressed installation")
    if unpacked[:4] != bytes.fromhex("b0ab0200"):
        raise ValueError("Unsupported installation container")
    _, pos = _blob(unpacked, 4)
    pos += 1
    name, pos = _blob(unpacked, pos)
    inner, _ = _blob(unpacked, pos)
    if name != b"installation.bif" or len(inner) < 8 or inner[:4] != b"BEAB":
        raise ValueError("Missing installation.bif")
    if struct.unpack_from("<I", inner, 4)[0] != 11:
        raise ValueError("Unsupported installation version")
    pos = 8
    for _ in range(4):
        _, pos = _blob(inner, pos)
    config, _ = _blob(inner, pos)
    if len(config) < 4 or struct.unpack_from("<H", config)[0] != 16:
        raise ValueError("Unsupported configuration version")
    _, pos = _blob(config, 4)
    material, _ = _blob(config, pos)
    if len(material) != 32:
        raise ValueError("Expected 32-byte installation material")
    return material


def derive_secret(data: bytes, password: str) -> str:
    """Return the 32-byte ACS secret as hex. Never log inputs or output."""
    return hashlib.sha256(installation_material(data) + password.encode("utf-8")).hexdigest()


def validate_secret(value: str) -> str:
    """Normalize a secret and reject device-label PKs and malformed input."""
    try:
        secret = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValueError("Expected a 32-byte ACS secret encoded as hex") from exc
    if len(secret) != 32:
        raise ValueError("Expected a 32-byte ACS secret, not the label's 16-byte PK")
    return secret.hex()

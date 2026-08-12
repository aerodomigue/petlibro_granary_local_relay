"""Minimal MQTT CONNECT packet reader/parser.

Used by `CredentialCaptureProxy` to read exactly one full CONNECT packet off
a raw socket (respecting the MQTT variable-length "remaining length"
encoding) and pull out the client ID, username and password fields - without
depending on any client-side MQTT library, since those aren't built to parse
packets sent *to* a broker.

Reference: MQTT 3.1 / 3.1.1 CONNECT packet layout.

  Fixed header:
    byte 0            : packet type (0x10) + flags (must be 0 for CONNECT)
    remaining length   : 1-4 bytes, variable-length encoding

  Variable header (within "remaining length" bytes):
    protocol name      : 2-byte length prefix + UTF-8 bytes ("MQIsdp"/"MQTT")
    protocol level      : 1 byte
    connect flags        : 1 byte (bit 7 = username present, bit 6 = password
                            present, bit 2 = will present)
    keep alive           : 2 bytes

  Payload (fields present depend on the connect flags, in this order):
    client identifier    : 2-byte length prefix + bytes (always present)
    will topic + message  : 2-byte length prefix + bytes each (if will flag)
    username               : 2-byte length prefix + bytes (if username flag)
    password               : 2-byte length prefix + bytes (if password flag)
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

CONNECT_PACKET_TYPE = 0x10
CONNECT_PACKET_TYPE_MASK = 0xF0
USERNAME_FLAG_BIT = 0x80
PASSWORD_FLAG_BIT = 0x40
WILL_FLAG_BIT = 0x04
MAX_REMAINING_LENGTH_BYTES = 4
CONTINUATION_BIT = 0x80
REMAINING_LENGTH_VALUE_MASK = 0x7F


class MalformedConnectPacketError(ValueError):
    """Raised when the bytes read from the socket aren't a valid CONNECT packet.

    Carries whatever raw bytes were already consumed from the socket
    (`raw_so_far`) so the caller can still forward them to the real broker
    instead of corrupting the stream - identity capture is best-effort, the
    feeder's connection must never depend on it.
    """

    def __init__(self, message: str, raw_so_far: bytes) -> None:
        super().__init__(message)
        self.raw_so_far = raw_so_far


@dataclass(frozen=True, slots=True)
class ConnectPacketFields:
    """Fields extracted from a device's CONNECT packet."""

    client_id: str
    username: str | None
    password: str | None


class _TrackingReader:
    """Reads from a socket while accumulating every byte consumed so far."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.raw = bytearray()

    def recv_exact(self, length: int) -> bytes:
        """Read exactly `length` bytes, raising `MalformedConnectPacketError` on early EOF."""
        chunk_buffer = bytearray()
        while len(chunk_buffer) < length:
            chunk = self._sock.recv(length - len(chunk_buffer))
            if not chunk:
                raise MalformedConnectPacketError(
                    "connection closed before a full packet was read", bytes(self.raw)
                )
            chunk_buffer.extend(chunk)
            self.raw.extend(chunk)
        return bytes(chunk_buffer)


def _read_remaining_length(reader: _TrackingReader) -> int:
    """Read the MQTT variable-length "remaining length" field."""
    multiplier = 1
    value = 0
    for _ in range(MAX_REMAINING_LENGTH_BYTES):
        byte_value = reader.recv_exact(1)[0]
        value += (byte_value & REMAINING_LENGTH_VALUE_MASK) * multiplier
        if not byte_value & CONTINUATION_BIT:
            return value
        multiplier *= 128
    raise MalformedConnectPacketError("remaining length field exceeds 4 bytes", bytes(reader.raw))


def _read_length_prefixed_string(buffer: bytes, offset: int, raw_so_far: bytes) -> tuple[str, int]:
    """Read a 2-byte-length-prefixed UTF-8 string from `buffer` at `offset`.

    Returns:
        A tuple of (decoded string, offset of the byte after this field).
    """
    if offset + 2 > len(buffer):
        raise MalformedConnectPacketError("truncated length prefix", raw_so_far)
    field_length = buffer[offset] << 8 | buffer[offset + 1]
    start = offset + 2
    end = start + field_length
    if end > len(buffer):
        raise MalformedConnectPacketError("truncated field", raw_so_far)
    return buffer[start:end].decode("utf-8", errors="replace"), end


def read_connect_packet(sock: socket.socket) -> tuple[bytes, ConnectPacketFields]:
    """Read one full CONNECT packet from `sock` and parse its identity fields.

    Args:
        sock: A connected TCP socket, positioned at the start of a new MQTT
            session (nothing read from it yet).

    Returns:
        A tuple of (the exact raw bytes of the packet, as received - to be
        forwarded unmodified to the real broker, parsed identity fields).

    Raises:
        MalformedConnectPacketError: If the first packet isn't a well-formed
            CONNECT packet. `error.raw_so_far` holds whatever bytes were
            already consumed, so the caller can still forward them.
    """
    reader = _TrackingReader(sock)

    first_byte = reader.recv_exact(1)
    if first_byte[0] & CONNECT_PACKET_TYPE_MASK != CONNECT_PACKET_TYPE:
        raise MalformedConnectPacketError(
            f"expected a CONNECT packet (0x10), got packet type 0x{first_byte[0] & CONNECT_PACKET_TYPE_MASK:02x}",
            bytes(reader.raw),
        )

    remaining_length = _read_remaining_length(reader)
    body = reader.recv_exact(remaining_length)
    raw_packet = bytes(reader.raw)

    offset = 0
    _protocol_name, offset = _read_length_prefixed_string(body, offset, raw_packet)
    if offset + 2 > len(body):
        raise MalformedConnectPacketError("truncated protocol level / connect flags", raw_packet)
    connect_flags = body[offset + 1]
    offset += 2  # protocol level (1 byte) + connect flags (1 byte)
    offset += 2  # keep alive (2 bytes)

    client_id, offset = _read_length_prefixed_string(body, offset, raw_packet)

    if connect_flags & WILL_FLAG_BIT:
        _will_topic, offset = _read_length_prefixed_string(body, offset, raw_packet)
        _will_message, offset = _read_length_prefixed_string(body, offset, raw_packet)

    username: str | None = None
    if connect_flags & USERNAME_FLAG_BIT:
        username, offset = _read_length_prefixed_string(body, offset, raw_packet)

    password: str | None = None
    if connect_flags & PASSWORD_FLAG_BIT:
        password, offset = _read_length_prefixed_string(body, offset, raw_packet)

    return raw_packet, ConnectPacketFields(client_id=client_id, username=username, password=password)

"""A blank JPEG of any size, encoded by hand so the bridge needs no imaging library."""

from __future__ import annotations

import struct


def _segment(marker: int, body: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(body) + 2) + body


def blank_jpeg(width: int, height: int) -> bytes:
    """A mid-grey, single-component baseline JPEG of `width` x `height` pixels.

    Every 8x8 block has all-zero coefficients, so with one-symbol Huffman tables (DC category 0
    and AC end-of-block, each coded `0`) each block takes two bits, all zero.
    """
    if width < 1 or height < 1 or width > 0xFFFF or height > 0xFFFF:
        raise ValueError(f"cannot encode a {width}x{height} JPEG")
    blocks = -(-width // 8) * -(-height // 8)
    one_symbol = bytes([1] + [0] * 15)  # one code of length 1
    return b"".join(
        (
            b"\xff\xd8",  # start of image
            _segment(0xDB, b"\x00" + b"\x01" * 64),  # quantisation table 0, all ones
            # baseline frame: 8-bit samples, one component (id 1, no subsampling, table 0)
            _segment(0xC0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"),
            _segment(0xC4, b"\x00" + one_symbol + b"\x00"),  # DC table 0: category 0
            _segment(0xC4, b"\x10" + one_symbol + b"\x00"),  # AC table 0: end of block
            _segment(
                0xDA, b"\x01\x01\x00\x00\x3f\x00"
            ),  # scan: component 1, tables 0/0
            bytes(-(-blocks * 2 // 8)),  # two zero bits per block
            b"\xff\xd9",  # end of image
        )
    )

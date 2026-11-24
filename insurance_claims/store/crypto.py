"""Encryption at rest for the SQL stores (SOP_DATA_KEY): AES-256-GCM per value.

A sealed value is text: `enc1:` + base64(96-bit random nonce + ciphertext + 128-bit tag). The
associated data binds it to its table, its conversation and its row (sequence number or effect
key), so a value copied into another row or conversation does not open. JSON text never starts
with `enc1:`, so plaintext rows written before a key was configured stay readable next to
encrypted ones. A sealed value read without the key, or with another key, is an
EncryptedDataError, never garbage.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any

from .base import EncryptedDataError

PREFIX = "enc1:"
_NONCE_BYTES = 12


class Cipher:
    def __init__(self, key: bytes | None) -> None:
        if key is not None and len(key) != 32:
            raise ValueError("the data key must be 32 bytes")
        self._aead: Any = None
        if key is not None:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # only when a key is set

            self._aead = AESGCM(key)

    @property
    def enabled(self) -> bool:
        return self._aead is not None

    @staticmethod
    def _aad(table: str, session_id: str, row: Any) -> bytes:
        return "\x1f".join(("sop", table, session_id, str(row))).encode("utf-8")

    def seal(self, text: str, table: str, session_id: str, row: Any = "") -> str:
        """The value to store: encrypted when a key is configured, else the text itself."""
        if self._aead is None:
            return text
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, text.encode("utf-8"), self._aad(table, session_id, row))
        return PREFIX + base64.b64encode(nonce + sealed).decode("ascii")

    def open(self, value: str, table: str, session_id: str, row: Any = "") -> str:
        """The stored value as text: decrypted if it was sealed, as it is if it was not."""
        if not value.startswith(PREFIX):
            return value
        where = f"{table} of conversation {session_id[:8]}"
        if self._aead is None:
            raise EncryptedDataError(
                f"the {where} is stored encrypted, but no data key is configured (set SOP_DATA_KEY "
                "to the key it was written with)"
            )
        from cryptography.exceptions import InvalidTag

        try:
            raw = base64.b64decode(value[len(PREFIX):], validate=True)
            plain = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], self._aad(table, session_id, row))
        except (InvalidTag, ValueError, binascii.Error):
            raise EncryptedDataError(
                f"the {where} could not be decrypted: SOP_DATA_KEY is not the key it was written "
                "with, or the stored value was altered"
            ) from None
        return plain.decode("utf-8")

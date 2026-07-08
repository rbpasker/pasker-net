"""Keychain value round-trip: multi-line / non-ASCII secrets survive verbatim.

Background: macOS `security -w` hands back a stored value HEX-ENCODED (not
verbatim) when it contains a newline or non-ASCII byte, silently corrupting a
multi-line secret like a PEM private key. `vault.py` fixes this by storing such
values base64-encoded behind a marker prefix and decoding them on read.

Two layers:
  * pure-function tests of `_encode_for_keychain` / `_decode_from_keychain`
    (no Keychain needed) — including that the stored form is single-line ASCII,
    which is *why* `security -w` no longer hex-mangles it;
  * real-Keychain integration tests (Darwin only, throwaway service, cleaned up
    in tearDown) — the library `KeychainVault` round-trip AND the CLI
    `set --file` → `get` identity, which is the case the fix exists for.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from secretvault.vault import (
    _B64_MARKER,
    _decode_from_keychain,
    _encode_for_keychain,
    _keychain_safe,
    KeychainVault,
)

# Repo root holding the importable `secretvault` package and the CLI entry.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "secretvault" / "__main__.py"

# A realistic multi-line secret (P-256 EC key shape), WITH a trailing newline —
# the exact thing that used to come back hex-encoded.
PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgqJIe11j5GJ2W7/QY\n"
    "51XOWnZEPUPDkNhUlb6IJ9WI0xugCgYIKoZIzj0DAQehRANCAARbaE1I2cW3Ojlg\n"
    "-----END PRIVATE KEY-----\n"
)


class EncodingUnitTests(unittest.TestCase):
    """Pure `_encode`/`_decode` behavior — no Keychain."""

    def test_simple_value_stored_verbatim(self) -> None:
        # Single-line printable ASCII is the common case: no wrapping, so old
        # secrets keep reading back unchanged (backward compatible).
        self.assertTrue(_keychain_safe("hunter2"))
        self.assertEqual(_encode_for_keychain("hunter2"), "hunter2")

    def test_multiline_is_wrapped_single_line_ascii(self) -> None:
        # The whole point: the STORED form has no newline and is ASCII, so
        # `security -w` returns it intact instead of hex-encoding it.
        self.assertFalse(_keychain_safe(PEM))
        stored = _encode_for_keychain(PEM)
        self.assertTrue(stored.startswith(_B64_MARKER))
        self.assertNotIn("\n", stored)
        self.assertTrue(stored.isascii())

    def test_roundtrip_identity_across_shapes(self) -> None:
        # `security -w` appends a trailing newline; model that with + "\n".
        for value in (
            "hunter2",
            PEM,
            "with spaces and = signs +/",
            "  leading-and-trailing-space  ",
            "unicode: café — 世界 🔑",
            "trailing-newline\n",
            "a\tb\tc",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _decode_from_keychain(_encode_for_keychain(value) + "\n"),
                    value,
                )

    def test_marked_but_garbage_falls_back_to_raw(self) -> None:
        # A real value that merely looks like the marker but isn't valid base64
        # must come back verbatim, not raise.
        raw = _B64_MARKER + "!!!not base64!!!"
        self.assertEqual(_decode_from_keychain(raw), raw)


@unittest.skipUnless(sys.platform == "darwin", "Keychain is macOS-only")
class KeychainIntegrationTests(unittest.TestCase):
    """Real `/usr/bin/security` round-trips under a throwaway service."""

    def setUp(self) -> None:
        self.service = f"secretvault-selftest-{os.getpid()}"
        self.vault = KeychainVault(service=self.service)
        self._keys: list[str] = []

    def tearDown(self) -> None:
        for key in self._keys:
            self.vault.delete(key)

    def _key(self, name: str) -> str:
        self._keys.append(name)
        return name

    def test_library_roundtrip_verbatim(self) -> None:
        for name, value in (
            ("SIMPLE", "hunter2"),
            ("PEM_KEY", PEM),
            ("UNICODE", "café — 世界"),
        ):
            with self.subTest(name=name):
                key = self._key(name)
                self.vault.set(key, value)
                self.assertEqual(self.vault.get(key), value)

    def test_cli_set_file_identity(self) -> None:
        # The `--file` path: store a file's contents, read them back through the
        # library, and assert byte-for-byte identity (incl. the PEM's newlines
        # and trailing newline). This is the workflow the feature exists for.
        key = self._key("FROM_FILE")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".pem", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(PEM)
            path = fh.name
        try:
            result = subprocess.run(
                [sys.executable, str(CLI), "--service", self.service,
                 "set", key, "--file", path, "--force"],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("stored", result.stdout)
            self.assertEqual(self.vault.get(key), PEM)
        finally:
            os.unlink(path)

    def test_cli_rejects_value_and_file_together(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "--service", self.service,
             "set", self._key("BOTH"), "somevalue", "--file", "/dev/null"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not both", result.stderr)


if __name__ == "__main__":
    unittest.main()

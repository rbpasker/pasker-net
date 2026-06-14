"""Generic secret vault — macOS Keychain backend with env-var fallback.

Two backends behind a common `SecretVault` interface:

- `KeychainVault` (macOS): shells out to `/usr/bin/security` and stores
  generic passwords under a single service name. All of a consuming
  app's secrets share one service; the secret's "account" name is the
  vault key. Read with `security find-generic-password -s <service>
  -a <key> -w`.

- `EnvironmentVault` (any platform): reads from environment variables
  with an optional prefix. Cannot persist (set/delete raise
  `NotImplementedError`).

Each consuming app picks its own service name (e.g. `"edmund"`,
`"roscoe"`, your project's slug) so secrets don't collide in the
Keychain. `default_vault(service=...)` returns `KeychainVault` on
Darwin, `EnvironmentVault` elsewhere.

Adapted from roscoe-mcp (`common/secretvault/`) and made
config-system-agnostic — no dependency on a particular app's config
loader; consumers compose this however they like.
"""
from __future__ import annotations

import os
import platform
import subprocess
from abc import ABC, abstractmethod


# Sentinel that a higher-level config loader may use to mark a value as
# "look this up in the vault by the same key." Kept here as a public
# constant so callers don't have to hard-code the string.
SECRET_SENTINEL = "SECRET"


class SecretVault(ABC):
    """Abstract interface for secret storage backends."""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Retrieve a secret. Returns None if not found."""

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        """Store a secret. Updates if key already exists."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a secret. Returns True if deleted, False if not found."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """True if the secret is stored."""


class KeychainVault(SecretVault):
    """macOS Keychain backend via `/usr/bin/security`.

    Each instance is bound to one service name; all keys live under that
    service. `security` errors are translated to None / False rather
    than propagating (the CLI exits non-zero on every "not found" path).
    """

    def __init__(self, service: str):
        if not service:
            raise ValueError("KeychainVault requires a non-empty service name")
        self._service = service

    @property
    def service(self) -> str:
        return self._service

    def get(self, key: str) -> str | None:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", self._service, "-a", key, "-w"],
                capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def set(self, key: str, value: str) -> None:
        # `-U` updates in place if the entry exists. We don't pre-delete
        # because that would invalidate any application that's already
        # been granted Keychain access to the entry.
        subprocess.run(
            ["security", "add-generic-password",
             "-s", self._service, "-a", key, "-w", value, "-U"],
            capture_output=True, check=True,
        )

    def delete(self, key: str) -> bool:
        try:
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", self._service, "-a", key],
                capture_output=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def exists(self, key: str) -> bool:
        try:
            subprocess.run(
                ["security", "find-generic-password",
                 "-s", self._service, "-a", key],
                capture_output=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def __repr__(self) -> str:
        return f"KeychainVault(service={self._service!r})"


class EnvironmentVault(SecretVault):
    """Environment-variable backend with optional key prefix.

    Read-only by design — env vars are process-scoped and can't be
    persisted from inside the process. set / delete raise
    `NotImplementedError`; the operator is expected to manage env vars
    through whatever mechanism set them in the first place (.env file,
    shell rc, container manifest, etc.).
    """

    def __init__(self, prefix: str = ""):
        self._prefix = prefix

    @property
    def prefix(self) -> str:
        return self._prefix

    def get(self, key: str) -> str | None:
        return os.environ.get(f"{self._prefix}{key}")

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError(
            "EnvironmentVault is read-only; set the env var through your "
            "shell / .env / container manifest."
        )

    def delete(self, key: str) -> bool:
        raise NotImplementedError(
            "EnvironmentVault is read-only; unset the env var through your "
            "shell / .env / container manifest."
        )

    def exists(self, key: str) -> bool:
        return f"{self._prefix}{key}" in os.environ

    def __repr__(self) -> str:
        return f"EnvironmentVault(prefix={self._prefix!r})"


def default_vault(service: str, *, env_prefix: str = "") -> SecretVault:
    """Return KeychainVault on macOS, EnvironmentVault elsewhere.

    `service` is required for KeychainVault (each app should pass its
    own service slug so secrets are namespaced cleanly). `env_prefix`
    applies only on the env fallback.
    """
    if platform.system() == "Darwin":
        return KeychainVault(service=service)
    return EnvironmentVault(prefix=env_prefix)

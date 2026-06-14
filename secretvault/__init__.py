"""secretvault — Keychain-backed secret storage with env-var fallback.

Public surface:

    from secretvault import (
        SecretVault,
        KeychainVault,
        EnvironmentVault,
        default_vault,
        SECRET_SENTINEL,
    )

See README.md for usage; vault.py for the actual implementation.
"""
from .vault import (
    SECRET_SENTINEL,
    EnvironmentVault,
    KeychainVault,
    SecretVault,
    default_vault,
)

__all__ = [
    "SECRET_SENTINEL",
    "EnvironmentVault",
    "KeychainVault",
    "SecretVault",
    "default_vault",
]

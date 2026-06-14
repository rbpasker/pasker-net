# pasker-net

Shared utility modules for personal projects.

## Modules

- [`secretvault/`](secretvault/) — macOS Keychain-backed secret storage with
  env-var fallback. Pluggable backends, namespace-per-app via Keychain service
  name. Adapted from roscoe-mcp.

## Use as a git submodule

In the consuming repo:

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

Then make Python able to find it — either add `vendor/pasker-net/` to
`sys.path` early in your app's entrypoint, or symlink the module under
your project.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from secretvault import default_vault, SECRET_SENTINEL
```

To sync to the latest pasker-net commit:

```bash
git submodule update --remote vendor/pasker-net
git add vendor/pasker-net
git commit -m "bump pasker-net submodule"
```

## secretvault quick start

```python
from secretvault import default_vault

vault = default_vault(service="myapp")
api_key = vault.get("ANTHROPIC_API_KEY")
if api_key is None:
    raise SystemExit(
        "ANTHROPIC_API_KEY not in keychain. Set it with:\n"
        "  python -m secretvault --service myapp set ANTHROPIC_API_KEY"
    )
```

CLI:

```bash
python -m secretvault --service myapp set ANTHROPIC_API_KEY     # prompts (hidden)
python -m secretvault --service myapp get ANTHROPIC_API_KEY     # prints to stdout
python -m secretvault --service myapp exists ANTHROPIC_API_KEY  # exit 0 if present
python -m secretvault --service myapp delete ANTHROPIC_API_KEY
python -m secretvault --service myapp list                       # all under service
```

`default_vault()` returns `KeychainVault` on macOS, `EnvironmentVault`
elsewhere. Service name namespaces the secrets — each consuming app
passes its own (`"edmund"`, `"roscoe"`, etc.) so keys don't collide.

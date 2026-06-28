# pasker-net — shared, vendored library

This package is a **shared library** vendored into multiple consumers as a git
submodule (e.g. `edmund/vendor/pasker-net`, `roscoe-mcp/common/...`, alfred).
Any consumer may need to change it, and different consumers may be pinned to
**different commits**.

## Rules for changes (read before editing)

- **Changes belong upstream, via PR.** If you change this package from inside a
  consumer's vendored copy, you MUST offer to push the change back to the
  canonical repo (`github.com/rbpasker/pasker-net`): open a branch and a PR
  (direct pushes to `main` are blocked), then bump the consumer's submodule
  pointer to the merged commit. Never leave a vendored copy silently diverged
  from origin — that's invisible to every other consumer.

- **A vendored copy may be pinned to an OLD commit.** Before concluding a
  feature is missing or broken, `git fetch` the submodule and check the
  canonical repo. The fix is often a submodule *bump*, not new code — don't
  reimplement something that already exists upstream.

- **Do not make out-of-band edits in a separate development-root clone of
  pasker-net.** Uncommitted/unpushed changes there are invisible to consumers
  and cause the same silent divergence. Make the change where you are working
  (the vendored copy), push it back via PR, then bump everywhere.

- **Keep it dependency-light and self-contained.** Consumers vendor this to
  avoid a package install; the macOS Keychain backend only needs
  `/usr/bin/security`. Don't add runtime dependencies casually.

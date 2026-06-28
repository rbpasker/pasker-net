# pasker-net — shared, vendored library

This package is a **shared library** vendored into multiple consumers as a git
submodule (e.g. `edmund/vendor/pasker-net`, `roscoe-mcp/common/...`, alfred).
Any consumer may need to change it, and different consumers may be pinned to
**different commits**.

## Where you are decides how you land a change

- **In this canonical `pasker-net` repo (the development directory): commit
  straight to `main`.** No PR needed. Commit and push directly. (Only commit or
  push when explicitly asked.)

- **From inside a consumer's vendored copy: submit a PR.** If you change this
  package from a consumer's vendored submodule, push the change back to the
  canonical repo (`github.com/rbpasker/pasker-net`) via a branch and PR, then
  bump the consumer's submodule pointer to the merged commit. Never leave a
  vendored copy silently diverged from origin — that's invisible to every other
  consumer.

## Other rules

- **A vendored copy may be pinned to an OLD commit.** Before concluding a
  feature is missing or broken, `git fetch` the submodule and check the
  canonical repo. The fix is often a submodule *bump*, not new code — don't
  reimplement something that already exists upstream.

- **Don't leave changes uncommitted/unpushed.** The hazard is silent divergence:
  edits that exist only in a working tree are invisible to consumers. Land the
  change where you're working — commit+push here, or PR+bump from a vendored
  copy.

- **Keep it dependency-light and self-contained.** Consumers vendor this to
  avoid a package install; the macOS Keychain backend only needs
  `/usr/bin/security`. Don't add runtime dependencies casually.

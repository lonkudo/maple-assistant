# AGENTS.md — how to work in this repository

## Testing: do not run redundant unit tests

**Do not run unit tests unless the operator explicitly asks for a unit test.**
This is a standing instruction, not a suggestion.

* A change is normally validated by the operator's field run, not by a test suite.
* Never re-run a suite that already passed in the same session.
* Never re-run tests for a second iteration of the same simple change (a constant, a
  timing, a click order, a log line, a wording fix).
* Never run the whole discovery set as a routine step.
* If a test file was edited in the same change, running **only the affected test
  cases once** is allowed — that is the one exception, and it is one run, not a loop.
* Documentation-only edits need no tests and no release ZIP.

The full instruction from the operator:

> don't do redundant unit test, unless i told that unit test is needed

## Releases

Every behaviour change ships as a new numbered ZIP:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

`release_now.ps1` advances the semantic `VERSION` (`X.Y.Z`, patch by default), builds
the normal package, writes `release/MoonEclipse-<version>.zip`, and removes the
previous ZIPs. The standalone package is then built from that staged package with
the same version:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_protected_release.ps1 -Version X.Y.Z
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\build_installer.ps1
```

`-SkipTests` is the normal mode: the tests are not the gate, the field run is.
Build a release only when the operator asks for it.

## Push flow: two repositories, two remotes each

This workspace holds **two Git repositories**, and each one has **two GitHub
remotes**. A change is delivered only when **all four** remote branches carry it.
Pushing just one of them has already left the canonical copy a release behind.

| Repository | Working copy | Remotes | Key |
| --- | --- | --- | --- |
| Client | this workspace root | `forbestop22/maple-assistant`, `lonkudo/maple-assistant` | see below |
| Server | `activation_service/` (its own `.git`) | `forbestop22/maple-assistant-server`, `lonkudo/maple-assistant-server` | see below |

* `~/.ssh/forbestop22_key` pushes the `forbestop22` mirror of **both** repositories.
* `~/.ssh/lonkudo_key` pushes the `lonkudo` (canonical) copy of **both** repositories.
* Select the key explicitly every time — a default identity is never assumed:

```powershell
$keyDir = ($env:USERPROFILE -replace '\\','/') + "/.ssh"      # forward slashes only
$env:GIT_SSH_COMMAND = "ssh -i `"$keyDir/lonkudo_key`" -o IdentitiesOnly=yes"
```

* Do **not** use `git push origin`: the client's `origin` has two push URLs that need
  different keys in one command, and the server's `origin` is HTTPS (no key). Push the
  explicit SSH URL per key:

```powershell
git -C <repo> push git@github.com:lonkudo/maple-assistant.git main
git -C <repo> push git@github.com:forbestop22/maple-assistant.git main
git -C <repo> push git@github.com:lonkudo/maple-assistant-server.git main
git -C <repo> push git@github.com:forbestop22/maple-assistant-server.git main
```

* Server first when the released client depends on a new endpoint; release tags
  (`release/vX.Y.Z`) go to both client remotes, naming the single tag:
  `git push <remote> refs/tags/release/vX.Y.Z`. Never `git push <remote> --tags` —
  it walks every historical tag and their LFS objects and can hang for minutes.
* Verify, do not assume: `git ls-remote <url> refs/heads/main` must equal
  `git rev-parse HEAD` for all four. The runnable snippet is in `README.md`
  ("Push flow: two repositories × two remotes").
* The server repository ignores `operator_console/node_modules/` and
  `static/operator_console/`; the console bundle is built **on the server**
  (`npm ci && npm run build`), never shipped through Git.

## Documentation

Do not edit `README.md`, `ARCHITECTURE.md`, or other documentation unless the operator
asks for it in that message. Commit or push only when asked.

## Vendor code

`autolie_api/` is vendor reference material: never modify anything inside it.

## Images

Images in this project are JPG. Do not add PNG files (a test in `test_image_io.py`
checks that no PNG is left in `screenshots/`).

## Reporting

Report what changed and how it was verified, then state the release ZIP path. Do not
claim a field result that only the operator can observe; say what was measured and
what remains unverified.

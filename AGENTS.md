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
the single `release/MapleAssistant` package, writes
`release/MapleAssistant-<version>.zip`, and removes the previous ZIP. `-SkipTests` is
the normal mode: the tests are not the gate, the field run is.

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

# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`scout` is a search CLI built because the built-in `WebSearch` tool is blocked
on the target machine by a GCP organization policy
(`constraints/vertexai.allowedPartnerModelFeatures`), which applies to every
Claude partner model — switching models does not help.

## The invariant

**Scout must never return an empty or wrong result set without saying why.**

Every change should be checked against that sentence. Concretely:

- Every engine consulted appends an `Attempt`, success or failure. Results
  empty *and* `attempts` empty is a bug, not an outcome.
- `EngineFailure` carries a status, not just a message. Never collapse
  `empty` into `rate_limited` or vice versa — they call for opposite responses.
- Exit code `1` (searched fine, found nothing) and `2` (every source failed)
  are different facts. Keep them apart.
- `detect_low_relevance` is biased toward false positives on purpose. Do not
  "fix" that.

## Non-obvious constraints, all measured on the target network

These are the reasons behind code that otherwise looks paranoid.

**Do not add an HTTP-only path for `brave` or `startpage`.** Plain HTTP gets
429 from Brave at the same moment Playwright succeeds on the identical URL.
An engine list that claims coverage without a browser is claiming coverage it
does not have.

**Do not add Bing.** It answers blocked requests with HTTP 200 and a page of
unrelated results rather than an error. This is what `low_relevance` exists to
catch, and it is not worth inviting in.

**Do not add Google directly, or DuckDuckGo, or Reddit.** The corporate NAT
egress IP is banned network-wide by Google (`/sorry/index`); DuckDuckGo and
Reddit are blocked at the network layer. Startpage reaches the Google index
from its own infrastructure and does work.

**Do not replace `truststore` with `verify=False`.** They treat the same
symptom and are not equivalent: `truststore` verifies against the OS trust
store, which is where the corporate proxy CA lives. Verification stays on.

**`browser_ignore_cert_errors` is different** — it genuinely disables
verification, for the browser only, and only because Chromium does not read
the OS trust store. Keep it configurable and keep the trade-off documented
where a user will see it.

**Use the Playwright Python bindings, not `playwright-cli`.** The CLI is a
stateful interactive driver (`open` / `goto` / `click e15` / `snapshot`) built
for step-by-step agent use. Scripted scraping through it pays a process
round-trip per step.

## Layout

```
src/scout/
  cli.py         argument parsing, subcommand dispatch, exit codes
  search.py      the fallback chain — where the audit invariant lives
  browser.py     Playwright lifecycle; one Chromium for the whole chain
  config.py      ~/.config/scout/config.toml
  doctor.py      self-check; required failure ⇒ non-zero exit
  net.py         requests session with truststore + proxy
  skill.py       dual-branch skill lookup
  errors.py      exceptions carrying their exit code
  output.py      JSON and pretty rendering
  engines/
    base.py      Engine protocol, Result, Attempt, EngineFailure, relevance
    brave.py  startpage.py     browser engines
    marginalia.py              plain HTTP
    verticals.py               seven keyless APIs
skills/scout/SKILL.md          force-included into the wheel
```

## Conventions

Borrowed from `capital-stack`, which established the "uv tool + bundled skill"
pattern this repo copies:

- Python 3.13, src layout, hatchling.
- ruff with `extend-select = ["ALL"]`, line length 120.
- `run()` converts `ScoutError` to an exit code and lets real bugs keep their
  traceback. Do not widen that catch.
- The skill ships inside the wheel via `[tool.hatch.build.targets.wheel.force-include]`,
  so `uv tool install` alone suffices. `locate_skill` has a source branch and a
  packaged branch — **both are hot paths**, test both.

## Testing

Engine parsers are split from their network calls (`marginalia.parse`, the
`_EXTRACT` scripts) so they can be tested against saved fixtures offline. Add
a fixture rather than a live call when adding an engine.

```bash
just check      # ruff + pytest
uv run scout doctor --offline
```

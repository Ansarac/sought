# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`sought` is a general-purpose web search CLI. Its distinguishing property is
not coverage — it is that every search carries an explicit audit trail of the
engines consulted and what each one did. That is useful anywhere, and it is
what makes the tool safe to put behind an agent.

It is also the fallback when the built-in `WebSearch` tool is unavailable:
some environments disable it by platform policy, and because the policy applies
at the platform level, switching models does not help.

## The invariant

**Sought must never return an empty or wrong result set without saying why.**

Every change should be checked against that sentence. Concretely:

- Every engine consulted appends an `Attempt`, success or failure. Results
  empty *and* `attempts` empty is a bug, not an outcome.
- `EngineFailure` carries a status, not just a message. Never collapse
  `empty` into `rate_limited` or vice versa — they call for opposite responses.
- Exit code `1` (searched fine, found nothing) and `2` (every source failed)
  are different facts. Keep them apart.
- `empty` means the engine said "nothing matched". A page sought does not
  understand — no results container, no known wall — is `error`. Measured:
  Brave moved its results out of `#results` and nineteen results were
  reported as `empty`. Never default an unrecognised page to `empty`.
- `detect_low_relevance` is biased toward false positives on purpose. Do not
  "fix" that.

## Non-obvious constraints, all measured

These are observations from running the tool against real networks, including
ones with a TLS-inspecting proxy and a shared NAT egress IP. They are the
reasons behind code that otherwise looks paranoid. None of them are hypotheses.

**Do not add an HTTP-only path for `brave` or `startpage`.** Plain HTTP gets
429 from Brave at the same moment Playwright succeeds on the identical URL.
An engine list that claims coverage without a browser is claiming coverage it
does not have.

**Do not add Bing.** It answers blocked requests with HTTP 200 and a page of
unrelated results rather than an error. This is what `low_relevance` exists to
catch, and it is not worth inviting in.

**Google exists only as an opt-in engine, by the owner's decision. Do not
widen it, and do not add DuckDuckGo or Reddit.** Google refuses any automated
browser per request (`/sorry/index`): measured over one shared datacenter
egress IP, a real browser with no cookies got results while every
Playwright-launched Chromium was refused, and adding only `--enable-automation`
to the real browser was enough to be refused too — the trigger is
`navigator.webdriver`, not the IP (`docs/findings/2026-10-egress-google-windows.md`).
Hiding that flag (`--disable-blink-features=AutomationControlled`, and a
context without a fixed viewport) is fingerprint evasion against Google's
automated-query policy. The repository owner decided to offer it anyway, as
`engines/google.py`, and the conditions are the decision — keep every one:

- Off by default (`[google] enabled = false`) and never in `DEFAULT_ORDER`.
  Asked for while disabled, it records an `error` attempt naming the key to
  set; it is never skipped silently.
- A local hourly budget (`google.max_per_hour`, default 100; `state.budget`)
  is checked before Chromium launches, and a skip is `rate_limited` "... —
  not asked" that starts no cooldown. Heavy automated use from one shared NAT
  or datacenter IP can make Google block the IP for everyone behind it.
  `--clear-state` does not reset it.
- Its own Chromium (`browser_session(..., hide_automation=True)`), launched
  only when google is actually asked. `brave` and `startpage` launch exactly
  as they would without it; the explicit-proxy rule below applies to both.
- Every `ok` attempt discloses it: `automation flag hidden (google.enabled)`.

Measured with sought's own launch path, headless, flag hidden: three
requests, three results pages. The organic links are `google.com/goto?url=<token>`
encrypted redirects; the destination URL is nowhere in the page, scripts
included. Measured: a cookieless GET to a token from another process got a
302 to the real destination, so `google.resolve_redirects` resolves them (one
request per result to `/goto`, redirects not followed, stops at `/sorry/`);
any that fail stay as redirects and the `ok` detail counts them. Never
reconstruct a URL from the breadcrumb. A client that runs no JavaScript gets
an HTTP 200 shell with no results, so a Google page without results is
`empty` only when it says "did not match any documents" — otherwise `error`.
DuckDuckGo and Reddit are commonly blocked at the network layer. Startpage
reaches the Google index with none of this and stays in the default chain.

**Do not replace `truststore` with `verify=False`.** They treat the same
symptom and are not equivalent: `truststore` verifies against the OS trust
store, which is where a locally installed proxy CA lives. Verification stays
on.

**`browser_ignore_cert_errors` is different** — it genuinely disables
verification, for the browser only, and only because Chromium does not read
the OS trust store. It defaults to `false`. Keep it configurable and keep the
trade-off documented where a user will see it.

**Do not present that flag as the fix for a TLS-inspecting proxy.** Chromium
on Linux reads a per-user NSS database at `~/.pki/nssdb`, which does not exist
until something creates it — the OS-installed proxy CA is unseen because
nobody put it where the browser looks, not because the browser cannot verify.
Measured: with the database absent, `brave` fails `ERR_CERT_AUTHORITY_INVALID`;
after `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n proxy-ca -i <root>.crt`,
`brave` and `startpage` both succeed with `browser_ignore_cert_errors = false`.
Import the root of the chain, not the intermediate. `doctor` recommends this
route first and the flag only as a fallback; keep that ordering.

**The NSS remedy is Linux-only; never print it elsewhere.** On Windows,
Chromium verifies against the Windows certificate store — measured behind a
TLS-inspecting proxy with its CA in that store: no certificate error, flag
off — and Windows' own `certutil` is an unrelated program that the NSS command
line breaks. `doctor._ca_import_hint` branches on the platform.

**On Windows, `requests` cannot follow a PAC script and Chromium does.** With
no proxy in the config or environment, Chromium uses the WinINET settings,
PAC included; `urllib.request.getproxies` reads only a manual `ProxyServer`
(and only with `ProxyEnable` on). Measured on a PAC-only machine: browser
engines via the proxy, `requests` direct. `net.system_proxy` exists so
`doctor` can say so instead of "all engines connect directly".

**Output must survive a non-UTF-8 code page.** A redirected stdout on Windows
is encoded with the legacy code page; a `✓` crashed `--pretty` and `doctor`
with exit 1, which reads as "found nothing". `cli.ensure_utf8` switches the
streams to UTF-8 at the entry point.

**Always hand Chromium its proxy explicitly, even one inherited from the
environment.** Chromium on Linux reads `HTTPS_PROXY` only outside a desktop
session; under GNOME it takes gsettings instead. Measured on a GNOME machine
with gsettings on `auto`, no PAC URL and a Kerberos firewall on direct egress:
`requests` used the env proxy and succeeded, Chromium went direct and got
HTTP 401 on roughly half its navigations (`blocked — HTTP 401` from both
browser engines). `net.environment_proxy` closes that gap.

**Use the Playwright Python bindings, not `playwright-cli`.** The CLI is a
stateful interactive driver (`open` / `goto` / `click e15` / `snapshot`) built
for step-by-step agent use. Scripted scraping through it pays a process
round-trip per step.

**A detected bot wall is a status, not an obstacle to route around.**
`BOT_WALL_MARKERS` in `browser.py` turns the wall into a `rate_limited`
attempt and the chain moves to the next engine. No retry loops, no reloads and
no CAPTCHA solving, for any engine. No fingerprint spoofing or stealth for
`brave`, `startpage` or `marginalia` — the audit trail is the product, and
recording the refusal is the correct behaviour. The one exception to "no
spoofing" is the opt-in `google` engine above: one flag, hidden only in its
own browser, only when the user switches it on, disclosed on every answer. It
gets no retries either; its `/sorry` page is a `rate_limited` attempt and a
cooldown like any other wall.

**Waiting out a site's own JavaScript proof-of-work is not solving it.**
Startpage often answers first with "Verifying your request... Calculating...
Difficulty: 6": a script the page runs in every visitor's browser, which then
moves on to the results by itself. sought's browser runs it unmodified; sought
only waits (`engines.challenge_wait_seconds`, default 60, counted from
arrival). Measured headless with nothing spoofed: results after 39 s, where
the old 15 s selector timeout had reported `rate_limited — bot wall` for a
refusal nobody made. One navigation, then patience — no reload, no
re-request. Still showing at the end of the allowance, it is `rate_limited`
(`proof-of-work challenge not completed within 60s`); passed, the `ok`
attempt says so (`passed proof-of-work challenge in 39s`). Anything that
wants a human, a CAPTCHA above all, stays a wall.

## Layout

```
src/sought/
  cli.py         argument parsing, subcommand dispatch, exit codes
  search.py      the fallback chain — where the audit invariant lives
  browser.py     Playwright lifecycle; one Chromium for the chain, a separate one for google
  config.py      ~/.config/sought/config.toml
  doctor.py      self-check; required failure ⇒ non-zero exit
  net.py         requests session with truststore + proxy
  state.py       ~/.cache/sought: cooldowns, short-lived result cache, google's hourly budget
  skill.py       dual-branch skill lookup
  errors.py      exceptions carrying their exit code
  output.py      JSON and pretty rendering
  engines/
    base.py      Engine protocol, Result, ResultList, Attempt, EngineFailure, relevance
    brave.py  startpage.py     browser engines, sharing the chain's Chromium
    google.py                  opt-in, off by default: own Chromium, flag hidden, budgeted
    marginalia.py              plain HTTP
    verticals.py               seven keyless APIs
skills/sought/SKILL.md         force-included into the wheel
```

## Conventions

The "uv tool + bundled skill" packaging pattern is the load-bearing one; the
rest is ordinary:

- Python 3.13, src layout, hatchling.
- ruff with `extend-select = ["ALL"]`, line length 120.
- `run()` converts `SoughtError` to an exit code and lets real bugs keep their
  traceback. Do not widen that catch.
- The skill ships inside the wheel via `[tool.hatch.build.targets.wheel.force-include]`,
  so `uv tool install` alone suffices. `locate_skill` has a source branch and a
  packaged branch — **both are hot paths**, test both.

## Testing

Engine parsers are split from their network calls (`marginalia.parse`,
`google.parse`, the `_EXTRACT` scripts) so they can be tested against saved
fixtures offline. `google.parse` reads the browser's own serialization of the
page, so its fixtures (`tests/fixtures/google_results*.html`, sanitised live
captures) are exactly what runs. Add a fixture rather than a live call when
adding an engine, and strip anything identifying from it (session tokens,
tracking attributes, the footer's location, the egress IP).

```bash
just check      # ruff + pytest
uv run sought doctor --offline
```

To isolate sought's state for a manual run, point `XDG_CACHE_HOME` at a temp
dir — and set `PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright` with it:
Playwright derives its browser path from `XDG_CACHE_HOME` too, and otherwise
reports Chromium missing.

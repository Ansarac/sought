# sought

A search CLI that never fails silently. It degrades across independent engines
and tells you exactly what it tried.

The design goal is not breadth of coverage. It is that **sought never quietly
returns a wrong or empty answer**. Every search comes back with an audit trail:
which engines were asked, in what order, and what each one did. That matters
most when something else is consuming the output — an agent, a script, a
pipeline — and has no way to eyeball a results page and notice it looks wrong.

## Why it works this way

Three findings shaped the design. All were measured, and the first and third
apply on any network at all; the second shows up wherever traffic leaves
through a shared egress IP or a filtering proxy.

**1. Fetching a search results page is worse than useless.** Bing does not
block scrapers; it poisons them. Three consecutive queries fetched as plain
HTML returned a supermarket homepage and a webmail help page — HTTP 200,
well-formed, no error of any kind. A tool that fails this way is more dangerous
than one that fails loudly, because nothing downstream can tell.

**2. Plain HTTP clients get refused; a real browser does not.** At the same
moment, `curl https://search.brave.com/...` returned 429 while Playwright
loading the identical URL returned results. Any scraper built on an HTTP
client alone is not usable in those conditions, whatever its engine list
claims.

**3. Relevance scores can launder garbage.** One tool under evaluation ran the
poisoned results through an ML reranker and assigned the unrelated pages a
relevance score of 0.38. A number next to a wrong answer is worse than no
number.

So sought uses a real browser where one is required, keeps a full audit trail
of every engine it consulted, and flags results that do not mention the query.

## Install

```bash
uv tool install git+https://github.com/Ansarac/sought
```

Or from a checkout:

```bash
git clone https://github.com/Ansarac/sought
cd sought
uv tool install .
```

Then:

```bash
sought doctor --fix          # installs chromium if missing
sought install-skill         # optional: install the Claude skill
```

## Use

```bash
sought "playwright bypass cloudflare"           # JSON (default)
sought "rust async runtimes" --pretty           # table
sought "MCP web search" --source hn             # one vertical source
sought "playwright" --source npm --limit 5
sought "playwright bypass cloudflare" --fresh   # skip the result cache for this search
```

Output is JSON by default because the envelope carries the audit trail:

```json
{
  "query": "anthropic claude pricing",
  "engine": "startpage",
  "cached": null,
  "attempts": [
    {"engine": "brave", "status": "rate_limited", "detail": "bot wall: \"verifying you're not a bot\""},
    {"engine": "startpage", "status": "ok", "count": 8}
  ],
  "low_relevance": false,
  "results": [{"title": "...", "url": "...", "snippet": "..."}]
}
```

`attempts` is the point of the whole tool. An empty `results` list next to a
populated `attempts` list is a real answer — it says *why*. An empty `results`
list next to an empty `attempts` list is a bug.

### Statuses

| status | meaning |
|---|---|
| `ok` | answered |
| `empty` | answered, matched nothing |
| `rate_limited` | quota exhausted or bot wall |
| `blocked` | the network or the engine refused |
| `timeout` | no response within the timeout |
| `error` | unexpected failure |

`empty` and `rate_limited` are never collapsed into "no results". They call for
opposite responses: reword the query, versus wait or use another engine.

### Cooldowns and the result cache

Both reduce the load sought itself puts on the engines. Neither removes
anything from the audit trail.

**Cooldowns.** When `brave`, `startpage`, `marginalia` or (if enabled)
`google` answers `rate_limited`, sought leaves that engine alone for
`engines.cooldown_seconds`
(default 600). Asking again seconds after a refusal does not get an answer —
on a shared egress IP it prolongs the ban for everyone behind it, and each
refused browser engine costs a selector timeout besides. A skipped engine is
still recorded, as `rate_limited` with a detail that says it was not asked:

```json
{"engine": "brave", "status": "rate_limited",
 "detail": "cooling down until 14:20, not asked this time (last refusal at 14:10: HTTP 429)"}
```

Only `rate_limited` starts a cooldown; `blocked` is usually a local fault
being fixed, and `timeout`, `empty` and `error` are not refusals. An `ok`
clears it. If every browser engine is cooling down, Chromium is not launched.
A skip counts as a failure for the exit code, exactly like a live 429.
`doctor` lists active cooldowns (offline too) and does not probe a cooling
engine.

**Result cache.** A search that returned results (and was not flagged
`low_relevance`) is replayed for `cache.ttl_seconds` (default 900) when the
same query — compared ignoring case and whitespace — is asked again with the
same source, engine order and limit. `cached` is `null` on a fresh search and
`{"age_seconds": N}` on a replay, whose `attempts` are the original search's;
`--pretty` prints `(cached, 3m old — use --fresh to search again)`.

`--fresh` skips the lookup and asks the engines (and caches the new result).
It does not override cooldowns — bypassing one is exactly the hammering it
exists to stop. `doctor` never reads or writes the cache: a replayed probe
would report an engine as working without having asked it.

State lives in `${XDG_CACHE_HOME:-~/.cache}/sought/` and is disposable; a
missing or corrupt file just means no cooldowns and a cache miss. Once you
have fixed what caused a refusal, `sought config --clear-state` ends every
cooldown and drops the cache. It deletes only the files sought writes there,
and says what it removed.

### `low_relevance`

Set when even the best single result fails to cover the query. Each result is
scored on its own — whole-token matches, weighted so a rare term counts for
more than a common one, with the URL given partial credit — and the flag is
raised when the strongest of them still falls short.

Scoring per result rather than across the set is the load-bearing part. A
union check ("did *any* result mention *any* token?") is disarmed by a single
incidental hit: the nonsense query `zqxjkvwmpf blorptangle 77 unfindable` went
unflagged because one title happened to contain "unfindable" while both
distinctive tokens matched nothing.

This is the only available signal against a poisoned results page, since such
a page arrives as a valid HTTP 200. It is deliberately biased toward false
positives — a spurious warning costs a second glance, a missed one costs a
wrong answer. A query of pure nonsense is always flagged, whatever the engine
returns.

### Exit codes

`0` results · `1` searched fine, found nothing · `2` every source failed ·
`130` interrupted.

`1` and `2` differ on purpose: one is an answer, the other is a malfunction.

## Sources

**General chain** (default, tried in order, no `--source` needed):

| engine | index | transport | notes |
|---|---|---|---|
| `brave` | Brave | Chromium | best general quality; rate limits after sustained use |
| `startpage` | Google | Chromium | Google's index without querying Google directly |
| `marginalia` | independent | plain HTTP | no browser needed; favours small non-commercial sites |

The three use different networks and different quotas, so one being rate
limited says nothing about the next.

**Vertical sources** (`--source`, keyless, ~1s, no fallback):

`wikipedia` · `github` · `hn` · `stackoverflow` · `npm` · `pypi` · `news`

`--source` disables the chain on purpose: an explicit choice should not be
silently swapped for something else.

`pypi` resolves an exact package name only — PyPI removed its full-text search
API, and sought says so rather than returning nothing.

`news` searches the Google News RSS feed, whose links are
`news.google.com/rss/articles/...` redirects that only a browser running
JavaScript can follow. sought decodes each one to the publisher's URL through
the same undocumented endpoint Google News' own page calls — two requests to
news.google.com per result, best effort. A link it cannot decode stays a
redirect, and the `ok` attempt says how many did (`3 of 10 article URLs left
as news.google.com redirects`); a 429 or a bot wall stops the decoding for the
rest of that search rather than asking again. That makes `news` the one
vertical that takes a few seconds rather than one: about 8 s for 10 results,
measured.

## Google (opt-in)

There is a fourth general engine, `google`, and it is **off by default**. It
exists because its owner decided to offer it; read this before switching it on.

Google refuses automated browsers per request. Measured over a shared
datacenter egress IP: a real browser with no cookies got results, every
Playwright-launched Chromium was sent to `/sorry/`, and the trigger was
`navigator.webdriver` — not the IP, not the user agent
([findings](docs/findings/2026-10-egress-google-windows.md), §3–4). The
`google` engine launches its own headless Chromium with
`--disable-blink-features=AutomationControlled`, which hides that flag. That
is fingerprint evasion against Google's policy on automated queries. It works
today; Google can close the gap any day, and the engine will then report
`rate_limited`.

**The risk is not only yours.** Heavy automated use from one NAT or
datacenter egress IP can make Google escalate from refusing one browser to
blocking the IP — for everyone behind it. So the engine has a local budget,
`max_per_hour` (default 100), a sliding hour checked before Chromium is even
launched; once it is spent, google appears in the audit trail as
`rate_limited` with `local budget 100/h used, next slot at 14:32 — not asked`.
A real `/sorry` page is `rate_limited` too, and starts the usual cooldown.
`sought config --clear-state` does not reset the budget.

To use it, set `enabled = true` **and** either add `"google"` to
`engines.order` or ask for it explicitly:

```toml
[google]
enabled = true
max_per_hour = 100

[engines]
order = ["brave", "startpage", "google", "marginalia"]   # optional
```

```bash
sought "rust tokio select macro" --source google
```

Asked for while disabled, it is recorded rather than skipped silently, and
the detail names the key to set:

```json
{"engine": "google", "status": "error",
 "detail": "disabled — google is opt-in; set `enabled = true` under [google] in the config file (...)"}
```

What changes when it is on, and what does not:

- Only google's own browser hides the flag. `brave` and `startpage` launch
  exactly as before, and google's Chromium is launched only when google is
  actually asked (enabled, not cooling down, within budget).
- Every answer says what was done:
  `{"engine": "google", "status": "ok", "count": 9, "detail": "automation flag hidden (google.enabled)"}`.
- **Result links are resolved for you.** Google now serves organic results as
  `https://www.google.com/goto?url=<token>`, an encrypted token; the
  destination URL is nowhere in the page. sought asks each token where it
  leads (one small request to `/goto` per result, never to `/search`, not
  counted against the budget) and reports the real URL. A token that does not
  resolve stays a working `/goto` link, and the detail says how many, e.g.
  `…; 2 of 9 result URLs left as google.com/goto redirects`.
- A Google page without results is `empty` only when Google says "did not
  match any documents". Anything else it did not recognise is `error`, never
  a claim that the web has no answer.
- `doctor` reports google on every run: `disabled (opt-in: [google] enabled =
  true)`, or, when enabled, a live probe — which spends one slot of the
  budget, and says so — followed by what is left.

## Terms of use

`sought` drives the Brave and Startpage result pages through a real browser,
and, if you enable it, Google's. All three services' terms restrict automated
querying of their result pages; Brave offers an official Search API for
programmatic access, which is the supported route for anything beyond
occasional interactive use. Marginalia is an
independent search engine and publishes an API of its own. The vertical sources
are documented public APIs. You are responsible for your own compliance with
whichever services you enable.

Worth stating plainly, because it is a design decision and not an oversight:
when sought hits a bot wall it does not try to get past it. The markers in
`BOT_WALL_MARKERS` (`src/sought/browser.py`) turn the wall into a
`rate_limited` attempt in the audit trail and the chain falls through to the
next engine. There is no retry loop and no CAPTCHA solving, for any engine,
and no fingerprint spoofing for the engines on by default. A refusal is a
result like any other, and recording it is more useful than defeating it. The
single exception is the opt-in `google` engine above, which hides one
automation flag in its own browser when — and only when — you switch it on,
and says so on every answer.

One page is not a refusal, and sought waits for it instead. Startpage often
answers first with "Verifying your request... Calculating... Difficulty: 6" —
a JavaScript proof-of-work the page runs in every visitor's browser before it
moves on to the results by itself. sought's browser runs it like any other,
unmodified; sought only waits, for up to `engines.challenge_wait_seconds`
(default 60) after one navigation, with no reload and no second request.
Measured headless: the results appeared after 39 s. The wait is on the
record either way — `ok` with `"detail": "passed proof-of-work challenge in
39s"`, or `rate_limited` with `"proof-of-work challenge not completed within
60s"` — so a slow search is explained rather than mysterious.

## Configuration

`~/.config/sought/config.toml`. Precedence: **CLI flags > config file >
environment > builtin defaults**.

```bash
sought config --init         # write an annotated default
sought config --path         # print the path
sought config                # print current contents
sought config --clear-state  # end cooldowns, drop cached results
```

```toml
[proxy]
# Empty inherits HTTP_PROXY / HTTPS_PROXY / NO_PROXY.
http  = ""
https = ""
no_proxy = ["localhost", "127.0.0.1"]

[engines]
order = ["brave", "startpage", "marginalia"]
timeout_seconds = 30
limit = 10
cooldown_seconds = 600   # leave a rate-limited engine alone this long; 0 disables
challenge_wait_seconds = 60  # how long to let Startpage's proof-of-work page finish

[cache]
ttl_seconds = 900        # replay identical successful searches this long; 0 disables

[tls]
system_trust = true
browser_ignore_cert_errors = false

[google]                 # opt-in; see "Google (opt-in)" above before changing
enabled = false
max_per_hour = 100
```

Setting a proxy here passes it to both transports explicitly. Left empty, each
transport reads the environment on its own — and the two do not agree when
`http_proxy` and `HTTP_PROXY` are both set to *different* values: Chromium
prefers the lowercase name, while Python's `urllib` resolution is
last-one-wins over the environment's iteration order. Measured, not inferred.
If your environment sets both cases, set the proxy here instead and the
ambiguity goes away.

**On Windows with a PAC script** (Internet Options → "Use automatic
configuration script"), the two transports also part ways, and no environment
variable shows it: Chromium follows the PAC script, `requests` cannot evaluate
one and connects directly. On networks where direct egress works anyway this
is harmless; where it does not, `marginalia` and the vertical sources fail
while the browser engines work. `doctor` reports the PAC script on its proxy
line, and says so explicitly when `marginalia` cannot connect. The fix is to
name the proxy the PAC script returns in `[proxy]`, which both transports then
use.

### A note on the two TLS settings

They are not the same knob and they are not equally safe.

`system_trust = true` makes `requests` verify against the **OS trust store**
instead of certifi's static bundle, via `truststore`. Behind a TLS-inspecting
proxy this is the *correct* fix: the proxy re-signs traffic with a CA your
machine already trusts and certifi has never heard of. Verification stays fully
on. Leave this true.

`browser_ignore_cert_errors` is a real trade-off, and it defaults to `false`.
Chromium does not read the OS trust store, so behind a TLS-inspecting proxy
every browser engine dies with `ERR_CERT_AUTHORITY_INVALID` — `doctor` will
report exactly that — leaving `marginalia` and the vertical sources working.
Setting it to `true` makes `brave` and `startpage` work again by disabling
certificate verification for the browser, which on a network you do not
control would expose those requests to interception.

**Usually you do not have to make that trade.** On Linux, Chromium reads a
per-user NSS database rather than the OS trust store, and it is empty by
default — that, not a missing feature, is why the OS-installed proxy CA goes
unseen. Import the CA there and the browser engines work with verification
fully on:

```bash
sudo apt install libnss3-tools                                  # provides certutil
mkdir -p ~/.pki/nssdb
certutil -d sql:$HOME/.pki/nssdb -N --empty-password            # first time only
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n proxy-ca -i /path/to/root-ca.crt
```

Import the **root** of the chain, not the leaf. `openssl s_client -connect
example.com:443 -showcerts` prints it, and on Debian and Ubuntu a CA the
system already trusts is usually in `/usr/local/share/ca-certificates/`.

Measured on a TLS-inspecting corporate proxy: with the database absent,
`brave` reports `blocked` / `ERR_CERT_AUTHORITY_INVALID`; with the root CA
imported, `brave` and `startpage` both return results and
`browser_ignore_cert_errors` stays `false`.

On **Windows** and **macOS** there is no separate database: Chromium verifies
against the OS store (the Windows certificate store, the keychain), the same
one `truststore` gives `requests`. Measured on Windows behind a TLS-inspecting
proxy whose CA was already in the Windows store: both browser engines verified
fine with the flag off. If a browser engine does fail on a certificate there,
the CA is missing from the OS store itself — `certutil -user -addstore Root
<ca>.crt` on Windows (a different program from the NSS `certutil` above),
`security add-trusted-cert` on macOS. `doctor` prints the command for the
platform it runs on.

Reach for the flag only where importing a CA is not possible:

```toml
[tls]
browser_ignore_cert_errors = true
```

## Diagnostics

```bash
sought doctor            # full check with live engine probes
sought doctor --offline  # skip the network probes
sought doctor --fix      # install a missing chromium
```

`doctor` exits non-zero when any required check fails. This is a deliberate
reaction to a tool that printed `Overall: OK` while reporting `chromium
missing` two lines above — a green summary that means nothing costs more time
than no summary at all.

Individual engines being down is a *warning*, not a failure; routing around
that is the chain's whole job. Only "every configured engine failed" is fatal
— including when every engine is cooling down, since a search would get
nothing either; the detail then says nothing was asked and points at
`sought config --clear-state`.

## Development

```bash
just            # list tasks
just test
just lint
just check      # lint + test
```

Or directly: `uv run pytest`, `uv run ruff check .`.

The Claude skill in `skills/sought/` is force-included into the wheel, so
`uv tool install` alone is enough for `sought install-skill` to work with no
checkout present.

## License

MIT. See [LICENSE](LICENSE).

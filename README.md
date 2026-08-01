# scout

A search CLI for networks that fight back. It degrades across independent
engines and tells you exactly what it tried.

The design goal is not breadth of coverage. It is that **scout never silently
returns a wrong or empty answer**.

## Why this exists

Three findings, each measured on a corporate network with a TLS-inspecting
proxy and a shared NAT egress IP, shaped everything below.

**1. Fetching a search results page is worse than useless.** Bing does not
block scrapers; it poisons them. Three consecutive queries fetched as plain
HTML returned a supermarket homepage and a webmail help page — HTTP 200,
well-formed, no error of any kind. A tool that fails this way is more dangerous
than one that fails loudly, because nothing downstream can tell.

**2. Plain HTTP clients get refused; a real browser does not.** At the same
moment, `curl https://search.brave.com/...` returned 429 while Playwright
loading the identical URL returned results. Any scraper built on an HTTP
client alone is not usable here, whatever its engine list claims.

**3. Relevance scores can launder garbage.** One tool under evaluation ran the
poisoned results through an ML reranker and assigned the unrelated pages a
relevance score of 0.38. A number next to a wrong answer is worse than no
number.

So scout uses a real browser where one is required, keeps a full audit trail of
every engine it consulted, and flags results that do not mention the query.

## Install

```bash
uv tool install .
scout doctor --fix          # installs chromium if missing
scout install-skill         # optional: install the Claude skill
```

## Use

```bash
scout "playwright bypass cloudflare"           # JSON (default)
scout "rust async runtimes" --pretty           # table
scout "MCP web search" --source hn             # one vertical source
scout "playwright" --source npm --limit 5
```

Output is JSON by default because the envelope carries the audit trail:

```json
{
  "query": "anthropic claude pricing",
  "engine": "startpage",
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

### `low_relevance`

Set when not one result mentions any significant token of the query. This is
the only available signal against a poisoned results page, since such a page
arrives as a valid HTTP 200. It is deliberately biased toward false positives —
a spurious warning costs a second glance, a missed one costs a wrong answer.

### Exit codes

`0` results · `1` searched fine, found nothing · `2` every source failed ·
`130` interrupted.

`1` and `2` differ on purpose: one is an answer, the other is a malfunction.

## Sources

**General chain** (default, tried in order, no `--source` needed):

| engine | index | transport | notes |
|---|---|---|---|
| `brave` | Brave | Chromium | best general quality; rate limits after sustained use |
| `startpage` | Google | Chromium | Google's index without Google's IP ban |
| `marginalia` | independent | plain HTTP | no browser needed; favours small non-commercial sites |

The three use different networks and different quotas, so one being rate
limited says nothing about the next.

**Vertical sources** (`--source`, keyless, ~1s, no fallback):

`wikipedia` · `github` · `hn` · `stackoverflow` · `npm` · `pypi` · `news`

`--source` disables the chain on purpose: an explicit choice should not be
silently swapped for something else.

`pypi` resolves an exact package name only — PyPI removed its full-text search
API, and scout says so rather than returning nothing.

## Configuration

`~/.config/scout/config.toml`. Precedence: **CLI flags > config file >
environment > builtin defaults**.

```bash
scout config --init     # write an annotated default
scout config --path     # print the path
scout config            # print current contents
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

[tls]
system_trust = true
browser_ignore_cert_errors = true
```

The proxy is passed to Playwright explicitly, because Chromium launched by
Playwright does not read proxy environment variables.

### A note on the two TLS settings

They are not the same knob and they are not equally safe.

`system_trust = true` makes `requests` verify against the **OS trust store**
instead of certifi's static bundle, via `truststore`. Behind a TLS-inspecting
proxy this is the *correct* fix: the proxy re-signs traffic with a CA your
machine already trusts and certifi has never heard of. Verification stays fully
on. Leave this true.

`browser_ignore_cert_errors = true` is a real trade-off. Chromium does not read
the OS trust store, so behind the same proxy every browser engine dies with
`ERR_CERT_AUTHORITY_INVALID`, and this flag is what makes them work. It does
disable certificate verification for the browser, which on an untrusted network
would expose those requests to interception. Set it to `false` on any network
you do not control; `brave` and `startpage` will stop working and `doctor` will
tell you why, leaving `marginalia` and the vertical sources.

## Diagnostics

```bash
scout doctor            # full check with live engine probes
scout doctor --offline  # skip the network probes
scout doctor --fix      # install a missing chromium
```

`doctor` exits non-zero when any required check fails. This is a deliberate
reaction to a tool that printed `Overall: OK` while reporting `chromium
missing` two lines above — a green summary that means nothing costs more time
than no summary at all.

Individual engines being down is a *warning*, not a failure; routing around
that is the chain's whole job. Only "every configured engine failed" is fatal.

## Development

```bash
just            # list tasks
just test
just lint
just check      # lint + test
```

Or directly: `uv run pytest`, `uv run ruff check .`.

The Claude skill in `skills/scout/` is force-included into the wheel, so
`uv tool install` alone is enough for `scout install-skill` to work with no
checkout present.

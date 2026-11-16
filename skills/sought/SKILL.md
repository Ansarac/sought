---
name: sought
description: Search the web and read web pages from the shell, with an audit trail of every engine consulted. Use whenever you need URLs you do not already have — documentation, error messages, library comparisons, community opinion, packages, repos, news — or the readable text of a known page (`sought fetch`). Works with no built-in web tools at all, and is also the fallback when a built-in WebSearch or WebFetch is unavailable or blocked. Use it whenever a search must not silently return wrong results.
compatibility: Requires the `sought` CLI on PATH (uv tool install git+https://github.com/Ansarac/sought). Any agent with a shell tool can use it.
---

# sought

Two commands, kept apart: `sought "<query>"` *discovers* URLs,
`sought fetch <url>` *reads* one. Both return a classified status, never a
silent empty answer.

Details live next to this file — read them only when the situation below
calls for it:

- [references/search.md](references/search.md) — every field and status of the search envelope, cooldowns, the cache
- [references/fetch.md](references/fetch.md) — the full fetch envelope, extraction methods, statuses
- [references/sources.md](references/sources.md) — vertical sources, `news` links, `brave_api` quota
- [references/google.md](references/google.md) — only if the user has enabled the opt-in `google` engine

## The one rule

**Never fetch a search engine results page yourself** — not with WebFetch,
curl, `sought fetch` or any other HTTP tool. Engines do not just block
scrapers; some serve HTTP 200 with plausible, unrelated results (measured:
three Bing queries returned a supermarket homepage and a webmail help page).
Nothing downstream can tell. Only `sought "<query>"` searches; fetch only
URLs it returned or that you were given.

## Search

```bash
sought "rust async runtime comparison" --limit 5   # JSON envelope (default; read this)
sought "MCP web search" --source hn                # one source, no fallback
sought "..." --pretty                              # table, for showing a human
```

```json
{"query": "...", "engine": "startpage", "cached": null,
 "attempts": [{"engine": "brave", "status": "rate_limited", "detail": "HTTP 429"},
              {"engine": "startpage", "status": "ok", "count": 5}],
 "low_relevance": false, "relevance": null,
 "results": [{"title": "...", "url": "...", "snippet": "..."}]}
```

Check three things before trusting the results:

1. **`low_relevance: true`** — even the best single result covers under half
   the query. Read it as "verify", not "discard": skim, reword, or re-run with
   another `--source`. Common and usually harmless for `news`.
2. **`attempts`** — why each engine did or did not answer. Empty `results`
   with populated `attempts` is a real answer; empty `results` *and* empty
   `attempts` is a sought bug — report it, do not conclude the web has nothing.
3. **Exit code** — `0` results · `1` searched fine, nothing matched (reword) ·
   `2` every source failed (read `attempts`, then `sought doctor`).

| status | do |
|---|---|
| `ok` / `empty` | — / reword the query |
| `rate_limited` | nothing; the chain moved on. Includes "cooling down … not asked": do not retry, and do not run `--clear-state` to get around it |
| `blocked` / `timeout` | `sought doctor` / retry once or raise `--timeout` |
| `error` | read `detail`; persisting across runs means a sought bug |

A slow search (tens of seconds) is usually Startpage's proof-of-work page,
and the `ok` detail says so. `cached: {"age_seconds": N}` is a replay; add
`--fresh` for a live answer. Full meanings: [references/search.md](references/search.md).

## Choose a source

Without `--source`, sought runs the general chain. If the user has a Brave
API key configured, `brave_api` is asked first (about 2 s, no browser); then
`brave → startpage → marginalia`, independent networks and quotas. Use it for
open-ended questions.

Prefer one vertical when the question has an obvious home — exact, ~1 s, no
bot walls, no fallback:

| `--source` | when |
|---|---|
| `wikipedia` | a concept, definition, background |
| `github` | finding or comparing repositories |
| `hn` | community opinion, experience reports |
| `stackoverflow` | a specific error message or programming problem |
| `npm` / `pypi` | package metadata (`pypi`: exact name only) |
| `news` | anything time-sensitive (a few seconds) |
| `brave` / `startpage` / `marginalia` | force one general engine, e.g. after `low_relevance` |

Never enable an opt-in engine, set or ask for an API key, or put a key on a
command line. Use `--source brave_api` or `--source google` only when the
user has said they are set up. See [references/sources.md](references/sources.md).

## Read a page

If your harness has its own page-fetch tool (WebFetch, say), use it on known
URLs. Otherwise — or when it is blocked, or you need to know exactly why a
page could not be read — use `sought fetch`:

```bash
sought fetch https://docs.python.org/3/library/textwrap.html --pretty
f=$(mktemp) && sought fetch <url> --pretty --max-chars 0 > "$f"; echo "$f"   # whole page, read in parts
```

`--pretty` prints the markdown itself: a status line, the content, and a last
line that says whether it was cut. Content stops at 20000 characters by
default, and a cut always ends with `— TRUNCATED: showing N of M characters`
— if what you need is missing, re-run with `--max-chars 0` before concluding
the page lacks it. Shell tools often truncate long output, so write a whole
page to a file and read it in parts — a new file per page (as `mktemp`
gives), never one fixed name, or parallel fetches overwrite each other and
one page's text gets cited as another's. The JSON envelope (without `--pretty`)
is there when a script needs the fields.

Exit `0` is `ok`; `2` is anything else, with the status on the first line:

| status | do |
|---|---|
| `blocked` | `HTTP 404`/`410`: dead URL — search again. Other 4xx: the site refuses; find another copy. `HTTP 5xx`: usually temporary — retry once later or use a mirror. 401/403, or an error line instead of an HTTP code (DNS, proxy, TLS/certificate): likely the network — `sought doctor` |
| `rate_limited` | 429, Cloudflare challenge or bot wall — do not retry in a loop; find the content elsewhere |
| `timeout` | retry once, or raise `--timeout` |
| `error` | the page needs JavaScript, is not text (PDF), or is too large — find another source (docs mirror, raw file, API) |

No browser: a JavaScript-only page is reported, not rendered. An `ok` with a
detail like `extracted 243 of the page's 3,059 words` may be partial. Cite
the final URL after redirects. More: [references/fetch.md](references/fetch.md).

## Workflow

1. A vertical source if the question has one, otherwise the general chain.
2. Check `low_relevance`, `attempts` and the exit code.
3. Pick the promising URLs.
4. Read them — your harness's fetch tool or `sought fetch`.

## When something is wrong

```bash
sought doctor            # full check with live probes; non-zero exit means a required check failed
sought doctor --offline  # skip the network probes
sought doctor --fix      # install a missing Chromium — the remedy when brave/startpage cannot start
sought config --init     # write an annotated config file, if there is none yet
```

One engine being down is a warning, not a failure — routing around it is the
chain's job. Configuration is `~/.config/sought/config.toml` (`sought config`
shows it, secrets redacted); changing it is the user's decision.

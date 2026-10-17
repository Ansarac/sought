---
name: sought
description: Search the web with an explicit audit trail, and as a fallback when the built-in WebSearch tool is unavailable or blocked. Use for finding documentation, comparing libraries, researching error messages, checking community discussion, or looking up packages, repos and news. Also use whenever a search must not silently return wrong results.
---

# sought

`sought` is a search CLI that degrades across independent engines and reports
exactly what it tried. Use it whenever you need to *discover* URLs. Once you
have a URL, fetch it normally.

## The one rule

**Never use WebFetch on a search engine results page.**

Search engines do not simply block scrapers. Some serve HTTP 200 with a page
of plausible, well-formatted, completely unrelated results. There is no error
to catch, and the output looks exactly like a successful search. A measured
example: three consecutive Bing queries through WebFetch returned a supermarket
homepage and a webmail help page, with no indication anything was wrong.

WebFetch on a *known URL* is accurate and cheap. Use it freely there. It is
only search result pages that lie.

## Basic use

```bash
sought "playwright bypass cloudflare 2025"        # JSON, for reading programmatically
sought "rust async runtime comparison" --pretty   # table, for showing a human
sought "..." --limit 5
sought "..." --fresh                              # skip a cached result for this exact search
```

JSON is the default because the envelope matters:

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

## Reading the envelope

Check these fields before you trust the results.

**`low_relevance: true`** — not one result mentions any significant word from
the query. Treat the results as unverified. Either the engine is serving
decoys, or the query was too abstract. Re-run with `--source startpage` or
reword before quoting anything.

**`attempts`** — every engine consulted, in order, whether or not it worked.
An empty `results` list with a populated `attempts` list is a real answer:
it tells you *why* there is nothing. Statuses:

| status | meaning | what to do |
|---|---|---|
| `ok` | answered | — |
| `empty` | answered, matched nothing | reword the query |
| `rate_limited` | quota or bot wall, or still cooling down from one | it fell through to the next engine; fine |
| `blocked` | network or engine refused | check `sought doctor` |
| `timeout` | no response in time | retry, or raise `--timeout` |
| `error` | unexpected failure | read `detail` |

If `results` is empty **and** `attempts` is empty, that is a bug — report it
rather than concluding the web has no answer.

**An `ok` attempt can carry a `detail` too.** It is something worth knowing
about an answer that did arrive. `startpage` with `passed proof-of-work
challenge in 39s` means Startpage made the browser compute a JavaScript
challenge first; it explains a search that took most of a minute, and the
results are as good as any. If the challenge does not finish in time (60 s by
default), the attempt is `rate_limited` with `proof-of-work challenge not
completed within 60s` and the chain moved on.

**Cooldowns.** After an engine answers `rate_limited`, sought leaves it alone
for a while (10 minutes by default) instead of asking again and prolonging the
ban. It still appears in `attempts`, as `rate_limited` with a detail like
`cooling down until 14:20, not asked this time (last refusal at 14:10: HTTP
429)` — read that as "not asked", not as a fresh refusal. Retrying sooner will
not help, and `--fresh` does not override it: use another engine or a vertical
source. If the cause has been fixed (the user changed network or proxy),
`sought config --clear-state` ends the cooldowns; do not run it just to retry
an engine that refused you.

**`cached`** — `null` for a search that just happened. `{"age_seconds": N}`
means this exact search (same query, source, order and limit) succeeded N
seconds ago and was replayed instead of asking the engines again; `attempts`
is that original search's audit trail. Only searches with results are cached,
for 15 minutes by default. Add `--fresh` if you need a live answer.

## Exit codes

`0` results · `1` searched fine, found nothing · `2` every source failed ·
`130` interrupted.

`1` and `2` mean opposite things. `1` is an answer; `2` is a broken pipe.

## Choosing a source

Without `--source`, sought runs the general chain: **brave → startpage →
marginalia**. Three independent networks and quotas, so one being rate limited
says nothing about the next. Take this default for open-ended questions.

With `--source`, that one source is asked and there is no fallback. Prefer a
vertical when the question has an obvious home — the answer is exact, it costs
about a second, and it is not affected by any bot wall.

| `--source` | reach for it when |
|---|---|
| `wikipedia` | a concept, a definition, background |
| `github` | finding a repository, comparing projects by stars |
| `hn` | community opinion and real-world experience reports |
| `stackoverflow` | a specific error message or programming problem |
| `npm` / `pypi` | package metadata. `pypi` resolves an *exact* name only — PyPI has no full-text search API |
| `news` | anything time-sensitive; the general chain is weak on recency |
| `brave` / `startpage` / `marginalia` | forcing one general engine, e.g. to retry after `low_relevance` |

`startpage` reaches the Google index. `marginalia` is an independent crawl that
favours small, non-commercial, technical sites — better for essays and
documentation, weaker for anything commercial.

### `google` (opt-in, usually off)

The user may have switched on a `google` engine; it is off by default and
never in the default chain. Do not ask for `--source google` unless the user
has said it is enabled — if it is not, you get one `error` attempt whose
detail names the config key, and exit code 2. Do not enable it yourself:
it hides the browser's automation flag from Google, and heavy use from a
shared network can get that network's IP blocked by Google for everyone on it.
That is the user's decision.

When google results do appear, read them like this:

- The `ok` attempt's detail says `automation flag hidden (google.enabled)`.
  That is a disclosure, not an error; the results are real Google results.
- Their URLs are normally the real destinations: sought resolves Google's
  `https://www.google.com/goto?url=...` redirects itself. Any it could not
  resolve stay as `/goto` links (they still work, with one redirect hop), and
  the detail says how many, e.g. `2 of 9 result URLs left as google.com/goto
  redirects`.
- `rate_limited` with `local budget 100/h used, next slot at 14:32 — not
  asked` means sought held itself back under the user's hourly limit; Google
  was not asked. Like a cooldown, it is not a refusal, and retrying before
  that time gets the same answer. Use another engine.
- `rate_limited` with `google /sorry wall` is Google refusing; a cooldown
  follows.

## Suggested workflow

1. Vertical source if the question has one, otherwise the general chain.
2. Check `low_relevance` and `attempts`.
3. Pick promising URLs from the results.
4. WebFetch those URLs for the actual content.

Searching is discovery; fetching is reading. Keep them separate.

## When something is wrong

```bash
sought doctor            # full check, including live engine probes
sought doctor --offline  # skip the network probes
sought doctor --fix      # install a missing chromium
```

`doctor` exits non-zero if any required check fails, so its summary line can be
trusted. Individual engines being down is a warning, not a failure — that is
what the chain is for. Only "every configured engine failed" is fatal. A
`cooldowns:` line lists engines searches are currently skipping.

Configuration (proxy, engine order, timeouts, cooldown, cache, TLS) lives at
`~/.config/sought/config.toml`; `sought config --init` writes an annotated one.

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
```

JSON is the default because the envelope matters:

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

## Reading the envelope

Check these two fields before you trust the results.

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
| `rate_limited` | quota or bot wall | it fell through to the next engine; fine |
| `blocked` | network or engine refused | check `sought doctor` |
| `timeout` | no response in time | retry, or raise `--timeout` |
| `error` | unexpected failure | read `detail` |

If `results` is empty **and** `attempts` is empty, that is a bug — report it
rather than concluding the web has no answer.

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
what the chain is for. Only "every configured engine failed" is fatal.

Configuration (proxy, engine order, timeouts, TLS) lives at
`~/.config/sought/config.toml`; `sought config --init` writes an annotated one.

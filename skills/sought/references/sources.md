# Sources in full

## The general chain

Without `--source`, sought tries general engines in order until one answers:
`brave → startpage → marginalia` by default, with `brave_api` asked first
when the user has a key configured (below). They are independent networks
and quotas, so one being rate limited says nothing about the next.

`startpage` reaches the Google index. `marginalia` is an independent crawl that
favours small, non-commercial, technical sites — better for essays and
documentation, weaker for anything commercial. `brave` and `startpage` drive a
real browser, so they take a few seconds, and Startpage's proof-of-work page
can stretch that to most of a minute.

With `--source`, that one source is asked and there is no fallback — an
explicit choice is never silently swapped for something else.

## Vertical sources

Keyless public APIs, about a second each, unaffected by bot walls.

| `--source` | reach for it when |
|---|---|
| `wikipedia` | a concept, a definition, background |
| `github` | finding a repository, comparing projects by stars |
| `hn` | community opinion and real-world experience reports |
| `stackoverflow` | a specific error message or programming problem |
| `npm` / `pypi` | package metadata. `pypi` resolves an *exact* name only — PyPI has no full-text search API, and sought says so rather than returning nothing |
| `news` | anything time-sensitive; the general chain is weak on recency |

## `news`

Results have the publisher and the date as their snippet
(`CNBC · Sat, 08 Aug 2026 07:00:00 GMT`) and, normally, the publisher's own
article URL. Google News only hands out `https://news.google.com/rss/articles/...`
redirect links, which you cannot fetch (they answer with a JavaScript page);
sought decodes them through an undocumented Google News endpoint. Any it could
not decode stay as redirect links and the `ok` attempt's detail says how many,
e.g. `3 of 10 article URLs left as news.google.com redirects` — and why, if
Google News started refusing part-way. Do not fetch those; search for the
title instead. Decoding is two extra requests per result, so `news` takes a
few seconds rather than one (measured: about 8 s for 10 results).

Short titles mean `low_relevance` is common for `news`; look at `matched` /
`missing` and the titles rather than discarding the results.

## `brave_api` (opt-in, needs the user's key)

The official Brave Search API, if the user has set a key (`[brave_api]
api_key` in the config, or `BRAVE_API_KEY`). With a key it is asked **first**
in the default chain (unless `engines.order` places it, or `auto_first =
false`), so a plain `sought "query"` usually comes back from `brave_api` in
about two seconds with no browser started. Without a key it is not in the
chain at all. Use `--source brave_api` only when the user has said a key is
set — without one you get one `error` attempt naming both places, and exit
code 2. Never set or ask for the key yourself, and never put one on a command
line.

- Same Brave index as `brave`, but no browser and no bot wall: a quick, exact
  answer.
- `rate_limited` here is the user's plan quota, not a bot wall; the detail
  carries the quota per window (`1s window: limit 50, remaining 0, ...,
  exhausted`; a `limit 0` window is "no cap reported", not spent). The chain
  moves on to the next engine by itself; retrying will not help.
- `error` with `API key rejected` means the key is wrong or revoked — tell the
  user, do not retry.
- At most 20 results per query. With a higher `--limit`, the `ok` attempt says
  `capped at 20 results by the API`.

## `google` (opt-in, usually off)

Off by default and never in the default chain. Read [google.md](google.md)
before using `--source google`, and only when the user has said it is enabled.

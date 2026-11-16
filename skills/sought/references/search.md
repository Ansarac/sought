# The search envelope in full

`sought "<query>"` prints one JSON document. Every key is always present.

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
  "relevance": null,
  "results": [{"title": "...", "url": "...", "snippet": "..."}]
}
```

`engine` is the engine whose results these are; `null` when none answered.

## `low_relevance` and `relevance`

`low_relevance: true` means even the *best single* result covers less than
half of the query's significant words (weighted by length, so a rare word
counts for more; a word found only in the URL counts partly). It does not mean
nothing matched: a flagged result may well contain some query words. The check
is biased toward flagging on purpose — a needless warning costs a second look,
a missed decoy page costs a wrong answer — so read it as "verify", not as
"discard".

`relevance` says what the check saw. `null` when not flagged; when flagged:

```json
"relevance": {"best_coverage": 0.38, "matched": ["berkshire"], "missing": ["abel", "earnings"]}
```

`best_coverage` is the best result's score (the flag is raised below 0.5),
`matched` the query words that result mentions in its title or snippet, and
`missing` the words *no* result mentions. Results that share the query's
subject but miss its rarer words mean the query is narrower than what was
found: skim them, or reword. Results that match nothing, or only stopword-ish
filler, are what a decoy page looks like: re-run with `--source startpage` or
another engine before quoting anything.

For `--source news`, titles are short and the snippet is only the publisher
and the date, so partial coverage is normal and the flag is common. Look at
`matched` / `missing` and the titles rather than throwing the results away.

## `attempts`

Every engine consulted, in order, whether or not it worked. An empty
`results` list with a populated `attempts` list is a real answer: it tells
you *why* there is nothing.

| status | meaning | what to do |
|---|---|---|
| `ok` | answered | — |
| `empty` | the engine said nothing matched | reword the query |
| `rate_limited` | quota or bot wall, or still cooling down from one | it fell through to the next engine; fine |
| `blocked` | network or engine refused | check `sought doctor` |
| `timeout` | no response in time | retry, or raise `--timeout` |
| `error` | unexpected failure, or `page not recognised` — the engine changed its layout and sought could not read it. Says nothing about the web | read `detail`; the chain already moved on. Persisting across runs ⇒ a sought bug worth reporting |

If `results` is empty **and** `attempts` is empty, that is a bug — report it
rather than concluding the web has no answer.

**An `ok` attempt can carry a `detail` too.** It is something worth knowing
about an answer that did arrive. `startpage` with `passed proof-of-work
challenge in 39s` means Startpage made the browser compute a JavaScript
challenge first; it explains a search that took most of a minute, and the
results are as good as any. If the challenge does not finish in time (60 s by
default), the attempt is `rate_limited` with `proof-of-work challenge not
completed within 60s` and the chain moved on.

## Cooldowns

After an engine answers `rate_limited`, sought leaves it alone for a while
(10 minutes by default) instead of asking again and prolonging the ban. It
still appears in `attempts`, as `rate_limited` with a detail like
`cooling down until 14:20, not asked this time (last refusal at 14:10: HTTP
429)` — read that as "not asked", not as a fresh refusal. Retrying sooner will
not help, and `--fresh` does not override it: use another engine or a vertical
source. If the cause has been fixed (the user changed network or proxy),
`sought config --clear-state` ends the cooldowns; do not run it just to retry
an engine that refused you. `sought doctor` lists active cooldowns on its
`cooldowns:` line. A `brave_api` detail starting `quota spent (30d window)`
is the plan's monthly quota: it is held until the reset Brave reported, which
can be days away.

## `cached`

`null` for a search that just happened. `{"age_seconds": N}` means this exact
search (same query, source, order and limit) succeeded N seconds ago and was
replayed instead of asking the engines again; `attempts` is that original
search's audit trail. Only searches with results, and not flagged
`low_relevance`, are cached, for 15 minutes by default. Add `--fresh` if you
need a live answer.

## Exit codes

`0` results · `1` searched fine, found nothing · `2` every source failed ·
`130` interrupted.

`1` and `2` mean opposite things. `1` is an answer; `2` is a broken pipe.

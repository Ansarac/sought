# `sought fetch` in full

```bash
sought fetch https://docs.python.org/3/library/textwrap.html            # JSON
sought fetch https://example.com/page --pretty --max-chars 5000         # markdown, cut at 5000
sought fetch https://example.com/long-page --pretty --max-chars 0       # no limit
```

One plain HTTP request (OS trust store, sought's proxy rules, an honest
`sought/<version>` User-Agent), readable content back as markdown. No browser:
a JavaScript-only page is reported, not rendered.

## `--pretty`

The first line is the status: `✓ <final url> — HTTP 200, text/html, readability`
on success, followed by any detail, a `redirected from` line, the title and
the markdown; or `✗ <url>: <status> — <reason>` and nothing else. The last
line is `— N characters`, or `— TRUNCATED: showing N of M characters
(--max-chars N; --max-chars 0 for all)` when the content was cut.

## The JSON envelope

Always the same keys:

```json
{"url": "...", "final_url": "...", "status": "ok", "detail": "",
 "http_status": 200, "content_type": "text/html", "method": "readability",
 "title": "...", "content": "# ...markdown...",
 "chars": 13709, "total_chars": 13709, "truncated": false, "max_chars": 20000}
```

- **`truncated: true`** — `content` stops at `max_chars` (default 20000) of
  `total_chars`. Re-run with `--max-chars 0` (or a larger N) if the part you
  need is missing; do not conclude the page lacks it.
- **`method`** — `readability`; `fallback:<selector>` (`main`, `article`,
  `[role=main]`, `#content`, `body`) when readability found too little, or
  kept under 2% of the page and a mostly-prose container holds far more
  (`readability kept 346 of the page's 67,573 words (0.5%); used <body>`); `verbatim` for `text/*` and JSON, passed through as is.
- **`detail` on `ok`** — e.g. `extracted 243 of the page's 3,059 words; the
  rest was judged navigation`: the extract may be partial. Usually right
  (a README inside a sidebar-heavy page), but check if something is missing.
- **`final_url`** — where redirects ended; cite that one.

## Statuses

| status | meaning | what to do |
|---|---|---|
| `ok` | content extracted | — |
| `blocked` | connection failed or HTTP 4xx/5xx (`detail: "HTTP 404"`) | 404/410: dead URL, search again. Other 4xx: the site refuses, find another copy. 5xx: usually temporary, retry once later or use a mirror. 401/403, or a connection error (DNS, proxy, TLS) instead of an HTTP code: likely the network — `sought doctor` |
| `rate_limited` | HTTP 429, a Cloudflare challenge, or a bot-wall page | do not retry in a loop; find the content elsewhere |
| `timeout` | no response within `--timeout` | retry once, or raise `--timeout` |
| `error` | `no readable content — page needs JavaScript`, an unsupported type (`application/pdf`), or a body over 10 MB | the page cannot be read this way; look for another source (docs mirror, raw file, API) |

Never `empty`: a page that yields no text is `error` with the reason, not `ok`
with an empty `content`. Exit `0` on `ok`, `2` otherwise; the envelope is on
stdout either way, the one-line reason on stderr.

Do not fetch `news.google.com/rss/articles/...` links — they answer with a
JavaScript page. Search for the article title instead (see
[sources.md](sources.md)).

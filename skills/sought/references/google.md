# The opt-in `google` engine

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

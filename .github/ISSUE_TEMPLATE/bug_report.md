---
name: Bug report
about: Something went wrong — a search that should have worked, an engine that failed oddly, a wrong exit code
title: ''
labels: bug
assignees: ''
---

> **Redact before you post.** `doctor` output and proxy URLs can contain
> credentials (`http://user:password@proxy.example.com:8080`), internal
> hostnames, and internal IPs. Replace the secret parts with `REDACTED` —
> keep the shape of the URL, drop the contents.

## What happened

<!-- One or two sentences. Include the exit code if the command failed:
     0 = results, 1 = searched fine but found nothing, 2 = every source failed. -->

## What you expected

## Command you ran

```console
$ sought ...
```

## `sought doctor --offline` output

<!-- REQUIRED. This one command answers most of what we would otherwise
     have to ask you: config location, proxy setup, TLS settings, whether
     Chromium is installed. -->

```console
$ sought doctor --offline
```

## The `attempts` array

<!-- REQUIRED for anything search-related. Re-run the failing query with
     --json and paste the "attempts" array. sought records one attempt per
     engine consulted, with a status — that array is the difference between
     "rate limited", "empty", and "blocked", and we cannot guess which. -->

```console
$ sought "your query" --json
```

```json
"attempts": [ ... ]
```

## Environment

- sought version: <!-- `sought --version` -->
- OS:
- Python: <!-- `python3 --version` -->
- Installed via: <!-- uv tool install / from a checkout / other -->
- Network: <!-- home / corporate proxy / VPN / TLS-inspecting proxy -->
- `tls.browser_ignore_cert_errors` setting: <!-- true / false / unset -->

## Anything else

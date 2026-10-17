default:
    @just --list

# Install dependencies and the browser sought needs.
setup:
    uv sync
    uv run playwright install chromium-headless-shell

test:
    uv run pytest

lint:
    uv run ruff check .

fmt:
    uv run ruff format .

fmt-check:
    uv run ruff format --check .

# The same three gates CI runs, so a green `just check` means a green CI.
check: lint fmt-check test

# Self-check without touching the network.
doctor:
    uv run sought doctor --offline

# Install sought globally from this checkout.
install:
    uv tool install --force .

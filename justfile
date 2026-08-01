default:
    @just --list

# Install dependencies and the browser sought needs.
setup:
    uv sync
    uv run playwright install chromium

test:
    uv run pytest

lint:
    uv run ruff check .

fmt:
    uv run ruff format .

check: lint test

# Self-check without touching the network.
doctor:
    uv run sought doctor --offline

# Install sought globally from this checkout.
install:
    uv tool install --force .

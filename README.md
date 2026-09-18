# OCR Enhanced Code Review

A Codex skill that combines the deterministic OpenCodeReview (OCR) CLI with Codex reasoning for high precision code reviews.

## Repository layout

- `skill/` — the installable skill, including its instructions, agent metadata, helper script, and validation dependencies.
- `tests/` — tests for the skill metadata and OCR preparation helper.
- `LICENSE` — MIT License.

## Usage

From a target repository, run the bundled preparation helper:

```bash
uv run --project skill skill/scripts/prepare_review.py --repo /path/to/repository
```

The helper asks OCR for the reviewable file set and applicable rules without invoking an LLM. Codex then reviews those files and verifies findings against the selected changes.

## Development

Install validation dependencies and run the test suite:

```bash
uv sync --project skill --group validation
uv run --project skill pytest
```

The project requires Python 3.10 or newer.

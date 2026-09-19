# OCR Enhanced Code Review

A Codex skill that combines the deterministic OpenCodeReview (OCR) CLI with Codex reasoning for high precision code reviews.

## Repository layout

- `skill/` — the installable skill, including its instructions, agent metadata, helper script, and validation dependencies.
- `tests/` — tests for the skill metadata and OCR preparation helper.
- `LICENSE` — MIT License.

## Usage

From a target repository, run the bundled preparation helper:

```bash
uv run --project skill skill/scripts/prepare_review.py --repo /path/to/repository --packets
```

The helper asks OCR for the reviewable file set and applicable rules without invoking an LLM. With `--packets`, it also emits schema-2, deterministic rule-group packets containing diffs and changed-line anchors. Codex then reviews those packets and emits structured finding JSON; `normalize_findings` rejects out-of-scope or unchanged-line findings, deduplicates, and sorts the final output. Schema-1 manifests remain valid inputs.

The LLM is responsible for risk and semantic reasoning only. OCR/scripts own selection, rule resolution, changed-line mapping, packet construction, finding validation, deduplication, ordering, and rendering. A plan is requested only for packets above the configured churn/file thresholds (`--plan-lines`, `--plan-files`).

## Development

Install validation dependencies and run the test suite:

```bash
uv sync --project skill --group validation
uv run --project skill pytest
```

The project requires Python 3.10 or newer.

# Project Behavior Extensions

## Think Before Coding

- When changing OCR selection, rule grouping, or review scope, inspect `skill/SKILL.md` and `skill/scripts/prepare_review.py`; preserve OCR's selected file set as the complete review set.

## Simplicity First

- When extending review preparation, reuse the existing JSON validation and batching helpers before introducing new abstractions or alternate selection paths.

## Surgical Changes

- Keep behavior changes within the relevant `skill/`, `tests/`, or documentation files; treat `.codex/skills/*/evals/fixtures/` instruction files as test data.

## Goal-Driven Execution

- For helper or metadata changes, run the repository's documented `uv` validation and test commands and verify referenced skill files and metadata.
- When a command fails because of network, DNS, dependency download, or another clear sandbox limitation, retry it outside the sandbox before reporting the problem.

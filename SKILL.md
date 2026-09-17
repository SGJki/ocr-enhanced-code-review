---
name: ocr-enhanced-code-review
description: Run high precision code reviews by using the local OpenCodeReview (OCR) CLI for deterministic change selection and rule resolution, then use Codex to inspect, verify, prioritize, and explain findings. Applies to working tree, commit, branch range, or full-file review requests when `ocr` is installed.
metadata:
  version: "0.1"
---

# OCR Enhanced Code Review

Use OCR as the deterministic review planner and Codex as the reasoning and verification layer. This keeps file coverage, path filtering, and review rules anchored to the repository rather than leaving them to prompt interpretation.

## Workflow

1. Identify the repository and review scope. Use workspace mode by default. Respect an explicit commit (`--commit`), range (`--from`/`--to`), path scan, exclude pattern, or rule file.
2. Run the bundled helper from the skill directory:

   ```bash
   uv run --project <skill-dir> <skill-dir>/scripts/prepare_review.py --repo <repo> [scope flags]
   ```

   It calls `ocr delegate preview --format json` and, for the selected paths, `ocr delegate rule --format json`. It never invokes an LLM. Each OCR command has a 300-second timeout by default; use `--command-timeout <seconds>` when the repository needs a different limit. Save the JSON output in memory or a temporary file; do not commit it. Use the machine's existing uv cache when available, for example `UV_CACHE_DIR=$HOME/.cache/uv uv run ...`.
3. Treat `selection.reviewable_files` as the complete review set. If it is empty, report that OCR found no reviewable changes and stop. Do not silently replace OCR's selection with an ad-hoc file list.
4. Read the selected patches with Git, including staged and unstaged changes in workspace mode and the correct `git diff`/`git show` form for commit or range mode. Read enough surrounding source to validate behavior and line numbers. For untracked files, inspect the file contents and mark findings against added lines.
5. Apply the rule text returned for each file group. Review every selected file at least once. Group files only when the manifest puts them in the same rule group or when their behavior is inseparable; preserve per-file coverage.
6. For each candidate finding, verify all of the following before reporting it:
   - the cited path exists in the selected set;
   - the cited line is changed (or is the smallest changed-line span that demonstrates the defect);
   - the defect is concrete and reproducible from repository evidence;
   - the rule and surrounding code support the claim;
   - the suggested fix does not assume unstated requirements.
7. Deduplicate findings that describe the same root cause. Sort by severity and user impact: correctness/security/data loss first, then reliability/performance, then maintainability. Omit style-only suggestions unless the resolved rule explicitly makes them actionable.
8. Return concise review comments with `path:line`, severity (`blocking`, `high`, `medium`, or `low`), explanation, and a concrete fix. End with a short summary of reviewed files and any limitations (for example, tests not run or ambiguous behavior).

## Scope mapping

- Workspace: `ocr delegate preview --repo <repo> --format json` and inspect staged, unstaged, and untracked changes.
- Commit: pass `--commit <hash>` and compare with its parent.
- Branch range: pass `--from <base> --to <head>`; use the merge-base semantics reported by OCR.
- Whole-file audit: run the helper with `--scan` (and optional `--path`) so it uses `ocr scan --preview` for selection, then apply the same per-file verification process. Run the LLM-backed `ocr scan` only when the user explicitly asks for OCR's own model review.

## Failure handling

- If `ocr` is missing, report the exact install/path problem and offer a normal Git-based review only if the user wants to continue without OCR.
- If OCR returns malformed JSON, a non-zero status, or an unsupported scope, stop before reasoning over an incomplete selection and show the command error.
- Never use `ocr review` from this skill: it invokes OCR's configured LLM and defeats the Codex-owned reasoning layer.

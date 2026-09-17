#!/usr/bin/env python3
"""Build a deterministic OCR review manifest without invoking an LLM."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
MAX_RULE_COMMAND_BYTES = 64 * 1024


class OCRResponseError(RuntimeError):
    """Raised when OCR returns data that cannot produce a complete manifest."""


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run_json(
    command: list[str], repo: Path, timeout_seconds: int
) -> tuple[dict[str, Any], str]:
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout_seconds}s: {' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to execute {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"command exited {result.returncode}: {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        output_preview = result.stdout[:1000]
        raise RuntimeError(
            f"command returned malformed JSON: {' '.join(command)}\n{output_preview}"
        ) from exc
    if not isinstance(payload, dict):
        raise OCRResponseError(
            f"command returned a non-object JSON value: {' '.join(command)}"
        )
    return payload, result.stderr


def object_list(payload: dict[str, Any], key: str, source: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise OCRResponseError(f"{source} has no list-valued {key}")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OCRResponseError(f"{source} {key}[{index}] is not an object")
    return value


def file_paths(files: list[dict[str, Any]], source: str) -> list[str]:
    paths: list[str] = []
    for index, item in enumerate(files):
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise OCRResponseError(f"{source} file {index} has no path")
        paths.append(path)
    duplicates = sorted(path for path, count in Counter(paths).items() if count > 1)
    if duplicates:
        raise OCRResponseError(f"{source} contains duplicate paths: {duplicates}")
    return paths


def path_batches(
    paths: list[str], base_command: list[str], max_bytes: int = MAX_RULE_COMMAND_BYTES
) -> list[list[str]]:
    base_size = sum(len(os.fsencode(part)) + 1 for part in [*base_command, "--"])
    batches: list[list[str]] = []
    current: list[str] = []
    current_size = base_size

    for path in paths:
        path_size = len(os.fsencode(path)) + 1
        if current and current_size + path_size > max_bytes:
            batches.append(current)
            current = []
            current_size = base_size
        current.append(path)
        current_size += path_size
    if current:
        batches.append(current)
    return batches


def validate_rule_coverage(
    groups: list[dict[str, Any]], selected_paths: list[str]
) -> None:
    covered_paths: list[str] = []
    for index, group in enumerate(groups):
        files = group.get("files")
        if not isinstance(files, list) or any(
            not isinstance(path, str) or not path for path in files
        ):
            raise OCRResponseError(f"OCR rule JSON groups[{index}] has invalid files")
        rule = group.get("rule")
        if not isinstance(rule, str) or not rule.strip():
            raise OCRResponseError(
                f"OCR rule JSON groups[{index}] has no non-empty rule"
            )
        covered_paths.extend(files)

    selected_counts = Counter(selected_paths)
    covered_counts = Counter(covered_paths)
    if covered_counts != selected_counts:
        missing = sorted(selected_counts.keys() - covered_counts.keys())
        unexpected = sorted(covered_counts.keys() - selected_counts.keys())
        duplicates = sorted(path for path, count in covered_counts.items() if count > 1)
        raise OCRResponseError(
            "OCR rule groups do not cover selected files exactly: "
            f"missing={missing}, unexpected={unexpected}, duplicates={duplicates}"
        )


def resolve_rule_groups(
    ocr: str,
    repo: Path,
    scope_flags: tuple[tuple[str, str | None], ...],
    paths: list[str],
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    base_command = [ocr, "delegate", "rule", "--repo", str(repo), "--format", "json"]
    for flag, value in scope_flags:
        if value:
            base_command.extend([flag, value])

    groups: list[dict[str, Any]] = []
    for batch in path_batches(paths, base_command):
        rules, _ = run_json([*base_command, "--", *batch], repo, timeout_seconds)
        raw_groups = object_list(rules, "groups", "OCR rule JSON")
        for raw_group in raw_groups:
            group = dict(raw_group)
            group["group_id"] = len(groups) + 1
            groups.append(group)

    validate_rule_coverage(groups, paths)
    return groups


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an OCR delegation manifest; no LLM is called."
    )
    parser.add_argument("--repo", default=".", help="Git repository root")
    parser.add_argument("--ocr", default="ocr", help="OCR executable")
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Use ocr scan --preview for full-file selection",
    )
    parser.add_argument("--path", help="Comma-separated paths for --scan")
    parser.add_argument("--commit", help="Commit or tag to review")
    parser.add_argument("--from", dest="from_ref", help="Start ref for a range")
    parser.add_argument("--to", dest="to_ref", help="End ref for a range")
    parser.add_argument("--exclude", help="Comma-separated OCR exclude patterns")
    parser.add_argument("--rule", help="Custom OCR rule JSON file")
    parser.add_argument("--background", help="Review context")
    parser.add_argument("--background-file", help="Markdown review context file")
    parser.add_argument(
        "--command-timeout",
        type=positive_int,
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        help="Timeout in seconds for each OCR command",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repository does not exist: {repo}", file=sys.stderr)
        return 2
    if shutil.which(args.ocr) is None and not Path(args.ocr).exists():
        print(f"error: OCR executable not found: {args.ocr}", file=sys.stderr)
        return 2
    if args.commit and (args.from_ref or args.to_ref):
        print("error: --commit cannot be combined with --from/--to", file=sys.stderr)
        return 2
    if bool(args.from_ref) != bool(args.to_ref):
        print("error: --from and --to must be supplied together", file=sys.stderr)
        return 2
    if args.scan and (args.commit or args.from_ref or args.to_ref):
        print(
            "error: --scan cannot be combined with commit or range scope",
            file=sys.stderr,
        )
        return 2
    if args.path and not args.scan:
        print("error: --path requires --scan", file=sys.stderr)
        return 2
    # The child process runs in the repository, so resolve caller-relative paths first.
    if args.ocr not in {"ocr"} and Path(args.ocr).exists():
        args.ocr = str(Path(args.ocr).expanduser().resolve())
    if args.rule:
        args.rule = str(Path(args.rule).expanduser().resolve())
    if args.background_file:
        args.background_file = str(Path(args.background_file).expanduser().resolve())

    preview_cmd = [args.ocr]
    preview_cmd.extend(["scan", "--preview"] if args.scan else ["delegate", "preview"])
    preview_cmd.extend(["--repo", str(repo), "--format", "json"])
    scope_flags = (
        ("--commit", args.commit),
        ("--from", args.from_ref),
        ("--to", args.to_ref),
        ("--exclude", args.exclude),
        ("--rule", args.rule),
        ("--background", args.background),
        ("--background-file", args.background_file),
    )
    for flag, value in scope_flags:
        if value:
            preview_cmd.extend([flag, value])
    if args.scan and args.path:
        preview_cmd.extend(["--path", args.path])

    try:
        raw_selection, preview_stderr = run_json(
            preview_cmd, repo, args.command_timeout
        )
        if args.scan:
            scan_files = object_list(raw_selection, "files", "OCR scan preview JSON")
            reviewable = [
                {
                    "path": item.get("path"),
                    "status": item.get("status", "scan"),
                    "insertions": item.get("insertions", 0),
                    "deletions": item.get("deletions", 0),
                }
                for item in scan_files
                if isinstance(item, dict) and item.get("will_review") is True
            ]
            selection = {
                "schema_version": "1",
                "mode": "scan",
                "repository": str(repo),
                "total_files": raw_selection.get("total_files", len(scan_files)),
                "reviewable_count": len(reviewable),
                "excluded_count": raw_selection.get("excluded_count", 0),
                "total_insertions": raw_selection.get("total_insertions", 0),
                "total_deletions": raw_selection.get("total_deletions", 0),
                "reviewable_files": reviewable,
            }
            selection["scan_preview"] = raw_selection
        else:
            selection = raw_selection
        files = object_list(selection, "reviewable_files", "OCR preview JSON")

        rule_groups: list[dict[str, Any]] = []
        if files:
            paths = file_paths(files, "OCR preview JSON")
            rule_groups = resolve_rule_groups(
                args.ocr,
                repo,
                scope_flags,
                paths,
                args.command_timeout,
            )

        manifest = {
            "schema_version": "1",
            "source": "ocr-delegate",
            "repository": str(repo),
            "selection": selection,
            "rule_groups": rule_groups,
            "ocr_stderr": preview_stderr.strip(),
        }
        json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

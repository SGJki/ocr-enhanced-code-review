#!/usr/bin/env python3
"""Build a deterministic OCR review manifest without invoking an LLM."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
MAX_RULE_COMMAND_BYTES = 64 * 1024
DEFAULT_PLAN_CHANGED_LINES = 80
DEFAULT_PLAN_FILES = 4
MAX_FINDING_TEXT = 20_000
FINDING_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FINDING_CATEGORIES = {
    "bug",
    "security",
    "performance",
    "concurrency",
    "data_integrity",
    "maintainability",
    "test",
    "other",
}
FINDING_CONFIDENCES = {"high", "medium", "low"}
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


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


def _run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed" + (f": {detail}" if detail else "")
        )
    return result.stdout


def changed_lines_from_diff(diff: str) -> dict[str, set[int]]:
    """Return added/modified new-file line numbers from a unified diff."""
    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            changed.setdefault(current_path, set())
            continue
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if current_path is None or not line or line.startswith("\\ No newline"):
            continue
        marker = line[0]
        if marker == "+":
            changed[current_path].add(new_line)
            new_line += 1
        elif marker == " ":
            new_line += 1
        # Deleted lines have no valid new-file anchor.
    return changed


def _scope_diff(repo: Path, selection: dict[str, Any]) -> str:
    mode = selection.get("mode", "workspace")
    if mode == "commit":
        commit = selection.get("commit")
        if not isinstance(commit, str) or not commit:
            raise OCRResponseError("commit selection has no commit ref")
        parent = _run_git(repo, ["rev-parse", f"{commit}^"]).strip()
        return _run_git(repo, ["diff", "--no-ext-diff", "--unified=3", parent, commit])
    if mode in {"range", "branch"}:
        from_ref = selection.get("from") or selection.get("base")
        to_ref = selection.get("to") or selection.get("head")
        if not isinstance(from_ref, str) or not isinstance(to_ref, str):
            raise OCRResponseError("range selection has no from/to refs")
        return _run_git(
            repo, ["diff", "--no-ext-diff", "--unified=3", from_ref, to_ref]
        )
    if mode == "scan":
        return ""
    return _run_git(repo, ["diff", "--no-ext-diff", "--unified=3", "HEAD"])


def _untracked_file_diff(repo: Path, path: str) -> tuple[str, set[int]]:
    file_path = repo / path
    if not file_path.is_file():
        return "", set()
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "[binary file omitted]", set()
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    if body:
        body += "\n"
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}", set(
        range(1, len(lines) + 1)
    )


def _file_diff_map(
    repo: Path, selection: dict[str, Any], paths: list[str]
) -> tuple[dict[str, str], dict[str, set[int]]]:
    diff = _scope_diff(repo, selection)
    changed = changed_lines_from_diff(diff)
    per_file: dict[str, list[str]] = {path: [] for path in paths}
    current: str | None = None
    pending: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            pending = [line]
            current = None
            continue
        if line.startswith("+++ b/"):
            current = line[6:].rstrip("\n")
            if current in per_file:
                per_file[current].extend(pending)
                pending = []
        elif current is None and pending:
            pending.append(line)
        if current in per_file:
            per_file[current].append(line)
    diffs = {path: "".join(lines) for path, lines in per_file.items() if lines}
    for path in paths:
        if selection.get("mode") == "scan":
            full_diff, full_lines = _untracked_file_diff(repo, path)
            if full_diff:
                diffs[path] = full_diff
                changed[path] = full_lines
        elif path not in diffs and path not in changed:
            untracked_diff, untracked_lines = _untracked_file_diff(repo, path)
            if untracked_diff:
                diffs[path] = untracked_diff
                changed[path] = untracked_lines
        changed.setdefault(path, set())
        diffs.setdefault(
            path, "[diff unavailable: binary, deleted, or outside local checkout]"
        )
    return diffs, changed


def plan_required(
    group: dict[str, Any],
    changed_lines: dict[str, set[int]],
    *,
    max_lines: int = DEFAULT_PLAN_CHANGED_LINES,
    max_files: int = DEFAULT_PLAN_FILES,
) -> bool:
    files = group.get("files", [])
    if not isinstance(files, list):
        return False
    return (
        len(files) > max_files
        or sum(len(changed_lines.get(path, set())) for path in files) > max_lines
    )


def build_review_packets(
    manifest: dict[str, Any],
    repo: Path,
    *,
    max_plan_lines: int = DEFAULT_PLAN_CHANGED_LINES,
    max_plan_files: int = DEFAULT_PLAN_FILES,
) -> list[dict[str, Any]]:
    """Build stable, group-scoped packets from an OCR manifest."""
    validate_manifest(manifest)
    selection = manifest.get("selection")
    groups = manifest.get("rule_groups")
    if not isinstance(selection, dict) or not isinstance(groups, list):
        raise OCRResponseError("manifest must contain selection and rule_groups")
    files = object_list(selection, "reviewable_files", "OCR preview JSON")
    paths = sorted(file_paths(files, "OCR preview JSON"))
    diffs, changed = _file_diff_map(repo, selection, paths)
    packets: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        group_paths = group.get("files")
        if not isinstance(group_paths, list):
            raise OCRResponseError(f"rule group {index} has invalid files")
        packet_files = []
        for path in sorted(group_paths):
            packet_files.append(
                {
                    "path": path,
                    "diff": diffs[path],
                    "changed_lines": sorted(changed[path]),
                }
            )
        packets.append(
            {
                "packet_id": f"group-{group.get('group_id', index)}",
                "group_id": group.get("group_id", index),
                "files": packet_files,
                "rule": group.get("rule", ""),
                "background": selection.get("background") or manifest.get("background"),
                "plan_required": plan_required(
                    group, changed, max_lines=max_plan_lines, max_files=max_plan_files
                ),
            }
        )
    return packets


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate both schema-1 OCR manifests and schema-2 packet manifests."""
    version = manifest.get("schema_version", "1")
    if version not in {"1", "2"}:
        raise OCRResponseError(f"unsupported manifest schema_version: {version}")
    selection = manifest.get("selection")
    groups = manifest.get("rule_groups")
    if not isinstance(selection, dict) or not isinstance(groups, list):
        raise OCRResponseError("manifest must contain selection and rule_groups")
    files = object_list(selection, "reviewable_files", "OCR manifest")
    paths = file_paths(files, "OCR manifest")
    validate_rule_coverage(groups, paths)
    if (
        version == "2"
        and "review_packets" in manifest
        and not isinstance(manifest["review_packets"], list)
    ):
        raise OCRResponseError("schema-2 review_packets must be a list")


def parse_plan(value: str | dict[str, Any]) -> dict[str, Any]:
    """Parse and constrain an optional LLM plan; invalid plans are warnings, not input."""
    try:
        plan = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise OCRResponseError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("checkpoints"), list):
        raise OCRResponseError("plan must be an object with a checkpoints list")
    checkpoints = []
    for item in plan["checkpoints"][:5]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in ("focus", "why")
        ):
            raise OCRResponseError("plan checkpoint requires non-empty focus and why")
        checkpoints.append(
            {
                "focus": item["focus"].strip(),
                "lines": item.get("lines", "")
                if isinstance(item.get("lines", ""), str)
                else "",
                "why": item["why"].strip(),
            }
        )
    return {"summary": str(plan.get("summary", "")).strip(), "checkpoints": checkpoints}


def validate_finding(
    finding: Any, selected_paths: set[str], changed_lines: dict[str, set[int]]
) -> list[str]:
    errors: list[str] = []
    if not isinstance(finding, dict):
        return ["finding must be an object"]
    required = (
        "path",
        "anchor",
        "severity",
        "category",
        "claim",
        "evidence",
        "impact",
        "fix",
        "confidence",
    )
    for key in required:
        if key not in finding:
            errors.append(f"missing {key}")
    path = finding.get("path")
    if not isinstance(path, str) or path not in selected_paths:
        errors.append("path is outside selected review set")
    anchor = finding.get("anchor")
    if (
        not isinstance(anchor, dict)
        or not isinstance(anchor.get("start_line"), int)
        or not isinstance(anchor.get("end_line"), int)
    ):
        errors.append("anchor requires integer start_line and end_line")
    elif anchor["start_line"] <= 0 or anchor["end_line"] < anchor["start_line"]:
        errors.append("anchor range is invalid")
    elif not (
        set(range(anchor["start_line"], anchor["end_line"] + 1))
        & changed_lines.get(path, set())
    ):
        errors.append("anchor does not overlap a changed line")
    severity = finding.get("severity")
    if severity not in FINDING_SEVERITY_ORDER:
        errors.append("invalid severity")
    category = finding.get("category")
    if category not in FINDING_CATEGORIES:
        errors.append("invalid category")
    confidence = finding.get("confidence")
    if confidence not in FINDING_CONFIDENCES:
        errors.append("invalid confidence")
    for key in ("claim", "evidence", "impact", "fix"):
        value = finding.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be non-empty")
        elif len(value) > MAX_FINDING_TEXT:
            errors.append(f"{key} exceeds {MAX_FINDING_TEXT} characters")
    return errors


def _finding_sort_key(finding: dict[str, Any]) -> tuple[Any, ...]:
    anchor = finding["anchor"]
    return (
        FINDING_SEVERITY_ORDER[finding["severity"]],
        finding["path"],
        anchor["start_line"],
        anchor["end_line"],
        finding["claim"],
    )


def normalize_findings(
    findings: list[Any], selected_paths: set[str], changed_lines: dict[str, set[int]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        errors = validate_finding(finding, selected_paths, changed_lines)
        if errors:
            rejected.append({"index": index, "errors": errors})
        else:
            valid.append(dict(finding))
    valid.sort(key=_finding_sort_key)
    unique: list[dict[str, Any]] = []
    for finding in valid:
        anchor = finding["anchor"]
        claim = finding["claim"].strip().casefold()
        duplicate = any(
            previous["path"] == finding["path"]
            and previous["category"] == finding["category"]
            and previous["claim"].strip().casefold() == claim
            and previous["anchor"]["start_line"] <= anchor["end_line"]
            and anchor["start_line"] <= previous["anchor"]["end_line"]
            for previous in unique
        )
        if not duplicate:
            unique.append(finding)
        else:
            rejected.append({"finding": finding, "errors": ["duplicate finding"]})
    return unique, rejected


def finding_needs_validation(
    finding: dict[str, Any], *, cross_file: bool = False, relocated: bool = False
) -> bool:
    return (
        finding.get("severity") in {"critical", "high"}
        or finding.get("confidence") != "high"
        or cross_file
        or relocated
    )


def validation_queue(
    findings: list[dict[str, Any]],
    *,
    cross_file_paths: set[str] | None = None,
    relocated_indexes: set[int] | None = None,
) -> list[dict[str, Any]]:
    cross_file_paths = cross_file_paths or set()
    relocated_indexes = relocated_indexes or set()
    return [
        finding
        for index, finding in enumerate(findings)
        if finding_needs_validation(
            finding,
            cross_file=finding.get("path") in cross_file_paths,
            relocated=index in relocated_indexes,
        )
    ]


def apply_validation_decision(
    finding: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Apply a validator result without allowing uncertainty to delete a finding."""
    decision = result.get("decision")
    if decision == "revise" and isinstance(result.get("revised_finding"), dict):
        revised = dict(finding)
        revised.update(result["revised_finding"])
        return revised, "revise"
    if decision == "keep":
        return finding, "keep"
    return finding, "uncertain"


def render_findings(findings: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for finding in findings:
        anchor = finding["anchor"]
        location = f"{finding['path']}:{anchor['start_line']}"
        lines.append(f"[{finding['severity']}] {location} ({finding['category']})")
        lines.append(f"{finding['claim']} Impact: {finding['impact']}")
        lines.append(f"Evidence: {finding['evidence']}")
        lines.append(f"Suggested fix: {finding['fix']}")
        lines.append("")
    return "\n".join(lines).rstrip()


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
    parser.add_argument(
        "--packets",
        action="store_true",
        help="Include deterministic, group-scoped review packets in the manifest",
    )
    parser.add_argument(
        "--plan-lines",
        type=positive_int,
        default=DEFAULT_PLAN_CHANGED_LINES,
        help="Changed-line threshold that marks a group as plan_required",
    )
    parser.add_argument(
        "--plan-files",
        type=positive_int,
        default=DEFAULT_PLAN_FILES,
        help="File-count threshold that marks a group as plan_required",
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
            "schema_version": "2",
            "source": "ocr-delegate",
            "repository": str(repo),
            "selection": selection,
            "rule_groups": rule_groups,
            "ocr_stderr": preview_stderr.strip(),
        }
        validate_manifest(manifest)
        if args.packets and files:
            manifest["review_packets"] = build_review_packets(
                manifest,
                repo,
                max_plan_lines=args.plan_lines,
                max_plan_files=args.plan_files,
            )
        json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

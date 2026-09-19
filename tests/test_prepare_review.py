import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill.scripts import prepare_review


class PrepareReviewTests(unittest.TestCase):
    def test_rule_command_terminates_options_for_dash_prefixed_path(self) -> None:
        response = {
            "groups": [
                {
                    "files": ["-x.py"],
                    "rule": "Review Python correctness.",
                }
            ]
        }
        with patch.object(
            prepare_review, "run_json", return_value=(response, "")
        ) as run_json:
            groups = prepare_review.resolve_rule_groups(
                "ocr", Path("/repo"), (), ["-x.py"], 10
            )

        command = run_json.call_args.args[0]
        self.assertEqual(command[-2:], ["--", "-x.py"])
        self.assertEqual(groups[0]["group_id"], 1)

    def test_object_list_rejects_non_object_entries(self) -> None:
        with self.assertRaisesRegex(
            prepare_review.OCRResponseError, r"reviewable_files\[0\]"
        ):
            prepare_review.object_list(
                {"reviewable_files": [None]},
                "reviewable_files",
                "OCR preview JSON",
            )

    def test_rule_coverage_rejects_missing_file(self) -> None:
        groups = [{"files": ["a.py"], "rule": "Review correctness."}]
        with self.assertRaisesRegex(
            prepare_review.OCRResponseError, r"missing=\['b.py'\]"
        ):
            prepare_review.validate_rule_coverage(groups, ["a.py", "b.py"])

    def test_path_batches_respect_byte_budget(self) -> None:
        batches = prepare_review.path_batches(
            ["a.py", "b.py", "c.py"], ["ocr"], max_bytes=14
        )
        self.assertEqual(batches, [["a.py"], ["b.py"], ["c.py"]])

    @patch("skill.scripts.prepare_review.subprocess.run")
    def test_run_json_reports_timeout(self, run: unittest.mock.Mock) -> None:
        run.side_effect = subprocess.TimeoutExpired(["ocr"], timeout=5)
        with self.assertRaisesRegex(RuntimeError, "timed out after 5s"):
            prepare_review.run_json(["ocr"], Path("/repo"), 5)

    def test_changed_lines_from_diff_maps_added_lines(self) -> None:
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -2,2 +2,3 @@
 old
+new
 same
"""
        self.assertEqual(prepare_review.changed_lines_from_diff(diff), {"a.py": {3}})

    def test_build_review_packets_is_stable_and_marks_large_groups(self) -> None:
        manifest = {
            "selection": {
                "mode": "workspace",
                "reviewable_files": [{"path": "b.py"}, {"path": "a.py"}],
            },
            "rule_groups": [
                {"group_id": 7, "files": ["b.py", "a.py"], "rule": "Correctness"}
            ],
        }
        diff = """--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
+x
"""
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(prepare_review, "_scope_diff", return_value=diff),
        ):
            repo = Path(directory)
            (repo / "b.py").write_text("one\ntwo\n", encoding="utf-8")
            packets = prepare_review.build_review_packets(
                manifest, repo, max_plan_lines=0
            )

        self.assertEqual(
            [item["path"] for item in packets[0]["files"]], ["a.py", "b.py"]
        )
        self.assertTrue(packets[0]["plan_required"])
        self.assertEqual(packets[0]["files"][0]["changed_lines"], [1])

    def test_workspace_without_head_synthesizes_selected_file_diff(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                prepare_review.subprocess,
                "run",
                return_value=unittest.mock.Mock(returncode=1),
            ),
        ):
            repo = Path(directory)
            (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
            diffs, changed = prepare_review._file_diff_map(
                repo, {"mode": "workspace"}, ["a.py"]
            )

        self.assertIn("+++ b/a.py", diffs["a.py"])
        self.assertEqual(changed["a.py"], {1})

    def test_review_packets_split_to_respect_byte_budget(self) -> None:
        manifest = {
            "schema_version": "2",
            "selection": {"reviewable_files": [{"path": "a.py"}]},
            "rule_groups": [{"group_id": 1, "files": ["a.py"], "rule": "Correctness"}],
        }
        diff = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,100 @@\n" + "".join(
            f"+value_{index} = {index}\n" for index in range(1, 101)
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                prepare_review,
                "_file_diff_map",
                return_value=({"a.py": diff}, {"a.py": set(range(1, 101))}),
            ),
        ):
            packets = prepare_review.build_review_packets(
                manifest, Path(directory), max_packet_bytes=700
            )

        self.assertGreater(len(packets), 1)
        self.assertTrue(
            all(prepare_review._json_size(packet) <= 700 for packet in packets)
        )
        self.assertTrue(all(packet["review_files"] == ["a.py"] for packet in packets))
        represented = {
            line
            for packet in packets
            for file in packet["files"]
            for line in file["changed_lines"]
        }
        self.assertEqual(represented, set(range(1, 101)))

    def test_normalize_findings_rejects_scope_and_changed_line_violations(self) -> None:
        finding = {
            "path": "other.py",
            "anchor": {"start_line": 9, "end_line": 9},
            "severity": "high",
            "category": "bug",
            "claim": "broken",
            "evidence": "evidence",
            "impact": "impact",
            "fix": "fix",
            "confidence": "high",
        }
        valid, rejected = prepare_review.normalize_findings(
            [finding], {"a.py"}, {"a.py": {1, 2}}
        )
        self.assertEqual(valid, [])
        self.assertIn("path is outside selected review set", rejected[0]["errors"])

    def test_finding_anchor_rejects_boolean_line_numbers(self) -> None:
        finding = {
            "path": "a.py",
            "anchor": {"start_line": True, "end_line": True},
            "severity": "high",
            "category": "bug",
            "claim": "broken",
            "evidence": "evidence",
            "impact": "impact",
            "fix": "fix",
            "confidence": "high",
        }
        self.assertIn(
            "anchor requires integer start_line and end_line",
            prepare_review.validate_finding(finding, {"a.py"}, {"a.py": {1}}),
        )

    def test_normalize_findings_deduplicates_and_sorts_by_severity(self) -> None:
        base = {
            "path": "a.py",
            "anchor": {"start_line": 2, "end_line": 2},
            "category": "bug",
            "claim": "same issue",
            "evidence": "evidence",
            "impact": "impact",
            "fix": "fix",
            "confidence": "high",
        }
        findings = [
            {**base, "severity": "low"},
            {**base, "severity": "low"},
            {
                **base,
                "severity": "critical",
                "anchor": {"start_line": 1, "end_line": 1},
            },
        ]
        valid, rejected = prepare_review.normalize_findings(
            findings, {"a.py"}, {"a.py": {1, 2}}
        )
        self.assertEqual([item["severity"] for item in valid], ["critical", "low"])
        self.assertEqual(len(rejected), 1)

    def test_parse_plan_limits_checkpoints(self) -> None:
        plan = prepare_review.parse_plan(
            {
                "summary": "summary",
                "checkpoints": [
                    {"focus": str(index), "why": "reason"} for index in range(8)
                ],
            }
        )
        self.assertEqual(len(plan["checkpoints"]), 5)

    def test_manifest_validation_keeps_schema_one_compatibility(self) -> None:
        prepare_review.validate_manifest(
            {
                "schema_version": "1",
                "selection": {"reviewable_files": [{"path": "a.py"}]},
                "rule_groups": [{"files": ["a.py"], "rule": "correctness"}],
            }
        )

    def test_validation_queue_keeps_uncertain_and_selects_risky_findings(self) -> None:
        finding = {
            "severity": "high",
            "confidence": "high",
            "path": "a.py",
        }
        self.assertEqual(prepare_review.validation_queue([finding]), [finding])
        revised, decision = prepare_review.apply_validation_decision(
            finding, {"decision": "uncertain", "reason": "ambiguous"}
        )
        self.assertEqual((revised, decision), (finding, "uncertain"))


if __name__ == "__main__":
    unittest.main()

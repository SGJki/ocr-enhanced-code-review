import subprocess
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


if __name__ == "__main__":
    unittest.main()

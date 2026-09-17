import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MetadataTests(unittest.TestCase):
    def test_skill_frontmatter_matches_directory_name(self) -> None:
        skill_text = (PROJECT_ROOT / "SKILL.md").read_text(encoding="utf-8")
        _, frontmatter, _ = skill_text.split("---", 2)
        metadata = yaml.safe_load(frontmatter)

        self.assertEqual(metadata["name"], PROJECT_ROOT.name)
        self.assertTrue(metadata["description"])

    def test_openai_prompt_explicitly_invokes_skill(self) -> None:
        config = yaml.safe_load(
            (PROJECT_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        )
        prompt = config["interface"]["default_prompt"]

        self.assertIn(f"${PROJECT_ROOT.name}", prompt)
        self.assertIn("full-file", prompt)


if __name__ == "__main__":
    unittest.main()

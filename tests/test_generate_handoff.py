from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_handoff.py"


def run(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


class HandoffIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.repository = self.base / "sample-project"
        self.output = self.base / "output"
        self.repository.mkdir()
        run(["git", "init", "-b", "main"], self.repository)
        run(["git", "config", "user.name", "Test User"], self.repository)
        run(["git", "config", "user.email", "test@example.invalid"], self.repository)
        (self.repository / "app.py").write_text("print('before')\n", encoding="utf-8")
        (self.repository / "README.md").write_text("# Sample\n", encoding="utf-8")
        run(["git", "add", "."], self.repository)
        run(["git", "commit", "-m", "initial"], self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def generate(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(self.repository),
                "--output-root",
                str(self.output),
                "--no-open-folder",
                *arguments,
            ],
            self.repository,
            check=check,
        )

    def latest(self) -> Path:
        return self.output / self.repository.name / "latest"

    def test_collects_changes_and_preserves_repository_state(self) -> None:
        (self.repository / "app.py").write_text("print('staged')\n", encoding="utf-8")
        run(["git", "add", "app.py"], self.repository)
        (self.repository / "README.md").write_text("# Updated\n", encoding="utf-8")
        (self.repository / "new_test.py").write_text("def test_new():\n    pass\n", encoding="utf-8")
        status_before = run(["git", "status", "--porcelain=v1"], self.repository).stdout
        head_before = run(["git", "rev-parse", "HEAD"], self.repository).stdout

        result = self.generate()

        self.assertIn("ChatGPT用成果物:", result.stdout)
        self.assertEqual(status_before, run(["git", "status", "--porcelain=v1"], self.repository).stdout)
        self.assertEqual(head_before, run(["git", "rev-parse", "HEAD"], self.repository).stdout)
        report = (self.latest() / "git-diff-all.txt").read_text(encoding="utf-8")
        self.assertIn("staged diff", report)
        self.assertIn("unstaged diff", report)
        self.assertIn("BEGIN UNTRACKED FILE: new_test.py", report)
        with zipfile.ZipFile(self.latest() / "review.zip") as archive:
            names = set(archive.namelist())
        self.assertIn("changed/app.py", names)
        self.assertIn("changed/README.md", names)
        self.assertIn("changed/new_test.py", names)

    def test_excludes_sensitive_binary_and_unsafe_files(self) -> None:
        (self.repository / ".env").write_text("PASSWORD=do-not-copy\n", encoding="utf-8")
        private_key_block = (
            "-----BEGIN " + "PRIVATE KEY-----\nabc\n-----END " + "PRIVATE KEY-----\n"
        )
        (self.repository / "private.key").write_text(
            private_key_block,
            encoding="utf-8",
        )
        (self.repository / "dump.sql").write_text("select 'secret';\n", encoding="utf-8")
        (self.repository / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.repository / "binary.dat").write_bytes(b"before\x00after")
        outside = self.base / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repository / "linked.txt").symlink_to(outside)
        (self.repository / "latin1.txt").write_bytes(b"caf\xe9")

        self.generate()

        report = (self.latest() / "git-diff-all.txt").read_text(encoding="utf-8")
        self.assertNotIn("do-not-copy", report)
        self.assertNotIn("BEGIN PRIVATE KEY", report)
        with zipfile.ZipFile(self.latest() / "review.zip") as archive:
            names = set(archive.namelist())
        excluded_files = (
            ".env",
            "private.key",
            "dump.sql",
            "image.png",
            "binary.dat",
            "linked.txt",
            "latin1.txt",
        )
        for excluded_file in excluded_files:
            self.assertNotIn(f"changed/{excluded_file}", names)

    def test_redacts_context_and_changed_text(self) -> None:
        (self.repository / "config.txt").write_text(
            'api_key = "super-secret-value"\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\n',
            encoding="utf-8",
        )
        context = self.base / "context.json"
        context.write_text(
            json.dumps(
                {
                    "purpose": 'client_secret="context-secret"',
                    "tests": "Bearer abcdefghijklmnopqrstuvwxyz",
                    "blockers": "none",
                }
            ),
            encoding="utf-8",
        )

        self.generate("--context-file", str(context))

        readme = (self.latest() / "README_FOR_CHATGPT.md").read_text(encoding="utf-8")
        report = (self.latest() / "git-diff-all.txt").read_text(encoding="utf-8")
        self.assertNotIn("context-secret", readme)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", readme)
        self.assertNotIn("super-secret-value", report)
        self.assertIn("[REDACTED]", readme)

    def test_detached_head_is_reported(self) -> None:
        run(["git", "checkout", "--detach"], self.repository)
        (self.repository / "app.py").write_text("print('detached')\n", encoding="utf-8")

        self.generate()

        readme = (self.latest() / "README_FOR_CHATGPT.md").read_text(encoding="utf-8")
        self.assertIn("Branch: (detached HEAD)", readme)

    def test_size_failure_keeps_previous_latest(self) -> None:
        (self.repository / "first.txt").write_text("small\n", encoding="utf-8")
        self.generate()
        previous = (self.latest() / "git-diff-all.txt").read_bytes()
        (self.repository / "large.txt").write_text("large-data-" * 10000, encoding="utf-8")

        result = self.generate("--max-size-mb", "0.0001", check=False)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("too large", result.stderr)
        self.assertEqual(previous, (self.latest() / "git-diff-all.txt").read_bytes())

    def test_archive_retains_ten_generations(self) -> None:
        for generation in range(12):
            (self.repository / "app.py").write_text(
                f"print({generation})\n",
                encoding="utf-8",
            )
            self.generate()

        archive = self.output / self.repository.name / "archive"
        generations = [path for path in archive.iterdir() if path.is_dir()]
        self.assertEqual(10, len(generations))


if __name__ == "__main__":
    unittest.main()

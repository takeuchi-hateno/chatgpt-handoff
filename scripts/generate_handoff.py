#!/usr/bin/env python3
"""Generate a local, secret-safe ChatGPT review handoff for a Git repository."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


DEFAULT_MAX_SIZE_MB = 50
HANDOFF_ROOT_NAME = "ChatGPT-Handoff"
LATEST_REVIEW_NAME = "LATEST_REVIEW.zip"
ARCHIVE_GENERATION_PATTERN = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")

EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    "node_modules",
    "vendor",
    "cache",
    "build",
    "dist",
    ".next",
    ".nuxt",
    "coverage",
    "tmp",
    "temp",
}

EXCLUDED_EXTENSIONS = {
    ".db",
    ".dump",
    ".gz",
    ".gif",
    ".ico",
    ".iso",
    ".jar",
    ".jpeg",
    ".jpg",
    ".key",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".p12",
    ".pfx",
    ".pem",
    ".png",
    ".bin",
    ".class",
    ".docx",
    ".dmg",
    ".o",
    ".pptx",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".sql",
    ".tar",
    ".tgz",
    ".webm",
    ".webp",
    ".wasm",
    ".xls",
    ".xlsx",
    ".zip",
}

SECRET_NAME_PATTERN = re.compile(
    r"(?:^|[-_. ])(?:credential|credentials|secret|secrets|"
    r"service[-_ ]account|private[-_ ]key)(?:[-_. ]|$)",
    re.IGNORECASE,
)

PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)

AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
COMMON_TOKEN_PATTERN = re.compile(
    r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xoxb-|xoxp-|xoxa-|AIza)"
    r"[A-Za-z0-9_\-]{16,}\b"
)
BEARER_TOKEN_PATTERN = re.compile(
    r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{20,}",
    re.IGNORECASE,
)
URL_CREDENTIAL_PATTERN = re.compile(
    r"(?P<prefix>https?://[^/\s:@]+:)(?P<password>[^/\s@]+)(?P<suffix>@)",
    re.IGNORECASE,
)
QUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>(?:[\"']?\b(?:api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|secret(?:[_-]?key)?|password|passwd)\b[\"']?"
    r"\s*[:=]\s*))"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
QUOTED_TOKEN_FIELD_PATTERN = re.compile(
    r"(?P<prefix>[\"'](?:token|refresh_token|id_token)[\"']\s*:\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
UNQUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>\b(?:aws_secret_access_key|database_url|db_url|dsn|"
    r"private_key_id|access_token|refresh_token|id_token)\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;}]+)",
    re.IGNORECASE,
)


class HandoffError(RuntimeError):
    """Raised for an expected, user-actionable handoff generation failure."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a local ChatGPT review handoff from a Git repository."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Git repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--purpose",
        default=None,
        help="Verified purpose from the current session.",
    )
    parser.add_argument(
        "--tests",
        default=None,
        help="Verified test/check results from the current session.",
    )
    parser.add_argument(
        "--blockers",
        default=None,
        help="Explicit blockers from the current session.",
    )
    parser.add_argument(
        "--context-file",
        default=None,
        help="JSON file with optional purpose, tests, and blockers fields.",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help=(
            "Artifact root directory. Defaults to ~/ChatGPT-Handoff. "
            "Useful for tests and isolated automation."
        ),
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"Maximum review ZIP size in MiB. Defaults to {DEFAULT_MAX_SIZE_MB}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect the repository and print the planned scope without writing artifacts.",
    )
    parser.add_argument(
        "--no-open-folder",
        action="store_true",
        help="Do not open the common handoff folder in Finder on macOS.",
    )
    return parser.parse_args()


def run_git(repository: Path, arguments: list[str], check: bool = True) -> str:
    command = ["git", *arguments]
    result = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        error_text = result.stderr.strip()
        if error_text == "":
            error_text = "git command failed without an error message"
        raise HandoffError(f"{' '.join(command)}: {error_text}")
    return result.stdout


def find_repository_root(repository_argument: str) -> Path:
    candidate = Path(repository_argument).expanduser().resolve()
    if not candidate.exists():
        raise HandoffError(f"Repository path does not exist: {candidate}")
    output = run_git(candidate, ["rev-parse", "--show-toplevel"])
    root_text = output.strip()
    if root_text == "":
        raise HandoffError("Git returned an empty repository root")
    return Path(root_text).resolve()


def normalise_git_path(path_text: str) -> str | None:
    if path_text == "":
        return None
    path = PurePosixPath(path_text)
    if path.is_absolute():
        return None
    if ".." in path.parts:
        return None
    return path.as_posix()


def split_null_terminated_names(output: str) -> list[str]:
    names: list[str] = []
    for item in output.split("\0"):
        normalised = normalise_git_path(item)
        if normalised is not None:
            names.append(normalised)
    return names


def get_repository_state(repository: Path) -> dict[str, object]:
    branch = run_git(repository, ["branch", "--show-current"]).strip()
    if branch == "":
        branch = "(detached HEAD)"

    head = run_git(repository, ["rev-parse", "HEAD"]).strip()
    remotes = run_git(repository, ["remote"], check=False).splitlines()
    remote_name = ""
    remote_head_ref = ""
    remote_head = ""

    if len(remotes) > 0:
        remote_name = remotes[0].strip()
        if remote_name != "":
            symbolic_ref = run_git(
                repository,
                [
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    f"refs/remotes/{remote_name}/HEAD",
                ],
                check=False,
            ).strip()
            remote_head_ref = symbolic_ref
            remote_head = run_git(
                repository,
                ["rev-parse", "--verify", f"refs/remotes/{remote_name}/HEAD"],
                check=False,
            ).strip()

    if remote_head_ref != "" and remote_head != "":
        remote_baseline = f"{remote_head_ref} ({remote_head})"
    else:
        remote_baseline = "Unavailable (no local remote HEAD reference)"

    staged_paths = split_null_terminated_names(
        run_git(
            repository,
            ["diff", "--cached", "--name-only", "--no-renames", "-z"],
        )
    )
    unstaged_paths = split_null_terminated_names(
        run_git(
            repository,
            ["diff", "--name-only", "--no-renames", "-z"],
        )
    )
    untracked_paths = split_null_terminated_names(
        run_git(repository, ["ls-files", "--others", "--exclude-standard", "-z"])
    )

    all_paths = sorted(
        set(staged_paths).union(unstaged_paths).union(untracked_paths)
    )
    reviewable_paths = [path for path in all_paths if is_reviewable_path(path)]

    return {
        "branch": branch,
        "head": head,
        "remote_name": remote_name,
        "remote_head_ref": remote_head_ref,
        "remote_head": remote_head,
        "remote_baseline": remote_baseline,
        "status": run_git(repository, ["status", "--short"]),
        "unstaged_stat": run_git(repository, ["diff", "--stat"]),
        "staged_stat": run_git(repository, ["diff", "--cached", "--stat"]),
        "staged_paths": staged_paths,
        "unstaged_paths": unstaged_paths,
        "untracked_paths": untracked_paths,
        "all_paths": all_paths,
        "reviewable_paths": reviewable_paths,
    }


def is_secret_name(path: PurePosixPath) -> bool:
    for part in path.parts:
        lower_part = part.lower()
        if lower_part == ".env" or lower_part.startswith(".env."):
            return True
        if SECRET_NAME_PATTERN.search(part):
            return True
    return False


def is_reviewable_path(path_text: str) -> bool:
    path = PurePosixPath(path_text)
    if is_secret_name(path):
        return False

    for part in path.parts:
        if part.lower() in EXCLUDED_DIRECTORY_NAMES:
            return False

    suffix = path.suffix.lower()
    if suffix in EXCLUDED_EXTENSIONS:
        return False
    return True


def path_inside_repository(repository: Path, relative_path: str) -> Path | None:
    path = repository.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved_path = path.resolve()
        resolved_path.relative_to(repository.resolve())
    except ValueError:
        return None
    return path


def read_utf8_text(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def redact_secret_like_values(text: str) -> str:
    redacted = PRIVATE_KEY_BLOCK_PATTERN.sub("[REDACTED PRIVATE KEY BLOCK]", text)
    redacted = AWS_ACCESS_KEY_PATTERN.sub("[REDACTED AWS ACCESS KEY]", redacted)
    redacted = COMMON_TOKEN_PATTERN.sub("[REDACTED TOKEN]", redacted)
    redacted = BEARER_TOKEN_PATTERN.sub(r"\1 [REDACTED TOKEN]", redacted)
    redacted = URL_CREDENTIAL_PATTERN.sub(
        r"\g<prefix>[REDACTED URL CREDENTIAL]\g<suffix>", redacted
    )
    redacted = QUOTED_SECRET_ASSIGNMENT_PATTERN.sub(
        r"\g<prefix>\g<quote>[REDACTED]\g<quote>", redacted
    )
    redacted = QUOTED_TOKEN_FIELD_PATTERN.sub(
        r"\g<prefix>\g<quote>[REDACTED]\g<quote>", redacted
    )
    redacted = UNQUOTED_SECRET_ASSIGNMENT_PATTERN.sub(
        r"\g<prefix>[REDACTED]", redacted
    )
    return redacted


def get_diff_for_paths(
    repository: Path,
    paths: Iterable[str],
    staged: bool,
) -> str:
    diff_parts: list[str] = []
    for path in paths:
        if not is_reviewable_path(path):
            continue
        arguments = ["diff"]
        if staged:
            arguments.append("--cached")
        arguments.extend(["--no-ext-diff", "--no-textconv", "--no-renames", "--", path])
        diff_output = run_git(repository, arguments)
        if diff_output != "":
            diff_parts.append(redact_secret_like_values(diff_output))
    return "\n".join(diff_parts)


def format_section(title: str, content: str) -> str:
    if content.strip() == "":
        content = "(none)"
    return f"## {title}\n\n{content.rstrip()}\n"


def format_context_value(value: object | None, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            item_text = str(item).strip()
            if item_text != "":
                values.append(f"- {item_text}")
        if len(values) == 0:
            return fallback
        return "\n".join(values)
    value_text = str(value).strip()
    if value_text == "":
        return fallback
    return value_text


def load_context(arguments: argparse.Namespace) -> dict[str, object]:
    context: dict[str, object] = {}
    if arguments.context_file is not None:
        context_path = Path(arguments.context_file).expanduser().resolve()
        try:
            loaded = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HandoffError(f"Could not read context JSON: {context_path}: {error}")
        if not isinstance(loaded, dict):
            raise HandoffError("Context JSON must contain an object at the top level")
        context.update(loaded)

    if arguments.purpose is not None:
        context["purpose"] = arguments.purpose
    if arguments.tests is not None:
        context["tests"] = arguments.tests
    if arguments.blockers is not None:
        context["blockers"] = arguments.blockers
    return context


def format_changed_files(repository: Path, paths: list[str]) -> str:
    lines: list[str] = []
    for path in paths:
        if is_reviewable_path(path):
            lines.append(f"- {path}")

    if len(lines) == 0:
        return "No reviewable changed text files were found."
    return "\n".join(lines)


def format_diff_report(
    repository: Path,
    state: dict[str, object],
) -> str:
    all_paths = state["all_paths"]
    reviewable_paths = state["reviewable_paths"]
    untracked_paths = state["untracked_paths"]
    if not isinstance(all_paths, list):
        raise HandoffError("Internal error: Git path list has an unexpected type")
    if not isinstance(reviewable_paths, list):
        raise HandoffError("Internal error: reviewable path list has an unexpected type")
    if not isinstance(untracked_paths, list):
        raise HandoffError("Internal error: untracked path list has an unexpected type")

    staged_diff = get_diff_for_paths(repository, reviewable_paths, staged=True)
    unstaged_diff = get_diff_for_paths(repository, reviewable_paths, staged=False)
    untracked_parts: list[str] = []

    for path in untracked_paths:
        if not is_reviewable_path(path):
            continue
        source_path = path_inside_repository(repository, path)
        if source_path is None:
            continue
        text = read_utf8_text(source_path)
        if text is None:
            continue
        untracked_parts.append(
            f"--- BEGIN UNTRACKED FILE: {path} ---\n"
            f"{redact_secret_like_values(text).rstrip()}\n"
            f"--- END UNTRACKED FILE: {path} ---"
        )

    if len(untracked_parts) == 0:
        untracked_text = "(no safe untracked UTF-8 text files)"
    else:
        untracked_text = "\n\n".join(untracked_parts)

    repository_path = str(repository)
    project_name = repository.name
    report_parts = [
        "# Git Diff All",
        "",
        f"Repository: {repository_path}",
        f"Project: {project_name}",
        f"Branch: {state['branch']}",
        f"HEAD: {state['head']}",
        f"Remote HEAD: {state['remote_baseline']}",
        "",
        format_section("git status --short", str(state["status"])),
        format_section("git diff --stat", str(state["unstaged_stat"])),
        format_section("git diff --cached --stat", str(state["staged_stat"])),
        format_section("staged diff", staged_diff),
        format_section("unstaged diff", unstaged_diff),
        format_section("untracked text files", untracked_text),
        "## Safety exclusions",
        "",
        "Sensitive paths, dependency/build/cache directories, database dumps, and binary/media contents were excluded.",
        "",
    ]
    return "\n".join(report_parts)


def build_readme(
    repository: Path,
    state: dict[str, object],
    context: dict[str, object],
    generated_at: str,
) -> str:
    purpose = redact_secret_like_values(
        format_context_value(
            context.get("purpose"), "Review current working-tree changes"
        )
    )
    tests = redact_secret_like_values(
        format_context_value(
            context.get("tests"),
            "No verified test/check result was provided by the current session.",
        )
    )
    blockers = redact_secret_like_values(
        format_context_value(
            context.get("blockers"),
            "No blocker was explicitly reported by the current session.",
        )
    )
    changed_files = format_changed_files(repository, state["all_paths"])

    return (
        "# ChatGPT Review Handoff\n\n"
        f"Project: {repository.name}\n"
        f"Repository: {repository}\n"
        f"GeneratedAt: {generated_at}\n"
        f"Branch: {state['branch']}\n"
        f"HEAD: {state['head']}\n"
        f"Remote/Baseline: {state['remote_baseline']}\n\n"
        "## Purpose\n\n"
        f"{purpose}\n\n"
        "## Git state\n\n"
        "See `git-diff-all.txt` for status, statistics, staged diff, unstaged diff, and safe untracked text files.\n\n"
        "## Tests / Checks\n\n"
        f"{tests}\n\n"
        "## Changed files\n\n"
        f"{changed_files}\n\n"
        "## Known blockers\n\n"
        f"{blockers}\n\n"
        "## Review request\n\n"
        "ChatGPTには以下を依頼:\n"
        "1. GO / NO-GO\n"
        "2. regression / security / transaction / auth / data consistency review\n"
        "3. documentation contradiction\n"
        "4. remaining blockers\n"
        "5. exact next action\n\n"
        "## Important\n\n"
        "- implementation is evidence, not necessarily specification\n"
        "- do not expose secrets\n"
        "- staging/production状態をlocal testから推測しない\n"
    )


def collect_handoff_files(repository: Path) -> list[str]:
    candidates: list[str] = []
    for candidate_name in ("handoff", "HANDOFF.md"):
        candidate = repository / candidate_name
        if not candidate.exists() or candidate.is_symlink():
            continue
        if candidate.is_file():
            candidates.append(candidate_name)
            continue
        if not candidate.is_dir():
            continue
        for child in sorted(candidate.rglob("*")):
            if child.is_symlink() or not child.is_file():
                continue
            try:
                relative_path = child.relative_to(repository).as_posix()
            except ValueError:
                continue
            if is_reviewable_path(relative_path):
                candidates.append(relative_path)
    return sorted(set(candidates))


def is_under_handoff(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return len(path.parts) > 0 and path.parts[0].lower() == "handoff"


def copy_safe_text_file(
    repository: Path,
    stage_root: Path,
    relative_path: str,
    destination_prefix: str,
) -> tuple[bool, str]:
    if not is_reviewable_path(relative_path):
        return False, "excluded by safety rules"
    source_path = path_inside_repository(repository, relative_path)
    if source_path is None:
        return False, "path resolves outside repository"
    text = read_utf8_text(source_path)
    if text is None:
        return False, "not a safe UTF-8 text file"

    destination_path = stage_root / destination_prefix
    destination_path = destination_path.joinpath(*PurePosixPath(relative_path).parts)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    redacted_text = redact_secret_like_values(text)
    destination_path.write_bytes(redacted_text.encode("utf-8"))
    return True, "included"


def write_stage_files(
    repository: Path,
    stage_root: Path,
    state: dict[str, object],
    context: dict[str, object],
    generated_at: str,
) -> tuple[list[str], list[str]]:
    readme = build_readme(repository, state, context, generated_at)
    (stage_root / "README_FOR_CHATGPT.md").write_text(readme, encoding="utf-8")

    diff_report = format_diff_report(repository, state)
    (stage_root / "git-diff-all.txt").write_text(diff_report, encoding="utf-8")

    included_files: list[str] = [
        "README_FOR_CHATGPT.md",
        "git-diff-all.txt",
    ]
    skipped_files: list[str] = []

    all_paths = state["all_paths"]
    if not isinstance(all_paths, list):
        raise HandoffError("Internal error: Git path list has an unexpected type")
    for relative_path in all_paths:
        if is_under_handoff(relative_path):
            continue
        included, reason = copy_safe_text_file(
            repository,
            stage_root,
            relative_path,
            "changed",
        )
        if included:
            included_files.append(f"changed/{relative_path}")
        else:
            skipped_files.append(f"{relative_path}: {reason}")

    handoff_paths = collect_handoff_files(repository)
    for relative_path in handoff_paths:
        included, reason = copy_safe_text_file(
            repository,
            stage_root,
            relative_path,
            "handoff",
        )
        if included:
            included_files.append(f"handoff/{relative_path}")
        else:
            skipped_files.append(f"{relative_path}: {reason}")

    return included_files, skipped_files


def create_zip(stage_root: Path, zip_path: Path, max_size_bytes: int) -> int:
    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for file_path in sorted(stage_root.rglob("*")):
            if not file_path.is_file() or file_path.is_symlink():
                continue
            if file_path == zip_path:
                continue
            archive_name = file_path.relative_to(stage_root).as_posix()
            archive.write(file_path, archive_name)

    size_bytes = zip_path.stat().st_size
    if size_bytes > max_size_bytes:
        size_mib = size_bytes / (1024 * 1024)
        limit_mib = max_size_bytes / (1024 * 1024)
        raise HandoffError(
            f"review.zip is too large ({size_mib:.2f} MiB; limit {limit_mib:.2f} MiB). "
            "Generation stopped before latest replacement. Inspect changed files, handoff, or large text content."
        )
    return size_bytes


def assert_no_unredacted_secrets(stage_root: Path) -> None:
    """Fail closed if generated text still contains a supported secret pattern."""
    for file_path in sorted(stage_root.rglob("*")):
        if not file_path.is_file() or file_path.is_symlink():
            continue
        text = read_utf8_text(file_path)
        if text is None:
            continue
        if redact_secret_like_values(text) != text:
            relative_path = file_path.relative_to(stage_root).as_posix()
            raise HandoffError(
                "Generated output still contains a secret-like value after redaction: "
                f"{relative_path}"
            )


def make_archive_generation_path(archive_root: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = archive_root / timestamp
    suffix = 1
    while candidate.exists():
        candidate = archive_root / f"{timestamp}-{suffix}"
        suffix += 1
    return candidate


def rotate_archives(archive_root: Path, keep_count: int = 10) -> list[str]:
    generations = [
        path
        for path in archive_root.iterdir()
        if path.is_dir() and ARCHIVE_GENERATION_PATTERN.match(path.name)
    ]
    generations.sort(key=lambda path: path.name, reverse=True)
    removed: list[str] = []
    for old_generation in generations[keep_count:]:
        shutil.rmtree(old_generation)
        removed.append(old_generation.name)
    return removed


def open_handoff_folder(handoff_root: Path) -> str | None:
    if platform.system() != "Darwin":
        return None
    open_command = shutil.which("open")
    if open_command is None:
        return "macOS `open` command was not available"
    result = subprocess.run(
        [open_command, str(handoff_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        error_text = result.stderr.strip()
        if error_text == "":
            error_text = "unknown Finder error"
        return f"Finder could not open {handoff_root}: {error_text}"
    return None


def print_dry_run(repository: Path, state: dict[str, object], handoff_paths: list[str]) -> None:
    all_paths = state["all_paths"]
    reviewable_paths = state["reviewable_paths"]
    print(f"Repository: {repository}")
    print(f"Project: {repository.name}")
    print(f"Branch: {state['branch']}")
    print(f"HEAD: {state['head']}")
    print(f"Remote/Baseline: {state['remote_baseline']}")
    print(f"Changed paths: {len(all_paths)}")
    print(f"Reviewable paths by path rules: {len(reviewable_paths)}")
    print(f"Handoff files discovered: {len(handoff_paths)}")
    print("No artifacts were written (--dry-run).")


def generate_handoff(arguments: argparse.Namespace) -> int:
    if arguments.max_size_mb <= 0:
        raise HandoffError("--max-size-mb must be greater than zero")

    repository = find_repository_root(arguments.repo)
    state = get_repository_state(repository)
    context = load_context(arguments)
    handoff_paths = collect_handoff_files(repository)

    if arguments.dry_run:
        print_dry_run(repository, state, handoff_paths)
        return 0

    max_size_bytes = int(arguments.max_size_mb * 1024 * 1024)
    if arguments.output_root is None:
        handoff_root = Path.home() / HANDOFF_ROOT_NAME
    else:
        handoff_root = Path(arguments.output_root).expanduser().resolve()
    project_root = handoff_root / repository.name
    latest_root = project_root / "latest"
    archive_root = project_root / "archive"
    global_latest = handoff_root / LATEST_REVIEW_NAME

    if handoff_root.is_symlink() or project_root.is_symlink() or latest_root.is_symlink():
        raise HandoffError(
            "Refusing to use a symlink at the handoff root, project root, or latest path"
        )
    if global_latest.is_symlink():
        raise HandoffError(
            f"Refusing to replace symlink at the global latest path: {global_latest}"
        )
    if global_latest.exists() and not global_latest.is_file():
        raise HandoffError(
            f"Global latest path is not a regular file: {global_latest}"
        )

    project_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)

    stage_root = Path(tempfile.mkdtemp(prefix=".latest-", dir=project_root))
    try:
        generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        included_files, skipped_files = write_stage_files(
            repository,
            stage_root,
            state,
            context,
            generated_at,
        )
        assert_no_unredacted_secrets(stage_root)
        stage_zip = stage_root / "review.zip"
        zip_size_bytes = create_zip(stage_root, stage_zip, max_size_bytes)

        if latest_root.exists():
            if not latest_root.is_dir():
                raise HandoffError(f"Existing latest path is not a directory: {latest_root}")
            archive_generation = make_archive_generation_path(archive_root)
            latest_root.rename(archive_generation)
        else:
            archive_generation = None

        stage_root.rename(latest_root)
        latest_review = latest_root / "review.zip"
        global_latest.parent.mkdir(parents=True, exist_ok=True)
        temporary_file_descriptor, temporary_file_name = tempfile.mkstemp(
            prefix=".LATEST_REVIEW-",
            suffix=".zip",
            dir=handoff_root,
        )
        os.close(temporary_file_descriptor)
        temporary_global_latest = Path(temporary_file_name)
        try:
            shutil.copy2(latest_review, temporary_global_latest)
            os.replace(temporary_global_latest, global_latest)
        finally:
            if temporary_global_latest.exists():
                temporary_global_latest.unlink()

        removed_archives = rotate_archives(archive_root)
        finder_warning = None
        if not arguments.no_open_folder:
            finder_warning = open_handoff_folder(handoff_root)

        if archive_generation is not None:
            print(f"Archived previous latest: {archive_generation}")
        print(f"Generated: {latest_root}")
        print(f"Review ZIP size: {zip_size_bytes / (1024 * 1024):.2f} MiB")
        print(f"Included files: {len(included_files)}")
        if len(skipped_files) > 0:
            print(f"Skipped files: {len(skipped_files)} (safety/text rules)")
        if len(removed_archives) > 0:
            print(f"Removed old archive generations: {len(removed_archives)}")
        if finder_warning is not None:
            print(f"Warning: {finder_warning}")
        print("ChatGPT用成果物: ~/ChatGPT-Handoff/LATEST_REVIEW.zip")
        return 0
    except Exception:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
        raise


def main() -> int:
    arguments = parse_arguments()
    try:
        return generate_handoff(arguments)
    except HandoffError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"ERROR: filesystem operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

---
name: chatgpt-handoff
description: Generate a local, secret-filtered ChatGPT review handoff for the current Git repository, including diffs, changed text files, README, archive rotation, and a stable latest ZIP path.
---

# ChatGPT Handoff

Create a lightweight review artifact when the user wants to hand current Git changes to ChatGPT. This workflow is local-only. Do not commit, push, deploy, migrate, connect to a database, create a remote repository, or upload the artifact.

## Run

Resolve the script from the installed Skill directory and run it from the target Git repository:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py"
```

Use `--repo /absolute/path/to/repository` when needed. Use `--dry-run` before writing when the scope is uncertain. The default output is `~/ChatGPT-Handoff/<project>/latest/`, with a stable copy at `~/ChatGPT-Handoff/LATEST_REVIEW.zip`.

Use `--output-root` only when the user requests another local destination or an isolated test needs one. Use `--no-open-folder` for automation or headless execution.

## Session evidence

Pass only facts verified in the current session:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chatgpt-handoff/scripts/generate_handoff.py" \
  --purpose "What should be reviewed" \
  --tests "pytest tests/test_review.py: PASS" \
  --blockers "No blocker was explicitly reported"
```

If evidence is unavailable, keep the script defaults. Do not infer staging, production, deployment, or test results.

## Safety

- Never modify the source repository, its index, `.gitignore`, branches, or remotes.
- Never use network access as part of this Skill.
- Exclude sensitive paths, dependencies, binary/media files, database dumps, and build artifacts.
- Redact supported secret-like values from diffs, copied text, handoff files, and session context.
- Treat secret filtering as defense in depth, not proof that arbitrary input is safe. If generation reports a secret-like value or abnormal size, stop and report the path or category without exposing the value.
- Keep the default 50 MiB ZIP ceiling unless the user explicitly requests another limit.

The ZIP contains the generated README and diff report plus safe changed source, tests, docs, and an existing `handoff` directory or `HANDOFF.md`. It does not contain the repository as a whole.

## Completion

Report the Skill path, invocation, and generated path briefly. End with exactly:

```text
ChatGPT用成果物: ~/ChatGPT-Handoff/LATEST_REVIEW.zip
```

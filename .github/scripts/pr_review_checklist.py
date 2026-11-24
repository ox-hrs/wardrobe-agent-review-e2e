#!/usr/bin/env python3
"""Deterministic PR hygiene checks for wardrobe.

Standard library only, so the job needs no model key. It mirrors the cheap half
of the EGM (google/elephant-goldfish-model) reviewer: the checks that need no
judgement, reported with the same file:line shape.

Checks:
  * relative Markdown links resolve in the repository tree
  * Markdown table rows stay on one line
  * workflows declare top-level ``permissions``, pin ``uses:`` to a full commit
    SHA, and set ``persist-credentials: false``
  * no credential-looking strings in added lines

Findings go to ``--out`` (JSON) and the GitHub job summary (Markdown).
Exit code: 1 when a Blocker finding exists, 0 otherwise.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

GITHUB_API_BASE = "https://api.github.com"
MAX_PAGES = 5
MAX_FILE_BYTES = 400_000

SKIP_PATH_PATTERNS: tuple[str, ...] = (
    ".scratch/**",
    "**/*.png",
    "**/*.jpg",
    "**/*.webp",
    "**/*.woff2",
    "**/*.pdf",
)
# `.docs/**` HTML/SVG/JSON are generated artifacts next to their markdown
# sources; only the sources are link-checked.
GENERATED_DIRS: tuple[str, ...] = (".docs/",)

LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# A ":" continuation line inside a table breaks GitHub row rendering.
TABLE_CONT_RE = re.compile(r"^\s*:")
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@(\S+?)(?:\s+#.*)?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PERMISSIONS_BLOCK_RE = re.compile(r"^permissions:", re.MULTILINE)
CREDENTIAL_RE = re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z]{20,}|github_pat_[0-9A-Za-z_]{20,})\b")


class CheckError(RuntimeError):
    """Raised when the GitHub API cannot be read."""


def _request(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "wardrobe-checklist",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), response.headers
    except urllib.error.HTTPError as exc:
        raise CheckError(f"GitHub API {exc.code} for {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CheckError(f"GitHub API unreachable for {url}: {exc.reason}") from exc


def _paginate(endpoint: str, token: str) -> list[Any]:
    items: list[Any] = []
    separator = "&" if "?" in endpoint else "?"
    url: str | None = f"{GITHUB_API_BASE}/{endpoint.lstrip('/')}{separator}per_page=100"
    pages = 0
    while url and pages < MAX_PAGES:
        pages += 1
        data, headers = _request(url, token)
        if isinstance(data, list):
            items.extend(data)
        url = None
        for part in (headers.get("Link") or "").split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    url = match.group(1)
    return items


def fetch_pr(repo: str, token: str, pr_number: int) -> dict[str, Any]:
    data, _ = _request(f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}", token)
    return data


def fetch_changed_files(repo: str, token: str, pr_number: int) -> list[dict[str, Any]]:
    return _paginate(f"repos/{repo}/pulls/{pr_number}/files", token)


def fetch_repo_paths(repo: str, token: str, branch: str) -> set[str]:
    data, _ = _request(
        f"{GITHUB_API_BASE}/repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1",
        token,
    )
    return {
        str(entry.get("path"))
        for entry in data.get("tree") or ()
        if entry.get("type") == "blob" and entry.get("path")
    }


def fetch_head_text(file_info: Mapping[str, Any]) -> str:
    raw_url = str(file_info.get("raw_url") or "")
    if not raw_url:
        return ""
    request = urllib.request.Request(
        raw_url, headers={"User-Agent": "wardrobe-checklist"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(MAX_FILE_BYTES + 1).decode("utf-8", errors="replace")
    except (urllib.error.URLError, UnicodeDecodeError):
        return ""


def right_side_lines(patch: str) -> set[int]:
    """RIGHT-side line numbers that the patch adds, from hunk headers."""
    added: set[int] = set()
    current = 0
    for line in patch.splitlines():
        header = re.match(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s*@@", line)
        if header:
            current = int(header.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(current)
            current += 1
        elif not line.startswith("-"):
            current += 1
    return added


def skipped_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in SKIP_PATH_PATTERNS)


def finding(severity: str, path: str, line: int, summary: str, fix: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "path": path,
        "line": line,
        "summary": summary,
        "fix": fix,
    }


def check_links(
    path: str, text: str, added: set[int], repo_paths: set[str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not path.endswith(".md"):
        return results
    parent = pathlib.PurePosixPath(path).parent
    for number, line in enumerate(text.splitlines(), start=1):
        if number not in added:
            continue
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            parsed = urllib.parse.urlparse(target)
            if not parsed.path:
                continue
            # Root-relative links resolve from the repository root; everything
            # else resolves from the directory of the file that contains it.
            if parsed.path.startswith("/"):
                candidate = parsed.path.lstrip("/")
            else:
                candidate = (parent / parsed.path).as_posix()
            if candidate in repo_paths:
                continue
            if parsed.fragment and any(
                f"{candidate}{suffix}" in repo_paths for suffix in (".html", ".svg")
            ):
                continue
            results.append(finding(
                "Major",
                path,
                number,
                f"Relative link `{target}` does not resolve in the repository tree.",
                "Point the link at an existing path, or use an absolute URL.",
            ))
    return results


def check_tables(path: str, text: str, added: set[int]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not path.endswith(".md"):
        return results
    in_table = False
    for number, line in enumerate(text.splitlines(), start=1):
        if TABLE_ROW_RE.match(line):
            in_table = True
            continue
        if in_table and number in added and TABLE_CONT_RE.match(line):
            results.append(finding(
                "Major",
                path,
                number,
                "Multiline table continuation row breaks GitHub table rendering.",
                "Keep every table row on one line.",
            ))
            continue
        if not line.strip():
            in_table = False
    return results


def check_workflow(path: str, text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not path.startswith(".github/workflows/") or not path.endswith((".yml", ".yaml")):
        return results
    if not PERMISSIONS_BLOCK_RE.search(text):
        results.append(finding(
            "Blocker",
            path,
            1,
            "Workflow declares no top-level `permissions:` block.",
            "Add `permissions: {}` and grant write scopes on the specific job.",
        ))
    uses_checkout = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = USES_RE.match(line)
        if not match:
            continue
        action, ref = match.group(1), match.group(2)
        if action.startswith("./") or action.startswith("docker://"):
            continue
        if action == "actions/checkout":
            uses_checkout = True
        if not SHA_RE.match(ref):
            results.append(finding(
                "Blocker",
                path,
                number,
                f"Action `{action}@{ref}` is not pinned to a full commit SHA.",
                "Pin `uses:` to a 40-character commit SHA with a version comment.",
            ))
    if uses_checkout and "persist-credentials: false" not in text:
        results.append(finding(
            "Major",
            path,
            1,
            "actions/checkout runs with persisted credentials.",
            "Set `persist-credentials: false` on every checkout step.",
        ))
    return results


def check_credentials(path: str, text: str, added: set[int]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if number not in added:
            continue
        if CREDENTIAL_RE.search(line):
            results.append(finding(
                "Blocker",
                path,
                number,
                "Possible credential committed in the diff.",
                "Move the value to a repository secret and rotate it.",
            ))
    return results


def render_summary(
    repo: str, pr: Mapping[str, Any], checked: Sequence[str], results: Sequence[Mapping[str, Any]]
) -> str:
    blockers = sum(1 for item in results if item.get("severity") == "Blocker")
    lines = [
        "## PR hygiene checklist",
        "",
        f"PR **#{pr.get('number')}** {pr.get('title') or ''} ({repo})",
        "",
        f"- Checked files: {len(checked)}",
        f"- Findings: {len(results)} ({blockers} blocker)",
        "",
    ]
    if results:
        lines += ["| Severity | Location | Finding | Fix |", "|---|---|---|---|"]
        for item in results:
            location = f"`{item.get('path')}:{item.get('line')}`"
            summary = str(item.get("summary", "")).replace("|", "\\|")
            fix = str(item.get("fix", "")).replace("|", "\\|")
            lines.append(f"| {item.get('severity')} | {location} | {summary} | {fix} |")
    else:
        lines.append("No hygiene findings.")
    lines.append("")
    return "\n".join(lines)


def local_tree(root: pathlib.Path) -> set[str]:
    try:
        output = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


def collect_local(root: pathlib.Path, paths: Sequence[str]) -> dict[str, tuple[str, set[int]]]:
    collected: dict[str, tuple[str, set[int]]] = {}
    for raw in paths:
        path = pathlib.PurePosixPath(raw.strip())
        target = root / path
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        collected[path.as_posix()] = (text, set(range(1, len(text.splitlines()) + 1)))
    return collected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="JohnPion-Zhang/wardrobe")
    parser.add_argument("--default-branch", default="master")
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--out", type=pathlib.Path, default=None)
    parser.add_argument("--local-files", default="")
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    args = parser.parse_args(argv)

    pr: Mapping[str, Any]
    checked: list[str] = []
    repo_paths: set[str] = set()
    file_texts: dict[str, tuple[str, set[int]]] = {}

    if args.local_files:
        pr = {"number": 0, "title": "(local files)"}
        repo_paths = local_tree(args.root)
        file_texts = collect_local(args.root, args.local_files.split(","))
        checked = sorted(file_texts)
    elif args.pr:
        token = os.environ.get("GH_TOKEN", "").strip()
        if not token:
            raise CheckError("GH_TOKEN environment variable is required")
        pr = fetch_pr(args.repo, token, args.pr)
        if str(pr.get("state", "")).lower() != "open":
            print(json.dumps({"action": "skip", "reason": "SKIP_NOT_OPEN", "pr": args.pr}))
            return 0
        repo_paths = fetch_repo_paths(args.repo, token, args.default_branch)
        files = fetch_changed_files(args.repo, token, args.pr)
        for info in files:
            path = str(info.get("filename") or "")
            patch = info.get("patch")
            if not path or skipped_path(path) or not isinstance(patch, str):
                continue
            if path.startswith(GENERATED_DIRS) and path.endswith((".html", ".svg", ".json")):
                continue
            file_texts[path] = (fetch_head_text(info), right_side_lines(patch))
        checked = sorted(file_texts)
    else:
        parser.error("pass --pr <number> or --local-files <paths>")

    results: list[dict[str, Any]] = []
    for path, (text, added) in sorted(file_texts.items()):
        if not text:
            continue
        results.extend(check_links(path, text, added, repo_paths))
        results.extend(check_tables(path, text, added))
        results.extend(check_workflow(path, text))
        results.extend(check_credentials(path, text, added))

    summary = render_summary(args.repo, pr, checked, results)
    if args.out is not None:
        args.out.write_text(
            json.dumps(
                {
                    "repo": args.repo,
                    "pr_number": args.pr,
                    "checked_files": checked,
                    "findings": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    print(summary)
    return 1 if any(item["severity"] == "Blocker" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())

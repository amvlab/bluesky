#!/usr/bin/env python3
"""
sync_upstream.py
================

Daily upstream-sync trigger.

Once a day this checks the upstream repo (default TUDelft-CNS-ATM/bluesky@master)
for commits pushed in the last N hours and fires a Claude Code routine once per
new commit. The ROUTINE does the actual work — cherry-pick/merge into the fork,
resolve conflicts with local improvements, open the PR, and update the tracking
file (its saved steps 1-7). This script only detects commits and hands each one
to the routine.

Run it:
    python sync_upstream.py            # check upstream + fire the routine
    python sync_upstream.py --dry-run  # check + print what would be sent, fire nothing

Config via environment (see .env.example):
    ROUTINE_TRIGGER_URL   the routine's /fire URL            (required)
    ROUTINE_TOKEN         per-routine bearer token, secret    (required)
    ROUTINE_BETA          beta header (default below)
    GITHUB_TOKEN          optional; lifts GitHub's 60/hr unauthenticated cap
    UPSTREAM_REPO         default TUDelft-CNS-ATM/bluesky
    UPSTREAM_BRANCH       default master
    LOOKBACK_HOURS        default 24

Re-firing a commit that's already been synced is harmless (the routine dedupes
against its tracking file), but each fire starts a real session against your
Claude Code usage quota.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

GITHUB_API = "https://api.github.com"
DEFAULT_BETA = "experimental-cc-routine-2026-04-01"


def _clean_env(name: str, default: str = "") -> str:
    """Env var with ALL whitespace removed (tokens/URLs never contain any).

    Secrets pasted into GitHub Actions sometimes pick up an embedded newline
    from a line wrap; that is illegal in an HTTP header and .strip() alone
    does not remove it.
    """
    return "".join(os.environ.get(name, default).split())


@dataclass
class Config:
    github_token: str = field(default_factory=lambda: _clean_env("GITHUB_TOKEN"))
    upstream_repo: str = os.environ.get("UPSTREAM_REPO", "TUDelft-CNS-ATM/bluesky")
    upstream_branch: str = os.environ.get("UPSTREAM_BRANCH", "master")
    max_commits: int = int(os.environ.get("MAX_COMMITS", "10"))
    lookback_hours: int = int(os.environ.get("LOOKBACK_HOURS", "24"))

    routine_url: str = field(default_factory=lambda: _clean_env("ROUTINE_TRIGGER_URL"))
    routine_token: str = field(default_factory=lambda: _clean_env("ROUTINE_TOKEN"))
    routine_beta: str = field(default_factory=lambda: _clean_env("ROUTINE_BETA", DEFAULT_BETA))


# --------------------------------------------------------------------------- #
# GitHub detection
# --------------------------------------------------------------------------- #

def _gh_headers(token: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_recent_commits(cfg: Config, since_iso: str) -> list[dict]:
    """Commits on the upstream branch pushed since `since_iso`."""
    url = f"{GITHUB_API}/repos/{cfg.upstream_repo}/commits"
    params = {"sha": cfg.upstream_branch, "since": since_iso, "per_page": 100}
    resp = requests.get(url, headers=_gh_headers(cfg.github_token), params=params, timeout=30)
    resp.raise_for_status()
    out = []
    for c in resp.json():
        commit = c.get("commit", {})
        message = commit.get("message", "") or ""
        out.append(
            {
                "sha": c["sha"],
                "summary": message.splitlines()[0] if message else "(no message)",
                "message": message,
                "author": (commit.get("author") or {}).get("name", "unknown"),
                "author_email": (commit.get("author") or {}).get("email", ""),
                "date": (commit.get("author") or {}).get("date", ""),
                "html_url": c.get("html_url", ""),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Routine fire
# --------------------------------------------------------------------------- #

def build_text(cfg: Config, commit: dict) -> str:
    """The /fire `text` field: run-specific context handed to the routine."""
    return (
        f"A new commit was pushed to the {cfg.upstream_branch} branch of "
        f"https://github.com/{cfg.upstream_repo}.\n\n"
        f"Commit hash: {commit['sha']}\n"
        f"Author: {commit['author']} <{commit['author_email']}>\n"
        f"Summary: {commit['summary']}\n"
        f"Commit URL: {commit['html_url']}\n\n"
        f"Full message:\n{commit['message']}\n\n"
        f"Run the upstream-sync for this commit: dedupe against the tracking file, "
        f"and if new, cherry-pick/merge it into the fork, resolve any conflicts with "
        f"local improvements, open the PR, and update the tracking file."
    )


def fire(cfg: Config, text: str, timeout: int = 60) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {cfg.routine_token}",
        "anthropic-beta": cfg.routine_beta,
        "Content-Type": "application/json",
    }
    return requests.post(cfg.routine_url, headers=headers, json={"text": text}, timeout=timeout)


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-8601 commit date to an aware UTC datetime (for sorting)."""
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _session_link(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    for key in ("session_url", "url", "session_id", "id"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    return json.dumps(data)[:200]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Daily upstream check + Claude Code routine fire.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect commits and print what would be sent, but fire nothing.")
    args = parser.parse_args()

    cfg = Config()

    if not args.dry_run:
        missing = []
        if not cfg.routine_url:
            missing.append("ROUTINE_TRIGGER_URL")
        if not cfg.routine_token:
            missing.append("ROUTINE_TOKEN")
        if missing:
            print("ERROR: missing " + ", ".join(missing) +
                  ". Get them from the routine's API-trigger modal "
                  "('Generate token' shows the token only once).", file=sys.stderr)
            return 2

    since = (datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)).isoformat()
    print(f"Checking {cfg.upstream_repo}@{cfg.upstream_branch} for commits since {since}")

    try:
        commits = list_recent_commits(cfg, since)
    except requests.HTTPError as exc:
        body = getattr(exc.response, "text", "")[:200]
        print(f"GitHub API error: {exc} — {body}", file=sys.stderr)
        print("(Unauthenticated GitHub API is 60 req/hour — set GITHUB_TOKEN.)", file=sys.stderr)
        return 1

    print(f"Found {len(commits)} commit(s) in the window.\n")
    if not commits:
        print("Nothing to sync.")
        return 0

    # Oldest-first so cherry-picks apply in chronological order.
    commits.sort(key=lambda c: _parse_dt(c.get("date", "")))

    if len(commits) > cfg.max_commits:
        skipped = commits[cfg.max_commits:]
        commits = commits[: cfg.max_commits]
        print(f"WARNING: window has more than MAX_COMMITS={cfg.max_commits} commits. "
              f"Firing the {cfg.max_commits} oldest; skipping {len(skipped)}:")
        for c in skipped:
            print(f"    skipped {c['sha'][:10]}  {c['summary']}")
        print("    (raise MAX_COMMITS, widen LOOKBACK_HOURS, or fire these manually — "
              "skipped commits are NOT retried automatically.)\n")

    failures = 0
    for c in commits:
        text = build_text(cfg, c)
        label = f"{c['sha'][:10]}  {c['summary']}"
        if args.dry_run:
            print(f"[dry-run] would fire for {label}\n--- text ---\n{text}\n")
            continue
        try:
            resp = fire(cfg, text)
            ok = resp.status_code < 400
            print(f"[{'ok' if ok else 'FAIL'} {resp.status_code}] fired {label}")
            if ok:
                link = _session_link(resp)
                if link:
                    print(f"    session: {link}")
            else:
                failures += 1
                print(f"    response: {resp.text[:300]}")
        except requests.RequestException as exc:
            failures += 1
            print(f"[FAIL] {label}: {exc}", file=sys.stderr)

    if args.dry_run:
        print("Dry run: nothing was sent.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
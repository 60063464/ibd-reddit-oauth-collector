#!/usr/bin/env python3
"""Fetch short-lived Reddit evidence through an approved OAuth client.

The collector intentionally omits usernames and writes raw text to an ignored,
short-lived cache. The Ideation Agent must paraphrase the evidence and purge the
raw file after producing the dated ideation artifact.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_SUBREDDITS = ("CrohnsDisease", "UlcerativeColitis", "IBD")
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


class CollectorError(RuntimeError):
    pass


def truncate(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def listing_children(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    if not isinstance(children, list):
        return []
    return [item for item in children if isinstance(item, dict)]


def parse_post(child: dict[str, Any], cutoff_epoch: float) -> dict[str, Any] | None:
    data = child.get("data")
    if not isinstance(data, dict):
        return None
    created = float(data.get("created_utc") or 0)
    if created < cutoff_epoch or data.get("stickied"):
        return None
    post_id = str(data.get("id") or "")
    subreddit = str(data.get("subreddit") or "")
    if not post_id or not subreddit:
        return None
    return {
        "signal_id": f"{subreddit}:{post_id}",
        "subreddit": subreddit,
        "created_utc": created,
        "permalink": f"https://www.reddit.com{data.get('permalink', '')}",
        "title": truncate(data.get("title"), 500),
        "body_excerpt": truncate(data.get("selftext"), 1800),
        "score": int(data.get("score") or 0),
        "num_comments": int(data.get("num_comments") or 0),
        "comments": [],
    }


def extract_comments(payload: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    result: list[dict[str, Any]] = []

    def visit(children: list[dict[str, Any]]) -> None:
        for child in children:
            if len(result) >= limit:
                return
            if child.get("kind") != "t1":
                continue
            data = child.get("data")
            if not isinstance(data, dict):
                continue
            body = truncate(data.get("body"), 900)
            if body and body not in ("[deleted]", "[removed]"):
                result.append(
                    {
                        "body_excerpt": body,
                        "score": int(data.get("score") or 0),
                        "created_utc": float(data.get("created_utc") or 0),
                    }
                )
            replies = data.get("replies")
            if isinstance(replies, dict):
                visit(listing_children(replies))

    visit(listing_children(payload[1]))
    return result


def evidence_status(
    posts: list[dict[str, Any]], min_posts: int, min_subreddits: int
) -> tuple[str, list[str]]:
    represented = sorted({post["subreddit"] for post in posts})
    reasons: list[str] = []
    if len(posts) < min_posts:
        reasons.append(f"only {len(posts)} recent posts; minimum is {min_posts}")
    if len(represented) < min_subreddits:
        reasons.append(
            f"only {len(represented)} subreddits represented; minimum is {min_subreddits}"
        )
    return ("live-evidence" if not reasons else "insufficient-live-evidence", reasons)


def purge_expired(directory: Path, max_age_hours: int, now: datetime | None = None) -> int:
    if not directory.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_hours * 3600
    removed = 0
    for path in directory.glob("reddit-raw-*.json"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


class RedditClient:
    def __init__(self, client_id: str, client_secret: str, user_agent: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.token = ""

    def authenticate(self) -> None:
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                "Authorization": f"Basic {credentials}",
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        payload = self._open_json(request)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise CollectorError("Reddit OAuth response did not contain an access token")
        self.token = str(token)

    def get_json(self, path: str, params: dict[str, Any]) -> Any:
        if not self.token:
            raise CollectorError("authenticate() must run before API requests")
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": self.user_agent,
            },
        )
        return self._open_json(request)

    @staticmethod
    def _open_json(request: urllib.request.Request) -> Any:
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise CollectorError(f"Reddit request failed with HTTP {exc.code}") from exc
                retry_after = int(exc.headers.get("Retry-After", "5"))
                time.sleep(min(retry_after, 60))
            except urllib.error.URLError as exc:
                if attempt == 3:
                    raise CollectorError(f"Reddit request failed: {exc.reason}") from exc
                time.sleep(2**attempt)
        raise CollectorError("Reddit request failed after retries")


def require_configuration(credentials_file: Path) -> tuple[str, str, str]:
    file_config: dict[str, Any] = {}
    if credentials_file.exists():
        try:
            loaded = json.loads(credentials_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectorError(f"Could not read credentials file {credentials_file}") from exc
        if not isinstance(loaded, dict):
            raise CollectorError(f"Credentials file {credentials_file} must contain a JSON object")
        file_config = loaded

    approved = os.environ.get("REDDIT_ACCESS_APPROVED") or file_config.get(
        "access_approved"
    )
    if approved not in ("yes", True):
        raise CollectorError(
            "Set REDDIT_ACCESS_APPROVED=yes only after Reddit has approved this exact use case"
        )
    client_id = str(
        os.environ.get("REDDIT_CLIENT_ID") or file_config.get("client_id") or ""
    ).strip()
    client_secret = str(
        os.environ.get("REDDIT_CLIENT_SECRET") or file_config.get("client_secret") or ""
    ).strip()
    user_agent = str(
        os.environ.get("REDDIT_USER_AGENT") or file_config.get("user_agent") or ""
    ).strip()
    missing = [
        name
        for name, value in (
            ("REDDIT_CLIENT_ID", client_id),
            ("REDDIT_CLIENT_SECRET", client_secret),
            ("REDDIT_USER_AGENT", user_agent),
        )
        if not value
    ]
    if missing:
        raise CollectorError(f"Missing required environment variables: {', '.join(missing)}")
    if "by /u/" not in user_agent or len(user_agent) < 20:
        raise CollectorError(
            "REDDIT_USER_AGENT must be descriptive, for example "
            "macos:ibd-youtube-signals:1.0 (by /u/yourname)"
        )
    return client_id, client_secret, user_agent


def collect(args: argparse.Namespace) -> int:
    client_id, client_secret, user_agent = require_configuration(
        Path(args.credentials_file).expanduser()
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    purge_expired(output.parent, args.retention_hours)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.window_days)
    client = RedditClient(client_id, client_secret, user_agent)
    client.authenticate()

    posts: list[dict[str, Any]] = []
    for subreddit in args.subreddits:
        payload = client.get_json(
            f"/r/{subreddit}/new",
            {"limit": args.posts_per_subreddit, "raw_json": 1},
        )
        subreddit_posts = [
            post
            for child in listing_children(payload)
            if (post := parse_post(child, cutoff.timestamp())) is not None
        ]
        subreddit_posts.sort(
            key=lambda post: (post["num_comments"], post["score"]), reverse=True
        )
        for post in subreddit_posts[: args.comment_threads_per_subreddit]:
            post_id = post["signal_id"].split(":", 1)[1]
            comments_payload = client.get_json(
                f"/comments/{post_id}",
                {"limit": args.comments_per_thread, "depth": 3, "raw_json": 1},
            )
            post["comments"] = extract_comments(
                comments_payload, args.comments_per_thread
            )
        posts.extend(subreddit_posts)

    posts.sort(key=lambda post: post["created_utc"], reverse=True)
    status, reasons = evidence_status(posts, args.min_posts, args.min_subreddits)
    document = {
        "schema_version": 1,
        "status": status,
        "failure_reasons": reasons,
        "fetched_at": now.isoformat(),
        "run_date": args.run_date,
        "window_days": args.window_days,
        "source": "Reddit Data API via approved OAuth client",
        "subreddits_requested": args.subreddits,
        "subreddits_represented": sorted({post["subreddit"] for post in posts}),
        "post_count": len(posts),
        "contains_usernames": False,
        "retention_hours": args.retention_hours,
        "posts": posts,
    }
    output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    output.chmod(0o600)
    print(f"{status}: wrote {len(posts)} posts to {output}")
    return 0 if status == "live-evidence" else 3


def purge_file(path: Path) -> int:
    if path.exists():
        path.unlink()
        print(f"Purged {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="Fetch current Reddit evidence")
    fetch.add_argument("--run-date", default=date.today().isoformat())
    fetch.add_argument("--output")
    fetch.add_argument("--subreddits", nargs="+", default=list(DEFAULT_SUBREDDITS))
    fetch.add_argument("--window-days", type=int, default=30)
    fetch.add_argument("--posts-per-subreddit", type=int, default=75)
    fetch.add_argument("--comment-threads-per-subreddit", type=int, default=10)
    fetch.add_argument("--comments-per-thread", type=int, default=12)
    fetch.add_argument("--min-posts", type=int, default=15)
    fetch.add_argument("--min-subreddits", type=int, default=2)
    fetch.add_argument("--retention-hours", type=int, default=48)
    fetch.add_argument(
        "--credentials-file",
        default="~/.codex/automations/weekly-ibd-youtube-ideation/reddit-oauth.json",
    )

    purge = subparsers.add_parser("purge", help="Delete one raw evidence file")
    purge.add_argument("path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "purge":
        return purge_file(Path(args.path))
    if not args.output:
        args.output = f".cache/reddit-signals/reddit-raw-{args.run_date}.json"
    try:
        return collect(args)
    except CollectorError as exc:
        print(f"collector error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

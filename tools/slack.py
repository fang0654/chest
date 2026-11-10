"""Slack integration: import GitHub links posted in a channel, announce new tools.

Both directions are off unless enabled in settings (SLACK_IMPORT_ENABLED,
SLACK_POST_ENABLED), and both use one channel (SLACK_CHANNEL_ID). Polling uses
conversations.history; no inbound webhooks or Events API are needed.

Bot token scopes: channels:history (public channel) or groups:history
(private channel), and chat:write. Invite the bot to the channel.
"""

import logging
import re
import time
from urllib.parse import quote_plus

import requests
from django.conf import settings

from .models import SlackChannelState, Tool
from .services import (
    RepoNotFound,
    ToolAlreadyExists,
    ToolImportError,
    TryAgainLater,
    canonical_github_url,
    find_existing_tool,
    import_tool_from_github,
)
from .utils import (
    GitHubRateLimited,
    github_patience,
    resolve_github_repo,
    summarize_description,
)

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api/"
HTTP_TIMEOUT = 10

# Slack wraps links as <https://github.com/o/r> or <https://github.com/o/r|label>.
# The URL part stops at '>', '|' or whitespace.
_GITHUB_LINK = re.compile(r"https?://(?:www\.)?github\.com/[^\s<>|]+", re.IGNORECASE)

# Message subtypes that are ordinary posts; others are joins, edits, deletions...
_ACCEPTED_SUBTYPES = {None, "bot_message", "thread_broadcast", "file_share"}


class SlackError(Exception):
    pass


RATE_LIMIT_RETRIES = 10
MAX_RETRY_AFTER = 120  # seconds


def _call(method, http_method="GET", **params):
    """Call a Slack Web API method and return its JSON, raising on error.

    On HTTP 429 it waits for Slack's Retry-After and tries again, so long
    scans (history import) ride out rate limits instead of failing.
    """
    token = settings.SLACK_BOT_TOKEN
    if not token:
        raise SlackError("SLACK_BOT_TOKEN is not set")
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(RATE_LIMIT_RETRIES):
        if http_method == "POST":
            resp = requests.post(SLACK_API + method, headers=headers, json=params,
                                 timeout=HTTP_TIMEOUT)
        else:
            resp = requests.get(SLACK_API + method, headers=headers, params=params,
                                timeout=HTTP_TIMEOUT)
        if resp.status_code == 429:  # rate limited
            wait = min(int(resp.headers.get("Retry-After", "5")), MAX_RETRY_AFTER)
            logger.info("Slack %s rate limited; waiting %ss", method, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
        return data
    raise SlackError(f"{method}: still rate limited after retries")


def extract_github_repos(text):
    """Canonical repo URLs linked in a Slack message, in order, no duplicates."""
    repos = []
    for match in _GITHUB_LINK.findall(text or ""):
        url = canonical_github_url(match.rstrip(".,;:!?)"))
        if url and url.lower() not in (r.lower() for r in repos):
            repos.append(url)
    return repos


def _message_text(message):
    """Message text plus legacy attachment text (some bots post links there)."""
    parts = [message.get("text", "")]
    for att in message.get("attachments", []) or []:
        parts += [att.get("text", ""), att.get("title_link", ""),
                  att.get("from_url", "")]
    return "\n".join(p for p in parts if p)


def _paginate(method, **params):
    """All "messages" from a paginated conversations.* call, oldest first."""
    messages, cursor = [], None
    while True:
        page = dict(params, limit=200)
        if cursor:
            page["cursor"] = cursor
        data = _call(method, **page)
        messages += data.get("messages", [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return sorted(messages, key=lambda m: float(m["ts"]))


def _fetch_new_messages(channel, oldest):
    """Top-level messages newer than `oldest` (all of them if None), oldest first."""
    params = {"channel": channel}
    if oldest:
        params["oldest"] = oldest  # exclusive
    return _paginate("conversations.history", **params)


def _fetch_thread_replies(channel, thread_ts):
    """Replies in a thread, oldest first, without the parent message."""
    return [m for m in _paginate("conversations.replies", channel=channel, ts=thread_ts)
            if m["ts"] != thread_ts]


def _new_summary():
    # gone: repos deleted from GitHub (ignored, only counted)
    return {"messages": 0, "imported": [], "skipped": [], "failed": [], "gone": []}


def _my_ids():
    """The bot's own user and bot IDs (raises SlackError on a bad token)."""
    me = _call("auth.test")
    return {me.get("user_id"), me.get("bot_id")} - {None}


def _dry_run_repo(repo, summary, seen):
    """Report what importing `repo` would do, resolving renames like a real run."""
    existing = find_existing_tool(repo)
    if not existing:
        try:
            current = resolve_github_repo(repo)
        except (GitHubRateLimited, requests.RequestException) as exc:
            summary["failed"].append(f"{repo}: {exc}")
            return
        if not current:
            summary["gone"].append(repo)
            return
        if current.lower() != repo.lower():
            if current.lower() in seen:
                return  # another link already resolved to this repo
            seen.add(current.lower())
        existing = find_existing_tool(current)
        repo = current if current == repo else f"{current} (linked as {repo})"
    if existing:
        summary["skipped"].append(f"{repo}: {existing.name} is already in Chest")
    else:
        summary["imported"].append(repo)


def _import_from_message(message, my_ids, summary, dry_run=False, seen=None,
                         delay=0):
    """Import every GitHub repo linked in one message, recording the outcome.

    delay: seconds to pause after each repo that needed GitHub requests, to
    stay under GitHub's rate limits during long imports.
    """
    summary["messages"] += 1
    own = message.get("user") in my_ids or message.get("bot_id") in my_ids
    if own or message.get("subtype") not in _ACCEPTED_SUBTYPES:
        return
    seen = seen if seen is not None else set()
    for repo in extract_github_repos(_message_text(message)):
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        if dry_run:
            _dry_run_repo(repo, summary, seen)
            if delay:
                time.sleep(delay)
            continue
        try:
            tool = import_tool_from_github(repo, source=Tool.SOURCE_SLACK)
            summary["imported"].append(tool.name)
            seen.add(tool.github_url.lower())
        except ToolAlreadyExists as exc:
            summary["skipped"].append(f"{repo}: {exc}")
            continue  # no GitHub request beyond resolution; no need to pause
        except RepoNotFound:
            summary["gone"].append(repo)
        except TryAgainLater as exc:
            summary["failed"].append(f"{repo}: {exc}")
        except ToolImportError as exc:
            summary["skipped"].append(f"{repo}: {exc}")
        except Exception as exc:  # anything unexpected
            logger.exception("Slack import of %s failed", repo)
            summary["failed"].append(f"{repo}: {exc}")
        if delay:
            time.sleep(delay)


def poll_channel(backfill_days=0):
    """Import tools from GitHub links posted since the last poll.

    The first poll of a channel starts from "now" (nothing is backfilled)
    unless backfill_days is given. Returns a summary dict.
    """
    channel = settings.SLACK_CHANNEL_ID
    if not channel:
        raise SlackError("SLACK_CHANNEL_ID is not set")

    # Fails fast on a bad token, before any state is recorded
    my_ids = _my_ids()

    state, _ = SlackChannelState.objects.get_or_create(channel_id=channel)
    if not state.last_ts:
        if backfill_days:
            state.last_ts = f"{time.time() - backfill_days * 86400:.6f}"
            state.save()
        else:
            # Start after the newest message. This also checks the bot can read
            # the channel (not_in_channel / channel_not_found surface here).
            latest = _call("conversations.history", channel=channel, limit=1)
            msgs = latest.get("messages") or []
            state.last_ts = msgs[0]["ts"] if msgs else f"{time.time():.6f}"
            state.save()
            logger.info("Slack channel %s: first poll, starting after %s",
                        channel, state.last_ts)
            return _new_summary()

    summary = _new_summary()
    with github_patience():
        for message in _fetch_new_messages(channel, state.last_ts):
            _import_from_message(message, my_ids, summary)
            # Advance past each message as it is handled, so a crash
            # mid-batch doesn't re-import what was already done.
            state.last_ts = message["ts"]
            state.save()

    logger.info(
        "Slack channel %s: %d new messages, imported %d, skipped %d, failed %d",
        channel, summary["messages"], len(summary["imported"]),
        len(summary["skipped"]), len(summary["failed"]),
    )
    return summary


def import_channel_history(days=None, threads=False, dry_run=False, progress=None,
                           delay=1.0):
    """Import GitHub links from the channel's whole history (or the last N days).

    Independent of the poller: it doesn't read or move the poller's position,
    and repos already in Chest are skipped, so it is safe to re-run (e.g. to
    retry failures). threads=True also reads replies in every thread, which
    costs one extra API call per thread. dry_run=True only reports which repos
    would be imported (it still asks GitHub where renamed repos live now).
    delay pauses between repos to stay under GitHub's rate limits; when GitHub
    does rate limit, it waits as long as GitHub asks. Deleted repos are
    ignored (counted in summary["gone"]). progress, if given, is called with
    status strings.
    """
    channel = settings.SLACK_CHANNEL_ID
    if not channel:
        raise SlackError("SLACK_CHANNEL_ID is not set")
    say = progress or (lambda msg: None)

    my_ids = _my_ids()
    oldest = f"{time.time() - days * 86400:.6f}" if days else None
    messages = _fetch_new_messages(channel, oldest)
    say(f"Fetched {len(messages)} top-level messages")

    summary, seen = _new_summary(), set()
    with github_patience():
        for i, message in enumerate(messages, 1):
            _import_from_message(message, my_ids, summary, dry_run, seen, delay)
            if threads and message.get("reply_count"):
                for reply in _fetch_thread_replies(channel, message["ts"]):
                    # Replies also sent to the channel already appear top-level
                    if reply.get("subtype") != "thread_broadcast":
                        _import_from_message(reply, my_ids, summary, dry_run,
                                             seen, delay)
            if i % 50 == 0:
                say(f"  {i}/{len(messages)} messages, {len(summary['imported'])} "
                    f"{'to import' if dry_run else 'imported'}, "
                    f"{len(summary['failed'])} failed so far")

    logger.info(
        "Slack history %s%s: %d messages, imported %d, skipped %d, failed %d, "
        "deleted repos ignored %d",
        channel, " (dry run)" if dry_run else "", summary["messages"],
        len(summary["imported"]), len(summary["skipped"]), len(summary["failed"]),
        len(summary["gone"]),
    )
    return summary


def _summary_line(tool, limit=200):
    return summarize_description(tool.description, limit)


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def announce_tool(tool):
    """Post a newly added tool to the channel. Never raises.

    Only call this for tools added through the web UI; it does nothing when
    SLACK_POST_ENABLED is off or the tool came from Slack.
    """
    if not settings.SLACK_POST_ENABLED or tool.source == Tool.SOURCE_SLACK:
        return False
    try:
        name = _escape(tool.name)
        lines = [f"New tool added to Chest: *<{tool.github_url}|{name}>*"]
        summary = _summary_line(tool)
        if summary:
            lines.append(_escape(summary))
        tags = ", ".join(t.name for t in tool.tags.all())
        if tags:
            lines.append(f"Tags: {_escape(tags)}")
        if settings.CHEST_PUBLIC_URL:
            url = f"{settings.CHEST_PUBLIC_URL.rstrip('/')}/?q={quote_plus(tool.name)}"
            lines.append(f"<{url}|View in Chest>")
        _call(
            "chat.postMessage", http_method="POST",
            channel=settings.SLACK_CHANNEL_ID,
            text="\n".join(lines),
            unfurl_links=False, unfurl_media=False,
        )
        return True
    except Exception:
        logger.exception("Posting %s to Slack failed", tool)
        return False

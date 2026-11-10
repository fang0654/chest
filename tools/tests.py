from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import slack
from .models import SlackChannelState, Tag, Tool
from .services import (
    RepoNotFound,
    ToolImportError,
    TryAgainLater,
    find_existing_tool,
    import_tool_from_github,
)
from .utils import GitHubRateLimited, github_patience, github_request, resolve_github_repo

README = "# Widget - does things\n\nWidget scans stuff quickly.\n\ntags: Phishing\n"


class no_network:
    """Patch out GitHub requests made while importing a tool.

    moved maps a repo URL to where GitHub now redirects it; repos in gone
    don't exist any more; rate_limited repos raise GitHubRateLimited.
    """

    def __init__(self, moved=None, gone=(), rate_limited=()):
        self.moved = {k.lower(): v for k, v in (moved or {}).items()}
        self.gone = {g.lower() for g in gone}
        self.rate_limited = {r.lower() for r in rate_limited}

    def resolve(self, url):
        key = url.lower()
        if key in self.rate_limited:
            raise GitHubRateLimited("GitHub is rate limiting requests to github.com")
        if key in self.gone:
            return None
        return self.moved.get(key, url)

    def __enter__(self):
        self.patches = [
            mock.patch("tools.services.extract_readme_content", return_value=README),
            mock.patch("tools.services.fetch_logo", return_value=None),
            mock.patch("tools.services.resolve_github_repo", side_effect=self.resolve),
            mock.patch("tools.slack.resolve_github_repo", side_effect=self.resolve),
            mock.patch("tools.slack.time.sleep"),  # --delay between repos
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


class ToolNameTests(TestCase):
    def test_names(self):
        from .utils import extract_tool_name
        cases = [
            ("# CHECK THE NEW LIBRARY https://github.com/vfeedio/pyvfeed\n", "toolswatch/vFeed", "vFeed"),
            ("# Domain Password Audit Tool (DPAT)\n", "clr2of8/DPAT", "Domain Password Audit Tool (DPAT)"),
            ("# Payloads All The Things\n", "swisskyrepo/PayloadsAllTheThings", "Payloads All The Things"),
            ("# This is a collection of things we found useful\n", "acme/stuff", "stuff"),
            ("# The Mimikatz post exploitation toolkit for Windows\n", "gentilkiwi/mimikatz",
             "The Mimikatz post exploitation toolkit for Windows"),
            ("# ffuf - Fuzz Faster U Fool\n", "ffuf/ffuf", "ffuf"),
            (".. image:: ./pictures/logo.png\n=================\n", "koutto/jok3r", "jok3r"),
            ("# Default HTTP Login Hunter\n", "InfosecMatter/default-http-login-hunter",
             "Default HTTP Login Hunter"),
            ("## Table of Contents\n", "projectdiscovery/nuclei", "nuclei"),
        ]
        for readme, repo, expected in cases:
            self.assertEqual(extract_tool_name(readme, f"https://github.com/{repo}"), expected, readme)


class ExtractGithubReposTests(TestCase):
    def test_slack_link_formats(self):
        text = (
            "check <https://github.com/acme/widget|widget> and "
            "<https://github.com/Other/Thing/tree/main/docs> plus "
            "https://github.com/acme/widget.git again, "
            "also <https://github.com/topics/security> and "
            "<https://gist.github.com/x/123> and https://github.com/solo."
        )
        self.assertEqual(
            slack.extract_github_repos(text),
            ["https://github.com/acme/widget", "https://github.com/Other/Thing"],
        )

    def test_no_links(self):
        self.assertEqual(slack.extract_github_repos("nothing here"), [])
        self.assertEqual(slack.extract_github_repos(None), [])


class ServiceTests(TestCase):
    def test_import_and_dedupe(self):
        with no_network():
            tool = import_tool_from_github("https://github.com/acme/widget/", source="web")
            self.assertEqual(tool.name, "Widget")
            self.assertEqual(tool.github_url, "https://github.com/acme/widget")
            self.assertEqual(tool.source, "web")
            # README "tags: Phishing" matches the seeded "Phishing" tag
            self.assertIn("Phishing", [t.name for t in tool.tags.all()])
            with self.assertRaisesMessage(ToolImportError, "already in Chest"):
                import_tool_from_github("https://github.com/ACME/widget.git")

    def test_find_existing_matches_variants(self):
        Tool.objects.create(name="W", github_url="https://github.com/acme/widget/", description="")
        for url in ["https://github.com/acme/widget", "https://github.com/Acme/Widget.git",
                    "https://github.com/acme/widget/blob/main/README.md"]:
            self.assertIsNotNone(find_existing_tool(url), url)
        self.assertIsNone(find_existing_tool("https://github.com/acme/widget2"))

    def test_rejects_non_github(self):
        with self.assertRaises(ToolImportError):
            import_tool_from_github("https://gitlab.com/acme/widget")

    def test_renamed_repo_stored_at_new_home_and_deduped(self):
        moved = {"https://github.com/oldorg/widget": "https://github.com/neworg/widget"}
        with no_network(moved=moved):
            tool = import_tool_from_github("https://github.com/oldorg/widget")
            self.assertEqual(tool.github_url, "https://github.com/neworg/widget")
            with self.assertRaisesMessage(ToolImportError, "already in Chest"):
                import_tool_from_github("https://github.com/neworg/widget")
        # Catalogued under the new name, linked by the old one
        with no_network(moved=moved):
            with self.assertRaisesMessage(ToolImportError, "already in Chest"):
                import_tool_from_github("https://github.com/oldorg/widget")

    def test_deleted_repo(self):
        with no_network(gone=["https://github.com/acme/gone"]):
            with self.assertRaises(RepoNotFound):
                import_tool_from_github("https://github.com/acme/gone")
        self.assertFalse(Tool.objects.exists())

    def test_rate_limited_is_try_again_later(self):
        with no_network(rate_limited=["https://github.com/acme/busy"]):
            with self.assertRaisesMessage(TryAgainLater, "try again later"):
                import_tool_from_github("https://github.com/acme/busy")


def fake_slack(history_messages, replies=None):
    """Fake slack._call: auth.test, conversations.history/replies, chat.postMessage.

    replies maps a thread's parent ts to its reply messages.
    """
    calls = []

    def call(method, http_method="GET", **params):
        calls.append((method, params))
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT", "bot_id": "BBOT"}
        if method == "conversations.history":
            oldest = float(params.get("oldest") or 0)
            msgs = list(reversed([m for m in history_messages if float(m["ts"]) > oldest]))
            if "limit" in params and params["limit"] < len(msgs):
                msgs = msgs[: params["limit"]]
            return {"ok": True, "messages": msgs, "has_more": False}
        if method == "conversations.replies":
            parent = next(m for m in history_messages if m["ts"] == params["ts"])
            return {"ok": True, "has_more": False,
                    "messages": [parent] + (replies or {}).get(params["ts"], [])}
        if method == "chat.postMessage":
            return {"ok": True}
        raise AssertionError(method)

    call.calls = calls
    return call


@override_settings(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL_ID="C1",
                   SLACK_IMPORT_ENABLED=True)
class PollChannelTests(TestCase):
    def test_first_poll_starts_from_now(self):
        call = fake_slack([{"ts": "100.0", "text": "<https://github.com/acme/old>"},
                           {"ts": "200.0", "text": "<https://github.com/acme/older>"}])
        with mock.patch.object(slack, "_call", call), no_network():
            summary = slack.poll_channel()
        self.assertEqual(summary["imported"], [])
        self.assertFalse(Tool.objects.exists())
        self.assertEqual(SlackChannelState.objects.get(channel_id="C1").last_ts, "200.0")

    def test_bad_token_records_nothing(self):
        with mock.patch.object(slack, "_call",
                               side_effect=slack.SlackError("auth.test: invalid_auth")):
            with self.assertRaises(slack.SlackError):
                slack.poll_channel()
        self.assertFalse(SlackChannelState.objects.exists())

    def test_imports_new_links_and_skips_noise(self):
        SlackChannelState.objects.create(channel_id="C1", last_ts="1000.0")
        messages = [
            {"ts": "999.0", "user": "U1", "text": "<https://github.com/acme/too-old>"},
            {"ts": "1001.0", "user": "U1", "text": "look <https://github.com/acme/widget>"},
            {"ts": "1002.0", "user": "U2", "text": "dupe https://github.com/acme/widget"},
            {"ts": "1003.0", "bot_id": "BBOT", "subtype": "bot_message",
             "text": "New tool <https://github.com/acme/ours|Ours>"},
            {"ts": "1004.0", "user": "U3", "subtype": "channel_join",
             "text": "<https://github.com/acme/joined>"},
            {"ts": "1005.0", "user": "U4", "text": "no links"},
        ]
        call = fake_slack(messages)
        with mock.patch.object(slack, "_call", call), no_network():
            summary = slack.poll_channel()

        self.assertEqual(summary["messages"], 5)
        self.assertEqual(summary["imported"], ["Widget"])
        self.assertEqual(len(summary["skipped"]), 1)  # the duplicate
        self.assertEqual(list(Tool.objects.values_list("github_url", "source")),
                         [("https://github.com/acme/widget", "slack")])
        self.assertEqual(SlackChannelState.objects.get().last_ts, "1005.0")

        # Nothing new on the next poll
        with mock.patch.object(slack, "_call", call), no_network():
            self.assertEqual(slack.poll_channel()["messages"], 0)

    def test_backfill(self):
        call = fake_slack([{"ts": "2000000000.0", "user": "U1",
                            "text": "<https://github.com/acme/widget>"}])
        with mock.patch.object(slack, "_call", call), no_network(), \
                mock.patch("tools.slack.time.time", return_value=2000000100.0):
            summary = slack.poll_channel(backfill_days=1)
        self.assertEqual(summary["imported"], ["Widget"])


@override_settings(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL_ID="C1",
                   CHEST_PUBLIC_URL="https://chest.example")
class AnnounceTests(TestCase):
    def make_tool(self, source):
        tool = Tool.objects.create(
            name="Widget <&>", github_url="https://github.com/acme/widget",
            description="# Widget\n\n![badge](x)\nScans [stuff](http://x) *fast*.",
            source=source,
        )
        tool.tags.add(Tag.objects.get(name="Phishing"))
        return tool

    @override_settings(SLACK_POST_ENABLED=False)
    def test_disabled_posts_nothing(self):
        call = fake_slack([])
        with mock.patch.object(slack, "_call", call):
            self.assertFalse(slack.announce_tool(self.make_tool("web")))
        self.assertEqual(call.calls, [])

    @override_settings(SLACK_POST_ENABLED=True)
    def test_posts_web_tool(self):
        call = fake_slack([])
        with mock.patch.object(slack, "_call", call):
            self.assertTrue(slack.announce_tool(self.make_tool("web")))
        method, params = call.calls[0]
        self.assertEqual(method, "chat.postMessage")
        self.assertEqual(params["channel"], "C1")
        text = params["text"]
        self.assertIn("<https://github.com/acme/widget|Widget &lt;&amp;&gt;>", text)
        self.assertIn("Scans stuff fast.", text)
        self.assertIn("Tags: Phishing", text)
        self.assertIn("https://chest.example/?q=Widget+%3C%26%3E", text)

    @override_settings(SLACK_POST_ENABLED=True)
    def test_never_echoes_slack_imports(self):
        call = fake_slack([])
        with mock.patch.object(slack, "_call", call):
            self.assertFalse(slack.announce_tool(self.make_tool("slack")))
        self.assertEqual(call.calls, [])

    @override_settings(SLACK_POST_ENABLED=True)
    def test_slack_failure_is_swallowed(self):
        with mock.patch.object(slack, "_call", side_effect=slack.SlackError("boom")):
            self.assertFalse(slack.announce_tool(self.make_tool("web")))


@override_settings(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL_ID="C1",
                   SLACK_POST_ENABLED=True)
class ViewTests(TestCase):
    def test_import_view_announces_and_rejects_duplicates(self):
        call = fake_slack([])
        with mock.patch.object(slack, "_call", call), no_network():
            self.client.post("/import/", {"github_url": "https://github.com/acme/widget"})
            resp = self.client.post("/import/", {"github_url": "https://github.com/acme/widget"},
                                    follow=True)
        self.assertEqual(Tool.objects.count(), 1)
        self.assertEqual(Tool.objects.get().source, "web")
        self.assertEqual([m for m, _ in call.calls], ["chat.postMessage"])
        self.assertContains(resp, "already in Chest")

    def test_submit_view_sets_tags_and_announces(self):
        tag = Tag.objects.get(name="Vpn")
        call = fake_slack([])
        with mock.patch.object(slack, "_call", call), no_network():
            self.client.post("/submit/", {"github_url": "https://github.com/acme/widget",
                                          "tags": [tag.id]})
        tool = Tool.objects.get()
        self.assertEqual(tool.description, "Widget scans stuff quickly. tags: Phishing")
        self.assertEqual(list(tool.tags.all()), [tag])
        self.assertEqual(len(call.calls), 1)


@override_settings(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL_ID="C1")
class HistoryImportTests(TestCase):
    MESSAGES = [
        {"ts": "10.0", "user": "U1", "text": "<https://github.com/acme/one>"},
        {"ts": "20.0", "user": "U1", "text": "thread", "reply_count": 2},
        {"ts": "30.0", "user": "U2", "text": "<https://github.com/acme/two> and "
                                             "<https://github.com/acme/one>"},
        {"ts": "40.0", "bot_id": "BBOT", "text": "<https://github.com/acme/ours>"},
        # Broadcast reply: appears top-level and in the thread
        {"ts": "25.0", "user": "U3", "subtype": "thread_broadcast",
         "thread_ts": "20.0", "text": "<https://github.com/acme/bcast>"},
    ]
    REPLIES = {"20.0": [
        {"ts": "21.0", "user": "U4", "thread_ts": "20.0",
         "text": "<https://github.com/acme/in-thread>"},
        {"ts": "25.0", "user": "U3", "subtype": "thread_broadcast",
         "thread_ts": "20.0", "text": "<https://github.com/acme/bcast>"},
    ]}

    def run_import(self, **kwargs):
        call = fake_slack(self.MESSAGES, self.REPLIES)
        with mock.patch.object(slack, "_call", call), no_network():
            return slack.import_channel_history(**kwargs), call

    def urls(self):
        return sorted(Tool.objects.values_list("github_url", flat=True))

    def test_whole_history_without_threads(self):
        SlackChannelState.objects.create(channel_id="C1", last_ts="35.0")
        summary, call = self.run_import()
        self.assertEqual(self.urls(), ["https://github.com/acme/bcast",
                                       "https://github.com/acme/one",
                                       "https://github.com/acme/two"])
        self.assertEqual(summary["skipped"], [])  # "one" linked twice: counted once
        self.assertNotIn("conversations.replies", [m for m, _ in call.calls])
        # The poller's position is untouched
        self.assertEqual(SlackChannelState.objects.get().last_ts, "35.0")
        self.assertTrue(all(t.source == "slack" for t in Tool.objects.all()))

    def test_threads(self):
        summary, _ = self.run_import(threads=True)
        self.assertIn("https://github.com/acme/in-thread", self.urls())
        self.assertEqual(len(self.urls()), 4)
        self.assertEqual(summary["failed"], [])

    def test_dry_run_imports_nothing(self):
        Tool.objects.create(name="One", github_url="https://github.com/acme/one",
                            description="")
        summary, _ = self.run_import(dry_run=True, threads=True)
        self.assertEqual(Tool.objects.count(), 1)
        self.assertEqual(sorted(summary["imported"]), [
            "https://github.com/acme/bcast", "https://github.com/acme/in-thread",
            "https://github.com/acme/two"])
        self.assertEqual(len(summary["skipped"]), 1)  # "one" exists (reported once)

    def test_moved_gone_and_rate_limited(self):
        moved = {"https://github.com/acme/two": "https://github.com/newco/two",
                 "https://github.com/acme/in-thread": "https://github.com/acme/one"}
        call = fake_slack(self.MESSAGES, self.REPLIES)
        with mock.patch.object(slack, "_call", call), \
                no_network(moved=moved, gone=["https://github.com/acme/bcast"],
                           rate_limited=["https://github.com/acme/one"]):
            summary = slack.import_channel_history(threads=True)
        # acme/one itself was rate limited, but the in-thread link redirects
        # to it and resolved fine, so it was imported via that link
        self.assertEqual(self.urls(), ["https://github.com/acme/one",
                                       "https://github.com/newco/two"])
        self.assertEqual(summary["gone"], ["https://github.com/acme/bcast"])
        self.assertEqual(len(summary["failed"]), 1)
        self.assertIn("try again later", summary["failed"][0])

    def test_dry_run_resolves(self):
        Tool.objects.create(name="Two", github_url="https://github.com/newco/two",
                            description="")
        moved = {"https://github.com/acme/two": "https://github.com/newco/two",
                 "https://github.com/acme/in-thread": "https://github.com/acme/one"}
        call = fake_slack(self.MESSAGES, self.REPLIES)
        with mock.patch.object(slack, "_call", call), \
                no_network(moved=moved, gone=["https://github.com/acme/bcast"]):
            summary = slack.import_channel_history(threads=True, dry_run=True)
        self.assertEqual(summary["imported"], ["https://github.com/acme/one"])
        self.assertEqual(summary["skipped"], [
            "https://github.com/newco/two (linked as https://github.com/acme/two): "
            "Two is already in Chest"])
        self.assertEqual(summary["gone"], ["https://github.com/acme/bcast"])
        self.assertFalse(Tool.objects.filter(source="slack").exists())

    def test_rerun_is_safe(self):
        self.run_import()
        summary, _ = self.run_import()
        self.assertEqual(summary["imported"], [])
        self.assertEqual(Tool.objects.count(), 3)

    def test_command_dry_run_output(self):
        from io import StringIO
        out = StringIO()
        call = fake_slack(self.MESSAGES, self.REPLIES)
        with mock.patch.object(slack, "_call", call), no_network():
            call_command("import_slack_history", "--dry-run", stdout=out)
        self.assertIn("Would import 3", out.getvalue())
        self.assertFalse(Tool.objects.exists())


class GitHubRequestTests(TestCase):
    def resp(self, status, headers=None, url="https://github.com/acme/widget"):
        return mock.Mock(status_code=status, headers=headers or {}, url=url)

    def test_default_patience_is_short(self):
        limited = self.resp(429, {"Retry-After": "600"})
        with mock.patch("tools.utils.requests.request", return_value=limited) as req, \
                mock.patch("tools.utils.time.sleep") as sleep:
            with self.assertRaises(GitHubRateLimited):
                github_request("GET", "https://raw.githubusercontent.com/a/b/HEAD/README.md")
        self.assertEqual(req.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [10, 10])

    def test_patient_waits_and_succeeds(self):
        seq = [self.resp(429), self.resp(403, {"x-ratelimit-remaining": "0",
                                               "Retry-After": "40"}), self.resp(200)]
        with mock.patch("tools.utils.requests.request", side_effect=seq), \
                mock.patch("tools.utils.time.sleep") as sleep, github_patience():
            self.assertEqual(github_request("GET", "https://github.com/x/y").status_code, 200)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [15, 40])

    def test_resolve(self):
        cases = [
            (self.resp(200, url="https://github.com/NewOrg/Widget"), "https://github.com/NewOrg/Widget"),
            (self.resp(404), None),
            (self.resp(451), None),
            (self.resp(200, url="https://github.com/login?return_to=x"), None),
        ]
        for resp, expected in cases:
            resp.raise_for_status = mock.Mock()
            with mock.patch("tools.utils.requests.request", return_value=resp) as req:
                self.assertEqual(resolve_github_repo("https://github.com/old/widget.git"),
                                 expected)
            self.assertEqual(req.call_args.args[:2], ("HEAD", "https://github.com/old/widget"))


@override_settings(SLACK_BOT_TOKEN="xoxb-test")
class RateLimitTests(TestCase):
    def test_waits_for_retry_after(self):
        limited = mock.Mock(status_code=429, headers={"Retry-After": "7"})
        ok = mock.Mock(status_code=200, headers={})
        ok.json.return_value = {"ok": True, "user_id": "U"}
        with mock.patch("tools.slack.requests.get", side_effect=[limited, limited, ok]), \
                mock.patch("tools.slack.time.sleep") as sleep:
            self.assertEqual(slack._call("auth.test")["user_id"], "U")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7, 7])


class CommandTests(TestCase):
    @override_settings(SLACK_IMPORT_ENABLED=False)
    def test_disabled(self):
        with self.assertRaisesMessage(CommandError, "disabled"):
            call_command("poll_slack")


class SummaryTests(TestCase):
    def test_summarize(self):
        from .utils import summarize_description
        readme = (
            "# Tool\n\n[![Build](https://x/badge.svg)](https://x)\n"
            "<p align=center><img src=logo.png></p>\n\n"
            "```\n$ secret --code\n```\n"
            "Tool **finds** [things](https://x) in `places`.\n\n"
            "Second paragraph here.\n\n## Install\n| a | b |\n"
        )
        self.assertEqual(summarize_description(readme),
                         "Tool finds things in places. Second paragraph here.")
        self.assertEqual(summarize_description("Title\n=====\n\nBody text."), "Title Body text.")
        long = summarize_description("word " * 200, limit=50)
        self.assertLessEqual(len(long), 50)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(summarize_description(None), "")


class ListPageTests(TestCase):
    def setUp(self):
        for i in range(60):
            Tool.objects.create(
                name=f"Tool {i:02d}", github_url=f"https://github.com/acme/t{i}",
                description=f"Short intro {i}.\n\n" + "FULLREADMEBODY " * 50,
                logo_checked_at=timezone.now(),
            )

    def test_paginates_and_keeps_filters(self):
        resp = self.client.get("/", {"q": "Tool"})
        self.assertEqual(len(resp.context["tools"]), 50)
        self.assertContains(resp, "Showing 1–50 of 60 tools")
        self.assertContains(resp, "?q=Tool&amp;page=2")
        resp2 = self.client.get("/", {"q": "Tool", "page": 2})
        self.assertEqual(len(resp2.context["tools"]), 10)

    def test_cards_carry_summary_not_full_readme(self):
        resp = self.client.get("/")
        self.assertContains(resp, "Short intro 59.")
        body = resp.content.decode()
        # Summary is capped; the full README isn't on the page
        self.assertLess(body.count("FULLREADMEBODY"), 50 * 50)
        self.assertNotIn("<style>", body)
        # Logo-less, already-checked tools get a placeholder, not an <img>
        self.assertNotIn("/logo/?v=", body)
        tool = Tool.objects.first()
        full = self.client.get(f"/tool/{tool.id}/description/")
        self.assertEqual(full.content.decode().count("FULLREADMEBODY"), 50)

    def test_query_count_is_flat(self):
        with self.assertNumQueries(4):  # count, all tags, page of tools, their tags
            self.client.get("/")


class LogoViewTests(TestCase):
    def test_checked_without_logo_never_hits_github(self):
        tool = Tool.objects.create(name="T", github_url="https://github.com/a/b",
                                   description="", logo_checked_at=timezone.now())
        with mock.patch("tools.views.extract_readme_content") as fetch:
            resp = self.client.get(f"/tool/{tool.id}/logo/")
        self.assertEqual(resp.status_code, 404)
        fetch.assert_not_called()

    def test_unchecked_looks_once(self):
        tool = Tool.objects.create(name="T", github_url="https://github.com/a/b", description="")
        with mock.patch("tools.views.extract_readme_content", return_value="# T") as fetch, \
                mock.patch("tools.views.fetch_logo", return_value=None):
            self.client.get(f"/tool/{tool.id}/logo/")
            self.client.get(f"/tool/{tool.id}/logo/")
        self.assertEqual(fetch.call_count, 1)
        tool.refresh_from_db()
        self.assertIsNotNone(tool.logo_checked_at)

    def test_serves_logo_with_cache_header(self):
        from io import BytesIO
        from PIL import Image
        from django.core.files.base import ContentFile
        buf = BytesIO()
        Image.new("RGB", (40, 40), "red").save(buf, "PNG")
        tool = Tool.objects.create(name="T", github_url="https://github.com/a/b", description="")
        tool.logo.save("t_logo.png", ContentFile(buf.getvalue()))
        try:
            resp = self.client.get(f"/tool/{tool.id}/logo/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("max-age=", resp["Cache-Control"])
            self.assertEqual(b"".join(resp.streaming_content)[:4], b"\x89PNG")
        finally:
            tool.logo.delete(save=False)

    def test_import_records_logo_check(self):
        with no_network():
            tool = import_tool_from_github("https://github.com/acme/widget")
        self.assertIsNotNone(tool.logo_checked_at)

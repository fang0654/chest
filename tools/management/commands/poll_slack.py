import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tools.slack import SlackError, poll_channel

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Import tools from GitHub links posted in the Slack channel "
        "(SLACK_CHANNEL_ID). Does nothing unless SLACK_IMPORT_ENABLED is on."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop", action="store_true",
            help="Keep running, polling every --interval seconds",
        )
        parser.add_argument(
            "--interval", type=int, default=settings.SLACK_POLL_INTERVAL,
            help="Seconds between polls with --loop (default: SLACK_POLL_INTERVAL)",
        )
        parser.add_argument(
            "--backfill-days", type=int, default=0,
            help="On the channel's first poll, import links from the last N days "
                 "instead of starting from now",
        )

    def handle(self, *args, **opts):
        if not opts["loop"]:
            if not settings.SLACK_IMPORT_ENABLED:
                raise CommandError("Slack import is disabled (SLACK_IMPORT_ENABLED)")
            self._poll(opts["backfill_days"])
            return

        if not settings.SLACK_IMPORT_ENABLED:
            # Stay up quietly so the container doesn't restart-loop; enabling
            # means setting the env var and recreating the container.
            self.stdout.write("Slack import disabled (SLACK_IMPORT_ENABLED); idling")
            while True:
                time.sleep(3600)

        interval = max(opts["interval"], 60)
        self.stdout.write(f"Polling Slack every {interval}s")
        while True:
            try:
                self._poll(opts["backfill_days"])
            except Exception as exc:  # keep the loop alive through outages
                logger.exception("Slack poll failed")
                self.stderr.write(f"Slack poll failed: {exc}")
            time.sleep(interval)

    def _poll(self, backfill_days):
        try:
            summary = poll_channel(backfill_days=backfill_days)
        except SlackError as exc:
            raise CommandError(str(exc))
        self.stdout.write(
            f"{summary['messages']} new messages; imported: "
            f"{', '.join(summary['imported']) or 'none'}"
        )
        for line in summary["skipped"]:
            self.stdout.write(f"  skipped {line}")
        for line in summary["failed"]:
            self.stderr.write(f"  failed {line}")

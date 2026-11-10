from django.core.management.base import BaseCommand, CommandError

from tools.slack import SlackError, import_channel_history


class Command(BaseCommand):
    help = (
        "Import tools from GitHub links in the Slack channel's entire history "
        "(SLACK_CHANNEL_ID). Repos already in Chest are skipped, so it is safe "
        "to re-run. Works even while SLACK_IMPORT_ENABLED is off, and doesn't "
        "change where the regular poller resumes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=None,
            help="Only look at the last N days (default: all history)",
        )
        parser.add_argument(
            "--threads", action="store_true",
            help="Also scan replies inside threads (one extra API call per thread)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List the repos that would be imported without importing them",
        )
        parser.add_argument(
            "--delay", type=float, default=1.0,
            help="Seconds to pause between repos, to stay under GitHub's rate "
                 "limits (default: 1)",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        try:
            summary = import_channel_history(
                days=opts["days"], threads=opts["threads"], dry_run=dry_run,
                progress=self.stdout.write, delay=opts["delay"],
            )
        except SlackError as exc:
            raise CommandError(str(exc))

        label = "Would import" if dry_run else "Imported"
        self.stdout.write(self.style.SUCCESS(
            f"{summary['messages']} messages scanned. {label} "
            f"{len(summary['imported'])}, skipped {len(summary['skipped'])}, "
            f"failed {len(summary['failed'])}, deleted from GitHub "
            f"{len(summary['gone'])} (ignored)."
        ))
        for item in summary["imported"]:
            self.stdout.write(f"  {'+' if not dry_run else '?'} {item}")
        if opts["verbosity"] > 1:
            for line in summary["skipped"]:
                self.stdout.write(f"  skipped {line}")
            for repo in summary["gone"]:
                self.stdout.write(f"  deleted {repo}")
        for line in summary["failed"]:
            self.stderr.write(f"  failed {line}")
        if summary["failed"]:
            self.stderr.write("Re-run the command to retry failures.")

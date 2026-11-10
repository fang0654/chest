from django.db import migrations
from django.utils import timezone


def mark_checked(apps, schema_editor):
    """Every existing tool had its logo looked up when it was created or
    imported. Record that, so the logo view doesn't ask GitHub again for the
    tools that have none ("Refresh from GitHub" still re-checks)."""
    Tool = apps.get_model("tools", "Tool")
    Tool.objects.filter(logo_checked_at__isnull=True).update(logo_checked_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ("tools", "0006_tool_logo_checked_at"),
    ]

    operations = [
        migrations.RunPython(mark_checked, migrations.RunPython.noop),
    ]

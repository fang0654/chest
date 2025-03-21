from django.db import migrations


def populate_tags(apps, schema_editor):
    Tag = apps.get_model("tools", "Tag")
    initial_tags = [
        "password-spraying",
        "vpn",
        "network-scanning",
        "web-exploitation",
        "privilege-escalation",
        "active-directory",
        "wireless",
        "forensics",
        "malware-analysis",
        "social-engineering",
        "phishing",
        "exploit-development",
        "post-exploitation",
        "cloud-security",
        "mobile-security",
        "reverse-engineering",
        "binary-analysis",
        "network-pivoting",
        "credential-dumping",
        "persistence",
        "exfiltration",
        "command-control",
        "payload-generation",
        "reconnaissance",
        "vulnerability-scanning",
        "web-application",
        "network-tunneling",
        "password-cracking",
        "wireless-attacks",
        "system-hardening",
    ]

    for tag_name in initial_tags:
        Tag.objects.create(name=tag_name.replace("-", " ").title(), slug=tag_name)


def remove_tags(apps, schema_editor):
    Tag = apps.get_model("tools", "Tag")
    Tag.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tools", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(populate_tags, remove_tags),
    ]

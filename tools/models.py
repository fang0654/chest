from django.db import models
from django.utils.text import slugify
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError

# Create your models here.


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Tool(models.Model):
    SOURCE_WEB = "web"
    SOURCE_SLACK = "slack"
    SOURCE_ADMIN = "admin"
    SOURCE_CHOICES = [
        (SOURCE_WEB, "Web UI"),
        (SOURCE_SLACK, "Slack"),
        (SOURCE_ADMIN, "Admin"),
    ]

    name = models.CharField(max_length=200)
    github_url = models.URLField()
    description = models.TextField()
    logo = models.ImageField(upload_to="tool_logos/", null=True, blank=True)
    tags = models.ManyToManyField(Tag, related_name="tools")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When we last looked for a logo in the README. Set even when none was
    # found, so the logo view doesn't ask GitHub again on every page view.
    logo_checked_at = models.DateTimeField(null=True, blank=True)
    # Where the tool was added; only web-UI additions are announced in Slack
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_ADMIN
    )

    def clean(self):
        if not self.github_url.startswith("https://github.com/"):
            raise ValidationError("GitHub URL must start with https://github.com/")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["-created_at"]


class SlackChannelState(models.Model):
    """How far the poller has read a Slack channel (Slack message timestamp)."""

    channel_id = models.CharField(max_length=32, unique=True)
    last_ts = models.CharField(max_length=32, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.channel_id} @ {self.last_ts or 'start'}"

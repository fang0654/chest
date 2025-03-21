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
    name = models.CharField(max_length=200)
    github_url = models.URLField()
    description = models.TextField()
    logo = models.ImageField(upload_to="tool_logos/", null=True, blank=True)
    tags = models.ManyToManyField(Tag, related_name="tools")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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

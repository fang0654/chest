from django import template
from django.utils.safestring import mark_safe
import markdown
import nh3

register = template.Library()

# Markdown output from untrusted READMEs is sanitized with nh3 (an allowlist
# HTML sanitizer). nh3's defaults keep ordinary formatting tags (headings,
# lists, tables, code, links, images) and drop <script>, <style>, <iframe>,
# event-handler attributes and javascript:/data: URLs.
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


@register.filter
def summary(text, limit=300):
    """Plain-text summary of a README for tool cards (auto-escaped)."""
    from tools.utils import summarize_description
    return summarize_description(text, int(limit))


@register.filter
def markdown_to_html(text):
    """Convert markdown text to sanitized HTML."""
    if not text:
        return ""
    html = markdown.markdown(text, extensions=["fenced_code", "tables"])
    clean = nh3.clean(
        html,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    return mark_safe(clean)

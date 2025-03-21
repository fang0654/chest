from django import template
import markdown

register = template.Library()

@register.filter
def markdown_to_html(text):
    """Convert markdown text to HTML."""
    if not text:
        return ""
    return markdown.markdown(text) 
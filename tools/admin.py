from django.contrib import admin
from .models import Tool, Tag
from .utils import (
    extract_readme_content,
    extract_readme_description,
    extract_logo_url,
    download_and_save_logo,
    extract_tool_name,
)
import pdb

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Tool)
class ToolAdmin(admin.ModelAdmin):
    list_display = ("name", "github_url", "created_at")
    search_fields = ("name", "github_url", "description")
    filter_horizontal = ("tags",)
    actions = ["repull_readme"]

    def repull_readme(self, request, queryset):
        for tool in queryset:
            readme_content = extract_readme_content(tool.github_url)
            
            if readme_content:
                # Extract and update tool name
                name = extract_tool_name(readme_content)
                if name:
                    tool.name = name

                # Update description
                description = extract_readme_description(readme_content)
                if description:
                    tool.description = description

                # Update logo
                logo_url = extract_logo_url(readme_content, tool.github_url)
                if logo_url:
                    logo_data = download_and_save_logo(logo_url, tool.name)
                    if logo_data:
                        tool.logo.save(*logo_data)

                tool.save()

        self.message_user(request, f"Successfully repulled README for {queryset.count()} tools")
    repull_readme.short_description = "Repull README content"

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseNotFound
from django.utils import timezone
from django.conf import settings
import os
from .models import Tool, Tag
from .forms import ToolEditForm
from .services import ToolImportError, import_tool_from_github
from .slack import announce_tool
from .utils import (
    GitHubRateLimited,
    extract_readme_content,
    fetch_logo,
    extract_tool_name,
)
from django.contrib import messages
from django.urls import reverse
import requests
import re
from bs4 import BeautifulSoup
import base64
from urllib.parse import urlparse
from django.views.decorators.http import require_http_methods


def tag_modal(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)
    search = request.GET.get('search', '')
    
    # Get all tags that aren't already on the tool
    available_tags = Tag.objects.exclude(id__in=tool.tags.all())
    
    if search:
        available_tags = available_tags.filter(name__icontains=search)
    
    return render(request, 'tools/tag_modal.html', {
        'tool': tool,
        'available_tags': available_tags
    })


def add_tag(request, tool_id, tag_id):
    if request.method != 'POST':
        return HttpResponse('Method not allowed', status=405)
        
    tool = get_object_or_404(Tool, id=tool_id)
    tag = get_object_or_404(Tag, id=tag_id)
    
    tool.tags.add(tag)
    
    if request.headers.get('HX-Request'):
        return render(request, 'tools/tool_card.html', {
            'tool': tool
        })
    
    return redirect('tool_list')


def remove_tag(request, tool_id, tag_id):
    tool = get_object_or_404(Tool, id=tool_id)
    tag = get_object_or_404(Tag, id=tag_id)
    
    tool.tags.remove(tag)
    
    return render(request, 'tools/tool_card.html', {
        'tool': tool
    })


def edit_tool(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)
    
    if request.method == 'POST':
        print("POST request received")
        print("Form data:", request.POST)
        print("HTMX request:", request.headers.get('HX-Request'))
        form = ToolEditForm(request.POST, instance=tool)
        if form.is_valid():
            print("Form is valid")
            form.save()
            if request.headers.get('HX-Request'):
                print("Returning HTMX response")
                return render(request, 'tools/tool_card.html', {
                    'tool': tool
                })
            return redirect('tool_list')
        else:
            print("Form errors:", form.errors)
    else:
        form = ToolEditForm(instance=tool)
    
    if request.headers.get('HX-Request'):
        return render(request, 'tools/edit_tool.html', {
            'form': form,
            'tool': tool
        })
    
    return render(request, 'tools/edit_tool.html', {
        'form': form,
        'tool': tool
    })


LOGO_CACHE_SECONDS = 7 * 24 * 3600  # card URLs carry ?v=<updated_at>


def _logo_response(tool):
    response = FileResponse(tool.logo.open("rb"), content_type="image/png")
    response["Cache-Control"] = f"public, max-age={LOGO_CACHE_SECONDS}"
    return response


def serve_tool_logo(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)

    if tool.logo:
        return _logo_response(tool)

    # Look for a logo at most once; "Refresh from GitHub" looks again.
    # Without this, every page view re-asked GitHub for every logo-less tool.
    if tool.logo_checked_at is None:
        try:
            readme_content = extract_readme_content(tool.github_url)
        except GitHubRateLimited:
            return HttpResponseNotFound("Logo not found")  # try again next view
        logo_data = fetch_logo(readme_content, tool.github_url, tool.name) \
            if readme_content else None
        tool.logo_checked_at = timezone.now()
        if logo_data:
            tool.logo.save(*logo_data)
            return _logo_response(tool)
        tool.save(update_fields=["logo_checked_at"])

    response = HttpResponseNotFound("Logo not found")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def submit_tool(request):
    if request.method == "POST":
        try:
            tool = import_tool_from_github(
                request.POST.get("github_url", ""),
                source=Tool.SOURCE_WEB,
                tag_ids=request.POST.getlist("tags"),
                full_readme=False,
            )
        except ToolImportError as exc:
            messages.error(request, str(exc))
            return redirect("tool_list")

        announce_tool(tool)
        return redirect("tool_list")

    tags = Tag.objects.all()
    return render(request, "tools/submit_tool.html", {"tags": tags})


def import_tool(request):
    """Import a tool from GitHub."""
    if request.method == "POST":
        github_url = request.POST.get("github_url")
        if not github_url:
            messages.error(request, "Please provide a GitHub URL")
            return redirect("tool_list")

        try:
            tool = import_tool_from_github(github_url, source=Tool.SOURCE_WEB)
        except ToolImportError as exc:
            messages.error(request, str(exc))
            return redirect("tool_list")

        announce_tool(tool)
        messages.success(request, f"Successfully imported {tool.name}")
        return redirect("tool_list")

    return render(request, "tools/import_modal.html")


class ToolListView(ListView):
    model = Tool
    template_name = "tools/search_results.html"
    context_object_name = "tools"
    paginate_by = 50

    def get_queryset(self):
        queryset = Tool.objects.prefetch_related("tags")
        search = self.request.GET.get("q")
        selected_tags = self.request.GET.getlist("tags")

        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(description__icontains=search)
            )

        if selected_tags:
            queryset = queryset.filter(tags__id__in=selected_tags).distinct()

        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["all_tags"] = Tag.objects.all()
        context["selected_tags"] = self.request.GET.getlist("tags")
        # Current search/filters, for pagination links
        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_query"] = params.urlencode()
        return context


def tool_card(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)
    return render(request, 'tools/tool_card.html', {'tool': tool})


def tool_description(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)
    return render(request, 'tools/tool_description.html', {
        'tool': tool
    })


@require_http_methods(["POST"])
def refresh_tool(request, tool_id):
    """Refresh tool information from GitHub."""
    try:
        tool = Tool.objects.get(id=tool_id)
        
        # Get fresh README content
        readme_content = extract_readme_content(tool.github_url)
        if not readme_content:
            messages.error(request, "Could not fetch README from GitHub")
            return HttpResponse(status=400)
        
        # Update tool information
        tool.name = extract_tool_name(readme_content, tool.github_url) or tool.name
        tool.description = readme_content
        
        # Update logo if available (saved together with the fields below)
        logo_result = fetch_logo(readme_content, tool.github_url, tool.name)
        tool.logo_checked_at = timezone.now()
        if logo_result:
            if tool.logo:
                tool.logo.delete(save=False)  # don't orphan the old file
            tool.logo.save(*logo_result, save=False)
        
        tool.save()
        messages.success(request, "Tool information refreshed successfully")
        
        # Return the updated tool card
        return render(request, "tools/tool_card.html", {"tool": tool})
    except Tool.DoesNotExist:
        messages.error(request, "Tool not found")
        return HttpResponse(status=404)
    except Exception as e:
        messages.error(request, f"Error refreshing tool: {str(e)}")
        return HttpResponse(status=500)

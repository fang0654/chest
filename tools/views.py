from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView
from django.db.models import Q
from django.http import HttpResponse, HttpResponseNotFound
from django.conf import settings
import os
from .models import Tool, Tag
from .forms import ToolEditForm
from .utils import (
    extract_readme_content,
    extract_readme_description,
    extract_logo_url,
    download_and_save_logo,
    extract_tool_name,
    extract_tags_from_readme,
)
import pdb
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


def serve_tool_logo(request, tool_id):
    tool = get_object_or_404(Tool, id=tool_id)

    if tool.logo:
        # If the logo exists, serve it directly
        return HttpResponse(tool.logo.read(), content_type="image/png")
    else:
        # If no logo exists, try to fetch and save it
        readme_content = extract_readme_content(tool.github_url)
        if readme_content:
            logo_url = extract_logo_url(readme_content, tool.github_url)
            if logo_url:
                logo_data = download_and_save_logo(logo_url, tool.name)
                if logo_data:
                    tool.logo.save(*logo_data)
                    return HttpResponse(tool.logo.read(), content_type="image/png")

        # If we still don't have a logo, return a 404
        return HttpResponseNotFound("Logo not found")


def submit_tool(request):
    if request.method == "POST":
        github_url = request.POST.get("github_url")
        readme_content = extract_readme_content(github_url)

        # Extract tool name from README
        name = extract_tool_name(readme_content)
        if not name:
            name = github_url.split("/")[-1]  # Fallback to repository name

        description = extract_readme_description(readme_content)
        logo_url = extract_logo_url(readme_content, github_url)
        tag_ids = request.POST.getlist("tags")

        tool = Tool.objects.create(
            name=name, github_url=github_url, description=description
        )

        # Handle logo if available
        if logo_url:
            logo_data = download_and_save_logo(logo_url, name)
            if logo_data:
                tool.logo.save(*logo_data)

        if tag_ids:
            tool.tags.set(tag_ids)

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
        
        # Extract README content
        readme_content = extract_readme_content(github_url)
        if not readme_content:
            messages.error(request, "Could not find README file in the repository")
            return redirect("tool_list")
        
        # Extract tool name and description
        tool_name = extract_tool_name(readme_content)
        if not tool_name:
            # Fallback to repository name if no name found
            tool_name = github_url.split("/")[-1]
        
        # Create the tool
        tool = Tool.objects.create(
            name=tool_name,
            github_url=github_url,
            description=readme_content
        )
        
        # Extract potential tags from README
        found_tags = extract_tags_from_readme(readme_content)
        
        # Find matching existing tags (case-insensitive)
        existing_tags = Tag.objects.filter(name__in=[tag.lower() for tag in found_tags])
        tool.tags.add(*existing_tags)
        
        # Update logo if available
        logo_url = extract_logo_url(readme_content, github_url)
        if logo_url:
            logo_result = download_and_save_logo(logo_url, tool_name)
            if logo_result:
                filename, content = logo_result
                tool.logo.save(filename, content, save=False)
        
        messages.success(request, f"Successfully imported {tool_name}")
        return redirect("tool_list")
    
    return render(request, "tools/import_modal.html")


class ToolListView(ListView):
    model = Tool
    template_name = "tools/search_results.html"
    context_object_name = "tools"

    def get_queryset(self):
        queryset = Tool.objects.all()
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
        return context

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.headers.get("HX-Request"):
            return render(request, "tools/search_results.html", self.get_context_data())
        return response


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
        tool.name = extract_tool_name(readme_content) or tool.name
        tool.description = readme_content
        
        # Update logo if available
        logo_url = extract_logo_url(readme_content, tool.github_url)
        if logo_url:
            logo_result = download_and_save_logo(logo_url, tool.name)
            if logo_result:
                filename, content = logo_result
                tool.logo.save(filename, content, save=False)
        
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

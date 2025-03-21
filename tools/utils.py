import requests
import markdown
import re
import os
from django.core.files.base import ContentFile
from django.utils.text import slugify
import pdb
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import base64
from io import BytesIO
from PIL import Image
import io


def extract_readme_content(github_url):
    """Extract README content from a GitHub repository as markdown."""
    # Convert GitHub URL to raw content URL
    raw_url = github_url.replace("github.com", "raw.githubusercontent.com")
    
    # Remove any trailing slashes
    raw_url = raw_url.rstrip("/")
    
    # Try different README paths
    readme_paths = [
        "/main/README.md",
        "/master/README.md",
        "/README.md"
    ]
    
    for path in readme_paths:
        try:
            full_url = raw_url + path
            response = requests.get(full_url)
            if response.status_code == 200:
                return response.text
        except requests.RequestException:
            continue
    
    return None


def extract_readme_description(readme_content):
    """Extract description from README content."""
    if not readme_content:
        return ""

    # Split content into lines and find the first paragraph
    lines = readme_content.split("\n")
    description = ""
    in_code_block = False
    paragraph_lines = []

    for line in lines:
        # Skip code blocks
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        # Skip headers and empty lines
        if line.strip().startswith("#") or not line.strip():
            continue

        # Found a non-empty line that's not in a code block
        paragraph_lines.append(line.strip())
        
        # If we have 5 lines, stop
        if len(paragraph_lines) >= 5:
            break

    return " ".join(paragraph_lines)


def extract_tool_name(readme_content):
    """Extract tool name from the first header in README content."""
    if not readme_content:
        return None

    # Split content into lines and find the first header
    lines = readme_content.split("\n")
    
    for line in lines:
        # Look for headers (h1 or h2)
        if line.strip().startswith("# "):  # h1
            name = line.strip("# ").strip()
        elif line.strip().startswith("## "):  # h2
            name = line.strip("# ").strip()
        else:
            continue

        # Strip markdown formatting
        # Remove images: ![alt text](url)
        name = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', name)
        # Remove links: [text](url)
        name = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', name)
        # Remove any remaining markdown characters
        name = re.sub(r'[*_`~]', '', name)
        # Clean up whitespace
        name = re.sub(r'\s+', ' ', name).strip()
        
        if name:
            return name
    
    return None


def extract_logo_url(readme_content, base_url):
    """Extract logo URL from README content."""
    if not readme_content:
        return None

    # Split content into sections (looking for markdown headers)
    sections = re.split(r"^#+\s+", readme_content, flags=re.MULTILINE)
    
    # Only look at the first two sections (introduction and description)
    for section in sections[:2]:
        # Convert markdown to HTML for the section
        html = markdown.markdown(section)
        
        # Parse the HTML
        soup = BeautifulSoup(html, "html.parser")
        
        # Look for images in this section
        images = soup.find_all("img")
        for img in images:
            src = img.get("src", "")
            if src:
                # Convert relative URLs to absolute URLs
                if src.startswith(("/", "http")):
                    logo_url = urljoin(base_url, src)
                else:
                    logo_url = urljoin(base_url + "/", src)

                # Check if the image is likely a logo
                if any(
                    keyword in logo_url.lower()
                    for keyword in ["logo", "icon", "badge", "shield"]
                ):
                    return logo_url

    return None


def download_and_save_logo(url, tool_name):
    """Download and save logo from URL."""
    try:
        response = requests.get(url)
        response.raise_for_status()

        # Convert image to PNG and resize if needed
        img = Image.open(BytesIO(response.content))
        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background

        # Resize if too large
        max_size = (200, 200)
        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)

        # Save as PNG
        output = BytesIO()
        img.save(output, format="PNG", optimize=True)
        output.seek(0)

        # Generate filename
        filename = f"{tool_name.lower().replace(' ', '_')}_logo.png"

        return filename, output
    except Exception:
        return None


def extract_tags_from_readme(readme_content):
    """Extract tags from README content based on tag text."""
    if not readme_content:
        return []
    
    # Convert to lowercase for case-insensitive matching
    content = readme_content.lower()
    
    # List of tag texts to look for
    tag_texts = [
        "tag:", "tags:", "category:", "categories:", 
        "type:", "types:", "kind:", "kinds:",
        "label:", "labels:", "topic:", "topics:"
    ]
    
    found_tags = set()
    
    # Split content into lines for better tag detection
    lines = readme_content.split('\n')
    
    for line in lines:
        line_lower = line.lower()
        
        # Check if line contains any of the tag texts
        for tag_text in tag_texts:
            if tag_text in line_lower:
                # Extract text after the tag text
                parts = line.split(':', 1)
                if len(parts) > 1:
                    # Split by common separators and clean up
                    tags = [tag.strip().strip('.,') for tag in parts[1].split(',')]
                    tags = [tag for tag in tags if tag]  # Remove empty tags
                    found_tags.update(tags)
    
    return list(found_tags)

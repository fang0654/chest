import contextvars
import logging
import re
import time
from contextlib import contextmanager
from io import BytesIO
from urllib.parse import urljoin, urlparse

import markdown
import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.utils.text import slugify
from PIL import Image, UnidentifiedImageError

HTTP_TIMEOUT = 10
MAX_LOGO_BYTES = 5 * 1024 * 1024
LOGO_MAX_SIZE = (200, 200)

# Raw file URLs are case-sensitive, so common spellings are tried in turn
README_FILENAMES = [
    "README.md", "readme.md", "Readme.md", "ReadMe.md", "README.MD", "readme.MD",
    "README.markdown", "README.rst", "readme.rst", "README.txt", "readme.txt",
    "README",
]

logger = logging.getLogger(__name__)


# --- GitHub rate limits -----------------------------------------------------
# github.com and raw.githubusercontent.com answer 429 (sometimes 403 with
# x-ratelimit-remaining: 0) when hit too often. Web requests can't wait long,
# so by default we retry briefly; long-running jobs (Slack poller, history
# import) wrap their work in github_patience() to wait as long as GitHub asks.

class GitHubRateLimited(Exception):
    """GitHub kept rate limiting after all retries."""


_patience = contextvars.ContextVar("github_patience", default=(2, 10))


@contextmanager
def github_patience(retries=8, max_wait=300):
    """Within this block, GitHub requests retry `retries` times, waiting up
    to `max_wait` seconds each time they're rate limited."""
    token = _patience.set((retries, max_wait))
    try:
        yield
    finally:
        _patience.reset(token)


def _rate_limited(resp):
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"


def _retry_wait(resp, attempt, max_wait):
    retry_after = resp.headers.get("Retry-After", "")
    if retry_after.isdigit():
        wait = int(retry_after)
    elif resp.headers.get("x-ratelimit-reset", "").isdigit():
        wait = int(resp.headers["x-ratelimit-reset"]) - int(time.time())
    else:
        wait = 15 * 2 ** attempt  # 15s, 30s, 60s, ...
    return max(1, min(wait, max_wait))


def github_request(method, url, **kwargs):
    """requests.request() that waits out GitHub rate limits.

    Raises GitHubRateLimited if still limited after the allowed retries, and
    lets requests.RequestException through for network errors.
    """
    retries, max_wait = _patience.get()
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    kwargs.setdefault("headers", {"User-Agent": "chest-tool-catalogue"})
    for attempt in range(retries + 1):
        resp = requests.request(method, url, **kwargs)
        if not _rate_limited(resp):
            return resp
        resp.close()
        if attempt == retries:
            break
        wait = _retry_wait(resp, attempt, max_wait)
        logger.info("GitHub rate limited on %s; waiting %ss", urlparse(url).netloc, wait)
        time.sleep(wait)
    raise GitHubRateLimited(f"GitHub is rate limiting requests to {urlparse(url).netloc}")


# First path segments on github.com that are site pages, not users or orgs
_GITHUB_RESERVED_OWNERS = {
    "about", "apps", "collections", "contact", "customer-stories", "codespaces",
    "enterprise", "events", "explore", "features", "issues", "login",
    "marketplace", "new", "notifications", "orgs", "organizations", "pricing",
    "pulls", "readme", "search", "security", "settings", "site", "sponsors",
    "team", "topics", "trending", "users", "join", "signup",
}


def parse_github_repo(github_url):
    """Return (owner, repo) from a GitHub URL, or None if it isn't one.

    Handles trailing slashes, ".git" suffixes and deep links such as
    https://github.com/owner/repo/tree/dev/docs.
    """
    if not github_url:
        return None
    parsed = urlparse(github_url.strip())
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if owner.lower() in _GITHUB_RESERVED_OWNERS:
        return None  # github.com/topics/x, /orgs/x, /features/x ... aren't repos
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def raw_base_url(github_url):
    """Base URL for raw files on the repo's default branch (HEAD)."""
    repo = parse_github_repo(github_url)
    if not repo:
        return None
    return f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/HEAD/"


def repo_name(github_url):
    repo = parse_github_repo(github_url)
    if repo:
        return repo[1]
    return github_url.rstrip("/").split("/")[-1]


def resolve_github_repo(github_url):
    """Follow GitHub's redirects to the repo's current https://github.com/<o>/<r>.

    Renamed or transferred repos redirect to their new home. Returns None if
    the repo no longer exists (deleted, made private, or taken down). Raises
    GitHubRateLimited, or requests.RequestException on network errors.
    """
    repo = parse_github_repo(github_url)
    if not repo:
        return None
    resp = github_request("HEAD", f"https://github.com/{repo[0]}/{repo[1]}",
                          allow_redirects=True)
    if resp.status_code in (404, 451):  # deleted / private, or taken down
        return None
    resp.raise_for_status()
    final = parse_github_repo(resp.url)
    if not final:  # redirected somewhere that isn't a repo (e.g. login)
        return None
    return f"https://github.com/{final[0]}/{final[1]}"


def extract_readme_content(github_url):
    """Fetch the README from the repository's default branch as markdown.

    Raises GitHubRateLimited if GitHub won't serve it right now, so callers
    can tell "no README" apart from "try again later".
    """
    base = raw_base_url(github_url)
    if not base:
        return None

    for filename in README_FILENAMES:
        try:
            response = github_request("GET", base + filename)
        except requests.RequestException:
            continue
        if response.status_code == 200:
            return response.text

    return None


_SUMMARY_SKIP = ("#", "!", "<", "[!", "|", "---", "===", "..", ">", "```", "~~~", "[![")


def summarize_description(text, limit=300):
    """Plain-text summary of a README: the first prose lines, up to `limit`.

    Skips headings, images/badges, HTML, tables, code blocks, quotes and RST
    directives, and strips markdown link/emphasis syntax.
    """
    out, length, in_code = [], 0, False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith(_SUMMARY_SKIP):
            continue
        if re.fullmatch(r"[-=*_~^]{3,}", line):  # setext / RST underlines
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)        # images
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)     # [text](url)
        line = re.sub(r"<[^>]+>", "", line)                       # inline HTML
        line = re.sub(r"[*_`]", "", line).strip()
        if not line:
            continue
        out.append(line)
        length += len(line) + 1
        if length >= limit:
            break
    summary = " ".join(out)
    if len(summary) > limit:
        summary = summary[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return summary


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


def _readme_soup(readme_content):
    # fenced_code keeps "# comments" inside code blocks from becoming headings
    html = markdown.markdown(readme_content, extensions=["fenced_code"])
    return BeautifulSoup(html, "html.parser")


# Headings that are README section titles, never a tool's name
_GENERIC_HEADINGS = {
    "table of contents", "contents", "toc", "overview", "introduction", "about",
    "description", "installation", "install", "usage", "getting started",
    "get started", "features", "documentation", "license", "readme",
}
_NAME_SEPARATORS = re.compile(r"\s+[-–—|:]\s+|:\s+")
_MAX_NAME_LENGTH = 60


def _clean_name(text):
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "ffuf - Fuzz Faster U Fool" -> "ffuf"; the tagline belongs in the description
    head = _NAME_SEPARATORS.split(text, maxsplit=1)[0].strip()
    return head or text


def extract_tool_name(readme_content, github_url=None):
    """Work out a tool's display name.

    Uses the README's first top-level (H1) heading, markdown or HTML, if it
    has usable text. Otherwise falls back to the repository name, since lower
    level headings are section titles ("Table of Contents", "Installation").
    """
    fallback = repo_name(github_url) if github_url else None
    if not readme_content:
        return fallback

    h1 = _readme_soup(readme_content).find("h1")
    if h1:
        name = _clean_name(h1.get_text(" ", strip=True))
        if _plausible_name(name, fallback):
            return name

    return fallback


_MAX_NAME_WORDS = 6


def _plausible_name(name, repo):
    """Whether an H1 reads like a tool name rather than an announcement.

    Rejects generic section titles, anything containing a URL, and long
    headings ("CHECK THE NEW LIBRARY ...") unless they mention the repo.
    """
    if not name or len(name) > _MAX_NAME_LENGTH:
        return False
    lowered = name.lower()
    if lowered in _GENERIC_HEADINGS or re.search(r"https?://|www\.", lowered):
        return False
    if name.startswith(".."):  # reStructuredText directive (".. image::")
        return False
    if len(name.split()) > _MAX_NAME_WORDS:
        squashed = re.sub(r"[^a-z0-9]", "", lowered)
        words = [w for w in re.split(r"[-_.]", (repo or "").lower()) if len(w) >= 3]
        return bool(words) and any(w in squashed for w in words)
    return True


_BADGE_HOSTS = (
    "img.shields.io", "shields.io", "badgen.net", "badge.fury.io", "travis-ci.org",
    "travis-ci.com", "codecov.io", "circleci.com", "coveralls.io", "app.codacy.com",
    "goreportcard.com", "godoc.org", "pkg.go.dev", "pepy.tech", "api.netlify.com", "readthedocs.org",
    "deepsource.io", "img.youtube.com", "api.star-history.com", "contrib.rocks",
)
_LOGO_HINTS = ("logo", "icon", "banner", "cover", "mascot", "brand")
# Pictures that show the tool rather than identify it
_NOT_LOGO_HINTS = ("screenshot", "screen_shot", "screen-shot", "demo", "diagram",
                   "example", "usage", "preview", "overview", "architecture")


def _absolute_image_url(src, github_url):
    """Resolve an <img src> from a README to a URL that returns image bytes."""
    src = src.strip()
    if src.startswith("//"):
        src = "https:" + src
    parsed = urlparse(src)
    if parsed.scheme in ("http", "https"):
        # github.com/<o>/<r>/blob/<ref>/path serves an HTML page; use the raw file
        if parsed.netloc.lower() in ("github.com", "www.github.com"):
            parts = parsed.path.split("/")
            if len(parts) > 4 and parts[3] == "blob":
                parts[3] = "raw"
                return parsed._replace(path="/".join(parts)).geturl()
        return src
    if parsed.scheme:
        return None  # data:, javascript:, etc.

    base = raw_base_url(github_url)
    if not base:
        return None
    # "/static/x.png" and "static/x.png" are both relative to the repo root
    return urljoin(base, src.lstrip("/"))


def _is_badge(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        any(host == h or host.endswith("." + h) for h in _BADGE_HOSTS)
        or "badge" in path
        or "/workflows/" in path
        or parsed.query.lower().endswith(".svg")  # e.g. godoc "?status.svg"
    )


def extract_logo_candidates(readme_content, github_url):
    """Return likely logo image URLs from the top of a README, best first.

    Only the part before the second heading is considered (logos sit above or
    under the title). An image qualifies only if its src or alt says "logo",
    "banner", etc. or contains the repo name (or a 4+ letter part of it, so
    "peass.png" matches PEASS-ng). Badges, SVGs, screenshots and diagrams are
    dropped. Many READMEs have no logo; then this returns [].
    """
    if not readme_content:
        return []

    sections = re.split(r"^#+\s+", readme_content, flags=re.MULTILINE)
    top = "\n# ".join(sections[:2])
    repo = (repo_name(github_url) or "").lower()
    repo_words = {repo} | {w for w in re.split(r"[-_.]", repo) if len(w) >= 4}
    repo_words.discard("")

    candidates = []
    for img in _readme_soup(top).find_all("img"):
        url = _absolute_image_url(img.get("src", ""), github_url)
        if not url or url in candidates or _is_badge(url):
            continue
        text = (url + " " + img.get("alt", "")).lower()
        if any(h in text for h in _NOT_LOGO_HINTS):
            continue
        if any(h in text for h in _LOGO_HINTS) or any(w in text for w in repo_words):
            candidates.append(url)

    return candidates


def extract_logo_url(readme_content, github_url):
    """Best single logo candidate (kept for callers that want just the URL)."""
    candidates = extract_logo_candidates(readme_content, github_url)
    return candidates[0] if candidates else None


# SVGs are rasterized at this width (height follows the aspect ratio), then
# thumbnailed like any other image. Rendering at a fixed size keeps a hostile
# width="100000" from allocating a huge surface.
SVG_RENDER_WIDTH = 400
MAX_ASPECT_RATIO = 6  # wider (or taller) than this is a badge or a banner strip


def _looks_like_svg(data, content_type):
    if "svg" in (content_type or "").lower():
        return True
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _render_svg(data):
    """Rasterize SVG bytes to PNG bytes, or None if it can't be rendered.

    README SVGs are untrusted. With unsafe=False (CairoSVG >= 2.7.1) external
    <image>/<use> references other than data: URLs are replaced with an empty
    image, so an SVG can't make the server read local files or reach internal
    URLs, and XML entities are refused (XXE, billion laughs). Gzipped SVGZ is
    rejected so a 5 MB download can't inflate into something much larger.
    """
    if data.startswith(b"\x1f\x8b"):
        return None
    try:
        import cairosvg  # needs the cairo C library; optional outside Docker
    except (ImportError, OSError):
        return None
    try:
        return cairosvg.svg2png(
            bytestring=data,
            output_width=SVG_RENDER_WIDTH,
            unsafe=False,  # never True: see docstring
        )
    except Exception:  # malformed SVGs raise all sorts of parser errors
        return None


def download_and_save_logo(url, tool_name):
    """Download an image and return (filename, ContentFile) as a 200px PNG.

    Raster images are decoded with Pillow; SVGs are rendered with CairoSVG.
    Returns None if the URL doesn't serve a decodable image, or if the image
    looks like a badge (too small or too wide).
    """
    try:
        response = github_request("GET", url, stream=True)
        response.raise_for_status()
        data = response.raw.read(MAX_LOGO_BYTES + 1, decode_content=True)
        if len(data) > MAX_LOGO_BYTES:
            return None

        is_svg = _looks_like_svg(data, response.headers.get("Content-Type"))
        if is_svg:
            data = _render_svg(data)
            if not data:
                return None

        img = Image.open(BytesIO(data))
        img.seek(0)  # first frame of animated GIFs
        width, height = img.size
        # Vector images have no meaningful pixel size, so only raster images
        # get the minimum-height check.
        if not is_svg and height < 32:
            return None
        if width / height > MAX_ASPECT_RATIO or height / width > MAX_ASPECT_RATIO:
            return None

        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
        img.thumbnail(LOGO_MAX_SIZE, Image.Resampling.LANCZOS)

        output = BytesIO()
        img.save(output, format="PNG", optimize=True)
    except (requests.RequestException, GitHubRateLimited, UnidentifiedImageError,
            OSError, ValueError):
        # No logo is better than a failed import; the logo view retries later
        return None

    filename = f"{slugify(tool_name) or 'tool'}_logo.png"
    return filename, ContentFile(output.getvalue())


def fetch_logo(readme_content, github_url, tool_name):
    """Try each logo candidate in order; return (filename, ContentFile) or None."""
    for url in extract_logo_candidates(readme_content, github_url):
        result = download_and_save_logo(url, tool_name)
        if result:
            return result
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

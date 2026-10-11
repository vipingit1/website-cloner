"""Create a bounded, same-origin static copy of a public website.

Only mirror sites you own or have permission to archive. The cloner honors
robots.txt by default and does not submit forms, log in, or execute JavaScript.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser


DEFAULT_OUTPUT_DIR = Path("D:/Generated-Outputs/website-clone")
USER_AGENT = "AuthorizedStaticSiteMirror/1.0 (+https://example.invalid/bot-info)"
CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(r"(@import\s+)(['\"])(.*?)\2", re.IGNORECASE)


def _safe_local_path(url: str, site_root: Path, *, page: bool = False) -> Path:
    """Map a URL to a safe path beneath site_root, distinguishing query URLs."""
    parts = urlsplit(url)
    segments = [
        segment
        for segment in unquote(parts.path).replace("\\", "/").split("/")
        if segment not in {"", ".", ".."}
    ]
    safe_segments = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", segment) for segment in segments]
    relative = PurePosixPath(*safe_segments) if safe_segments else PurePosixPath()

    if not safe_segments or parts.path.endswith("/"):
        relative = relative / "index.html"
    elif page and not relative.suffix:
        relative = relative.with_name(relative.name + ".html")

    local_path = site_root.joinpath(*relative.parts)
    if parts.query:
        digest = hashlib.sha256(parts.query.encode("utf-8")).hexdigest()[:10]
        local_path = local_path.with_name(f"{local_path.stem}__{digest}{local_path.suffix}")

    # Resolve before use so unusual URL segments cannot escape the output folder.
    resolved_root = site_root.resolve()
    resolved_path = local_path.resolve()
    if resolved_root not in resolved_path.parents and resolved_path != resolved_root:
        raise ValueError(f"URL maps outside the output directory: {url}")
    return resolved_path


def _is_page_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not Path(path).suffix or path.endswith((".html", ".htm", ".xhtml")) or path.endswith("/")


def _relative_link(source_file: Path, target_file: Path) -> str:
    return quote(os.path.relpath(target_file, source_file.parent).replace(os.sep, "/"), safe="/._-~")


class _HTMLMirrorParser(HTMLParser):
    """Rewrite same-origin links while preserving the page's HTML structure."""

    def __init__(self, rewrite: Callable[[str, bool], str], page_link: Callable[[str], None]):
        super().__init__(convert_charrefs=False)
        self.rewrite = rewrite
        self.page_link = page_link
        self.output: list[str] = []

    def _start_tag(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool = False) -> None:
        lower_tag = tag.lower()
        rel_value = next((value or "" for key, value in attrs if key.lower() == "rel"), "").lower()
        rewritten_attrs: list[str] = []

        for name, value in attrs:
            lower_name = name.lower()
            if value is None:
                rewritten_attrs.append(name)
                continue

            if lower_name == "href":
                if lower_tag == "a":
                    self.page_link(value)
                    value = self.rewrite(value, False)
                elif lower_tag == "link" and any(word in rel_value for word in ("stylesheet", "icon", "preload", "manifest")):
                    value = self.rewrite(value, True)
            elif lower_name in {"src", "poster"}:
                value = self.rewrite(value, True)
            elif lower_name == "srcset":
                candidates: list[str] = []
                for candidate in value.split(","):
                    fields = candidate.strip().split()
                    if fields:
                        fields[0] = self.rewrite(fields[0], True)
                        candidates.append(" ".join(fields))
                value = ", ".join(candidates)
            elif lower_name == "style":
                value = self.rewrite_css(value)

            rewritten_attrs.append(f'{name}="{html.escape(value, quote=True)}"')

        slash = " />" if closed else ">"
        self.output.append(f"<{tag}{(' ' + ' '.join(rewritten_attrs)) if rewritten_attrs else ''}{slash}")

    def rewrite_css(self, css: str) -> str:
        def replace_url(match: re.Match[str]) -> str:
            raw_url = match.group(2).strip()
            if not raw_url or raw_url.startswith(("#", "data:", "mailto:", "javascript:")):
                return match.group(0)
            return f"url({match.group(1)}{self.rewrite(raw_url, True)}{match.group(1)})"

        return CSS_URL_PATTERN.sub(replace_url, css)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")

    def unknown_decl(self, data: str) -> None:
        self.output.append(f"<![{data}]>")


def clone_website(
    start_url: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    max_pages: int = 100,
    max_depth: int = 3,
    delay: float = 0.5,
    timeout: float = 20.0,
    respect_robots: bool = True,
) -> Path:
    """Mirror a public website to disk and return the local output directory.

    Crawling is restricted to the starting origin. Page count, depth, per-file
    size, and request delay are bounded to reduce load on the remote site.
    """
    start_url = urldefrag(start_url.strip())[0]
    parsed_start = urlsplit(start_url)
    if parsed_start.scheme not in {"http", "https"} or not parsed_start.netloc:
        raise ValueError("start_url must be an absolute http:// or https:// URL")
    if max_pages < 1 or max_depth < 0 or delay < 0 or timeout <= 0:
        raise ValueError("max_pages must be positive; depth/delay nonnegative; timeout positive")

    site_root = Path(output_dir).expanduser().resolve()
    site_root.mkdir(parents=True, exist_ok=True)
    origin = (parsed_start.scheme.lower(), parsed_start.netloc.lower())
    pages: deque[tuple[str, int]] = deque([(start_url, 0)])
    queued_pages = {start_url}
    saved_pages: set[str] = set()
    saved_assets: dict[str, Path] = {}
    robots: RobotFileParser | None = None
    last_request_at = 0.0
    max_file_bytes = 25 * 1024 * 1024
    max_total_fetches = max_pages * 10
    fetch_count = 0

    if respect_robots:
        robots_url = urlunsplit((parsed_start.scheme, parsed_start.netloc, "/robots.txt", "", ""))
        robots = RobotFileParser(robots_url)
        try:
            request = Request(robots_url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                robots.parse(response.read(512 * 1024).decode("utf-8", errors="replace").splitlines())
        except (HTTPError, URLError, TimeoutError, OSError):
            # A missing or unavailable robots.txt does not prevent an authorized crawl.
            robots = None

    def same_origin(url: str) -> bool:
        parsed = urlsplit(url)
        return (parsed.scheme.lower(), parsed.netloc.lower()) == origin

    def fetch(url: str) -> tuple[bytes, str] | None:
        nonlocal last_request_at, fetch_count
        if fetch_count >= max_total_fetches:
            return None
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return None
        wait = delay - (time.monotonic() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read(max_file_bytes + 1)
                content_type = response.headers.get_content_type()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            print(f"Skipped {url}: {error}")
            return None
        finally:
            last_request_at = time.monotonic()
            fetch_count += 1
        if len(data) > max_file_bytes:
            print(f"Skipped oversized file: {url}")
            return None
        return data, content_type

    def rewrite_css(css: str, base_url: str, source_file: Path) -> str:
        def localize(raw_url: str) -> str:
            absolute = urldefrag(urljoin(base_url, raw_url.strip()))[0]
            if not same_origin(absolute):
                return raw_url
            local_target = download_asset(absolute)
            return _relative_link(source_file, local_target) if local_target else raw_url

        def replace_url(match: re.Match[str]) -> str:
            raw_url = match.group(2).strip()
            if not raw_url or raw_url.startswith(("#", "data:", "mailto:", "javascript:")):
                return match.group(0)
            return f"url({match.group(1)}{localize(raw_url)}{match.group(1)})"

        css = CSS_URL_PATTERN.sub(replace_url, css)

        def replace_import(match: re.Match[str]) -> str:
            return f"{match.group(1)}{match.group(2)}{localize(match.group(3))}{match.group(2)}"

        return CSS_IMPORT_PATTERN.sub(replace_import, css)

    def download_asset(url: str) -> Path | None:
        absolute = urldefrag(url)[0]
        if not same_origin(absolute):
            return None
        if absolute in saved_assets:
            return saved_assets[absolute]
        local_path = _safe_local_path(absolute, site_root)
        # Mark before parsing CSS so cyclic imports terminate.
        saved_assets[absolute] = local_path
        result = fetch(absolute)
        if result is None:
            saved_assets.pop(absolute, None)
            return None
        data, content_type = result
        if content_type == "text/css" or urlsplit(absolute).path.lower().endswith(".css"):
            css_text = data.decode("utf-8", errors="replace")
            css_text = rewrite_css(css_text, absolute, local_path)
            data = css_text.encode("utf-8")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return local_path

    def page_link(raw_url: str, page_depth: int, base_url: str) -> None:
        absolute = urldefrag(urljoin(base_url, raw_url.strip()))[0]
        if same_origin(absolute) and _is_page_url(absolute) and page_depth < max_depth:
            if absolute not in queued_pages and absolute not in saved_pages and len(queued_pages) < max_pages:
                queued_pages.add(absolute)
                pages.append((absolute, page_depth + 1))

    while pages and len(saved_pages) < max_pages:
        page_url, depth = pages.popleft()
        if page_url in saved_pages:
            continue
        if not same_origin(page_url):
            continue
        response = fetch(page_url)
        if response is None:
            continue
        data, content_type = response
        local_page = _safe_local_path(page_url, site_root, page=True)
        local_page.parent.mkdir(parents=True, exist_ok=True)

        if content_type in {"text/html", "application/xhtml+xml"} or _is_page_url(page_url):
            charset_match = re.search(rb"charset=([\w-]+)", data[:2048], re.IGNORECASE)
            encoding = charset_match.group(1).decode("ascii", errors="ignore") if charset_match else "utf-8"
            source = data.decode(encoding or "utf-8", errors="replace")

            def rewrite_html_url(raw_url: str, is_asset: bool) -> str:
                absolute = urldefrag(urljoin(page_url, raw_url.strip()))[0]
                if not raw_url.strip() or not same_origin(absolute):
                    return raw_url
                if is_asset or not _is_page_url(absolute):
                    local_asset = download_asset(absolute)
                    return _relative_link(local_page, local_asset) if local_asset else raw_url
                return _relative_link(local_page, _safe_local_path(absolute, site_root, page=True))

            parser = _HTMLMirrorParser(
                rewrite=rewrite_html_url,
                page_link=lambda link: page_link(link, depth, page_url),
            )
            parser.feed(source)
            parser.close()
            local_page.write_text("".join(parser.output), encoding="utf-8")
        else:
            local_page.write_bytes(data)

        saved_pages.add(page_url)
        print(f"Saved page {len(saved_pages)}/{max_pages}: {page_url}")

    print(f"Finished: {len(saved_pages)} pages, {len(saved_assets)} assets in {site_root}")
    return site_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Make a bounded static copy of an authorized website.")
    parser.add_argument("url", help="Starting http:// or https:// URL")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output folder")
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum HTML pages (default: 100)")
    parser.add_argument("--max-depth", type=int, default=3, help="Link depth from the starting page (default: 3)")
    parser.add_argument("--delay", type=float, default=0.5, help="Minimum seconds between requests (default: 0.5)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds (default: 20)")
    parser.add_argument("--ignore-robots", action="store_true", help="Do not check robots.txt")
    args = parser.parse_args()

    try:
        clone_website(
            args.url,
            output_dir=args.output,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            delay=args.delay,
            timeout=args.timeout,
            respect_robots=not args.ignore_robots,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

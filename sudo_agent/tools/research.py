"""Web research without an API key: search results and readable page text."""
from __future__ import annotations

import ipaddress
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx

from .base import ToolContext, ToolResult, tool

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SUDO/0.1"}
_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_search(html: str) -> list[dict]:
    snippets = [unescape(_TAG.sub("", s)).strip() for s in _SNIPPET.findall(html)]
    results = []
    for i, (href, title) in enumerate(_RESULT.findall(html)):
        link = unescape(href)
        if "uddg=" in link:
            link = parse_qs(urlparse(link).query).get("uddg", [link])[0]
        results.append({"title": unescape(_TAG.sub("", title)).strip(), "url": link,
                        "snippet": snippets[i] if i < len(snippets) else ""})
    return results


def is_public_host(host: str) -> bool:
    """Stops page content from steering fetches at this PC or the local network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            return False
    return True


@tool("web_search", "Search the web. Returns titles, links and snippets.", {"query": {"type": "string"}}, ["query"],
      group="research", phase="OBSERVE", retries=2, timeout=30, untrusted=True)
def web_search(args, ctx):
    response = httpx.post("https://html.duckduckgo.com/html/", data={"q": args["query"]},
                          headers=_HEADERS, timeout=20, follow_redirects=True)
    response.raise_for_status()
    results = parse_search(response.text)
    if not results:
        return ToolResult("The search returned no parsable results. Try browser_open with a search page.", ok=False)
    return ToolResult("\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results[:10]))


@tool("web_fetch", "Fetch a web page as plain text (public sites only). Use the browser tools for pages that need JavaScript or a login.",
      {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"],
      group="research", phase="OBSERVE", retries=2, timeout=30, untrusted=True)
def web_fetch(args: dict, ctx: ToolContext):
    url = args["url"]
    limit = min(args.get("max_chars", 20000), 80000)
    for _ in range(6):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ToolResult("Only http and https addresses are allowed.", ok=False)
        if not ctx.allow_private_fetch and not is_public_host(parsed.hostname):
            return ToolResult("That address is on this PC or the private network, so it was not fetched.", ok=False)
        response = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=False)
        if response.is_redirect and response.headers.get("location"):
            url = str(httpx.URL(url).join(response.headers["location"]))
            continue
        break
    else:
        return ToolResult("Too many redirects.", ok=False)
    if response.status_code >= 400:
        return ToolResult(f"HTTP {response.status_code} from {url}", ok=False)
    kind = response.headers.get("content-type", "")
    text = html_to_text(response.text) if "html" in kind else response.text
    note = f"\n[truncated: {len(text)} characters total]" if len(text) > limit else ""
    return ToolResult(f"Source: {url}\n{text[:limit]}{note}")

"""HTML -> markdown/text, metadata and link extraction (selectolax/lexbor)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..utils import canonical_url, is_plausible_content_url, normalize_space, unique

SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "iframe", "form", "button", "input",
    "select", "textarea", "nav", "footer", "aside", "template", "link", "meta",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "main", "ul", "ol", "table", "dl",
    "blockquote", "pre", "figure", "figcaption",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def parse_html(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html or "<html></html>")


def html_to_text(html: str, *, max_chars: int = 200_000) -> str:
    tree = parse_html(html)
    for tag in SKIP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    text = body.text(separator="\n", strip=True)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text[:max_chars]


def html_to_markdown(html: str, base_url: str = "", *, max_chars: int = 200_000) -> str:
    tree = parse_html(html)
    for tag in SKIP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    rendered = _render_children(root, base_url)
    markdown = _normalize_markdown(rendered)
    return markdown[:max_chars]


def _children(node) -> list[Any]:
    result = []
    child = node.child
    while child is not None:
        result.append(child)
        child = child.next
    return result


def _render_children(node, base_url: str) -> str:
    return "".join(_render(child, base_url) for child in _children(node))


def _render(node, base_url: str) -> str:
    tag = node.tag
    if tag == "-text":
        raw = node.text(deep=False) if _supports_deep(node) else node.text()
        return re.sub(r"[ \t\r\f\v]+", " ", raw or "")
    if tag in SKIP_TAGS:
        return ""
    if tag == "br":
        return "\n"
    if tag == "hr":
        return "\n\n---\n\n"
    if tag in _HEADINGS:
        inner = normalize_space(_render_children(node, base_url))
        return f"\n\n{'#' * _HEADINGS[tag]} {inner}\n\n" if inner else ""
    if tag == "li":
        inner = normalize_space(_render_children(node, base_url))
        return f"\n- {inner}" if inner else ""
    if tag == "tr":
        cells = [
            normalize_space(_render_children(cell, base_url))
            for cell in node.css("td, th")
        ]
        return "\n| " + " | ".join(cells) + " |" if any(cells) else ""
    if tag == "pre":
        inner = node.text()
        return f"\n\n```\n{inner}\n```\n\n" if inner.strip() else ""
    if tag == "a":
        inner = normalize_space(_render_children(node, base_url))
        href = (node.attributes or {}).get("href", "")
        if not inner:
            return ""
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            absolute = urljoin(base_url, href)
            if absolute.startswith(("http://", "https://")):
                return f"[{inner}]({absolute})"
        return inner
    if tag == "img":
        attrs = node.attributes or {}
        alt = normalize_space(attrs.get("alt", ""))
        return f"![{alt}]({attrs.get('src', '')})" if alt else ""
    if tag == "strong" or tag == "b":
        inner = normalize_space(_render_children(node, base_url))
        return f"**{inner}**" if inner else ""
    if tag == "em" or tag == "i":
        inner = normalize_space(_render_children(node, base_url))
        return f"*{inner}*" if inner else ""

    inner = _render_children(node, base_url)
    if tag in BLOCK_TAGS or tag in {"body", "html"}:
        return f"\n\n{inner}\n\n"
    return inner


def _supports_deep(node) -> bool:
    return True


def _normalize_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r", "").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def page_metadata(html: str, url: str = "") -> dict[str, Any]:
    tree = parse_html(html)
    title = tree.css_first("title")
    description = tree.css_first('meta[name="description"]')
    canonical = tree.css_first('link[rel="canonical"]')
    og_title = tree.css_first('meta[property="og:title"]')
    og_image = tree.css_first('meta[property="og:image"]')
    h1 = tree.css_first("h1")
    return {
        "title": normalize_space(title.text()) if title else "",
        "description": normalize_space(description.attributes.get("content", ""))
        if description
        else "",
        "canonical": urljoin(url, canonical.attributes.get("href", "")) if canonical else "",
        "og_title": normalize_space(og_title.attributes.get("content", "")) if og_title else "",
        "og_image": og_image.attributes.get("content", "") if og_image else "",
        "h1": normalize_space(h1.text()) if h1 else "",
        "json_ld": extract_json_ld(html),
    }


def extract_json_ld(html: str) -> list[dict[str, Any]]:
    tree = parse_html(html)
    blocks: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or node.text_content() if hasattr(node, "text_content") else node.text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites embed multiple concatenated objects; try a lenient slice.
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                continue
            try:
                payload = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
        blocks.extend(_flatten_json_ld(payload))
    return blocks


def _flatten_json_ld(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        result: list[dict[str, Any]] = []
        for item in payload:
            result.extend(_flatten_json_ld(item))
        return result
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("@graph"), list):
        result = []
        for item in payload["@graph"]:
            result.extend(_flatten_json_ld(item))
        return result
    if "itemListElement" in payload and isinstance(payload["itemListElement"], list):
        result = []
        for item in payload["itemListElement"]:
            candidate = item.get("item", item) if isinstance(item, dict) else item
            result.extend(_flatten_json_ld(candidate))
        if result:
            return result
    return [payload]


def extract_links(html: str, base_url: str, *, same_site: bool = True, limit: int = 400) -> list[str]:
    tree = parse_html(html)
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    found: list[str] = []
    for node in tree.css("a[href]"):
        href = (node.attributes or {}).get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        host = (urlsplit(absolute).hostname or "").lower().removeprefix("www.")
        if same_site and host != base_host:
            continue
        if not is_plausible_content_url(absolute):
            continue
        found.append(canonical_url(absolute))
        if len(found) >= limit:
            break
    return unique(found)


def root_domain_type(html: str) -> str:
    """Coarse page type used by the heuristic assignment in the extraction learner."""
    tree = parse_html(html)
    if tree.css_first("article"):
        return "article"
    if tree.css_first("table"):
        return "table"
    items = tree.css("li")
    if len(items) > 10:
        return "list"
    return "detail"

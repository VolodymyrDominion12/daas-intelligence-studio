"""Site audit: contacts, socials, tech stack, booking/chat signals from the company site."""

from __future__ import annotations

from typing import Any

from ..scraping.page import extract_links, parse_html
from ..utils import find_emails, find_phones, normalize_domain, normalize_space
from .base import Enricher, EnrichmentContext, EnrichmentResult
from .tech_detect import detect_booking, detect_chat, detect_socials, detect_tech_stack


class SiteAuditEnricher(Enricher):
    key = "site_audit"
    title = "Аудит сайту (контакти, соцмережі, технології)"
    produces = [
        "email", "emails", "phone", "phones", "socials", "tech_stack",
        "has_online_booking", "has_chat", "site_title",
    ]
    requires = ["website"]

    def applies(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("website"))

    async def run(self, payload: dict[str, Any], ctx: EnrichmentContext) -> EnrichmentResult:
        website = str(payload.get("website") or "").strip()
        if not website:
            return EnrichmentResult(skipped=True, notes=["no website"])
        result = await ctx.fetcher.fetch(website)
        if result.page is None:
            return EnrichmentResult(
                skipped=True, notes=[f"site audit skipped: {result.error or result.kind}"]
            )
        page = result.page
        html = page.html
        text = page.text

        emails = find_emails(text)
        mailto = [
            link.split(":", 1)[1].split("?")[0].strip()
            for link in _mailto_links(html)
            if "@" in link
        ]
        emails = sorted({email.lower() for email in [*emails, *mailto]})
        phones = find_phones(text)
        socials = detect_socials(html)
        stack = detect_tech_stack(html)
        booking = detect_booking(html)
        chat = detect_chat(html)

        # Try a contact page when the homepage has no contact details.
        if not emails or not phones:
            contact_url = _contact_url(html, page.final_url or website)
            if contact_url:
                contact = await ctx.fetcher.fetch(contact_url)
                if contact.page is not None:
                    emails = sorted({*emails, *find_emails(contact.page.text)})
                    phones = sorted({*phones, *find_phones(contact.page.text)})
                    socials = socials or detect_socials(contact.page.html)

        tree = parse_html(html)
        title_node = tree.css_first("title")
        site_title = normalize_space(title_node.text()) if title_node else ""

        values: dict[str, Any] = {
            "emails": emails[:10],
            "phones": phones[:10],
            "socials": socials,
            "tech_stack": stack,
            "has_online_booking": booking if (booking or bool(text)) else None,
            "has_chat": chat if (chat or bool(text)) else None,
            "site_title": site_title or None,
        }
        if emails:
            values["email"] = emails[0]
        if phones:
            values["phone"] = phones[0]
        values = {key: value for key, value in values.items() if value not in (None, "", [], {})}

        notes = [f"audited {normalize_domain(website) or website}"]
        if result.kind != "ok":
            notes.append(f"fetch kind={result.kind}")
        return EnrichmentResult(values=values, notes=notes)


def _mailto_links(html: str) -> list[str]:
    tree = parse_html(html)
    return [
        node.attributes.get("href", "")
        for node in tree.css('a[href^="mailto:"]')
        if node.attributes
    ]


def _contact_url(html: str, base_url: str) -> str | None:
    for url in extract_links(html, base_url, same_site=True, limit=200):
        lowered = url.lower()
        if any(token in lowered for token in ("contact", "kontakt", "kontakty", "contacts", "about")):
            return url
    return None

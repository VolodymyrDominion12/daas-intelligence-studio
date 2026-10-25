"""Prompt-injection and hallucination guardrails for untrusted page content.

The extraction model never receives tools; page content is always passed inside an
explicit untrusted block, and suspicious instruction patterns are reported upstream
so the pipeline can flag the page instead of trusting its output (docs/02 §3).
"""

from __future__ import annotations

import re

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I)),
    ("system_override", re.compile(r"<\s*/?\s*(system|assistant|developer)\s*>", re.I)),
    ("role_hijack", re.compile(r"\byou\s+are\s+now\s+(a|an|the)\b", re.I)),
    ("reveal_secret", re.compile(r"(reveal|print|show)\s+(the\s+)?(api[\s_-]?key|token|prompt|instructions)", re.I)),
    ("exfiltrate", re.compile(r"(send|post|upload)\s+.{0,40}\b(to|at)\s+https?://", re.I)),
    ("tool_hijack", re.compile(r"(call|use|invoke)\s+(the\s+)?(tool|function|shell|exec)", re.I)),
    ("json_breakout", re.compile(r"```\s*json\s*\}\s*\}", re.I)),
]

_WHITESPACE = re.compile(r"[ \t]+")


def detect_injection(text: str) -> list[str]:
    if not text:
        return []
    findings: list[str] = []
    sample = text[:200_000]
    for name, pattern in INJECTION_PATTERNS:
        if pattern.search(sample):
            findings.append(name)
    return findings


def wrap_untrusted(text: str, *, label: str = "page") -> str:
    """Delimit untrusted content so the system prompt can refer to it unambiguously."""
    cleaned = text.replace("<untrusted", "&lt;untrusted").replace("</untrusted", "&lt;/untrusted")
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return f"<untrusted_{label}>\n{cleaned}\n</untrusted_{label}>"


def sanitize_for_llm(text: str, *, limit: int = 60_000, redact: bool = False) -> str:
    from ..utils import redact_pii, truncate

    value = truncate(text or "", limit)
    if redact:
        value = redact_pii(value)
    return value

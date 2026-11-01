"""Heuristic CSS-schema learner (calibration phase).

No LLM required: find a repeated item group on a listing page, assign columns to
descendant nodes by class/itemprop/label synonyms, then evaluate the schema across
the calibration samples. The LLM is only used when this fails (see engine.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import CssField, CssSchema, FieldSpec, TaskSpec
from ..scraping.page import parse_html
from ..utils import coerce_value, find_emails, find_phones, normalize_space, short_hash
from .synonyms import field_tokens, normalize_token, text_matches_field

MAX_NODES = 2500
UNSTABLE_CLASS = re.compile(r"^(active|open|selected|row|col|item|container|content|block|flex|grid|w-\d+|is-.*|has-.*)$", re.I)
CLASS_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{1,40}$")
DTYPE_HINT = {"url": "a", "image": "img", "datetime": "time"}


@dataclass
class Group:
    parent: Any
    children: list[Any]
    score: float
    signature: str


@dataclass
class Candidate:
    node: Any
    selector: str
    descriptor: str
    value: str
    attr: str | None = None
    depth: int = 0
    hints: set[str] = field(default_factory=set)
    score: float = 0.0


def learn_schema(samples: list[tuple[str, str]], spec: TaskSpec) -> CssSchema:
    """Return the best schema found across calibration samples (may be empty)."""
    best: CssSchema | None = None
    best_rank: tuple = (-1, -1.0, -1)

    for url, html in samples:
        if not html:
            continue
        tree = parse_html(html)
        group = _best_group(tree)
        schema = (
            _schema_from_group(tree, group, spec)
            if group is not None
            else _schema_detail(tree, spec)
        )
        if schema is None or not schema.fields:
            continue
        schema.sample_url = url
        records, coverage = _evaluate(schema, samples, spec)
        schema.coverage = {key: round(value * 100, 1) for key, value in coverage.items()}
        schema.score = round(records * (sum(coverage.values()) / max(1, len(coverage))), 2)
        rank = (1 if records else 0, schema.score, len(schema.fields))
        if best is None or rank > best_rank:
            best, best_rank = schema, rank
    if best is None:
        return CssSchema(container="", fields=[], method="css", notes=["no schema learned"])
    if best.score <= 0:
        best.notes.append("learned schema extracted nothing — LLM fallback recommended")
    return best


# ---------------------------------------------------------------------------
# repeated-group discovery
# ---------------------------------------------------------------------------


def _iter_nodes(root, max_nodes: int = MAX_NODES):
    stack: list[tuple[Any, int]] = [(root, 0)]
    seen = 0
    while stack and seen < max_nodes:
        node, depth = stack.pop()
        if node.tag in ("-text", "script", "style", "svg"):
            continue
        seen += 1
        yield node, depth
        if depth >= 7:
            continue
        children = []
        child = node.child
        while child is not None:
            children.append(child)
            child = child.next
        for child in reversed(children):
            stack.append((child, depth + 1))


def _classes(node) -> list[str]:
    raw = (node.attributes or {}).get("class", "") or ""
    return [item for item in raw.split() if CLASS_SAFE.match(item)]


def _stable_classes(nodes: list[Any]) -> list[str]:
    common: set[str] | None = None
    for node in nodes:
        classes = {cls for cls in _classes(node) if not UNSTABLE_CLASS.match(cls)}
        common = classes if common is None else (common & classes)
    return sorted(common or [])


def _best_group(tree) -> Group | None:
    best: Group | None = None
    for node, _depth in _iter_nodes(tree.root):
        children = []
        child = node.child
        while child is not None:
            if child.tag not in ("-text", "script", "style"):
                children.append(child)
            child = child.next
        if len(children) < 2:
            continue
        signature_key = (children[0].tag, tuple(_stable_classes(children)))
        if signature_key[1] == () and not _stable_classes([node]):
            continue
        texts = [normalize_space(item.text(strip=True)) for item in children]
        lengths = [len(text) for text in texts]
        if sum(1 for length in lengths if length >= 12) < 2:
            continue
        avg = sum(lengths) / len(lengths)
        if avg < 18:
            continue
        has_structure = sum(
            1 for item in children if item.css_first("a, h1, h2, h3, h4, img, [itemprop]")
        )
        if has_structure < len(children) * 0.5:
            continue
        penalty = 0.5 if node.tag in ("body", "html", "ul", "ol", "nav", "footer") else 1.0
        score = (len(children) ** 0.7) * (avg**0.35) * penalty
        if node.tag in ("body", "html"):
            continue
        if best is None or score > best.score:
            best = Group(
                parent=node,
                children=children,
                score=round(score, 3),
                signature=f"{children[0].tag}.{'-'.join(signature_key[1])}",
            )
    return best


def _container_selector(tree, group: Group) -> str | None:
    tag = group.children[0].tag
    for cls in _stable_classes(group.children):
        selector = f"{tag}.{cls}"
        if len(tree.css(selector)) >= len(group.children):
            return selector
    for cls in _stable_classes([group.parent]):
        selector = f"{group.parent.tag}.{cls} > {tag}"
        if len(tree.css(selector)) >= len(group.children):
            return selector
    parent_selector = _unique_selector(group.parent, tree, max_depth=4)
    if parent_selector and not parent_selector.endswith(":nth-of-type(1)"):
        return f"{parent_selector} > {tag}"
    return _unique_selector(group.children[0], tree, max_depth=4)


def _schema_from_group(tree, group: Group, spec: TaskSpec) -> CssSchema | None:
    container = _container_selector(tree, group)
    if not container:
        return None
    fields: list[CssField] = []
    for column in spec.columns:
        if column.is_ai:
            continue
        assignment = _assign_column(group.children, column)
        if assignment is None and column.key == "name":
            fields.append(
                CssField(key="name", selector="h1, h2, h3, h4, [itemprop='name'], .name, .title",
                         dtype="string", confidence=0.55, aliases=column.aliases)
            )
            continue
        if assignment is None:
            continue
        selector, attr, confidence = assignment
        fields.append(
            CssField(
                key=column.key,
                selector=selector,
                attr=attr,
                dtype=column.dtype,
                confidence=round(confidence, 2),
                aliases=column.aliases,
            )
        )
    notes = [f"group: {len(group.children)} items, container={container}"]
    return CssSchema(
        container=container,
        item_scope=True,
        fields=fields,
        method="css",
        item_signature=group.signature,
        notes=notes,
    )


def _schema_detail(tree, spec: TaskSpec) -> CssSchema | None:
    body = tree.body
    if body is None:
        return None
    candidates = _collect_candidates(tree, body)
    fields: list[CssField] = []
    for column in spec.columns:
        if column.is_ai or column.method == "enrichment":
            continue
        chosen = _best_candidate(candidates, column)
        if chosen is None and column.key == "name":
            heading = tree.css_first("h1, h2")
            if heading is not None:
                selector = _unique_selector(heading, tree, max_depth=5)
                if selector:
                    fields.append(
                        CssField(
                            key="name",
                            selector=selector,
                            dtype="string",
                            confidence=0.8,
                            aliases=column.aliases,
                        )
                    )
            continue
        if chosen is None:
            continue
        selector = _unique_selector(chosen.node, tree, max_depth=5)
        if not selector:
            continue
        fields.append(
            CssField(
                key=column.key,
                selector=selector,
                attr=chosen.attr,
                dtype=column.dtype,
                confidence=round(min(0.85, 0.45 + chosen.score / 2), 2),
                aliases=column.aliases,
            )
        )
    if not fields:
        return None
    return CssSchema(
        container="body",
        item_scope=False,
        fields=fields,
        method="css",
        notes=["detail page mode"],
    )


# ---------------------------------------------------------------------------
# field assignment
# ---------------------------------------------------------------------------


def _collect_candidates(tree, scope) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node, depth in _iter_nodes(scope):
        if node is scope:
            continue
        attrs = node.attributes or {}
        value = normalize_space(node.text(strip=True))
        has_link = node.tag == "a" and attrs.get("href")
        if not value and not has_link and node.tag != "img":
            continue
        descriptor_parts = [
            " ".join(_classes(node)),
            attrs.get("itemprop", ""),
            attrs.get("data-field", ""),
            attrs.get("data-testid", ""),
            attrs.get("name", ""),
            attrs.get("id", ""),
            attrs.get("aria-label", ""),
            attrs.get("title", ""),
        ]
        parent = node.parent
        if parent is not None:
            descriptor_parts.append(" ".join(_classes(parent)))
            sibling = node.prev
            while sibling is not None and sibling.tag == "-text":
                text = normalize_space(sibling.text())
                if text:
                    descriptor_parts.append(text[:40])
                    break
                sibling = sibling.prev
            previous = node.prev
            if previous is not None and previous.tag in ("dt", "th", "label", "strong", "b", "span", "div"):
                label = normalize_space(previous.text(strip=True))
                if 0 < len(label) <= 40:
                    descriptor_parts.append(label)
        descriptor = normalize_token(" ".join(part for part in descriptor_parts if part))
        selector = _unique_selector(node, scope, max_depth=4) or ""
        attr = None
        if node.tag == "a" and attrs.get("href"):
            attr = "href"
        elif node.tag == "img" and attrs.get("src"):
            attr = "src"
        elif node.tag == "time" and attrs.get("datetime"):
            attr = "datetime"
        hints: set[str] = set()
        if attr == "href":
            hints.add("url")
        if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", value):
            hints.add("email")
        if re.search(r"\+?38[\s(-]*0\d{2}", value):
            hints.add("phone")
        if any(symbol in value for symbol in ("₴", "$", "€", "грн")) or re.search(
            r"\d[\d\s.,]{2,}", value
        ):
            hints.add("number")
        candidates.append(
            Candidate(
                node=node,
                selector=selector,
                descriptor=descriptor,
                value=value,
                attr=attr,
                depth=depth,
                hints=hints,
            )
        )
    return candidates


def _assign_column(children: list[Any], column: FieldSpec) -> tuple[str, str | None, float] | None:
    scores: dict[str, tuple[float, str | None, str]] = {}
    for child in children:
        candidates = _collect_candidates(None, child)
        chosen = _best_candidate(candidates, column)
        if chosen is None or not chosen.selector:
            continue
        key = f"{chosen.selector}|{chosen.attr}"
        current = scores.get(key)
        if current is None or chosen.score > current[0]:
            scores[key] = (chosen.score, chosen.attr, chosen.selector)
    if not scores:
        return None
    _key, (score, attr, selector) = max(scores.items(), key=lambda item: item[1][0])
    confidence = min(0.95, 0.4 + score / 1.6)
    return selector, attr, confidence


def _best_candidate(candidates: list[Candidate], column: FieldSpec) -> Candidate | None:
    best: Candidate | None = None
    best_score = 0.0
    for candidate in candidates:
        score = _score(candidate, column)
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < 0.45:
        return None
    best_score = max(best_score, 0.45)
    best.score = min(1.0, best_score)
    return best


def _score(candidate: Candidate, column: FieldSpec) -> float:
    if not candidate.value and not candidate.attr:
        return 0.0
    tokens = field_tokens(column)
    descriptor_parts = set(candidate.descriptor.split("_"))
    hits = tokens & descriptor_parts
    score = 0.0
    if hits:
        score += min(0.9, 0.45 * len(hits))
    label_score = text_matches_field(candidate.descriptor, column)
    score += label_score * 0.4

    if column.dtype == "email" and "email" in candidate.hints:
        score += 0.55
    if column.dtype == "phone" and "phone" in candidate.hints:
        score += 0.5
    if column.dtype in ("number", "integer") and "number" in candidate.hints:
        score += 0.15
    if column.dtype == "url" and candidate.attr == "href":
        score += 0.4
    if column.dtype in ("url", "image") and candidate.attr in ("href", "src"):
        score += 0.2
    if column.dtype == "datetime" and candidate.attr == "datetime":
        score += 0.4

    if len(candidate.value) > 400:
        score -= 0.4
    if candidate.node.tag in ("body", "html", "ul", "ol", "table"):
        score -= 0.3
    if not candidate.value and column.dtype in ("string", "number"):
        score -= 0.3
    return max(0.0, score)


# ---------------------------------------------------------------------------
# selectors & evaluation
# ---------------------------------------------------------------------------


def _unique_selector(node, root, *, max_depth: int = 3) -> str | None:
    chain: list[Any] = []
    current = node
    while current is not None and len(chain) < max_depth:
        if current.tag in ("-text", "html"):
            break
        chain.append(current)
        selector = _chain_selector(chain)
        if selector and _count(root, selector) == 1:
            return selector
        current = current.parent
    if not chain:
        return None
    return _chain_selector(chain)


def _chain_selector(chain: list[Any]) -> str:
    parts: list[str] = []
    for node in reversed(chain):
        classes = _stable_classes([node])
        if classes:
            parts.append(f"{node.tag}.{classes[0]}")
        else:
            parts.append(f"{node.tag}:nth-of-type({_sibling_index(node)})")
    return " > ".join(parts)


def _sibling_index(node) -> int:
    index = 1
    sibling = node.prev
    while sibling is not None:
        if sibling.tag == node.tag:
            index += 1
        sibling = sibling.prev
    return index


def _count(root, selector: str) -> int:
    try:
        return len(root.css(selector))
    except Exception:
        return 0


def _evaluate(
    schema: CssSchema, samples: list[tuple[str, str]], spec: TaskSpec
) -> tuple[int, dict[str, float]]:
    from .css import extract_css

    counts: dict[str, int] = {field.key: 0 for field in schema.fields}
    total_items = 0
    for _url, html in samples:
        outcome = extract_css(html, schema, spec)
        for item in outcome.items:
            total_items += 1
            for key in counts:
                value = item.get(key)
                if value not in (None, "", []):
                    counts[key] += 1
    coverage = {
        key: (count / total_items if total_items else 0.0) for key, count in counts.items()
    }
    return total_items, coverage


def fill_from_text(text: str, column: FieldSpec) -> Any:
    """Deterministic last-resort extraction for contact/price columns."""
    if column.dtype == "email":
        emails = find_emails(text, limit=1)
        return emails[0] if emails else None
    if column.dtype == "phone":
        phones = find_phones(text, limit=1)
        return phones[0] if phones else None
    if column.dtype in ("number", "integer") and "price" in column.key:
        from ..utils import find_money

        money = find_money(text, limit=1)
        return money[0][0] if money else None
    return None


def schema_signature(schema: CssSchema) -> str:
    raw = "|".join(
        f"{field.key}:{field.selector}:{field.attr}" for field in sorted(schema.fields, key=lambda f: f.key)
    )
    return short_hash(f"{schema.container}|{raw}")


def coerce_any(value: Any, dtype: str) -> Any:
    return coerce_value(value, dtype)

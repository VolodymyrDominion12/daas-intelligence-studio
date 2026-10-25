"""Planner: natural-language request -> validated TaskSpec (LLM first, templates fallback)."""

from __future__ import annotations

import json
import logging

from ..config import Settings
from ..discovery import SearchProvider, dedupe_domains, query_from_prompt
from ..llm.base import LLMBudget, LLMRequest
from ..llm.prompts import PLANNER_SYSTEM, planner_user
from ..llm.router import LLMRouter
from ..models import (
    EnrichmentSpec,
    FieldSpec,
    Limits,
    SourcePolicy,
    TaskSpec,
)
from ..presets import build_spec
from ..utils import normalize_domain

logger = logging.getLogger(__name__)

MAX_COLUMNS = 18


class Planner:
    def __init__(
        self,
        settings: Settings,
        router: LLMRouter,
        *,
        enrichers: list[str],
        search: SearchProvider | None = None,
        store=None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.enrichers = enrichers
        self.search = search
        self.store = store

    async def plan(
        self,
        prompt: str,
        seeds: list[str],
        *,
        title: str | None = None,
        budget: LLMBudget | None = None,
        allow_discovery: bool = True,
        run_id: str | None = None,
    ) -> TaskSpec:
        spec = build_spec(
            prompt,
            seeds,
            max_pages=self.settings.max_pages_per_run,
            sample_size=self.settings.sample_size,
            budget_usd=self.settings.llm_budget_usd,
            max_depth=self.settings.max_depth,
        )
        llm_spec: TaskSpec | None = None
        if self.router.available:
            self.router.bind(task_id=None, run_id=run_id, budget_usd=self.settings.llm_budget_usd)
            llm_spec = await self._plan_with_llm(prompt, seeds, budget=budget)
        if llm_spec is not None:
            spec = self._merge(spec, llm_spec)

        if not spec.seeds and allow_discovery and self.search is not None:
            discovered = await self._discover(prompt)
            if discovered:
                spec.seeds = discovered
                spec.warning = None
                spec.rationale += f" Стартові URL знайдено через {self.search.name}."

        if title:
            spec.title = title[:60]
        if not spec.seeds:
            spec.warning = (
                "Немає стартових URL — додайте 1-3 посилання на каталог або сторінку компанії "
                "(або налаштуйте DAAS_SEARXNG_URL для авто-пошуку)."
            )
        spec.limits = Limits(
            max_pages=self.settings.max_pages_per_run,
            max_depth=self.settings.max_depth,
            budget_usd=self.settings.llm_budget_usd,
            sample_size=self.settings.sample_size,
            per_domain_rps=self.settings.per_domain_rps,
        )
        self._attach_source_policies(spec)
        return spec

    # -- llm --------------------------------------------------------------
    async def _plan_with_llm(
        self, prompt: str, seeds: list[str], *, budget: LLMBudget | None
    ) -> TaskSpec | None:
        request = LLMRequest(
            purpose="plan",
            system=PLANNER_SYSTEM.replace("{enrichers}", ", ".join(self.enrichers)),
            user=planner_user(prompt, seeds=seeds, enrichers=self.enrichers),
            schema={"type": "object", "properties": {"columns": {"type": "array"}}},
            max_tokens=2200,
            prefer="quality",
        )
        response = await self.router.complete_json(request, budget=budget)
        if not response.ok or not isinstance(response.data, dict):
            logger.info("planner LLM unavailable: %s", response.error)
            return None
        payload = response.data
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list):
            return None

        columns: list[FieldSpec] = []
        for raw in raw_columns[:MAX_COLUMNS]:
            if not isinstance(raw, dict):
                continue
            if raw.get("method") not in {"css", "llm", "enrichment"}:
                raw["method"] = "css"
            if raw.get("method") == "llm" and not raw.get("prompt"):
                raw["method"] = "css"
            try:
                column = FieldSpec.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - tolerate individual bad columns
                logger.debug("dropping invalid LLM column: %s", exc)
                continue
            if column.key not in {existing.key for existing in columns}:
                columns.append(column)
        if len(columns) < 3:
            return None

        enrichment: list[EnrichmentSpec] = []
        for raw in payload.get("enrichment") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("enricher") or "")
            if name not in self.enrichers:
                continue
            fields = [str(field) for field in raw.get("fields") or [] if str(field)]
            enrichment.append(EnrichmentSpec(enricher=name, fields=fields))

        return TaskSpec(
            title=str(payload.get("title") or "")[:60],
            prompt=prompt,
            entity=str(payload.get("entity") or "company")[:30],
            columns=columns,
            seeds=[],
            extraction="auto",
            enrichment=enrichment,
            rationale=str(payload.get("rationale") or "")[:500],
            assumptions=[str(item)[:200] for item in payload.get("assumptions") or []][:6],
        )

    def _merge(self, base: TaskSpec, llm: TaskSpec) -> TaskSpec:
        columns = list(llm.columns)
        keys = {column.key for column in columns}
        for column in base.columns:
            if column.key not in keys:
                columns.append(column)
        if "name" not in {column.key for column in columns}:
            columns.insert(0, base.column_map["name"])
        enrichment = list(llm.enrichment)
        known = {item.enricher for item in enrichment}
        for item in base.enrichment:
            if item.enricher not in known:
                enrichment.append(item)
        return TaskSpec(
            title=llm.title or base.title,
            prompt=base.prompt,
            entity=llm.entity or base.entity,
            columns=columns,
            seeds=base.seeds,
            extraction="auto",
            enrichment=enrichment,
            limits=base.limits,
            rationale=llm.rationale or base.rationale,
            assumptions=llm.assumptions or base.assumptions,
            warning=base.warning,
        )

    # -- discovery --------------------------------------------------------
    async def _discover(self, prompt: str) -> list[str]:
        if self.search is None:
            return []
        query = query_from_prompt(prompt)
        hits = await self.search.search(query, limit=self.settings.search_max_results)
        return [hit.url for hit in dedupe_domains(hits)][:5]

    # -- governance -------------------------------------------------------
    def _attach_source_policies(self, spec: TaskSpec) -> None:
        policies: dict[str, SourcePolicy] = {}
        for seed in spec.seeds:
            domain = normalize_domain(seed)
            if not domain:
                continue
            policy = self.store.get_source_policy(domain) if self.store else None
            if policy is None:
                policy = SourcePolicy(domain=domain, tos_risk="unknown")
                if self.store:
                    self.store.upsert_source_policy(policy)
            policies[domain] = policy
        spec.source_policy = policies


def spec_summary(spec: TaskSpec) -> dict:
    return json.loads(spec.model_dump_json())

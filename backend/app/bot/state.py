"""Atomic JSON persistence for Telegram subscriber profiles and trial/plan state."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SubscriberPlan = Literal["free", "trial", "pro", "b2b"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubscriberProfile(BaseModel):
    """Per-chat subscriber state stored independently from Studio databases."""

    chat_id: str
    plan: SubscriberPlan = "trial"
    trial_ends_at: datetime | None = None
    plan_expires_at: datetime | None = None
    active_task_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class _StorePayload(BaseModel):
    version: int = 1
    subscribers: dict[str, SubscriberProfile] = Field(default_factory=dict)


class BotSubscriberStore:
    """Thread/process-safe atomic JSON store for subscriber profiles."""

    def __init__(
        self,
        path: Path,
        *,
        trial_days: int = 7,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self.trial_days = max(0, int(trial_days))
        self._now = now_fn or _utcnow

    def _load(self) -> _StorePayload:
        if not self.path.exists():
            return _StorePayload()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _StorePayload()
        if not isinstance(raw, dict):
            return _StorePayload()
        return _StorePayload.model_validate(raw)

    def _save(self, payload: _StorePayload) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(
            json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def get(self, chat_id: str) -> SubscriberProfile | None:
        return self._load().subscribers.get(str(chat_id))

    def list_all(self) -> list[SubscriberProfile]:
        payload = self._load()
        return sorted(payload.subscribers.values(), key=lambda item: item.created_at)

    def effective_plan(
        self, profile: SubscriberProfile, *, now: datetime | None = None
    ) -> SubscriberPlan:
        current = now or self._now()
        if profile.plan == "trial":
            if profile.trial_ends_at is not None and profile.trial_ends_at <= current:
                return "free"
            return "trial"
        if profile.plan in {"pro", "b2b"}:
            if profile.plan_expires_at is not None and profile.plan_expires_at <= current:
                return "free"
            return profile.plan
        return "free"

    def can_use_instant(self, chat_id: str, *, now: datetime | None = None) -> bool:
        profile = self.get(chat_id)
        if profile is None:
            return self.trial_days > 0
        return self.effective_plan(profile, now=now) in {"trial", "pro", "b2b"}

    def ensure_profile(self, chat_id: str) -> tuple[SubscriberProfile, bool]:
        cid = str(chat_id)
        payload = self._load()
        existing = payload.subscribers.get(cid)
        if existing is not None:
            return existing, False
        now = self._now()
        if self.trial_days > 0:
            profile = SubscriberProfile(
                chat_id=cid,
                plan="trial",
                trial_ends_at=now + timedelta(days=self.trial_days),
                created_at=now,
                updated_at=now,
            )
        else:
            profile = SubscriberProfile(
                chat_id=cid,
                plan="free",
                created_at=now,
                updated_at=now,
            )
        payload.subscribers[cid] = profile
        self._save(payload)
        return profile, True

    def set_active_task(self, chat_id: str, task_id: str) -> SubscriberProfile:
        cid = str(chat_id)
        payload = self._load()
        now = self._now()
        profile = payload.subscribers.get(cid)
        if profile is None:
            profile, _ = self.ensure_profile(cid)
            payload = self._load()
        updated = profile.model_copy(update={"active_task_id": task_id, "updated_at": now})
        payload.subscribers[cid] = updated
        self._save(payload)
        return updated

    def grant_plan(
        self, chat_id: str, plan: SubscriberPlan, *, days: int = 30
    ) -> SubscriberProfile:
        cid = str(chat_id)
        payload = self._load()
        now = self._now()
        profile = payload.subscribers.get(cid)
        if profile is None:
            profile, _ = self.ensure_profile(cid)
            payload = self._load()

        duration = max(1, int(days)) if plan != "free" else 0
        if plan == "trial":
            updated = profile.model_copy(
                update={
                    "plan": "trial",
                    "trial_ends_at": now + timedelta(days=duration),
                    "plan_expires_at": None,
                    "updated_at": now,
                }
            )
        elif plan in {"pro", "b2b"}:
            updated = profile.model_copy(
                update={
                    "plan": plan,
                    "plan_expires_at": now + timedelta(days=duration),
                    "updated_at": now,
                }
            )
        else:
            updated = profile.model_copy(
                update={
                    "plan": "free",
                    "trial_ends_at": now,
                    "plan_expires_at": None,
                    "updated_at": now,
                }
            )
        payload.subscribers[cid] = updated
        self._save(payload)
        return updated

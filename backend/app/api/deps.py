"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..runtime import Studio


def get_studio(request: Request) -> Studio:
    studio = getattr(request.app.state, "studio", None)
    if studio is None:  # pragma: no cover - startup guarantees this
        raise RuntimeError("studio is not initialized")
    return studio


StudioDep = Annotated[Studio, Depends(get_studio)]

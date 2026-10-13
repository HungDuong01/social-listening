"""Platform session (login) endpoints.

No credential ever passes through this service: ``/login`` opens a real
browser window and waits for the person to sign in themselves.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import SessionOut
from app.core.enums import Platform

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=list[SessionOut])
async def list_sessions(request: Request) -> list[dict[str, object]]:
    return request.app.state.session_service.describe_all()


@router.post("/{platform}/login", response_model=SessionOut)
async def begin_login(platform: Platform, request: Request) -> dict[str, object]:
    return await request.app.state.session_service.begin_login(platform)


@router.post("/{platform}/validate", response_model=SessionOut)
async def validate_session(platform: Platform, request: Request) -> dict[str, object]:
    return await request.app.state.session_service.validate(platform)


@router.delete("/{platform}", response_model=SessionOut)
async def clear_session(platform: Platform, request: Request) -> dict[str, object]:
    return request.app.state.session_service.clear(platform)

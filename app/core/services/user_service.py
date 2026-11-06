"""User administration endpoints (admin only).

There is no create-user endpoint: accounts are provisioned just in time by the
first Entra sign-in, and entitlements come from directory groups.
"""

import asyncio

from fastapi import APIRouter, Depends

from app.config.constants import Roles, RouteTags
from app.core.dependencies.auth import require_role
from app.core.handlers import document_handler
from app.core.handlers.session_registry_handler import session_registry_handler
from app.core.handlers.user_handler import user_handler
from app.core.models.user_model import User

user_router = APIRouter(prefix="/users", tags=[RouteTags.USERS])


@user_router.get("", response_model=list[User])
async def list_users(_: dict = Depends(require_role(Roles.ADMIN))):
    return await asyncio.to_thread(user_handler.list_users)


@user_router.delete("/{user_id}/sessions")
async def delete_user_sessions(user_id: int,
                               _: dict = Depends(require_role(Roles.ADMIN))):
    """Erase every project owned by a user, including its documents and decks."""
    session_ids = await asyncio.to_thread(session_registry_handler.list_for_user, user_id)
    for session_id in session_ids:
        await asyncio.to_thread(document_handler.delete_session, session_id)
    await asyncio.to_thread(session_registry_handler.remove_all_for_user, user_id)
    return {"user_id": user_id, "sessions_deleted": len(session_ids)}

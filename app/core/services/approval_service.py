"""Deck approval endpoints. Members submit; approvers and admins decide; anyone authed can read."""

from fastapi import APIRouter, Depends

from app.config.constants import ApprovalStatus, Roles, RouteTags
from app.config.exceptions import NotFoundError
from app.core.dependencies.auth import require_auth, require_role
from app.core.handlers.approval_handler import approval_handler
from app.core.models.approval_model import (
    Approval, DecisionRequest, SubmitApprovalRequest,
)

approval_router = APIRouter(prefix="/approvals", tags=[RouteTags.APPROVALS])


@approval_router.get("", response_model=list[Approval])
async def list_approvals(status: str | None = None, session_id: str | None = None,
                         _: dict = Depends(require_auth)):
    return approval_handler.list(status=status, session_id=session_id)


@approval_router.get("/{session_id}/{deck_version}", response_model=Approval)
async def get_approval(session_id: str, deck_version: int,
                       _: dict = Depends(require_auth)):
    approval = approval_handler.get(session_id, deck_version)
    if not approval:
        raise NotFoundError("approval not found")
    return approval


@approval_router.post("/submit", response_model=Approval, status_code=201)
async def submit(body: SubmitApprovalRequest,
                 claims: dict = Depends(require_role(Roles.MEMBER))):
    return approval_handler.submit(body.session_id, body.deck_version,
                                   claims.get("username", "?"), body.note)


@approval_router.post("/{session_id}/{deck_version}/approve", response_model=Approval)
async def approve(session_id: str, deck_version: int, body: DecisionRequest,
                  claims: dict = Depends(require_role(Roles.APPROVER))):
    return _decide(session_id, deck_version, ApprovalStatus.APPROVED, claims, body)


@approval_router.post("/{session_id}/{deck_version}/reject", response_model=Approval)
async def reject(session_id: str, deck_version: int, body: DecisionRequest,
                 claims: dict = Depends(require_role(Roles.APPROVER))):
    return _decide(session_id, deck_version, ApprovalStatus.REJECTED, claims, body)


def _decide(session_id: str, deck_version: int, status: str,
            claims: dict, body: DecisionRequest) -> Approval:
    result = approval_handler.decide(session_id, deck_version, status,
                                     claims.get("username", "?"), body.note)
    if not result:
        raise NotFoundError("approval not found")
    return result

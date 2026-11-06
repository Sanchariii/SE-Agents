"""Correlation-id helpers around the logging contextvar."""

import uuid
from app.utilities.logger_util import request_id_ctx


def set_request_id(value: str | None = None) -> str:
    cid = value or str(uuid.uuid4())
    request_id_ctx.set(cid)
    return cid


def get_request_id() -> str:
    return request_id_ctx.get()


def reset_request_id(token=None) -> None:
    request_id_ctx.set("-")
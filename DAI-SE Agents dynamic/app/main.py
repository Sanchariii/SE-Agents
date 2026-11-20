"""Entrypoint: bootstrap configuration, then run uvicorn."""

import uvicorn

from app.config.constants import AppConstants, AppType
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

if __name__ == "__main__":
    get_settings()                                   # runs the env / Key Vault bootstrap
    try:
        uvicorn.run("app.core.services.server:app",
                    host=AppConstants.SERVICE_HOST,
                    port=AppConstants.SERVICE_PORT,
                    reload=False)
    except Exception as exc:                         
        log.exception("unable to launch FastAPI: %s", exc)
        raise

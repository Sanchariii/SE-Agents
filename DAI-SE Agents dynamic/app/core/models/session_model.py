"""API models for project (session) metadata."""

from pydantic import BaseModel, Field, field_validator

from app.config.constants import SessionTitleConstants


class UpdateSessionTitleRequest(BaseModel):
    """Payload for renaming a project."""

    title: str = Field(min_length=1, max_length=SessionTitleConstants.MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        """Trim surrounding whitespace and reject titles that are blank after trimming.

        Args:
            value: Raw title received from the client.

        Returns:
            The trimmed title.

        Raises:
            ValueError: When the title is empty once trimmed.
        """
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

from fastapi import Header, HTTPException

from .config import settings


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Optional static API key check for write endpoints.

    Auth is disabled unless CALLHARNESS_API_KEY is set (self-hosted default).
    """
    if not settings.api_key:
        return
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if token != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

"""Optional dashboard authentication helpers."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


security = HTTPBasic(auto_error=False)


class DashboardAuth:
    """Simple optional HTTP Basic auth for the dashboard."""

    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self.username = (username or "").strip()
        self.password = password or ""

    @property
    def enabled(self) -> bool:
        """Return whether auth is configured."""
        return bool(self.username and self.password)

    def dependency(self, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
        """Protect routes when credentials are configured."""
        if not self.enabled:
            return
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(credentials.username or "", self.username)
        password_ok = secrets.compare_digest(credentials.password or "", self.password)
        if not (user_ok and password_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid dashboard credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        request.state.dashboard_user = credentials.username

    def public_status(self) -> dict[str, Any]:
        """Return a safe status payload for the frontend."""
        return {"enabled": self.enabled, "username": self.username if self.enabled else None}

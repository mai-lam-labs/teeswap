"""Dashboard session management."""

import secrets
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "teeswap_operator"
SESSION_TTL = 86400


@dataclass(slots=True)
class OperatorSession:
    token: str
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL


class OperatorSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, OperatorSession] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = OperatorSession(token=token)
        return token

    def valid(self, token: str) -> bool:
        session = self._sessions.get(token)
        if session is None or session.is_expired:
            self._sessions.pop(token, None)
            return False
        return True

    def remove(self, token: str) -> None:
        self._sessions.pop(token, None)

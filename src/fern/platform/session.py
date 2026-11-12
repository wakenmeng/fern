"""Signed session tokens binding a visitor to one demo space and one customer.

The space id always comes from this token on the server side; requests can never name a
different space (DESIGN §2.1).
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Session:
    space_id: UUID
    customer_id: UUID

    @property
    def principal(self) -> str:
        return f"customer:{self.customer_id}"


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def issue(session: Session, secret: str) -> str:
    payload = f"{session.space_id}.{session.customer_id}".encode()
    token = payload + b"." + _sign(payload, secret).encode()
    return base64.urlsafe_b64encode(token).decode().rstrip("=")


def verify(token: str, secret: str) -> Session | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload, _, signature = raw.rpartition(b".")
        if not hmac.compare_digest(signature.decode(), _sign(payload, secret)):
            return None
        space_id, customer_id = payload.decode().split(".")
        return Session(space_id=UUID(space_id), customer_id=UUID(customer_id))
    except ValueError:
        return None

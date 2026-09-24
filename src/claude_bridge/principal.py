"""Who a browser request acts for. Threads, uploads, the current thread and settings are kept per `owner`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    owner: str = ""  # "" = the single shared namespace (single-password login, or a host without users)
    admin: bool = False
    name: str = ""


ANONYMOUS = Principal()


def as_principal(value: Any) -> Principal:
    """`browser_auth` may return a Principal; anything else (None, True, a host's own user object) means the shared namespace."""
    return value if isinstance(value, Principal) else ANONYMOUS

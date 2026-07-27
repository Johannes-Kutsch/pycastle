from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycastle.session.role import RoleSession


def prepare_fingerprint_gate(role_session: "RoleSession", fingerprint: str) -> None:
    stored = role_session.read_fingerprint()
    if stored != fingerprint:
        role_session.discard()

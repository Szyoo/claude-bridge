from __future__ import annotations


class BridgeError(Exception):
    status = 400

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status


class BadRequest(BridgeError):
    status = 400


class NotFound(BridgeError):
    status = 404


class ChatBusy(BridgeError):
    status = 409


class QuotaExceeded(BridgeError):
    status = 429

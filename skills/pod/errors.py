"""Stable, reportable policy failures."""

class PodError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail

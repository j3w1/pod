"""Refusals: a stable code, what happened, and what to change."""


class PodError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None, *, fix: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.fix = fix

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": str(self)}
        if self.fix:
            result["fix"] = self.fix
        if self.detail:
            result["detail"] = self.detail
        return result

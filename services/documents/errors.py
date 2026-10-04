"""Stable document failures surfaced through durable operations."""


class PreparationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

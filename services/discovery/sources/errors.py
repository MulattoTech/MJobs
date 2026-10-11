"""Provider-independent collection failures with explicit cooldown policy."""


class SourceError(ValueError):
    def __init__(self, code, retry_seconds=60):
        super().__init__(code)
        self.code = code
        self.retry_seconds = retry_seconds

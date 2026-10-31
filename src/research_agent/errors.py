class ResearchError(Exception):
    def __init__(self, code, message="", details=None):
        self.code = code
        self.message = message or code
        self.details = details or {}
        super().__init__(self.message)


class InvariantViolation(ResearchError):
    def __init__(self, message="Data integrity check failed"):
        super().__init__("invariant_violation", message)


class Fenced(ResearchError):
    def __init__(self):
        super().__init__("lease_lost")


class Stopped(ResearchError):
    pass


class ToolFailure(ResearchError):
    def __init__(self, code, *, retryable=False, retry_after=None, fatal=False):
        super().__init__(code)
        self.retryable = retryable
        self.retry_after = retry_after
        self.fatal = fatal

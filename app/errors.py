class InvoiceError(Exception):
    """Base class for all domain errors."""


class TransientError(InvoiceError):
    """Temporary failure (network, rate limit, 5xx). Safe to retry."""


class PermanentError(InvoiceError):
    """Failure that retrying will not fix (bad credentials, corrupt file)."""


class UnsupportedFileType(InvoiceError):
    pass


class DuplicateFileError(InvoiceError):
    def __init__(self, existing_id: int):
        super().__init__(f"This exact file was already uploaded as invoice #{existing_id}")
        self.existing_id = existing_id

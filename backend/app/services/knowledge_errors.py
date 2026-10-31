from __future__ import annotations


class CaptureRetryNotAllowedError(RuntimeError):
    pass


class UploadMaterialValidationError(ValueError):
    pass

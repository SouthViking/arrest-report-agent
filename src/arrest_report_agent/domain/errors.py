"""Application-owned exceptions."""

from __future__ import annotations


class ContextUnavailableError(RuntimeError):
    """Raised when the Discord report context cannot be reconstructed."""


class ReportOwnerSubmissionError(ContextUnavailableError):
    """Raised when an owner bootstrap does not match the report submission contract."""


class PublicationError(RuntimeError):
    """Raised when Discord publication cannot complete."""

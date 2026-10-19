"""Validation result models for generated tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValidationStatus(Enum):
    NOT_RUN = "not_run"
    VALIDATED = "validated"            # compiled and passed
    SYNTAX_ERROR = "syntax_error"
    COMPILE_ERROR = "compile_error"
    TEST_FAILED = "test_failed"
    DUPLICATE = "duplicate"
    NOT_VALIDATED = "not_validated"    # could not compile/run; reason explains why


@dataclass
class ValidationResult:
    status: ValidationStatus = ValidationStatus.NOT_RUN
    message: str = ""
    details: str = ""  # compiler / runner output

    @property
    def ok(self) -> bool:
        return self.status is ValidationStatus.VALIDATED

    def badge_text(self) -> str:
        texts = {
            ValidationStatus.NOT_RUN: "",
            ValidationStatus.VALIDATED: "Validated (compiled and passed)",
            ValidationStatus.SYNTAX_ERROR: "Syntax error",
            ValidationStatus.COMPILE_ERROR: "Compile error",
            ValidationStatus.TEST_FAILED: "Test failed",
            ValidationStatus.DUPLICATE: "Duplicate of an existing test",
            ValidationStatus.NOT_VALIDATED: "Not validated",
        }
        text = texts[self.status]
        if self.message and self.status is not ValidationStatus.VALIDATED:
            text += f": {self.message}"
        return text

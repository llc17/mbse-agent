"""S3-A structural contracts only; no physical solver is registered."""

from .schema import (
    ContractError, design_digest, library_digest, load_library, seal_design,
    validate_design, confirm_design,
)
from .compiler import ContractGenerator, compile_design

__all__ = [
    "ContractError", "ContractGenerator", "compile_design", "confirm_design",
    "design_digest", "library_digest", "load_library", "seal_design", "validate_design",
]

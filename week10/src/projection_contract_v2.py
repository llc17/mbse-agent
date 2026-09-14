"""Static V9 revision-two projection prompt.

The V1 prompt is imported verbatim as the immutable base.  This module adds
only generic output-shape guidance; it has no request input and no case data.
The frozen V8 parser and verifier remain authoritative.
"""

from __future__ import annotations

from week9.src.schemas import sha256_text
from week10.src.projection_contract import (
    PROMPT_SHA256 as BASE_PROMPT_SHA256,
)
from week10.src.projection_contract import SYSTEM_PROMPT as V1_SYSTEM_PROMPT


PROMPT_VERSION: str = "v9-certified-requirement-projection-prompt-2"

_V2_OUTPUT_CHECKLIST: str = r'''

Before returning a ready proposal, apply this prioritized checklist. It
clarifies the existing contract and never replaces any rule above.

1. Return valid strict JSON only: exactly the stated JSON object, with no
calculations, explanations, comments, markdown, or any text outside that JSON
object. Every numeric JSON value must be a finite JSON number, never an
expression such as a formula or calculation string.
2. Select exactly one topology and apply all of its existing capture,
normalization, repeat, derivation, and blocking rules above.
3. For ready, emit exactly one observation for every canonical name and no
other observation. The required observation counts are rc_filter=4,
rlc_filter=5, opamp_inverting=5, thermal_single_room=4, and
thermal_dual_room=8. Each canonical name occurs exactly once in its template's
observations array.
4. For repeated equivalent observations of the same canonical field, output
only the first observation. Do not repeat a field merely because its source
text occurs again. Conflicting repeats remain blocked under the rule above.
5. Keep the joint-initial-temperature distinction: one successful dual-room
joint initial-temperature source produces two distinct observations,
initial_temperature_a and initial_temperature_b, which share that same source
span. This is not a duplicate because the canonical names differ.

For RLC derivation, f0 means the captured cutoff_frequency normalized by V8 to
Hz, and L means the captured inductance normalized by V8 to H. Do not require,
invent, or output a separate resonance-frequency field. These symbols only
describe the existing certified derivation; any derived numeric value is a
finite JSON number, not an expression.
'''

SYSTEM_PROMPT: str = V1_SYSTEM_PROMPT + _V2_OUTPUT_CHECKLIST
PROMPT_SHA256: str = sha256_text(PROMPT_VERSION + "\n" + SYSTEM_PROMPT)


__all__ = [
    "BASE_PROMPT_SHA256",
    "PROMPT_SHA256",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "V1_SYSTEM_PROMPT",
]

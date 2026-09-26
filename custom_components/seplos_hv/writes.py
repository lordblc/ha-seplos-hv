"""Pure validation helpers for parameter writes.

Deliberately HA-free (no homeassistant import) so it is unit-testable on a machine
with no ``homeassistant`` package installed, same as protocol.py and client.py.
SeplosHvCoordinator.async_write_param (coordinator.py) calls into this module to
range-check a proposed value and to enforce the "no more than 20% in one step"
guard before ever building a frame.
"""

from __future__ import annotations

# Absolute-value ranges per parameter unit. Delay fields (trip_delay_s/recover_delay_s)
# are range-checked separately, below, regardless of the parameter's own unit.
_UNIT_RANGES: dict[str, tuple[float, float]] = {
    "mV": (2000, 4000),
    "V": (0, 4000),  # no V-unit parameter exists today; kept for completeness
    "A": (0, 300),  # checked against abs(value) - current parameters can be negative
    "C": (-40, 90),
    "%": (0, 100),
    "ohm/V": (0, float("inf")),
}

_DELAY_RANGE_S = (0, 600)

# Fields with no delay semantics at all - the 20% step guard only ever applies to these.
_STEP_GUARDED_FIELDS = ("trip", "recover")


class ParamValidationError(ValueError):
    """Raised when a proposed parameter write fails a range or step-size check."""


def check_value_range(unit: str, field: str, value: float) -> None:
    """Raise ParamValidationError if ``value`` is outside the safe range for ``unit``/``field``.

    Delay fields (trip_delay_s/recover_delay_s) are checked against 0-600 s regardless of
    the parameter's unit. Everything else is checked against the unit's own range table.
    An unrecognised unit is *not* an error here - it has no known range, so no check is
    applied (the caller/coordinator already restricts which params exist at all).
    """
    if field in ("trip_delay_s", "recover_delay_s"):
        lo, hi = _DELAY_RANGE_S
        if not lo <= value <= hi:
            raise ParamValidationError(
                f"delay {value}s is outside the allowed range {lo}-{hi}s"
            )
        return

    bounds = _UNIT_RANGES.get(unit)
    if bounds is None:
        return
    lo, hi = bounds
    check_value = abs(value) if unit == "A" else value
    if not lo <= check_value <= hi:
        raise ParamValidationError(
            f"value {value} {unit} is outside the allowed range {lo}-{hi} {unit}"
        )


def check_step_guard(field: str, old_value: float, new_value: float, *, force: bool) -> None:
    """Raise ParamValidationError if a trip/recover change exceeds 20% in one step.

    Only trip/recover are step-guarded (delay fields are not). Pass ``force=True`` to
    bypass this check entirely (still subject to check_value_range).
    """
    if force or field not in _STEP_GUARDED_FIELDS:
        return
    if old_value == 0:
        if new_value != 0:
            raise ParamValidationError(
                "cannot change a zero-valued trip/recover threshold without force=True "
                "(no baseline to compute a percentage step against)"
            )
        return
    step = abs(new_value - old_value) / abs(old_value)
    if step > 0.20:
        raise ParamValidationError(
            f"change from {old_value} to {new_value} is a {step:.0%} step, "
            "more than the 20% single-step guard allows; pass force=True to override"
        )

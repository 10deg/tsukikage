"""Small, dependency-free one-dimensional numerical helpers.

These replace the ad-hoc fixed-iteration bisection loops that were duplicated
throughout the legacy code (full-moon search, new-moon search, crescent window
refinement, transit search).  All routines work on plain callables returning
floats and therefore carry no SPICE or NumPy dependency.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Tuple

__all__ = [
    "bisect_root",
    "bracket_root",
    "golden_minimum",
    "bracket_minimum",
    "wrap_angle",
    "clamp",
]

ScalarFn = Callable[[float], float]

_SQRT5 = math.sqrt(5.0)
_INV_PHI = (_SQRT5 - 1.0) * 0.5  # 1/phi ~= 0.6180339887


def clamp(value: float, low: float, high: float) -> float:
    """Clamp *value* into the closed interval ``[low, high]``."""
    if low > high:
        raise ValueError("low must not exceed high")
    return low if value < low else (high if value > high else value)


def wrap_angle(angle: float, period: float = 2.0 * math.pi, center: float = 0.0) -> float:
    """Wrap *angle* into ``[center - period/2, center + period/2)``."""
    return (angle - center + period * 0.5) % period - period * 0.5 + center


def bisect_root(
    func: ScalarFn,
    low: float,
    high: float,
    *,
    tol: float = 1e-9,
    max_iter: int = 100,
) -> float:
    """Refine a bracketed root of *func* by bisection.

    Unlike the legacy fixed-iteration loops, this stops as soon as the bracket
    is narrower than *tol* (in the units of the independent variable, i.e.
    seconds for time searches), which makes the accuracy explicit and keeps
    worst-case iteration counts low.

    Raises
    ------
    ValueError
        If the interval does not bracket a sign change.
    """
    f_low = func(low)
    f_high = func(high)
    if f_low == 0.0:
        return low
    if f_high == 0.0:
        return high
    if f_low * f_high > 0.0:
        raise ValueError(
            f"bisect_root: interval [{low}, {high}] does not bracket a root "
            f"(f(low)={f_low!r}, f(high)={f_high!r})"
        )
    for _ in range(max_iter):
        if high - low <= tol:
            break
        mid = 0.5 * (low + high)
        f_mid = func(mid)
        if f_mid == 0.0:
            return mid
        if f_low * f_mid > 0.0:
            low, f_low = mid, f_mid
        else:
            high, f_high = mid, f_mid
    return 0.5 * (low + high)


def bracket_root(
    func: ScalarFn,
    start: float,
    step: float,
    *,
    max_steps: int = 64,
    prefer: str = "forward",
) -> Optional[Tuple[float, float]]:
    """Expand from *start* until *func* changes sign.

    Parameters
    ----------
    func
        Scalar function.
    start
        Initial abscissa.
    step
        Initial increment; doubled on every unsuccessful attempt.
    max_steps
        Maximum number of expansions.
    prefer
        ``'forward'`` searches ``start``, ``start+step``, … ;
        ``'backward'`` searches ``start``, ``start-step``, … ;
        ``'both'`` tries forward first, then backward.

    Returns
    -------
    (low, high) or None
        A bracketing interval, or ``None`` if no sign change was found.
    """

    def _scan(direction: int) -> Optional[Tuple[float, float]]:
        a = start
        f_a = func(a)
        delta = step * direction
        for _ in range(max_steps):
            b = a + delta
            f_b = func(b)
            if f_a == 0.0:
                return (a, b)
            if f_a * f_b < 0.0:
                return (a, b) if a < b else (b, a)
            a, f_a = b, f_b
            delta *= 1.6
        return None

    if prefer == "forward":
        return _scan(+1)
    if prefer == "backward":
        return _scan(-1)
    if prefer == "both":
        return _scan(+1) or _scan(-1)
    raise ValueError("prefer must be 'forward', 'backward' or 'both'")


def golden_minimum(
    func: ScalarFn,
    low: float,
    high: float,
    *,
    tol: float = 1e-6,
    max_iter: int = 200,
) -> Tuple[float, float]:
    """Golden-section minimisation on ``[low, high]``.

    Returns
    -------
    (x, f_x)
        Abscissa of the minimum and the function value there.

    The bracket must contain the minimum.  Convergence is declared when the
    bracket width falls below *tol*.
    """
    a, b = low, high
    c = b - _INV_PHI * (b - a)
    d = a + _INV_PHI * (b - a)
    f_c = func(c)
    f_d = func(d)
    for _ in range(max_iter):
        if abs(b - a) <= tol:
            break
        if f_c < f_d:
            b, d, f_d = d, c, f_c
            c = b - _INV_PHI * (b - a)
            f_c = func(c)
        else:
            a, c, f_c = c, d, f_d
            d = a + _INV_PHI * (b - a)
            f_d = func(d)
    x = 0.5 * (a + b)
    return x, func(x)


def bracket_minimum(
    func: ScalarFn,
    start: float,
    step: float,
    *,
    max_steps: int = 64,
) -> Optional[Tuple[float, float, float]]:
    """Find a triple ``(a, b, c)`` with ``f(b) < f(a)`` and ``f(b) < f(c)``.

    Uses the downhill-simplex style expansion of Brent's method.  Returns
    ``None`` when no local minimum is found within *max_steps* expansions.
    """
    a = start
    b = start + step
    f_a = func(a)
    f_b = func(b)
    if f_b > f_a:
        a, b = b, a
        f_a, f_b = f_b, f_a
        step = -step
    for _ in range(max_steps):
        c = b + step
        f_c = func(c)
        if f_b <= f_c:
            return (a, b, c) if a < c else (c, b, a)
        a, f_a = b, f_b
        b, f_b = c, f_c
        step *= 1.6
    return None

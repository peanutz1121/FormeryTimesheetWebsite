"""
Pure helper functions for working with shift times and hours.

These functions have no database access, so they are easy to unit test and can
be imported from models, forms and services without circular imports.

Calculation rules (also documented in README.md):

* Shift length is the clock difference between time out and time in, to the
  minute. If time out is earlier than time in, the shift is treated as an
  overnight shift that ends on the following day.
* Time in and time out being identical is rejected as ambiguous (0 hours or
  24 hours?).
* Unpaid break minutes are subtracted from the shift length. A break that
  consumes the whole shift is invalid.
* When hours are *calculated*, up to STANDARD_DAILY_HOURS count as regular
  hours and anything above that is overtime.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
MINUTES_PER_DAY = 24 * 60


def shift_minutes(time_in, time_out):
    """Minutes from time_in to time_out, wrapping past midnight if needed."""
    start = time_in.hour * 60 + time_in.minute
    end = time_out.hour * 60 + time_out.minute
    return (end - start) % MINUTES_PER_DAY


def crosses_midnight(time_in, time_out):
    return (time_out.hour * 60 + time_out.minute) < (time_in.hour * 60 + time_in.minute)


def minutes_to_hours(minutes):
    return (Decimal(minutes) / Decimal(60)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def calculate_total_hours(time_in, time_out, break_minutes=0):
    """
    Net hours worked (shift length minus unpaid break), or None when the
    result is not a positive duration.
    """
    net = shift_minutes(time_in, time_out) - int(break_minutes or 0)
    if net <= 0:
        return None
    return minutes_to_hours(net)


def split_regular_overtime(total_hours):
    """Split a total into (regular, overtime) using STANDARD_DAILY_HOURS."""
    standard = Decimal(str(settings.STANDARD_DAILY_HOURS))
    total = Decimal(total_hours)
    regular = min(total, standard)
    overtime = max(total - standard, Decimal("0"))
    return regular.quantize(TWO_PLACES), overtime.quantize(TWO_PLACES)

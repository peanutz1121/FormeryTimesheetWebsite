"""Time in Lieu (TIL) balance helpers."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, Value, When
from django.utils import timezone

from .models import AuditLog, TimeInLieuEntry, TimeInLieuRequest
from .services import log_audit

ZERO = Decimal("0.00")
_DEC = DecimalField(max_digits=10, decimal_places=2)
Kind = TimeInLieuEntry.Kind


def _sum_for(*kinds):
    return Sum(Case(When(til_entries__kind__in=kinds, then=F("til_entries__hours")),
                    default=Value(ZERO), output_field=_DEC), output_field=_DEC)


def balance_for(user):
    """Return (earned, used, balance) for one user."""
    rows = TimeInLieuEntry.objects.filter(employee=user).values("kind").annotate(h=Sum("hours"))
    totals = {r["kind"]: r["h"] or ZERO for r in rows}
    earned = totals.get(Kind.EARNED, ZERO)
    used = totals.get(Kind.TAKEN, ZERO) + totals.get(Kind.DEDUCTED, ZERO)
    return earned, used, earned - used


def balances_queryset():
    """All users annotated with til_earned / til_used (one query)."""
    User = get_user_model()
    return User.objects.annotate(
        til_earned=_sum_for(Kind.EARNED),
        til_used=_sum_for(Kind.TAKEN, Kind.DEDUCTED),
    )


# --------------------------------------------------------------------------
# Approval workflow for requests to bank earned hours
# --------------------------------------------------------------------------
Status = TimeInLieuRequest.Status


class TilRequestError(Exception):
    """A request cannot be moved to the requested state (message is user-safe)."""


def pending_requests_count():
    return TimeInLieuRequest.objects.filter(status=Status.PENDING).count()


def pending_taken_hours(employee):
    """Hours of time off already requested and awaiting a decision."""
    total = TimeInLieuRequest.objects.filter(
        employee=employee, status=Status.PENDING, kind=TimeInLieuRequest.Kind.TAKEN,
    ).aggregate(h=Sum("hours"))["h"]
    return total or ZERO


def submit_request(employee, kind, work_date, hours, reason, request=None):
    req = TimeInLieuRequest.objects.create(
        employee=employee, kind=kind, work_date=work_date, hours=hours, reason=reason,
    )
    log_audit(employee, AuditLog.Action.TIL_REQUESTED, req,
              details=f"{employee.get_username()}: {req.get_kind_display()} {req.hours} h for {req.work_date}",
              request=request)
    return req


def _lock_pending(pk):
    req = TimeInLieuRequest.objects.select_for_update().select_related("employee").get(pk=pk)
    if req.status != Status.PENDING:
        raise TilRequestError(f"This request has already been {req.get_status_display().lower()}.")
    return req


@transaction.atomic
def approve_request(pk, reviewer, comment="", request=None):
    """Approve a pending request and bank the hours. An admin cannot approve their own."""
    req = _lock_pending(pk)
    if req.employee_id == reviewer.pk:
        raise TilRequestError("You cannot approve your own request. Another administrator must do it.")
    if req.kind == TimeInLieuRequest.Kind.TAKEN:
        available = balance_for(req.employee)[2]
        if req.hours > available:
            raise TilRequestError(
                f"Only {available:.2f} banked hours are available, so this time off cannot be approved. "
                "Reject it or correct the balance first.")
    entry = TimeInLieuEntry.objects.create(
        employee=req.employee, entry_date=req.work_date, kind=req.kind,
        hours=req.hours, note=req.reason, recorded_by=reviewer,
    )
    req.status, req.entry = Status.APPROVED, entry
    req.reviewed_by, req.reviewed_at, req.review_comment = reviewer, timezone.now(), comment
    req.save(update_fields=["status", "entry", "reviewed_by", "reviewed_at", "review_comment"])
    log_audit(reviewer, AuditLog.Action.TIL_REQUEST_APPROVED, req,
              details=f"{req.employee.get_username()}: {req.get_kind_display()} {req.hours} h for {req.work_date}", request=request)
    return req


@transaction.atomic
def reject_request(pk, reviewer, comment="", request=None):
    req = _lock_pending(pk)
    if req.employee_id == reviewer.pk:
        raise TilRequestError("You cannot review your own request. Another administrator must do it.")
    req.status = Status.REJECTED
    req.reviewed_by, req.reviewed_at, req.review_comment = reviewer, timezone.now(), comment
    req.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment"])
    log_audit(reviewer, AuditLog.Action.TIL_REQUEST_REJECTED, req,
              details=f"{req.employee.get_username()}: {req.get_kind_display()} {req.hours} h for {req.work_date}. {comment}".strip(),
              request=request)
    return req


@transaction.atomic
def cancel_request(pk, user, request=None):
    """The requester withdraws their own pending request."""
    req = _lock_pending(pk)
    if req.employee_id != user.pk:
        raise TilRequestError("You can only cancel your own requests.")
    req.status = Status.CANCELLED
    req.save(update_fields=["status"])
    log_audit(user, AuditLog.Action.TIL_REQUEST_CANCELLED, req,
              details=f"{user.get_username()}: {req.get_kind_display()} {req.hours} h for {req.work_date}", request=request)
    return req

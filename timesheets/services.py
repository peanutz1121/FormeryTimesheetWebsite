"""
Business logic that changes data: audit logging and the approval workflow.

Workflow (status transitions)
-----------------------------
    submit  : draft / rejected  -> submitted   (the entry's owner, or an admin)
    approve : submitted         -> approved    (admin only)
    reject  : submitted / approved -> rejected (admin only; rejecting an
              approved entry is how an administrator reverses it)

Editing an entry through the administrator form may also set any status; every
status change - however it happens - writes an ApprovalHistory row and an
AuditLog row.
"""
from django.db import transaction
from django.utils import timezone

from .models import ApprovalHistory, AuditLog, TimesheetEntry

Status = TimesheetEntry.Status

TRANSITIONS = {
    "submit": {"from": {Status.DRAFT, Status.REJECTED}, "to": Status.SUBMITTED},
    "approve": {"from": {Status.SUBMITTED}, "to": Status.APPROVED},
    "reject": {"from": {Status.SUBMITTED, Status.APPROVED}, "to": Status.REJECTED},
}

_AUDIT_ACTION_FOR_STATUS = {
    Status.SUBMITTED: AuditLog.Action.ENTRY_SUBMITTED,
    Status.APPROVED: AuditLog.Action.ENTRY_APPROVED,
    Status.REJECTED: AuditLog.Action.ENTRY_REJECTED,
}


class TransitionError(Exception):
    """Raised when a workflow action is not allowed from the current status."""


def client_ip(request):
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def log_audit(actor, action, obj=None, details="", request=None, username=""):
    """Write one AuditLog row. ``obj`` may be any model instance (or None)."""
    actor_obj = actor if (actor is not None and getattr(actor, "pk", None)) else None
    return AuditLog.objects.create(
        actor=actor_obj,
        actor_username=(actor_obj.get_username() if actor_obj else username)[:150],
        action=action,
        model_name=obj.__class__.__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        object_repr=str(obj)[:255] if obj is not None else "",
        details=details,
        ip_address=client_ip(request),
    )


@transaction.atomic
def change_status(entry, new_status, actor, comment="", request=None):
    """
    Move ``entry`` to ``new_status``, recording history and an audit row.
    This function does NOT check permissions or allowed transitions - callers
    use ``perform_transition`` for that. Returns True if anything changed.
    """
    old_status = entry.status
    if old_status == new_status:
        return False

    now = timezone.now()
    entry.status = new_status
    if new_status == Status.SUBMITTED:
        entry.submitted_at = now
        entry.reviewed_by = None
        entry.reviewed_at = None
        entry.rejection_reason = ""
    elif new_status in (Status.APPROVED, Status.REJECTED):
        entry.reviewed_by = actor
        entry.reviewed_at = now
        entry.rejection_reason = comment[:500] if new_status == Status.REJECTED else ""
    entry.save(update_fields=[
        "status", "submitted_at", "reviewed_by", "reviewed_at",
        "rejection_reason", "updated_at",
    ])

    ApprovalHistory.objects.create(
        timesheet=entry,
        reviewer=actor,
        previous_status=old_status,
        new_status=new_status,
        comments=comment[:500],
    )
    log_audit(
        actor,
        _AUDIT_ACTION_FOR_STATUS.get(new_status, AuditLog.Action.ENTRY_STATUS_CHANGED),
        entry,
        details=f"{old_status} -> {new_status}" + (f". {comment}" if comment else ""),
        request=request,
    )
    return True


def perform_transition(entry, action, actor, comment="", request=None):
    """Validate a named workflow action against TRANSITIONS, then apply it."""
    rule = TRANSITIONS[action]
    if entry.status not in rule["from"]:
        allowed = ", ".join(sorted(s.label for s in rule["from"]))
        raise TransitionError(
            f"Cannot {action} a timesheet that is {entry.get_status_display().lower()}. "
            f"Only {allowed.lower()} timesheets can be {rule['to'].label.lower()}."
        )
    return change_status(entry, rule["to"], actor, comment=comment, request=request)

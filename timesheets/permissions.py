"""
Authorization helpers.

Roles are derived from Django's built-in user flags:

* Administrator: an active user with ``is_staff`` or ``is_superuser``.
* Employee: any other authenticated, active user.

Every protected view enforces these rules on the server. Hiding a link in a
template is only a convenience, never the security control.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def is_admin(user):
    return bool(
        user is not None
        and user.is_authenticated
        and user.is_active
        and (user.is_staff or user.is_superuser)
    )


def admin_required(view_func):
    """Require login first, then administrator rights (403 otherwise)."""

    @wraps(view_func)
    def checked(request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied("Administrator access is required.")
        return view_func(request, *args, **kwargs)

    return login_required(checked)


# Status values are duplicated as plain strings here to keep this module free
# of model imports (models import nothing from here, but views import both).
DRAFT, SUBMITTED, APPROVED, REJECTED = "draft", "submitted", "approved", "rejected"


def can_edit_entry(user, entry):
    """Admins may correct any entry. Employees: own draft/rejected entries only."""
    if is_admin(user):
        return True
    return entry.employee_id == user.id and entry.status in (DRAFT, REJECTED)


def can_delete_entry(user, entry):
    """Admins may delete any entry. Employees: only their own drafts."""
    if is_admin(user):
        return True
    return entry.employee_id == user.id and entry.status == DRAFT


def can_submit_entry(user, entry):
    if entry.status not in (DRAFT, REJECTED):
        return False
    return is_admin(user) or entry.employee_id == user.id

from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditLog, EmployeeProfile
from .services import log_audit

User = get_user_model()


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    """Every account (including superusers made with createsuperuser) gets a profile."""
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    log_audit(user, AuditLog.Action.LOGIN, user, request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        log_audit(user, AuditLog.Action.LOGOUT, user, request=request)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    # Only the username is recorded - never the password.
    username = str(credentials.get("username", ""))[:150]
    log_audit(
        None, AuditLog.Action.LOGIN_FAILED, details=f"Failed sign-in for username '{username}'",
        request=request, username=username,
    )

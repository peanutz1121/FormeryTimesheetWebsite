"""
Project / section access control (enforced on the server for every request).

* Administrators (``is_staff`` / ``is_superuser``) can use every section and project.
* An employee can use a project only when an administrator has assigned it to them
  AND, if the project sits in a restricted section (Admin, Marketing & Social,
  Networking), the administrator has also granted them that section.
* Permissions are read from the database on every request, so a change takes
  effect as soon as it is saved.
"""
from django.db.models import Q

from .models import Project, SectionAccess
from .permissions import is_admin

RESTRICTED = [s.value for s in Project.RESTRICTED_SECTIONS]


def allowed_sections(user):
    """Restricted section values this user may open."""
    if is_admin(user):
        return list(RESTRICTED)
    if not (user and user.is_authenticated and user.is_active):
        return []
    return list(SectionAccess.objects.filter(user=user, section__in=RESTRICTED)
                .values_list("section", flat=True))


def accessible_projects(user):
    """Projects this user may see and log time against."""
    if is_admin(user):
        return Project.objects.all()
    if not (user and user.is_authenticated and user.is_active):
        return Project.objects.none()
    return Project.objects.filter(access_grants__user=user).filter(
        ~Q(section__in=RESTRICTED) | Q(section__in=allowed_sections(user))
    ).distinct()


def can_access_project(user, project):
    return accessible_projects(user).filter(pk=getattr(project, "pk", project)).exists()

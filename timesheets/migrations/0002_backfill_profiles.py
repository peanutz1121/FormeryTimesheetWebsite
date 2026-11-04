from django.conf import settings
from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    """Give every pre-existing account an EmployeeProfile with a unique employee ID."""
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    Profile = apps.get_model("timesheets", "EmployeeProfile")
    existing = set(Profile.objects.values_list("employee_id", flat=True))
    number = Profile.objects.count() + 1
    for user in User.objects.filter(profile__isnull=True).order_by("pk"):
        while f"EMP-{number:04d}" in existing:
            number += 1
        code = f"EMP-{number:04d}"
        existing.add(code)
        Profile.objects.create(user=user, employee_id=code)


class Migration(migrations.Migration):
    dependencies = [("timesheets", "0001_initial")]
    operations = [migrations.RunPython(create_missing_profiles, migrations.RunPython.noop)]

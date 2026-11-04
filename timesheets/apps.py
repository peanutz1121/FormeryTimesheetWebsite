from django.apps import AppConfig


class TimesheetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "timesheets"
    verbose_name = "Formery Timesheet Database"

    def ready(self):
        from . import signals  # noqa: F401  (registers signal handlers)

from decimal import Decimal

from django import template
from django.utils.html import format_html

from timesheets.models import TimesheetEntry

register = template.Library()

_STATUS_LABELS = {s.value: s.label for s in TimesheetEntry.Status}


@register.filter
def hrs(value):
    """Format hours with two decimals (None -> 0.00)."""
    if value is None or value == "":
        return "0.00"
    try:
        return f"{Decimal(value):,.2f}"
    except Exception:
        return str(value)


@register.filter
def person(user):
    """Full name, falling back to username."""
    if user is None:
        return "-"
    return user.get_full_name() or user.get_username()


@register.simple_tag
def status_badge(status):
    label = _STATUS_LABELS.get(status, str(status).title())
    return format_html('<span class="badge badge-{}">{}</span>', status, label)


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    """Current query string with the given parameters replaced/removed (None removes)."""
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@register.simple_tag(takes_context=True)
def sort_qs(context, key):
    """Query string for a column header link that toggles ascending/descending."""
    params = context["request"].GET.copy()
    current = params.get("sort") or "-work_date"
    params["sort"] = f"-{key}" if current == key else key
    params.pop("page", None)
    return params.urlencode()


@register.simple_tag(takes_context=True)
def sort_arrow(context, key):
    current = context["request"].GET.get("sort") or "-work_date"
    if current == key:
        return format_html('<span class="sort-arrow" aria-label="sorted ascending">&#9650;</span>')
    if current == f"-{key}":
        return format_html('<span class="sort-arrow" aria-label="sorted descending">&#9660;</span>')
    return ""

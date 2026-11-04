from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("timesheets.urls")),
]

# Django automatically renders templates/403.html, 404.html and 500.html for
# the matching HTTP errors when DEBUG is False.

from django.contrib import admin
from django.urls import include, path


admin.site.site_header = "C10 Magic"
admin.site.site_title = "C10 Magic"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("cards.urls")),
]

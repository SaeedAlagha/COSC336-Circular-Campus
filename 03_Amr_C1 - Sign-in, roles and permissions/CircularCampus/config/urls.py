from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
]

handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"

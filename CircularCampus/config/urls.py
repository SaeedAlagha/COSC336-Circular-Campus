from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("catalog/", include("apps.catalog.urls")),
    path("requests/", include("apps.exchange.urls")),
    path("", include("apps.workflow.urls")),
    path("maintenance/", include("apps.maintenance.urls")),
    path("assistant/", include("apps.intelligence.urls")),
    path("reports/", include("apps.reporting.urls")),
    path("i18n/", include("django.conf.urls.i18n")),  # bonus: language switch (POST /i18n/setlang/)
]

handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"

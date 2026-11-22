from django.urls import path

from . import views

app_name = "catalog"
urlpatterns = [
    path("", views.CatalogView.as_view(), name="list"),
    path("my-assets/", views.CatalogView.as_view(mine=True, nav="assets"), name="mine"),
    path("new/", views.AssetFormView.as_view(), name="new"),
    path("<uuid:pk>/", views.AssetDetailView.as_view(), name="asset"),
    path("<uuid:pk>/edit/", views.AssetFormView.as_view(), name="edit"),
    path("<uuid:pk>/action/", views.AssetActionView.as_view(), name="action"),
    path("<uuid:pk>/attachments/<int:number>/", views.AttachmentDownloadView.as_view(), name="attachment"),
]

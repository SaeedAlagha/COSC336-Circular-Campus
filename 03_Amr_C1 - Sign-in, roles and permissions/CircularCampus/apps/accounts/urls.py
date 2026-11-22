from django.urls import path

from . import views

app_name = "accounts"
urlpatterns = [
    path("sign-in/", views.SignInView.as_view(), name="login"),
    path("sign-out/", views.SignOutView.as_view(), name="logout"),
    path("admin/accounts/", views.UserListView.as_view(), name="users"),
    path("admin/accounts/<uuid:pk>/", views.UserDetailView.as_view(), name="user"),
    path("admin/departments/", views.DepartmentListView.as_view(), name="departments"),
]

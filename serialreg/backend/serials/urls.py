from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BindingViewSet, IssueNumberViewSet, IssueViewSet, ItemViewSet,
    TimelineViewSet, TitleViewSet,
)

router = DefaultRouter()
router.register("titles", TitleViewSet)
router.register("numbers", IssueNumberViewSet)
router.register("issues", IssueViewSet)
router.register("items", ItemViewSet, basename="item")
router.register("bindings", BindingViewSet)
router.register("timeline", TimelineViewSet, basename="timeline")

urlpatterns = [
    path("", include(router.urls)),
]

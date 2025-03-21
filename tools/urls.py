from django.urls import path
from . import views

urlpatterns = [
    path("", views.ToolListView.as_view(), name="tool_list"),
    path("submit/", views.submit_tool, name="submit_tool"),
    path("import/", views.import_tool, name="import_tool"),
    path("tool/<int:tool_id>/logo/", views.serve_tool_logo, name="serve_tool_logo"),
    path("tool/<int:tool_id>/edit/", views.edit_tool, name="edit_tool"),
    path("tool/<int:tool_id>/tag-modal/", views.tag_modal, name="tag_modal"),
    path("tool/<int:tool_id>/add-tag/<int:tag_id>/", views.add_tag, name="add_tag"),
    path("tool/<int:tool_id>/remove-tag/<int:tag_id>/", views.remove_tag, name="remove_tag"),
    path("tool/<int:tool_id>/card/", views.tool_card, name="tool_card"),
    path("tool/<int:tool_id>/description/", views.tool_description, name="tool_description"),
    path("tool/<int:tool_id>/refresh/", views.refresh_tool, name="refresh_tool"),
]

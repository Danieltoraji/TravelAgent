from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("status/", views.status, name="status"),
    path("agent/", views.agent_info, name="agent_info"),
    path("profile/", views.profile, name="profile"),

    path("tools/", views.list_tools, name="list_tools"),
    path("tools/invoke/", views.invoke_tool_llm, name="invoke_tool_llm"),
    path("tools/<str:name>/", views.get_tool_spec, name="get_tool_spec"),
    path("tools/<str:name>/invoke/", views.invoke_tool, name="invoke_tool"),

    path("hotels/", views.hotels, name="hotels"),
    path("hotels/<str:hotel_id>/", views.hotel_detail, name="hotel_detail"),
    path("hotel-tags/", views.hotel_tags, name="hotel_tags"),

    path("timeline/history/", views.timeline_history, name="timeline_history"),
    path("timeline/", views.timeline, name="timeline"),

    path("plan/", views.plan, name="plan"),

    path("booking/prepare/", views.booking_prepare, name="booking_prepare"),
    path("booking/<str:booking_id>/confirm/", views.booking_confirm, name="booking_confirm"),
    path("booking/<str:booking_id>/mark-confirmed/", views.booking_mark_confirmed, name="booking_mark_confirmed"),
    path("booking/<str:booking_id>/cancel/", views.booking_cancel, name="booking_cancel"),
    path("booking/<str:booking_id>/payment/", views.booking_payment, name="booking_payment"),
    path("booking/", views.list_bookings, name="list_bookings"),
    path("booking/<str:booking_id>/", views.get_booking, name="get_booking"),

    path("actions/", views.list_actions, name="list_actions"),
    path("actions/<str:action_id>/approve/", views.approve_action, name="approve_action"),
    path("actions/<str:action_id>/reject/", views.reject_action, name="reject_action"),

    path("events/", views.list_events, name="list_events"),
    path("replans/", views.list_replans, name="list_replans"),
    path("replans/<int:index>/", views.get_replan, name="get_replan"),
    path("tool-calls/", views.tool_calls, name="tool_calls"),

    path("execution/poll/", views.execution_poll, name="execution_poll"),
    path("execution/lookahead/", views.execution_lookahead, name="execution_lookahead"),

    # 演示专用：突发事件注入（真链路 → 决策 → 重规划），见 docs/demo_event_injection.md
    path("debug/inject/", views.debug_inject, name="debug_inject"),

    path("export/ics/", views.export_ics, name="export_ics"),
    path("export/markdown/", views.export_markdown, name="export_markdown"),

    path("config/", views.config_info, name="config_info"),
    path("config/reload/", views.config_reload, name="config_reload"),

    # C 端对话（旅行助手），见 docs/chat_api.md
    path("chat/", views.chat, name="chat"),
]

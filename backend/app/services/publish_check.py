"""发布资格检查的兼容命名模块；实现全部收拢在 app.services.publish_gate。"""
from __future__ import annotations

from app.services.publish_gate import *  # noqa: F401,F403
from app.services.publish_gate import (  # noqa: F401
    PublishChecker,
    PublishGate,
    PublishQualification,
    accepted_verification,
    attach_verification,
    build_schedule_publish_state,
    check_publish_gate,
    check_publish_qualification,
    check_schedule_publish,
    check_work_publish,
    collect_publish_checks,
    duplicate_message,
    evaluate_publish_gate,
    find_verification,
    is_publish_accepted,
    record_verification,
    start_execution_guard,
    sync_schedule_todos,
    verify_publish,
)


def check_publish(module: str, entry: dict):  # pragma: no cover - 薄别名
    return check_publish_qualification(module, entry)

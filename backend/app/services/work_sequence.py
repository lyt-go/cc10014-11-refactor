"""工序编排领域服务：编排修缮工序顺序，发布工序时检查前置工序完成状态、安全交底和工匠到岗。"""
from __future__ import annotations

from typing import Any

from app.services import publish_gate as gate
from app.services.base import BaseModuleService
from app.store import store


class WorkSequenceService(BaseModuleService):
    MODULE = "work_sequence"
    ENTITY = "工序卡"
    OPERATION_TERM = "发布工序"
    LIST_FIELDS = ["工序名称", "前置工序", "所需工匠", "施工区域", "预计工时", "编排人", "工序状态", "备注说明"]
    REQUIRED_FIELDS = ["工序名称", "前置工序", "所需工匠"]
    STATUS_ORDER = ["待排期", "已发布", "施工中", "已完成"]
    ACTION_RULES = {"发布工序": "已发布", "开始施工": "施工中", "完成工序": "已完成"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        # 已受理的发布重复提交（旧页面重发/响应丢失重试）：直接返回当次条目与
        # 原核验记录，不再走一遍状态流转，也不重复记核验账。
        if action == gate.WORK_PUBLISH_ACTION:
            entry = store.find(self.MODULE, entry_id)
            if entry is not None:
                record = gate.accepted_verification(self.MODULE, entry, action)
                if record is not None:
                    gate.sync_schedule_todos(str(entry.get("工序名称", "") or ""))
                    return entry, gate.duplicate_message(
                        self.ENTITY, action, record, str(entry.get("status", "") or ""))
            updated, message = super().run_action(entry_id, action)
            if updated is not None and "核验记录编号" not in message:
                rec = gate.find_verification(self.MODULE, entry_id, action)
                message += gate.success_suffix(rec)
            return updated, message
        return super().run_action(entry_id, action)

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "发布工序":
            result = gate.check_work_publish(entry)
            # 把本次检查结果（含缺项编号）留在条目上，并同步到排期发布待办。
            entry["发布资格检查"] = result
            entry["最近检查"] = result
            entry["发布缺项"] = result["missing"]
            gate.sync_schedule_todos(str(entry.get("工序名称", "") or ""))
            if not result["ready"]:
                return False, result["message"]
            return True, ""
        if action == "开始施工":
            process_name = entry.get("工序名称", "")
            # 检查工匠是否已到岗
            craftsmen = store.query_by_field("craftsman_assign", "分配工序", process_name)
            if craftsmen and any(c.get("status") != "已到岗" for c in craftsmen):
                return False, "该工序的工匠尚未全部到岗"
            # 检查关联排期是否已发布
            schedules = store.query_by_field("construction_schedule", "计划工序", process_name)
            if schedules:
                unreleased = [s for s in schedules if s.get("status") == "编制中"]
                if unreleased:
                    return False, "关联施工排期还未发布"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "发布工序":
            process_name = str(entry.get("工序名称", "") or "")
            result = gate.check_work_publish(entry)
            record, _created = gate.record_verification(
                module=self.MODULE,
                entry_id=entry_id,
                action=action,
                process_name=process_name,
                gate_result=result,
            )
            gate.attach_verification(entry, record)
            # 同步发布待办：本工序已受理后，对应排期待办移除工序缺项。
            for sched in gate.sync_schedule_todos(process_name):
                effects.append(dict(sched))
        if action == "完成工序":
            process_name = entry.get("工序名称", "")
            # 工序完成后联动更新关联施工排期
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process_name:
                    if sched.get("status") == "执行中":
                        sched["里程碑节点"] = f"{process_name}已完成"
                        effects.append(dict(sched))
            # 联动更新修缮日志中关联日志
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process_name:
                    if log.get("status") in ("已审核", "已填写"):
                        log["status"] = "已归档"
                        log["pending"] = False
                        effects.append(dict(log))
            # 前置工序完成回执到达：刷新依赖它的工序对应排期发布待办。
            gate.sync_dependent_work_todos(str(process_name or ""))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 返回工序依赖图
        all_rows = store.rows(self.MODULE)
        base["dependency_graph"] = [
            {"工序": r.get("工序名称"), "前置": r.get("前置工序"), "状态": r.get("status")}
            for r in all_rows
        ]
        base["completed"] = [r.get("工序名称") for r in all_rows if r.get("status") == "已完成"]
        return base

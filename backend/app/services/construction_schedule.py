"""施工排期领域服务：排期制定与变更签证、工序编排联动，变更签认后自动调整。"""
from __future__ import annotations

from typing import Any

from app.services import publish_gate as gate
from app.services.base import BaseModuleService
from app.store import store


class ConstructionScheduleService(BaseModuleService):
    MODULE = "construction_schedule"
    ENTITY = "排期计划"
    OPERATION_TERM = "发布排期"
    LIST_FIELDS = ["计划工序", "开始日期", "结束日期", "资源配置", "里程碑节点", "编制人", "计划版本", "备注说明"]
    REQUIRED_FIELDS = ["计划工序", "开始日期", "结束日期"]
    STATUS_ORDER = ["编制中", "已发布", "执行中", "已延期"]
    ACTION_RULES = {"发布排期": "已发布", "启动执行": "执行中", "申请延期": "已延期"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]):
        entry, missing = super().create_entry(values)
        if entry is not None and not missing:
            # 建卡即生成第一版「发布待办」，不必等第一次点发布。
            gate.sync_schedule_todos(str(entry.get("计划工序", "") or ""))
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        # 已受理的排期发布重复提交：返回当次排期和原核验记录，不重复记账。
        if action == gate.SCHEDULE_PUBLISH_ACTION and entry is not None:
            record = gate.accepted_verification(self.MODULE, entry, action)
            if record is not None:
                process = str(entry.get("计划工序", "") or "")
                gate.sync_schedule_todos(process)
                return entry, gate.duplicate_message(
                    self.ENTITY, action, record, str(entry.get("status", "") or ""))
            updated, message = super().run_action(entry_id, action)
            if updated is not None and "核验记录编号" not in message:
                rec = gate.find_verification(self.MODULE, entry_id, action)
                message += gate.success_suffix(rec)
            return updated, message
        # 现场没收到启动回执时重复点「启动执行」：若已进入执行中，凭这份排期继续。
        if action == gate.SCHEDULE_START_ACTION and entry is not None \
                and entry.get("status") == "执行中":
            return entry, (
                "排期已启动执行，当次启动已受理；无需重填开工单，"
                f"直接凭本排期#{entry_id}继续现场执行"
            )
        return super().run_action(entry_id, action)

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process = entry.get("计划工序", "")
        if action == "发布排期":
            result = gate.check_schedule_publish(entry)
            entry["发布资格检查"] = result
            entry["最近检查"] = result
            entry["发布缺项"] = result["missing"]
            gate.sync_schedule_todos(str(process or ""))
            if not result["ready"]:
                return False, result["message"]
            return True, ""
        if action == "启动执行":
            # 检查是否有关联变更签证未处理
            cs = store.query_by_field("change_order", "影响工序", process)
            pending_changes = [c for c in cs if c.get("status") == "已提出"]
            if pending_changes:
                return False, f"工序「{process}」有{len(pending_changes)}条待签认变更"
            # 两份发布（工序发布、排期发布）都必须已受理；未受理时给出带编号的
            # 具体缺项和原入口，方便班组在启动入口直接交接。
            passed, reason, summary = gate.start_execution_guard(entry)
            entry["发布资格"] = summary
            if not passed:
                return False, reason
        if action == "申请延期":
            # 检查是否有变更签证支撑延期
            cs = store.query_by_field("change_order", "影响工序", process)
            if not any(c.get("status") == "已签认" for c in cs):
                return False, "延期需要已签认的变更签证支撑"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "发布排期":
            process = str(entry.get("计划工序", "") or "")
            # 留存排期发布自己的核验记录（不借用工序发布的成功结果）。
            result = gate.check_schedule_publish(entry)
            record, _created = gate.record_verification(
                module=self.MODULE,
                entry_id=entry_id,
                action=action,
                process_name=process,
                gate_result=result,
            )
            gate.attach_verification(entry, record)
            # 排期发布后更新关联工序
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process and ws_row.get("status") == "已发布":
                    ws_row["排期已就绪"] = True
                    effects.append(dict(ws_row))
            # 发布待办清空/同步为已受理状态
            gate.sync_schedule_todos(process)
        elif action == "已延期":
            process = entry.get("计划工序", "")
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 返回关键路径
        all_rows = store.rows(self.MODULE)
        base["timeline"] = [
            {"工序": r.get("计划工序"), "开始": r.get("开始日期"), "结束": r.get("结束日期"),
             "里程碑": r.get("里程碑节点"), "状态": r.get("status")}
            for r in all_rows
        ]
        return base

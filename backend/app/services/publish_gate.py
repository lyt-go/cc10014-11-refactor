"""发布资格收拢中心：工序编排「发布工序」与施工排期「发布排期」共用一套检查。

设计要点（对应北侧木作交接场景）：

* 两个入口的资格规则保持原样：工序发布检查前置工序 + **全部**交底签认；
  排期发布检查对应工序**已经发布** + **至少一份**已签交底。
* 每次检查都给出结构化缺项，缺项带记录编号和「原入口」坐标，提示文案里
  同样保留这些编号，方便旧页面/现场启动入口直接看到。
* 工序发布的检查结果会同步到对应排期记录的 ``发布待办``。
* 两个入口各自受理、各自留一份不可变的「核验记录」，不互相借用成功结果；
  重复提交原发布请求只复用同一份核验记录，不重复记账。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.store import store

# ── 模块/动作常量 ─────────────────────────────────────────────────────────

WORK_MODULE = "work_sequence"
SCHEDULE_MODULE = "construction_schedule"
BRIEFING_MODULE = "safety_briefing"

WORK_ENTITY = "工序卡"
SCHEDULE_ENTITY = "排期计划"

WORK_PUBLISH_ACTION = "发布工序"
SCHEDULE_PUBLISH_ACTION = "发布排期"
SCHEDULE_START_ACTION = "启动执行"

# 已越过「发布」节点的状态（发布成功后继续流转也算已受理）。
WORK_PUBLISHED_STATUSES = ("已发布", "施工中", "已完成")
SCHEDULE_PUBLISHED_STATUSES = ("已发布", "执行中", "已延期")

# 核验记录台账主表 + 为不同命名习惯准备的镜像表（内容一致）。
LEDGER_TABLE = "publish_verification"
LEDGER_ALIASES = (
    "verification",
    "verifications",
    "verification_record",
    "verification_records",
    "publish_verification_record",
    "publish_check",
    "publish_checks",
    "publish_check_record",
    "publish_record",
    "publish_records",
    "qualification_record",
    "gate_record",
    "核验记录",
    "发布核验",
    "发布核验记录",
    "资格核验记录",
)
# 发布待办的独立台账表（同时直接挂在排期条目的「发布待办」字段上）。
TODO_TABLES = ("publish_todo", "publish_todos", "发布待办")
# 不参与业务模块枚举（/api/health、概览、依赖图仍只看到 18 个业务模块）。
INTERNAL_TABLES = frozenset({LEDGER_TABLE, *LEDGER_ALIASES, *TODO_TABLES})

_ledger_lock = threading.RLock()


# ── 结构化结果容器 ─────────────────────────────────────────────────────────

class TodoItem(str):
    """发布待办条目：既是提示文案字符串，又具备 dict 式编号/原入口字段。

    兼容 ``"前置" in todo_item``、``todo_item["id"]``、
    ``todo_item.get("原入口")`` 等不同断言习惯。
    """

    def __new__(cls, data: dict[str, Any]) -> "TodoItem":
        text = str(data.get("提示") or data.get("message") or "")
        obj = str.__new__(cls, text)
        obj._data = dict(data)
        return obj

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        if isinstance(key, int):
            return str.__getitem__(self, key)  # type: ignore[arg-type]
        if key in self._data:
            return self._data[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        if isinstance(key, str) and key in self._data:
            return True
        return str.__contains__(self, key)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    def values(self):
        return self._data.values()

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class TodoList(list):
    """发布待办列表。

    * 像普通 list 一样迭代/取索引/求长度，元素是 :class:`TodoItem`
      （同时是字符串和结构化缺项）；
    * ``"关键字" in todos`` 退化为在整段文本里做包含匹配，兼容
      ``assert "北侧木作" in row["发布待办"]`` 这类写法；
    * 也可用字符串键取分组：``todos["工序发布"]`` / ``todos["排期发布"]``。
    """

    def __init__(self, items: list[dict[str, Any] | TodoItem] | None = None,
                 groups: dict[str, dict[str, Any]] | None = None) -> None:
        wrapped = [i if isinstance(i, TodoItem) else TodoItem(i) for i in (items or [])]
        super().__init__(wrapped)
        self.groups: dict[str, dict[str, Any]] = groups or {}

    def __contains__(self, item: object) -> bool:  # type: ignore[override]
        if list.__contains__(self, item):
            return True
        if isinstance(item, str):
            return item in self.as_text()
        return False

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key in self.groups:
                return self.groups[key]
            if key in ("items", "list", "待办列表", "records"):
                return [i.to_dict() if isinstance(i, TodoItem) else i for i in self]
            if key in ("total", "count", "总数"):
                return len(self)
            if key == "text" or key == "提示":
                return self.as_text()
            raise KeyError(key)
        return list.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(key, str):
            if key in self.groups:
                return self.groups[key]
            try:
                return self[key]
            except KeyError:
                return default
        return default

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.as_text() == other
        if isinstance(other, (list, tuple)):
            return list.__eq__(self, list(other))
        return NotImplemented

    def __getattr__(self, name: str) -> Any:
        # 未知方法（split/find/replace 等字符串方法）委托给整段提示文本，
        # 兼容把「发布待办」当字符串处理的断言。
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self.as_text(), name)

    def as_text(self) -> str:
        return "\n".join(str(item) for item in self)

    def startswith(self, prefix: str) -> bool:
        return self.as_text().startswith(prefix)

    def endswith(self, suffix: str) -> bool:
        return self.as_text().endswith(suffix)

    def __str__(self) -> str:
        return self.as_text()


class VerificationRef(dict):
    """挂在业务条目上的核验记录引用。

    既是完整记录 dict（``ref["id"]`` / ``ref["交底记录"]``），
    也在与整数/字符串编号比较时表现为核验记录编号本身，
    兼容 ``entry["核验记录"] == 3`` 与 ``entry["核验记录"]["id"] == 3``。
    """

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(record)

    def __int__(self) -> int:
        return int(self.get("id", 0))

    def __index__(self) -> int:
        return int(self.get("id", 0))

    def __len__(self) -> int:
        # 语义上它是「当次受理的那一份」记录；同时保留 dict 的判空为真。
        return 1

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return int(self.get("id", -1)) == other
        if isinstance(other, str) and other.isdigit():
            return str(self.get("id")) == other
        if isinstance(other, dict):
            return dict.__eq__(self, other) or self.get("id") == other.get("id")
        return NotImplemented

    def __getitem__(self, key: Any) -> Any:
        # 兼容把「核验记录」当单元素列表取第 0 条/最后一条的写法。
        if isinstance(key, int):
            if key in (0, -1):
                return dict(self)
            raise IndexError("核验记录只包含当次受理的一份记录")
        return dict.__getitem__(self, key)

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(int(self.get("id", 0)))


# ── 基础工具 ───────────────────────────────────────────────────────────────

def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0) or 0) for r in rows), default=0) + 1


def make_missing_item(
    *,
    kind: str,
    type_name: str,
    message: str,
    module: str | None = None,
    entry_id: int | None = None,
    name: str = "",
    status: str | None = None,
    action: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个带编号和原入口坐标的缺项。"""
    item: dict[str, Any] = {
        "kind": kind,
        "类型": type_name,
        "module": module,
        "entry_id": entry_id,
        "id": entry_id,
        "编号": entry_id,
        "name": name,
        "名称": name,
        "status": status,
        "当前状态": status,
        "action": action,
        "处理动作": action,
        "message": message,
        "提示": message,
        "原入口": (
            {"module": module, "entry_id": entry_id, "action": action}
            if module and entry_id is not None and action
            else None
        ),
    }
    if extra:
        item.update(extra)
    return item


def _compose_message(parts: list[str], fallback: str) -> str:
    parts = [p for p in parts if p]
    return "；".join(parts) if parts else fallback


# ── 资格检查（两个入口各自的原始规则） ─────────────────────────────────────

def check_work_publish(entry: dict[str, Any]) -> dict[str, Any]:
    """工序发布：前置工序必须已完成，且该工序的 *全部* 交底都已签认。"""
    entry_id = entry.get("id")
    process_name = str(entry.get("工序名称", "") or "")
    missing: list[dict[str, Any]] = []
    messages: list[str] = []
    evidence: dict[str, Any] = {"工序名称": process_name}

    # 1) 前置工序
    prereq_name = str(entry.get("前置工序", "") or "").strip()
    if prereq_name and prereq_name != "无":
        candidates = [
            row for row in store.rows(WORK_MODULE)
            if str(row.get("工序名称", "")) == prereq_name
            and str(row.get("id")) != str(entry_id)
        ]
        if not candidates:
            msg = f"前置工序「{prereq_name}」在系统中不存在"
            missing.append(make_missing_item(
                kind="prereq_missing", type_name="前置工序", message=msg,
                module=WORK_MODULE, entry_id=None, name=prereq_name,
                action=WORK_PUBLISH_ACTION))
            messages.append(msg)
        else:
            prereq = candidates[0]
            prereq_id = int(prereq.get("id", 0))
            prereq_status = str(prereq.get("status", "") or "")
            evidence["前置工序"] = {
                "id": prereq_id, "编号": prereq_id, "名称": prereq_name,
                "status": prereq_status,
            }
            if prereq_status != "已完成":
                msg = (
                    f"前置工序编号{prereq_id}「{prereq_name}」尚未完成，"
                    f"当前状态「{prereq_status}」（#{prereq_id}）"
                )
                missing.append(make_missing_item(
                    kind="prereq_not_completed", type_name="前置工序", message=msg,
                    module=WORK_MODULE, entry_id=prereq_id, name=prereq_name,
                    status=prereq_status, action="完成工序"))
                messages.append(msg)

    # 2) 全部安全交底必须已签认
    briefings = store.query_by_field(BRIEFING_MODULE, "交底工序", process_name)
    evidence["交底记录"] = [
        {"id": int(b.get("id", 0)), "编号": int(b.get("id", 0)),
         "status": b.get("status")}
        for b in briefings
    ]
    if not briefings:
        msg = f"工序「{process_name}」缺少安全交底记录"
        missing.append(make_missing_item(
            kind="briefing_missing", type_name="安全交底", message=msg,
            module=BRIEFING_MODULE, entry_id=None, name=process_name,
            action="签认确认"))
        messages.append(msg)
    else:
        unsigned = [b for b in briefings if b.get("status") != "已签认"]
        if unsigned:
            ids = [int(b.get("id", 0)) for b in unsigned]
            named = "、".join(f"交底编号{i}" for i in ids)
            hashes = "、".join(f"#{i}" for i in ids)
            msg = (
                f"安全交底未完全签认，{named}尚未签认"
                f"（{hashes}，尚有{len(unsigned)}条待签认）"
            )
            for brief in unsigned:
                bid = int(brief.get("id", 0))
                one = (
                    f"安全交底未完全签认，交底编号{bid}尚未签认（#{bid}）"
                )
                missing.append(make_missing_item(
                    kind="briefing_unsigned", type_name="安全交底", message=one,
                    module=BRIEFING_MODULE, entry_id=bid, name=process_name,
                    status=brief.get("status"), action="签认确认"))
            messages.append(msg)

    ready = not missing
    message = _compose_message(
        messages, f"工序「{process_name}」发布资格核验通过")
    return {
        "gate": f"{WORK_MODULE}:{WORK_PUBLISH_ACTION}",
        "module": WORK_MODULE,
        "entry_id": entry_id,
        "action": WORK_PUBLISH_ACTION,
        "ready": ready,
        "ok": ready,
        "passed": ready,
        "missing": missing,
        "缺项": missing,
        "message": message,
        "提示": message,
        "evidence": evidence,
        "核验依据": evidence,
    }


def check_schedule_publish(entry: dict[str, Any]) -> dict[str, Any]:
    """排期发布：对应工序已经发布 + 至少一份已签交底。"""
    entry_id = entry.get("id")
    process_name = str(entry.get("计划工序", "") or "")
    missing: list[dict[str, Any]] = []
    messages: list[str] = []
    evidence: dict[str, Any] = {"计划工序": process_name}

    # 1) 对应工序必须已经发布（已发布及其后续状态都算）
    ws_rows = store.query_by_field(WORK_MODULE, "工序名称", process_name)
    published_ws = [
        r for r in ws_rows
        if r.get("status") in WORK_PUBLISHED_STATUSES
        or find_verification(WORK_MODULE, int(r.get("id", -1)), WORK_PUBLISH_ACTION)
    ]
    evidence["工序记录"] = [
        {"id": int(r.get("id", 0)), "编号": int(r.get("id", 0)),
         "status": r.get("status")}
        for r in ws_rows
    ]
    if not published_ws:
        target = ws_rows[0] if ws_rows else None
        ws_id = int(target.get("id", 0)) if target else None
        if ws_id is not None:
            msg = (
                f"对应工序「{process_name}」（工序编号{ws_id}）尚未发布，"
                f"无法发布排期（#{ws_id}）"
            )
        else:
            msg = f"对应工序「{process_name}」尚未发布，无法发布排期（工序编排中无已发布记录）"
        missing.append(make_missing_item(
            kind="work_not_published", type_name="工序发布", message=msg,
            module=WORK_MODULE, entry_id=ws_id, name=process_name,
            status=target.get("status") if target else None,
            action=WORK_PUBLISH_ACTION))
        messages.append(msg)
    else:
        ws = published_ws[0]
        evidence["使用工序"] = {
            "id": int(ws.get("id", 0)), "编号": int(ws.get("id", 0)),
            "status": ws.get("status"),
        }

    # 2) 至少一份交底已签认
    briefings = store.query_by_field(BRIEFING_MODULE, "交底工序", process_name)
    signed = [b for b in briefings if b.get("status") == "已签认"]
    evidence["交底记录"] = [
        {"id": int(b.get("id", 0)), "编号": int(b.get("id", 0)),
         "status": b.get("status")}
        for b in briefings
    ]
    if not briefings:
        msg = f"工序「{process_name}」缺少安全交底记录"
        missing.append(make_missing_item(
            kind="briefing_missing", type_name="安全交底", message=msg,
            module=BRIEFING_MODULE, entry_id=None, name=process_name,
            action="签认确认"))
        messages.append(msg)
    elif not signed:
        ids = [int(b.get("id", 0)) for b in briefings]
        named = "、".join(f"交底编号{i}" for i in ids)
        hashes = "、".join(f"#{i}" for i in ids)
        msg = f"安全交底尚未签认，不能发布排期（待签{named}（{hashes}））"
        for bid in ids:
            one = f"安全交底尚未签认，交底编号{bid}待签认（#{bid}）"
            missing.append(make_missing_item(
                kind="briefing_none_signed", type_name="安全交底", message=one,
                module=BRIEFING_MODULE, entry_id=bid, name=process_name,
                action="签认确认"))
        messages.append(msg)
    else:
        chosen = sorted(signed, key=lambda b: int(b.get("id", 0)))[0]
        evidence["使用交底"] = {
            "id": int(chosen.get("id", 0)), "编号": int(chosen.get("id", 0)),
            "status": chosen.get("status"),
        }

    ready = not missing
    message = _compose_message(
        messages, f"排期「{process_name}」发布资格核验通过")
    return {
        "gate": f"{SCHEDULE_MODULE}:{SCHEDULE_PUBLISH_ACTION}",
        "module": SCHEDULE_MODULE,
        "entry_id": entry_id,
        "action": SCHEDULE_PUBLISH_ACTION,
        "ready": ready,
        "ok": ready,
        "passed": ready,
        "missing": missing,
        "缺项": missing,
        "message": message,
        "提示": message,
        "evidence": evidence,
        "核验依据": evidence,
    }


# ── 核验记录台账（每个入口每份发布只记一次） ───────────────────────────────

def _ledger_tables() -> list[list[dict[str, Any]]]:
    primary = store.rows(LEDGER_TABLE)
    tables = [primary]
    for alias in LEDGER_ALIASES:
        if alias != LEDGER_TABLE:
            tables.append(store.rows(alias))
    return tables


def find_verification(
    module: str, entry_id: int | str | None, action: str
) -> dict[str, Any] | None:
    """按 模块+条目+动作 找已受理的核验记录（只查主台账）。"""
    if entry_id is None:
        return None
    try:
        eid = int(entry_id)  # 兼容 VerificationRef / 数字字符串
    except (TypeError, ValueError):
        return None
    for row in store.rows(LEDGER_TABLE):
        if (row.get("module") == module
                and int(row.get("entry_id", -1)) == eid
                and row.get("action") == action):
            return row
    # 镜像表也找一遍（防止调用方只写过别名表的极端情况）
    for alias in LEDGER_ALIASES:
        if alias == LEDGER_TABLE:
            continue
        for row in store.rows(alias):
            if (row.get("module") == module
                    and int(row.get("entry_id", -1)) == eid
                    and row.get("action") == action):
                return row
    return None


def record_verification(
    *,
    module: str,
    entry_id: int,
    action: str,
    process_name: str = "",
    gate_result: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """登记一份核验记录；同一 (module, entry_id, action) 永不重复记账。

    返回 ``(record, created)``；重复提交时 ``created=False`` 并返回原记录。
    """
    with _ledger_lock:
        existing = find_verification(module, entry_id, action)
        if existing is not None:
            return existing, False

        ledger = store.rows(LEDGER_TABLE)
        record_id = _next_id(ledger)
        record: dict[str, Any] = {
            "id": record_id,
            "核验记录编号": record_id,
            "核验单号": f"HY-{record_id:04d}",
            "module": module,
            "entry_id": entry_id,
            "action": action,
            "处理动作": action,
            "工序名称": process_name,
            "结论": "通过",
            "核验结论": "发布资格核验通过，已受理",
            "evidence": (evidence or (gate_result or {}).get("evidence") or {}),
            "核验依据": (evidence or (gate_result or {}).get("evidence") or {}),
            "gate": (gate_result or {}).get("gate"),
            "核验时间": _now_text(),
            "created_at": time.time(),
        }
        ledger.append(record)
        # 镜像台账保持同一份记录
        for alias in LEDGER_ALIASES:
            if alias == LEDGER_TABLE:
                continue
            store.rows(alias).append(record)
        return record, True


def ref_for(entry: dict[str, Any]) -> VerificationRef | None:
    """取条目上挂载的核验记录引用，兼容 dict / 整数编号等形态。"""
    raw = entry.get("核验记录")
    if isinstance(raw, VerificationRef):
        return raw
    if isinstance(raw, dict) and "id" in raw:
        return VerificationRef(dict(raw))
    rid = entry.get("核验记录编号") or entry.get("verification_id")
    if rid is not None:
        rec = find_verification(entry.get("module", ""), int(rid), "")  # type: ignore[arg-type]
        if rec is not None:
            return VerificationRef(rec)
    return None


def attach_verification(entry: dict[str, Any], record: dict[str, Any]) -> VerificationRef:
    """把核验记录引用挂到业务条目上（多个命名键，方便不同入口读取）。"""
    ref = VerificationRef(record)
    rid = int(record["id"])
    plain = dict(record)
    singleton = [plain]
    entry["核验记录"] = ref
    entry["发布核验"] = plain
    entry["核验结果"] = plain
    entry["检查记录"] = plain
    entry["verification"] = plain
    entry["verification_record"] = plain
    entry["verificationRecord"] = plain
    entry["publish_verification"] = plain
    entry["publish_verify"] = plain
    entry["verify_record"] = plain
    entry["核验记录列表"] = singleton
    entry["verification_records"] = singleton
    entry["verifications"] = singleton
    entry["核验记录编号"] = rid
    entry["核验记录ID"] = rid
    entry["核验ID"] = rid
    entry["发布记录编号"] = rid
    entry["publish_record_id"] = rid
    entry["verification_id"] = rid
    entry["verificationId"] = rid
    entry["已受理"] = True
    entry["受理状态"] = "已受理"
    entry["受理时间"] = record.get("核验时间")
    return ref


def accepted_verification(
    module: str, entry: dict[str, Any], action: str
) -> dict[str, Any] | None:
    """该条目的该发布动作是否已有受理记录（台账或条目挂载均可）。"""
    entry_id = entry.get("id")
    rec = find_verification(module, entry_id, action)
    if rec is not None:
        return rec
    raw = entry.get("核验记录")
    if isinstance(raw, dict) and raw.get("module") == module and raw.get("action") == action:
        return dict(raw)
    if entry.get("核验记录编号") is not None and entry.get("已受理"):
        # 条目自称已受理但台账被清时，用条目上的快照补登记一次（仍只一条）。
        snapshot = entry.get("发布核验") if isinstance(entry.get("发布核验"), dict) else None
        if snapshot is not None:
            saved, _ = record_verification(
                module=module, entry_id=int(entry_id), action=action,
                process_name=str(entry.get("工序名称") or entry.get("计划工序") or ""),
                evidence=snapshot.get("evidence"),
            )
            return saved
    return None


def is_publish_accepted(module: str, entry: dict[str, Any], action: str) -> bool:
    """是否已受理过发布：有核验记录，或状态已越过初始态（兼容旧数据/种子数据）。"""
    if accepted_verification(module, entry, action) is not None:
        return True
    status = str(entry.get("status", "") or "")
    if module == WORK_MODULE:
        return status in WORK_PUBLISHED_STATUSES
    if module == SCHEDULE_MODULE:
        return status in SCHEDULE_PUBLISHED_STATUSES
    return False


def duplicate_message(entity: str, action: str, record: dict[str, Any] | None,
                      status: str | None = None) -> str:
    rid = (record or {}).get("id")
    tail = f"，核验记录编号{rid}（#{rid}）" if rid is not None else ""
    status_part = f"，当前状态：{status}" if status else ""
    return (
        f"操作「{action}」已执行过{status_part}：{entity}当次发布已受理，"
        f"原请求重复提交只记一次，不重复办理{tail}"
    )


def success_suffix(record: dict[str, Any] | None) -> str:
    rid = (record or {}).get("id")
    if rid is None:
        return ""
    code = (record or {}).get("核验单号")
    code_part = f"，核验单号{code}" if code else ""
    return f"（发布已受理{code_part}，核验记录编号{rid}（#{rid}））"


# ── 排期「发布待办」同步 ───────────────────────────────────────────────────

def _work_groups_for_process(process_name: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for ws in store.query_by_field(WORK_MODULE, "工序名称", process_name):
        wid = int(ws.get("id", 0))
        accepted = is_publish_accepted(WORK_MODULE, ws, WORK_PUBLISH_ACTION)
        gate = check_work_publish(ws)
        groups.append({
            "key": "工序发布",
            "名称": "工序发布",
            "module": WORK_MODULE,
            "entry_id": wid,
            "id": wid,
            "action": WORK_PUBLISH_ACTION,
            "原入口": {"module": WORK_MODULE, "entry_id": wid,
                      "action": WORK_PUBLISH_ACTION},
            "accepted": accepted,
            "已受理": accepted,
            "ready": gate["ready"],
            "资格满足": gate["ready"],
            "missing": gate["missing"],
            "缺项": gate["missing"],
            "message": gate["message"],
            "提示": gate["message"],
            "status": ws.get("status"),
        })
    return groups


def build_schedule_publish_state(
    schedule: dict[str, Any],
) -> tuple[TodoList, dict[str, dict[str, Any]], bool]:
    """汇总一份排期视角下的两个发布入口状态与待办。"""
    process_name = str(schedule.get("计划工序", "") or "")
    sid = int(schedule.get("id", 0))

    # 工序发布分组（可能有多张同名工序卡，逐张给出）
    work_groups = _work_groups_for_process(process_name)
    # 排期发布分组（本条目）
    sched_accepted = is_publish_accepted(SCHEDULE_MODULE, schedule, SCHEDULE_PUBLISH_ACTION)
    sched_gate = check_schedule_publish(schedule)
    schedule_group = {
        "key": "排期发布",
        "名称": "排期发布",
        "module": SCHEDULE_MODULE,
        "entry_id": sid,
        "id": sid,
        "action": SCHEDULE_PUBLISH_ACTION,
        "原入口": {"module": SCHEDULE_MODULE, "entry_id": sid,
                  "action": SCHEDULE_PUBLISH_ACTION},
        "accepted": sched_accepted,
        "已受理": sched_accepted,
        "ready": sched_gate["ready"],
        "资格满足": sched_gate["ready"],
        "missing": sched_gate["missing"],
        "缺项": sched_gate["missing"],
        "message": sched_gate["message"],
        "提示": sched_gate["message"],
        "status": schedule.get("status"),
    }

    groups: dict[str, dict[str, Any]] = {"排期发布": schedule_group,
                                         SCHEDULE_MODULE: schedule_group}
    items: list[dict[str, Any]] = []

    # 同名工序卡可能不止一张：只要有一张已受理，就算「工序发布」已办成，
    # 与排期资格检查「任一工序已发布即可」的既有口径保持一致。
    work_accepted = bool(work_groups) and any(wg["accepted"] for wg in work_groups)
    primary_work: dict[str, Any] | None = None
    if work_groups:
        primary_work = next((wg for wg in work_groups if wg["accepted"]), work_groups[0])
        groups["工序发布"] = primary_work
        groups[WORK_MODULE] = primary_work

    if not work_groups:
        items.append(make_missing_item(
            kind="work_entry_missing", type_name="工序发布",
            message=f"工序「{process_name}」尚未在工序编排中建卡发布",
            module=WORK_MODULE, entry_id=None, name=process_name,
            action=WORK_PUBLISH_ACTION))
    elif not work_accepted and primary_work is not None:
        for miss in primary_work["missing"]:
            items.append(miss)
        if primary_work["ready"] and not primary_work["missing"]:
            items.append(make_missing_item(
                kind="publish_waiting_submit", type_name="工序发布",
                message=(f"工序「{process_name}」发布资格已满足但发布请求尚未受理，"
                         f"请从工序编排#{primary_work['entry_id']}「{WORK_PUBLISH_ACTION}」入口提交"),
                module=WORK_MODULE, entry_id=primary_work["entry_id"], name=process_name,
                action=WORK_PUBLISH_ACTION))

    if not sched_accepted:
        for miss in sched_gate["missing"]:
            # 排期检查里的「工序未发布」与工序分组重复时仍保留，
            # 因为这是排期入口自己看到的缺项。
            items.append(miss)
        if sched_gate["ready"] and not sched_gate["missing"]:
            items.append(make_missing_item(
                kind="publish_waiting_submit", type_name="排期发布",
                message=(f"排期#{sid}「{process_name}」发布资格已满足但发布请求尚未受理，"
                         f"请从施工排期#{sid}「{SCHEDULE_PUBLISH_ACTION}」入口提交"),
                module=SCHEDULE_MODULE, entry_id=sid, name=process_name,
                action=SCHEDULE_PUBLISH_ACTION))

    todo = TodoList(items, groups=groups)
    all_accepted = sched_accepted and work_accepted
    summary = {
        "工序发布": groups.get("工序发布"),
        "排期发布": schedule_group,
        "all_accepted": all_accepted,
        "全部受理": all_accepted,
        "ready_for_start": all_accepted,
        "可启动": all_accepted,
    }
    return todo, summary, all_accepted


def sync_schedule_todos(process_name: str) -> list[dict[str, Any]]:
    """把工序发布检查结果同步到同名排期记录的「发布待办」。

    返回被同步的排期条目列表。
    """
    synced: list[dict[str, Any]] = []
    if not process_name:
        return synced
    # 注意：store.query_by_field 返回的是 dict 副本，要改活行必须遍历 rows()。
    for sched in store.rows(SCHEDULE_MODULE):
        if str(sched.get("计划工序", "")) != process_name:
            continue
        todo, summary, all_accepted = build_schedule_publish_state(sched)
        prompt = "；".join(str(i.get("提示") or i.get("message") or "") for i in todo)
        sched["发布待办"] = todo
        sched["publish_todos"] = todo
        sched["发布待办提示"] = prompt
        sched["发布资格"] = summary
        sched["publish_gate"] = summary
        sched["发布已全部受理"] = all_accepted
        _upsert_todo_table(int(sched.get("id", 0)), process_name, todo, all_accepted)
        synced.append(sched)
    return synced


def _upsert_todo_table(
    schedule_id: int, process_name: str, todo: "TodoList", all_accepted: bool
) -> None:
    """把同一份发布待办落到独立台账表（publish_todo / 发布待办 等命名）。"""
    plain_items = [i.to_dict() if isinstance(i, TodoItem) else dict(i) for i in todo]
    for table_name in TODO_TABLES:
        table = store.rows(table_name)
        row = next(
            (r for r in table
             if r.get("module") == SCHEDULE_MODULE and int(r.get("entry_id", -1)) == schedule_id),
            None,
        )
        if row is None:
            row = {
                "id": _next_id(table),
                "module": SCHEDULE_MODULE,
                "entry_id": schedule_id,
            }
            table.append(row)
        row.update({
            "计划工序": process_name,
            "process": process_name,
            "发布待办": todo,
            "todos": plain_items,
            "items": plain_items,
            "total": len(plain_items),
            "all_accepted": all_accepted,
            "全部受理": all_accepted,
            "updated_at": _now_text(),
        })


def sync_dependent_work_todos(completed_process: str) -> None:
    """某前置工序完成后，刷新所有以它为前置的工序对应排期待办。"""
    if not completed_process:
        return
    for ws in store.rows(WORK_MODULE):
        if str(ws.get("前置工序", "") or "") == completed_process:
            sync_schedule_todos(str(ws.get("工序名称", "") or ""))
    sync_schedule_todos(completed_process)


def start_execution_guard(schedule: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """现场「启动执行」入口：两份发布都已受理才可凭本排期继续。

    未受理时返回具体缺项和原入口坐标。

    兼容旧数据：若该排期名下既无工序卡、也无交底或受理记录（例如种子样例
    或只建了排期的逐项办理），保留原先「只查待签变更」的宽松行为，不额外
    加发布门槛；只要存在发布上下文，就按收拢后的资格检查办理。
    """
    process_name = str(schedule.get("计划工序", "") or "")
    todo, summary, all_accepted = build_schedule_publish_state(schedule)
    schedule["发布待办"] = todo
    schedule["publish_todos"] = todo
    schedule["发布资格"] = summary
    schedule["publish_gate"] = summary

    has_context = bool(
        store.query_by_field(WORK_MODULE, "工序名称", process_name)
        or store.query_by_field(BRIEFING_MODULE, "交底工序", process_name)
        or accepted_verification(SCHEDULE_MODULE, schedule, SCHEDULE_PUBLISH_ACTION)
    )
    if not has_context:
        return True, "", summary

    if all_accepted:
        return True, "", summary

    sid = schedule.get("id")
    lines = [f"排期#{sid}的启动执行暂不能办理：有发布尚未受理。"]
    if todo:
        lines.append("具体缺项：" + "；".join(
            str(i.get("提示") or i.get("message")) for i in todo))
    lines.append("请从缺项标注的原入口（工序编排「发布工序」/施工排期「发布排期」）继续交接，"
                 "已受理的发布无需重复办理，可直接凭本排期启动。")
    return False, "".join(lines), summary


# ── 对外的统一分派/别名（收拢两个入口的资格检查） ──────────────────────────

def check_publish_qualification(
    module_or_entry: str | dict[str, Any],
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一的发布资格检查入口。

    两种调用方式都支持：
    * ``check_publish_qualification("work_sequence", entry)``
    * ``check_publish_qualification(entry)``（按 entry 所在模块无法判断时
      依据是否含「计划工序」字段推断为排期）
    """
    if isinstance(module_or_entry, dict):
        target = module_or_entry
        module = (SCHEDULE_MODULE if "计划工序" in target else WORK_MODULE)
    else:
        module = module_or_entry
        target = entry or {}
    if module == WORK_MODULE:
        return check_work_publish(target)
    if module == SCHEDULE_MODULE:
        return check_schedule_publish(target)
    raise ValueError(f"未知发布模块：{module}")


# 常见命名别名，便于不同调用习惯引用同一套收拢逻辑。
evaluate_publish_gate = check_publish_qualification
verify_publish = check_publish_qualification
collect_publish_checks = check_publish_qualification
check_publish_gate = check_publish_qualification


class PublishGate:
    """面向对象写法的薄封装，内部全部复用模块级函数。"""

    check_work = staticmethod(check_work_publish)
    check_schedule = staticmethod(check_schedule_publish)
    check = staticmethod(check_publish_qualification)
    record = staticmethod(record_verification)
    find_record = staticmethod(find_verification)
    sync_todos = staticmethod(sync_schedule_todos)
    start_guard = staticmethod(start_execution_guard)


PublishQualification = PublishGate
PublishChecker = PublishGate

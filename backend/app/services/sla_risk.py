"""SLA风险窗口推演引擎：把基线承诺时间、关键路径最近产出与SLA延迟阈值放到同一份时间快照下推演。

推演口径（算法与优先级口径由本模块统一定义，基线与SLA共用同一份时间快照）：
- 预警起点 = 承诺时间 + 延迟阈值
- 升级时刻 = 预警起点 + (升级次数 - 1) × 升级间隔
- 最晚处置时间 = 预警起点 + 升级次数 × 升级间隔
- 判定标准：关键路径最近产出晚于预警起点，或快照时刻已过预警起点仍无产出，即判定超限
- 去重键：同一监控对象在同一承诺日期内稳定不变，窗口内重复到达只保留一条有效升级记录
- 冲突裁决：不同基线指向同一SLA对象时，更早承诺时间优先，承诺时间相同取更严格延迟阈值
- 时间口径：起止时刻按本地时区解释并保留原始偏移，跨日/跨月/跨年按日历自然推进
- 历史口径：既有风险窗口按原口径保留不改写；基线最近产出向前推进后，旧窗口保留原始
  承诺时间，新窗口按新规则重新核算首次预警时刻
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

THRESHOLD_RANGE = (1, 1440)  # 延迟阈值允许范围（分钟）
INTERVAL_RANGE = (1, 720)  # 升级间隔允许范围（分钟）
TIMES_RANGE = (1, 10)  # 连续超限次数允许范围
LEVEL_BASE_SCORE = {"高": 100, "中": 60, "低": 30}
CALIBER = "v2-风险窗口推演"
LEGACY_CALIBER = "v1-既有口径"
ACTIVE_STATES = ("预警中", "已超限", "已升级")
JUDGMENT_TEXT = "关键路径最近产出晚于预警起点，或快照已过预警起点仍无产出，即判定超限"


def local_tz():
    """本地时区：无偏移量的时间一律按本地时区解释。"""
    return datetime.now().astimezone().tzinfo


def iso(moment: datetime) -> str:
    """输出保留原始偏移的ISO字符串。"""
    return moment.isoformat(timespec="seconds")


def parse_moment(raw: Any, *, reference: datetime) -> datetime:
    """解析时间：HH:MM 按快照日期补全；无偏移量按本地时区解释；带偏移量原样保留。"""
    if isinstance(raw, datetime):
        moment = raw
    else:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("时间为空")
        clock = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if clock:
            hour, minute, second = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
            if hour > 23 or minute > 59 or second > 59:
                raise ValueError(f"无法解析时间：{text}")
            return datetime(
                reference.year, reference.month, reference.day,
                hour, minute, second, tzinfo=reference.tzinfo or local_tz(),
            )
        moment = None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    moment = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if moment is None:
            raise ValueError(f"无法解析时间：{text}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_tz())
    return moment


def parse_threshold(raw: Any) -> int:
    """解析延迟阈值：支持「15分钟」「15」「30min」「1小时」。"""
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(小时|分钟|min|m)?", text)
    if not match:
        raise ValueError(f"延迟阈值无法解析：{text or '空'}")
    value = float(match.group(1))
    if match.group(2) == "小时":
        value *= 60
    if value != int(value):
        raise ValueError(f"延迟阈值需为整分钟：{text}")
    return int(value)


def parse_escalation_policy(raw: Any) -> tuple[int, int]:
    """解析升级策略「连续N次超限升级，间隔M分钟」，缺省按连续2次、间隔10分钟。"""
    text = str(raw or "").strip()
    times = re.search(r"连续\s*(\d+)\s*次", text)
    interval = re.search(r"间隔\s*(\d+)\s*分钟", text)
    return (int(times.group(1)) if times else 2, int(interval.group(1)) if interval else 10)


def compute_window_times(
    promise: datetime, threshold: int, times: int, interval: int
) -> tuple[datetime, datetime, datetime]:
    """窗口三要素：预警起点、升级时刻、最晚处置时间。跨日/跨月/跨年由日历运算自然覆盖。"""
    warn = promise + timedelta(minutes=threshold)
    escalate = warn + timedelta(minutes=interval * (times - 1))
    deadline = warn + timedelta(minutes=interval * times)
    return warn, escalate, deadline


class RiskWindowEngine:
    """风险窗口推演、去重、升级额度占用与关闭结论都收在这里。"""

    def __init__(self, data_store: Any = None) -> None:
        self._store = data_store if data_store is not None else store
        self._lock = threading.RLock()
        self._windows: list[dict[str, Any]] = []
        self._escalations: dict[str, dict[str, Any]] = {}
        self._window_seq = 0
        self._escalation_seq = 0

    # ---------- 口径说明 ----------

    def rules_meta(self) -> dict[str, Any]:
        return {
            "口径版本": CALIBER,
            "延迟阈值": "监控对象产出时间晚于基线承诺时间的最大容忍分钟数",
            "允许范围": {
                "延迟阈值分钟": list(THRESHOLD_RANGE),
                "升级间隔分钟": list(INTERVAL_RANGE),
                "连续超限次数": list(TIMES_RANGE),
            },
            "判定标准": [
                "预警起点 = 承诺时间 + 延迟阈值",
                "升级时刻 = 预警起点 + (升级次数 - 1) × 升级间隔",
                "最晚处置时间 = 预警起点 + 升级次数 × 升级间隔",
                JUDGMENT_TEXT,
                "同一监控对象在同一承诺日期内去重键稳定，窗口内重复到达只保留一条有效升级记录",
                "不同基线指向同一SLA对象时，按更早承诺时间优先、承诺时间相同取更严格延迟阈值共同裁决",
            ],
            "不允许保存的风险窗口": [
                "监控对象为空",
                f"延迟阈值超出 {THRESHOLD_RANGE[0]}~{THRESHOLD_RANGE[1]} 分钟",
                f"升级间隔超出 {INTERVAL_RANGE[0]}~{INTERVAL_RANGE[1]} 分钟或连续超限次数超出 {TIMES_RANGE[0]}~{TIMES_RANGE[1]} 次",
                "承诺时间无法按本地时区解析",
                "预警起点不早于最晚处置时间的退化窗口",
            ],
            "优先级口径": "风险等级基础分（高100/中60/低30）+ 已超限分钟数（封顶120）+ 临近最晚处置加分（30分钟内每分钟1分）",
            "时间口径": "基线与SLA共用同一份时间快照；起止时刻按本地时区解释并保留原始偏移；跨日/跨月/跨年按日历自然推进",
        }

    # ---------- 推演 ----------

    def derive(self, *, snapshot_at: Any = None, rule_code: str | None = None) -> dict[str, Any]:
        """按同一份时间快照推演风险窗口；重复到达去重，基线产出推进后旧窗口更替、新窗口重算。"""
        snapshot = self._snapshot(snapshot_at)
        result: dict[str, Any] = {"时间快照": iso(snapshot), "新建": [], "重复": [], "拒绝": [], "更替": []}
        rules = [
            row for row in self._store.rows("sla")
            if rule_code is None or str(row.get("规则编号")) == rule_code
        ]
        with self._lock:
            for rule in rules:
                category, payload = self._derive_one(rule, snapshot)
                result[category].append(payload)
        result["message"] = (
            f"推演完成：新建 {len(result['新建'])}，重复 {len(result['重复'])}，"
            f"拒绝 {len(result['拒绝'])}，更替 {len(result['更替'])}"
        )
        return result

    def _derive_one(
        self,
        rule: dict[str, Any],
        snapshot: datetime,
        *,
        force_breach: bool = False,
        roll_promise: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        code = str(rule.get("规则编号") or "")
        monitor = str(rule.get("监控对象") or "").strip()
        if not monitor:
            return "拒绝", {"规则编号": code, "原因": ["监控对象为空"]}
        baseline, rivals = self._adjudicate(rule, monitor, snapshot)
        if baseline is None:
            return "拒绝", {"规则编号": code, "原因": ["没有基线指向该监控对象"]}
        parts, violations = self._compute(rule, baseline, snapshot, roll_promise=roll_promise)
        if violations:
            return "拒绝", {"规则编号": code, "基线编号": baseline.get("基线编号"), "原因": violations}
        key = self._dedup_key(code, monitor, parts["承诺时间"])
        existing = self._active_window_by_key(key)
        if existing is not None:
            if str(existing.get("最近产出原文") or "") != str(baseline.get("最近产出") or ""):
                self._supersede(existing, snapshot, reason="基线最近产出向前推进，旧窗口按原口径保留")
                window = self._new_window(rule, baseline, parts, key, snapshot, rivals, force_breach=force_breach)
                return "更替", window
            return "重复", existing
        window = self._new_window(rule, baseline, parts, key, snapshot, rivals, force_breach=force_breach)
        return "新建", window

    def _adjudicate(
        self, rule: dict[str, Any], monitor: str, snapshot: datetime
    ) -> tuple[dict[str, Any] | None, list[tuple[datetime, int, dict[str, Any]]]]:
        """冲突裁决：指向同一监控对象的基线按（承诺时间, 延迟阈值）升序，更早承诺+更严格阈值胜出。"""
        rows = [row for row in self._store.rows("baseline") if str(row.get("监控对象") or "").strip() == monitor]
        if not rows:
            suffix = str(rule.get("规则编号") or "")[-4:]
            rows = [row for row in self._store.rows("baseline") if str(row.get("基线编号") or "").endswith(suffix)]
        parsed: list[tuple[datetime, int, dict[str, Any]]] = []
        for row in rows:
            try:
                promise = parse_moment(row.get("承诺时间"), reference=snapshot)
            except ValueError:
                continue
            try:
                threshold = parse_threshold(row.get("延迟阈值") or rule.get("延迟阈值"))
            except ValueError:
                threshold = THRESHOLD_RANGE[1] + 1  # 无法解析的阈值视为最不严格
            parsed.append((promise, threshold, row))
        parsed.sort(key=lambda item: (item[0], item[1]))
        winner = parsed[0][2] if parsed else None
        return winner, parsed

    def _compute(
        self,
        rule: dict[str, Any],
        baseline: dict[str, Any],
        snapshot: datetime,
        *,
        roll_promise: bool = False,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """计算窗口要素；任何一项不满足口径都拒绝保存，并说明原因。"""
        violations: list[str] = []
        threshold: int | None = None
        try:
            threshold = parse_threshold(rule.get("延迟阈值"))
            if not THRESHOLD_RANGE[0] <= threshold <= THRESHOLD_RANGE[1]:
                violations.append(f"延迟阈值 {threshold} 分钟超出允许范围 {THRESHOLD_RANGE[0]}~{THRESHOLD_RANGE[1]}")
        except ValueError as exc:
            violations.append(str(exc))
        times, interval = parse_escalation_policy(rule.get("升级策略"))
        if not INTERVAL_RANGE[0] <= interval <= INTERVAL_RANGE[1]:
            violations.append(f"升级间隔 {interval} 分钟超出允许范围 {INTERVAL_RANGE[0]}~{INTERVAL_RANGE[1]}")
        if not TIMES_RANGE[0] <= times <= TIMES_RANGE[1]:
            violations.append(f"连续超限次数 {times} 超出允许范围 {TIMES_RANGE[0]}~{TIMES_RANGE[1]}")
        promise: datetime | None = None
        try:
            promise = parse_moment(baseline.get("承诺时间"), reference=snapshot)
        except ValueError:
            violations.append(f"承诺时间「{baseline.get('承诺时间')}」无法按本地时区解析")
        latest_output: datetime | None = None
        raw_output = str(baseline.get("最近产出") or "").strip()
        if raw_output:
            try:
                latest_output = parse_moment(raw_output, reference=snapshot)
            except ValueError:
                violations.append(f"最近产出「{raw_output}」无法按本地时区解析")
        if violations or promise is None or threshold is None:
            return None, violations
        if roll_promise and latest_output is not None:
            while promise <= latest_output:
                promise += timedelta(days=1)  # 产出推进后，承诺时间按日历向前滚动（覆盖跨日/跨月/跨年）
        warn, escalate, deadline = compute_window_times(promise, threshold, times, interval)
        if not warn < deadline:
            return None, ["预警起点不早于最晚处置时间，属于退化窗口，不允许保存"]
        return {
            "承诺时间": promise,
            "预警起点": warn,
            "升级时刻": escalate,
            "最晚处置时间": deadline,
            "延迟阈值分钟": threshold,
            "升级次数": times,
            "升级间隔分钟": interval,
            "最近产出": latest_output,
        }, []

    def _new_window(
        self,
        rule: dict[str, Any],
        baseline: dict[str, Any],
        parts: dict[str, Any],
        key: str,
        snapshot: datetime,
        rivals: list[tuple[datetime, int, dict[str, Any]]],
        *,
        force_breach: bool = False,
    ) -> dict[str, Any]:
        self._window_seq += 1
        code = f"RW-{self._window_seq:04d}"
        if len(rivals) > 1:
            codes = "、".join(str(item[2].get("基线编号")) for item in rivals)
            note = f"{codes} 指向同一监控对象，按更早承诺时间与更严格阈值共同裁决，选中 {baseline.get('基线编号')}"
        else:
            note = f"唯一基线 {baseline.get('基线编号')}，无需裁决"
        window = {
            "id": self._window_seq,
            "窗口编号": code,
            "去重键": key,
            "规则编号": rule.get("规则编号"),
            "基线编号": baseline.get("基线编号"),
            "监控对象": rule.get("监控对象"),
            "通知渠道": rule.get("通知渠道"),
            "风险等级": rule.get("告警级别"),
            "承诺时间": iso(parts["承诺时间"]),
            "延迟阈值分钟": parts["延迟阈值分钟"],
            "升级次数": parts["升级次数"],
            "升级间隔分钟": parts["升级间隔分钟"],
            "预警起点": iso(parts["预警起点"]),
            "升级时刻": iso(parts["升级时刻"]),
            "最晚处置时间": iso(parts["最晚处置时间"]),
            "最近产出": iso(parts["最近产出"]) if parts["最近产出"] else None,
            "最近产出原文": str(baseline.get("最近产出") or ""),
            "状态": "预警中",
            "时间快照": iso(snapshot),
            "口径版本": CALIBER,
            "裁决说明": note,
            "判定标准": JUDGMENT_TEXT,
            "事件轨迹": [],
            "关闭时间": None,
            "关闭依据": None,
            "升级记录": None,
        }
        if force_breach:
            breached = True
        elif parts["最近产出"] is not None:
            breached = parts["最近产出"] > parts["预警起点"]
        else:
            breached = snapshot >= parts["预警起点"]
        if breached:
            window["状态"] = "已超限"
            record, _ = self._acquire_locked(key, code, snapshot, source="风险窗口推演")
            window["升级记录"] = record
        self._windows.append(window)
        self._sync_baseline(baseline, window)
        self._sync_dag(baseline, window)
        return window

    # ---------- 去重键与升级额度 ----------

    @staticmethod
    def _dedup_key(rule_code: str, monitor: str, promise: datetime) -> str:
        """稳定去重键：同一监控对象在同一承诺日期内不变。"""
        digest = hashlib.sha1(f"{rule_code}|{monitor}|{promise.date().isoformat()}".encode("utf-8")).hexdigest()[:8]
        return f"DK-{rule_code}-{promise:%Y%m%d}-{digest}"

    def _active_window_by_key(self, key: str) -> dict[str, Any] | None:
        for window in reversed(self._windows):
            if window["去重键"] == key and window["状态"] in ACTIVE_STATES:
                return window
        return None

    def _active_window_by_rule(self, rule_code: str) -> dict[str, Any] | None:
        for window in reversed(self._windows):
            if window["规则编号"] == rule_code and window["状态"] in ACTIVE_STATES:
                return window
        return None

    def _acquire_locked(
        self, key: str, window_code: str, snapshot: datetime, *, source: str
    ) -> tuple[dict[str, Any] | None, str]:
        """升级额度按去重键占用：已被占用时后到操作不被接受，窗口内只保留一条有效升级记录。"""
        active = self._escalations.get(key)
        if active and active["状态"] == "占用中":
            return None, f"升级额度已被 {active['升级编号']} 占用，后到操作不被接受"
        self._escalation_seq += 1
        record = {
            "id": self._escalation_seq,
            "升级编号": f"ESC-{self._escalation_seq:04d}",
            "去重键": key,
            "窗口编号": window_code,
            "占用时间": iso(snapshot),
            "状态": "占用中",
            "释放时间": None,
            "来源": source,
        }
        self._escalations[key] = record
        return record, "升级额度占用成功"

    def acquire_escalation(
        self, *, window_id: int | None = None, snapshot_at: Any = None
    ) -> tuple[dict[str, Any] | None, str]:
        """手工占用升级额度：两个告警任务争抢相同去重键时只允许一个成功。"""
        snapshot = self._snapshot(snapshot_at)
        with self._lock:
            window = next((w for w in self._windows if w["id"] == window_id), None)
            if window is None:
                return None, f"风险窗口 {window_id} 不存在"
            if window["状态"] not in ACTIVE_STATES:
                return None, f"窗口 {window['窗口编号']} 已{window['状态']}，不能再占用升级额度"
            record, message = self._acquire_locked(window["去重键"], window["窗口编号"], snapshot, source="手工占用")
            if record is not None:
                window["升级记录"] = record
                if window["状态"] == "预警中":
                    window["状态"] = "已升级"
                self._sync_baseline(self._find_baseline(window["基线编号"]), window)
            return record, message

    # ---------- 异常/恢复事件 ----------

    def ingest_event(
        self, *, rule_code: str, event_time: Any, event_type: str, note: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """异常事件：首次到达生成窗口并占用升级额度，窗口内重复到达只保留一条；恢复事件写入关闭结论。"""
        rule = next((r for r in self._store.rows("sla") if str(r.get("规则编号")) == rule_code), None)
        if rule is None:
            return None, f"SLA规则 {rule_code} 不存在"
        try:
            moment = self._snapshot(event_time)
        except ValueError as exc:
            return None, str(exc)
        with self._lock:
            if event_type == "异常":
                active = self._active_window_by_rule(rule_code)
                if active is None:
                    _, window = self._derive_one(rule, moment, force_breach=True)
                    window["事件轨迹"].append({"时间": iso(moment), "类型": "异常", "说明": note or "首次超限到达"})
                    return window, "已生成风险窗口并占用升级额度"
                if active["状态"] == "预警中":
                    active["状态"] = "已超限"
                    record, _ = self._acquire_locked(active["去重键"], active["窗口编号"], moment, source="异常事件")
                    active["升级记录"] = record
                    active["事件轨迹"].append({"时间": iso(moment), "类型": "异常", "说明": note or "预警窗口升级为超限"})
                    self._sync_baseline(self._find_baseline(active["基线编号"]), active)
                    return active, "预警窗口已转为超限并占用升级额度"
                active["事件轨迹"].append({"时间": iso(moment), "类型": "异常", "说明": note or "窗口内重复到达"})
                return active, "窗口内重复到达，仅保留一条有效升级记录"
            if event_type == "恢复":
                active = self._active_window_by_rule(rule_code)
                if active is None:
                    return None, "没有处于活动状态的风险窗口可关闭"
                conclusion = note or f"{active['监控对象']} 于 {iso(moment)} 恢复产出，连续超限告警解除"
                active["事件轨迹"].append({"时间": iso(moment), "类型": "恢复", "说明": conclusion})
                self._close_locked(active, moment, conclusion)
                return active, "告警已恢复，关闭结论已写入处置记录"
            return None, f"事件类型「{event_type}」不支持，仅接受 异常/恢复"

    # ---------- 关闭与更替 ----------

    def close_window(
        self, window_id: int, *, conclusion: str, closed_at: Any = None
    ) -> tuple[dict[str, Any] | None, str]:
        """关闭窗口：必须留下可追溯的关闭依据；历史窗口保留原通知渠道与承诺时间。"""
        if not str(conclusion or "").strip():
            return None, "关闭依据不能为空，告警恢复后必须留下可追溯的关闭结论"
        with self._lock:
            window = next((w for w in self._windows if w["id"] == window_id), None)
            if window is None:
                return None, f"风险窗口 {window_id} 不存在"
            if window["状态"] == "已关闭":
                return None, f"窗口 {window['窗口编号']} 已关闭，关闭结论不可覆盖"
            if window["状态"] == "已更替":
                return None, f"窗口 {window['窗口编号']} 已被新窗口更替，无需关闭"
            try:
                moment = self._snapshot(closed_at)
            except ValueError as exc:
                return None, str(exc)
            self._close_locked(window, moment, str(conclusion).strip())
            return window, "窗口已关闭，关闭结论已写入处置记录"

    def _close_locked(self, window: dict[str, Any], moment: datetime, conclusion: str) -> None:
        window["状态"] = "已关闭"
        window["关闭时间"] = iso(moment)
        window["关闭依据"] = conclusion
        record = self._escalations.get(window["去重键"])
        if record and record["状态"] == "占用中":
            record["状态"] = "已释放"
            record["释放时间"] = iso(moment)
            record["释放原因"] = "窗口关闭"
        baseline = self._find_baseline(window["基线编号"])
        if baseline is not None:
            baseline["处置结论"] = f"{window['窗口编号']} 于 {iso(moment)} 关闭：{conclusion}"
            baseline["监控状态"] = "已恢复"
            self._sync_baseline(baseline, window)

    def _supersede(self, window: dict[str, Any], snapshot: datetime, *, reason: str) -> None:
        """旧窗口按原口径保留：承诺时间、通知渠道等原始字段一律不改写。"""
        window["状态"] = "已更替"
        window["更替时间"] = iso(snapshot)
        window["更替说明"] = reason
        record = self._escalations.get(window["去重键"])
        if record and record["状态"] == "占用中":
            record["状态"] = "已释放"
            record["释放时间"] = iso(snapshot)
            record["释放原因"] = "窗口更替"

    # ---------- 基线产出推进 ----------

    def advance_baseline(
        self, baseline_code: str, *, new_output: Any, snapshot_at: Any = None
    ) -> tuple[dict[str, Any] | None, str]:
        """基线最近产出向前推进：旧窗口保留原始承诺时间，新窗口按新规则重新核算首次预警时刻。"""
        with self._lock:
            baseline = next(
                (b for b in self._store.rows("baseline") if str(b.get("基线编号")) == baseline_code), None
            )
            if baseline is None:
                return None, f"基线 {baseline_code} 不存在"
            snapshot = self._snapshot(snapshot_at)
            try:
                parse_moment(new_output, reference=snapshot)
            except ValueError as exc:
                return None, str(exc)
            baseline["最近产出"] = str(new_output).strip()
            superseded = [
                w for w in self._windows
                if w["基线编号"] == baseline_code and w["状态"] in ACTIVE_STATES
            ]
            for window in superseded:
                self._supersede(window, snapshot, reason=f"基线最近产出推进至 {new_output}，旧窗口按原口径保留")
            created: list[dict[str, Any]] = []
            monitor = str(baseline.get("监控对象") or "").strip()
            for rule in self._store.rows("sla"):
                if str(rule.get("监控对象") or "").strip() != monitor:
                    continue
                _, payload = self._derive_one(rule, snapshot, roll_promise=True)
                if isinstance(payload, dict) and payload.get("窗口编号"):
                    created.append(payload)
            return {
                "基线编号": baseline_code,
                "最近产出": baseline["最近产出"],
                "更替窗口": [w["窗口编号"] for w in superseded],
                "新建窗口": [w["窗口编号"] for w in created],
                "时间快照": iso(snapshot),
            }, "基线最近产出已推进，旧窗口按原口径保留，新窗口已重新核算"

    # ---------- 查询 ----------

    def windows(self, *, rule_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._windows)
        if rule_code:
            rows = [w for w in rows if w["规则编号"] == rule_code]
        if status:
            rows = [w for w in rows if w["状态"] == status]
        return rows

    def escalations(self) -> list[dict[str, Any]]:
        return list(self._escalations.values())

    def queue(self, *, snapshot_at: Any = None) -> list[dict[str, Any]]:
        """处置队列：按风险优先级降序输出活动窗口。"""
        snapshot = self._snapshot(snapshot_at)
        items: list[dict[str, Any]] = []
        for window in self._windows:
            if window["状态"] not in ACTIVE_STATES:
                continue
            warn = parse_moment(window["预警起点"], reference=snapshot)
            deadline = parse_moment(window["最晚处置时间"], reference=snapshot)
            overdue = 0
            if window["状态"] in ("已超限", "已升级"):
                overdue = max(0, int((snapshot - warn).total_seconds() // 60))
            remaining = int((deadline - snapshot).total_seconds() // 60)
            score = LEVEL_BASE_SCORE.get(str(window.get("风险等级")), 30)
            score += min(overdue, 120)
            score += 30 - min(max(remaining, 0), 30)
            record = window.get("升级记录") or {}
            items.append({
                "窗口编号": window["窗口编号"],
                "去重键": window["去重键"],
                "规则编号": window["规则编号"],
                "基线编号": window["基线编号"],
                "监控对象": window["监控对象"],
                "风险等级": window["风险等级"],
                "状态": window["状态"],
                "预警起点": window["预警起点"],
                "最晚处置时间": window["最晚处置时间"],
                "剩余分钟": remaining,
                "已超限分钟": overdue,
                "通知渠道": window["通知渠道"],
                "升级编号": record.get("升级编号"),
                "优先级分": score,
                "id": window["id"],
            })
        items.sort(key=lambda item: (-item["优先级分"], item["最晚处置时间"]))
        return items

    # ---------- 同步 ----------

    def _find_baseline(self, code: Any) -> dict[str, Any] | None:
        return next((b for b in self._store.rows("baseline") if str(b.get("基线编号")) == str(code)), None)

    def _sync_baseline(self, baseline: dict[str, Any] | None, window: dict[str, Any]) -> None:
        """处理结果同步到基线配置清单：窗口摘要、监控状态与时间快照写回基线行。"""
        if baseline is None:
            return
        baseline["风险窗口"] = f"{window['窗口编号']} {window['状态']} {window['预警起点']}~{window['最晚处置时间']}"
        baseline["监控状态"] = window["状态"] if window["状态"] in ACTIVE_STATES else baseline.get("监控状态")
        baseline["风险窗口快照"] = window["时间快照"]

    def _sync_dag(self, baseline: dict[str, Any], window: dict[str, Any]) -> None:
        """相邻业务入口：关键路径上的DAG节点同步显示告警来源路径，多路告警并存展示。"""
        nodes = set(re.findall(r"DAG-\d+", str(baseline.get("关键路径") or "")))
        if not nodes:
            return
        source = f"{window['规则编号']}/{window['基线编号']}/{window['窗口编号']}"
        for row in self._store.rows("dag"):
            if str(row.get("节点编号")) not in nodes:
                continue
            existing = [item for item in str(row.get("告警来源路径") or "").split("、") if item]
            if source not in existing:
                existing.append(source)
            row["告警来源路径"] = "、".join(existing)

    # ---------- 工具 ----------

    def _snapshot(self, raw: Any) -> datetime:
        if raw in (None, ""):
            return datetime.now().astimezone()
        return parse_moment(raw, reference=datetime.now().astimezone())


risk_engine = RiskWindowEngine()

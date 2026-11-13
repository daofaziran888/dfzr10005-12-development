"""SLA风险窗口推演的口径测试：窗口计算、去重、裁决、并发占用、关闭结论与边界场景。"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime

from app.services.sla_risk import (
    CALIBER,
    RiskWindowEngine,
    compute_window_times,
    parse_moment,
)
from app.store import Store

SNAP = "2026-11-13 08:20"


def make_engine() -> RiskWindowEngine:
    return RiskWindowEngine(data_store=Store())


def only_window(engine: RiskWindowEngine, rule_code: str) -> dict:
    rows = [w for w in engine.windows() if w["规则编号"] == rule_code]
    assert len(rows) == 1, f"期望 {rule_code} 只有一条窗口，实际 {len(rows)} 条"
    return rows[0]


class WindowMathTest(unittest.TestCase):
    """SLA-0001 ↔ BASL-0001：08:00 基线、15 分钟阈值、两次连续超限的升级间隔。"""

    def test_sla0001_window_math(self):
        engine = make_engine()
        result = engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        self.assertEqual(len(result["新建"]), 1, result)
        window = result["新建"][0]
        self.assertEqual(window["基线编号"], "BASL-0001")
        self.assertEqual(window["监控对象"], "dws_trade_daily")
        self.assertEqual(window["延迟阈值分钟"], 15)
        self.assertEqual(window["升级次数"], 2)
        self.assertEqual(window["升级间隔分钟"], 10)
        self.assertEqual(window["预警起点"][11:16], "08:15")  # 08:00 + 15分钟
        self.assertEqual(window["升级时刻"][11:16], "08:25")  # 首次预警 + (2-1)×10分钟
        self.assertEqual(window["最晚处置时间"][11:16], "08:35")  # 首次预警 + 2×10分钟
        # 最近产出 08:18 晚于预警起点 08:15 → 判定超限并占用升级额度
        self.assertEqual(window["状态"], "已超限")
        self.assertIsNotNone(window["升级记录"])
        # 处置记录要素：监控对象、通知渠道、风险等级、关闭依据位
        self.assertEqual(window["通知渠道"], "电话+短信")
        self.assertEqual(window["风险等级"], "高")
        self.assertIn("关闭依据", window)
        self.assertEqual(window["口径版本"], CALIBER)

    def test_derive_rejects_unsavable_windows(self):
        engine = make_engine()
        sla_rows = engine._store.rows("sla")
        sla_rows[0]["延迟阈值"] = "0分钟"  # 低于允许范围
        sla_rows[1]["延迟阈值"] = "2000分钟"  # 高于允许范围
        sla_rows[2]["监控对象"] = ""  # 监控对象为空
        result = engine.derive(snapshot_at=SNAP)
        self.assertEqual(len(result["新建"]), 0)
        self.assertEqual(len(result["拒绝"]), 3)
        reasons = "；".join("、".join(item["原因"]) for item in result["拒绝"])
        self.assertIn("超出允许范围", reasons)
        self.assertIn("监控对象为空", reasons)
        self.assertEqual(engine.windows(), [])  # 不允许保存的窗口不落库
        meta = engine.rules_meta()
        self.assertIn("不允许保存的风险窗口", meta)
        self.assertEqual(meta["允许范围"]["延迟阈值分钟"], [1, 1440])


class DedupAndAdjudicationTest(unittest.TestCase):
    """稳定去重键、重复到达去重、多基线冲突裁决。"""

    def test_dedup_key_stable_and_repeat_arrival_merged(self):
        engine = make_engine()
        first = engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        second = engine.derive(snapshot_at="2026-11-13 08:25", rule_code="SLA-0001")
        self.assertEqual(len(second["重复"]), 1)
        self.assertEqual(len(second["新建"]), 0)
        window = only_window(engine, "SLA-0001")
        self.assertEqual(first["新建"][0]["去重键"], window["去重键"])
        self.assertTrue(window["去重键"].startswith("DK-SLA-0001-20261113-"))
        # 窗口内重复到达只保留一条有效升级记录
        active = [e for e in engine.escalations() if e["状态"] == "占用中"]
        self.assertEqual(len(active), 1)

    def test_conflict_adjudicated_by_earlier_promise(self):
        engine = make_engine()
        # BASL-0002(09:30) 与 BASL-0004(10:15) 指向同一监控对象 → 更早承诺时间胜出
        result = engine.derive(snapshot_at="2026-11-13 10:20", rule_code="SLA-0002")
        window = result["新建"][0]
        self.assertEqual(window["基线编号"], "BASL-0002")
        self.assertIn("共同裁决", window["裁决说明"])
        self.assertIn("BASL-0004", window["裁决说明"])

    def test_conflict_tie_broken_by_stricter_threshold(self):
        engine = make_engine()
        baselines = engine._store.rows("baseline")
        for row in baselines:
            if row["基线编号"] == "BASL-0004":
                row["承诺时间"] = "09:30"  # 与 BASL-0002 相同承诺时间
                row["延迟阈值"] = "10分钟"  # 更严格阈值
        result = engine.derive(snapshot_at="2026-11-13 10:20", rule_code="SLA-0002")
        window = result["新建"][0]
        self.assertEqual(window["基线编号"], "BASL-0004")
        self.assertEqual(window["延迟阈值分钟"], 30)  # 阈值口径仍取SLA规则


class EscalationConcurrencyTest(unittest.TestCase):
    """两个告警任务并发争抢相同升级额度和去重键时只允许一个成功。"""

    def test_late_acquire_not_accepted(self):
        engine = make_engine()
        engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")  # 推演已占用额度
        window = only_window(engine, "SLA-0001")
        record, message = engine.acquire_escalation(window_id=window["id"])
        self.assertIsNone(record)
        self.assertIn("后到操作不被接受", message)

    def test_concurrent_acquire_only_one_wins(self):
        engine = make_engine()
        # SLA-0003 在 17:00 快照下未到预警起点，窗口为预警中、额度空闲
        engine.derive(snapshot_at="2026-11-13 17:00", rule_code="SLA-0003")
        window = only_window(engine, "SLA-0003")
        self.assertEqual(window["状态"], "预警中")
        outcomes: list[tuple[bool, str]] = []

        def occupy():
            record, message = engine.acquire_escalation(window_id=window["id"])
            outcomes.append((record is not None, message))

        threads = [threading.Thread(target=occupy) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        successes = [ok for ok, _ in outcomes if ok]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(sum(1 for ok, _ in outcomes if not ok), 1)
        occupied = [e for e in engine.escalations() if e["状态"] == "占用中"]
        self.assertEqual(len(occupied), 1)


class Sla0003EventFlowTest(unittest.TestCase):
    """SLA-0003：17:45、18:05、18:20 三次异常只留一条活动升级记录，18:35 恢复写关闭结论。"""

    def test_event_flow(self):
        engine = make_engine()
        first, msg1 = engine.ingest_event(rule_code="SLA-0003", event_time="2026-11-13 17:45", event_type="异常")
        self.assertIsNotNone(first, msg1)
        # 首次预警按 17:45 计算（承诺 17:30 + 15 分钟阈值）
        self.assertEqual(first["预警起点"][11:16], "17:45")
        self.assertEqual(first["状态"], "已超限")
        second, msg2 = engine.ingest_event(rule_code="SLA-0003", event_time="2026-11-13 18:05", event_type="异常")
        third, msg3 = engine.ingest_event(rule_code="SLA-0003", event_time="2026-11-13 18:20", event_type="异常")
        self.assertIn("重复到达", msg2)
        self.assertIn("重复到达", msg3)
        # 风险队列只保留一条活动升级记录
        queue = engine.queue(snapshot_at="2026-11-13 18:25")
        self.assertEqual(len([q for q in queue if q["规则编号"] == "SLA-0003"]), 1)
        self.assertEqual(second["窗口编号"], first["窗口编号"])
        self.assertEqual(third["窗口编号"], first["窗口编号"])
        occupied = [e for e in engine.escalations() if e["状态"] == "占用中"]
        self.assertEqual(len(occupied), 1)
        # 18:35 恢复 → 关闭结论可追溯
        closed, msg4 = engine.ingest_event(rule_code="SLA-0003", event_time="2026-11-13 18:35", event_type="恢复")
        self.assertIsNotNone(closed, msg4)
        self.assertEqual(closed["状态"], "已关闭")
        self.assertEqual(closed["关闭时间"][11:16], "18:35")
        self.assertIn("恢复", closed["关闭依据"])
        # 历史窗口保留原通知渠道和承诺时间
        self.assertEqual(closed["通知渠道"], "短信")
        self.assertEqual(closed["承诺时间"][11:16], "17:30")
        # 升级额度已释放，处置结论同步到基线配置清单
        self.assertEqual(engine.escalations()[0]["状态"], "已释放")
        baseline = next(b for b in engine._store.rows("baseline") if b["基线编号"] == "BASL-0003")
        self.assertIn("关闭", baseline["处置结论"])
        self.assertEqual(len(engine.windows(rule_code="SLA-0003")), 1)

    def test_close_requires_traceable_conclusion(self):
        engine = make_engine()
        engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        window = only_window(engine, "SLA-0001")
        rejected, message = engine.close_window(window["id"], conclusion="")
        self.assertIsNone(rejected)
        self.assertIn("关闭依据", message)
        closed, _ = engine.close_window(window["id"], conclusion="产出已补齐，告警解除", closed_at="2026-11-13 08:40")
        self.assertEqual(closed["状态"], "已关闭")
        self.assertEqual(closed["关闭依据"], "产出已补齐，告警解除")
        again, message = engine.close_window(window["id"], conclusion="重复关闭")
        self.assertIsNone(again)
        self.assertIn("不可覆盖", message)


class BoundaryAndTimezoneTest(unittest.TestCase):
    """窗口边界覆盖跨日、跨月、跨年；起止时刻按本地时区解释并保留原始偏移。"""

    def test_cross_day_month_year(self):
        ref = parse_moment("2026-11-13 12:00", reference=datetime.now().astimezone())
        warn, _, _ = compute_window_times(parse_moment("23:50", reference=ref), 20, 2, 10)
        self.assertEqual((warn.day, warn.hour, warn.minute), (14, 0, 10))  # 跨日
        warn, _, _ = compute_window_times(parse_moment("2026-01-31 23:50", reference=ref), 20, 2, 10)
        self.assertEqual((warn.month, warn.day, warn.hour, warn.minute), (2, 1, 0, 10))  # 跨月
        warn, _, _ = compute_window_times(parse_moment("2025-12-31 23:55", reference=ref), 10, 2, 10)
        self.assertEqual((warn.year, warn.month, warn.day, warn.minute), (2026, 1, 1, 5))  # 跨年

    def test_offset_preserved_and_naive_interpreted_local(self):
        ref = datetime.now().astimezone()
        aware = parse_moment("2026-11-13T08:00:00+08:00", reference=ref)
        self.assertEqual(aware.utcoffset().total_seconds(), 8 * 3600)
        self.assertIn("+08:00", aware.isoformat())
        naive = parse_moment("2026-11-13 08:00", reference=ref)
        self.assertIsNotNone(naive.tzinfo)  # 按本地时区解释
        self.assertEqual((naive.hour, naive.minute), (8, 0))
        clock = parse_moment("08:00", reference=ref)
        self.assertEqual((clock.year, clock.month, clock.day), (ref.year, ref.month, ref.day))

    def test_window_keeps_original_offset(self):
        engine = make_engine()
        baseline = next(b for b in engine._store.rows("baseline") if b["基线编号"] == "BASL-0001")
        baseline["承诺时间"] = "2026-11-13T08:00:00+08:00"
        result = engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        window = result["新建"][0]
        self.assertIn("+08:00", window["承诺时间"])
        self.assertIn("+08:00", window["预警起点"])


class AdvanceAndSnapshotTest(unittest.TestCase):
    """基线最近产出推进、同一份时间快照、处理结果同步。"""

    def test_advance_rolls_promise_and_keeps_old_window(self):
        engine = make_engine()
        engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        old = only_window(engine, "SLA-0001")
        old_promise = old["承诺时间"]
        result, message = engine.advance_baseline(
            "BASL-0001", new_output="2026-11-14 07:55", snapshot_at="2026-11-14 07:56"
        )
        self.assertIsNotNone(result, message)
        self.assertEqual(result["更替窗口"], [old["窗口编号"]])
        # 旧窗口保留原始承诺时间与原口径
        self.assertEqual(old["状态"], "已更替")
        self.assertEqual(old["承诺时间"], old_promise)
        self.assertEqual(old["承诺时间"][5:10], "11-13")
        # 新窗口重新计算首次预警时刻（承诺时间滚动到 11-14 08:00）
        windows = engine.windows(rule_code="SLA-0001")
        self.assertEqual(len(windows), 2)
        new = [w for w in windows if w["状态"] != "已更替"][0]
        self.assertEqual(new["承诺时间"][5:10], "11-14")
        self.assertEqual(new["预警起点"][11:16], "08:15")
        self.assertEqual(new["口径版本"], CALIBER)
        self.assertNotEqual(new["去重键"], old["去重键"])

    def test_same_snapshot_shared_by_sla_and_baseline(self):
        engine = make_engine()
        result = engine.derive(snapshot_at="2026-11-13 10:20")
        snapshots = {w["时间快照"] for w in result["新建"]}
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots.pop()
        for row in engine._store.rows("baseline"):
            if "风险窗口快照" in row:
                self.assertEqual(row["风险窗口快照"], snapshot)

    def test_results_synced_to_baseline_and_dag(self):
        engine = make_engine()
        engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        baseline = next(b for b in engine._store.rows("baseline") if b["基线编号"] == "BASL-0001")
        window = only_window(engine, "SLA-0001")
        self.assertIn(window["窗口编号"], baseline["风险窗口"])
        self.assertEqual(baseline["监控状态"], "已超限")
        dag = {row["节点编号"]: row for row in engine._store.rows("dag")}
        source = f"SLA-0001/BASL-0001/{window['窗口编号']}"
        self.assertEqual(dag["DAG-0001"]["告警来源路径"], source)
        self.assertEqual(dag["DAG-0002"]["告警来源路径"], source)
        self.assertNotIn("告警来源路径", dag["DAG-0003"])


class QueuePriorityTest(unittest.TestCase):
    """处置队列按风险优先级排序，关闭后退出队列。"""

    def test_queue_sorted_by_priority(self):
        engine = make_engine()
        engine.derive(snapshot_at="2026-11-13 10:20")
        queue = engine.queue(snapshot_at="2026-11-13 10:20")
        self.assertGreaterEqual(len(queue), 2)
        scores = [item["优先级分"] for item in queue]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 高等级且已超限的 SLA-0001 排在中等级的 SLA-0002 之前
        top = queue[0]
        self.assertEqual(top["规则编号"], "SLA-0001")
        self.assertEqual(top["风险等级"], "高")
        self.assertGreater(top["已超限分钟"], 0)

    def test_closed_window_leaves_queue(self):
        engine = make_engine()
        engine.derive(snapshot_at=SNAP, rule_code="SLA-0001")
        window = only_window(engine, "SLA-0001")
        engine.close_window(window["id"], conclusion="产出已补齐", closed_at="2026-11-13 08:40")
        queue = engine.queue(snapshot_at="2026-11-13 08:41")
        self.assertEqual([q for q in queue if q["规则编号"] == "SLA-0001"], [])


if __name__ == "__main__":
    unittest.main()

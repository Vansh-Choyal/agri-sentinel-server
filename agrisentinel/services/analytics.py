"""Streaming aggregation: bounded memory and indexed time scans, no invented samples."""
import math
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from ..models import Alert, Inspection, METRICS, Robot, Telemetry, Zone, db, iso, utcnow
from ..validation import Invalid, timestamp


def period_bounds(args, report=False):
    period = args.get("period", "daily" if report else "24h")
    hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "daily": 24, "weekly": 168, "monthly": 720}
    if period not in hours:
        raise Invalid("Invalid period")
    end = timestamp(args["to"], "to") if args.get("to") else utcnow()
    start = timestamp(args["from"], "from") if args.get("from") else end - timedelta(hours=hours[period])
    if start >= end or end - start > timedelta(days=31):
        raise Invalid("Time range must be positive and no longer than 31 days")
    return start, end, period


class Accumulator:
    def __init__(self):
        self.metrics = {key: {"count": 0, "sum": 0., "min": None, "max": None, "sin": 0., "cos": 0.} for key in METRICS}
        self.count = 0
        self.states = defaultdict(int)
        self.sources = defaultdict(int)

    def add(self, row):
        self.count += 1
        self.states[row.state] += 1
        self.sources[row.source] += 1
        for key, stat in self.metrics.items():
            value = getattr(row, key)
            if value is None:
                continue
            stat["count"] += 1
            stat["sum"] += value
            stat["min"] = value if stat["min"] is None else min(value, stat["min"])
            stat["max"] = value if stat["max"] is None else max(value, stat["max"])
            if key == "heading_deg":
                stat["sin"] += math.sin(math.radians(value))
                stat["cos"] += math.cos(math.radians(value))

    def summary(self):
        result = {}
        for key, stat in self.metrics.items():
            count = stat["count"]
            average = stat["sum"] / count if count else None
            if key == "heading_deg" and count:
                average = math.degrees(math.atan2(stat["sin"], stat["cos"])) % 360 if math.hypot(stat["sin"], stat["cos"]) > 1e-8 else None
            result[key] = {"count": count, "min": stat["min"], "max": stat["max"], "avg": round(average, 3) if average is not None else None}
        return result


def aggregate(args, report=False):
    from .demo import profile
    start, end, period = period_bounds(args, report)
    zone_ids = args.get("zones", "")
    zones = zone_ids.split(",") if zone_ids else []
    if len(zones) > 12:
        raise Invalid("Compare no more than 12 zones")
    robot_id = args.get("robot_id")
    statement = select(Telemetry).where(Telemetry.recorded_at >= start, Telemetry.recorded_at < end)
    if zones:
        statement = statement.where(Telemetry.zone_id.in_(zones))
    if robot_id:
        statement = statement.where(Telemetry.robot_id == robot_id)
    total = Accumulator()
    by_zone = defaultdict(Accumulator)
    by_robot = defaultdict(Accumulator)
    buckets = defaultdict(Accumulator)
    seconds = max(1, math.ceil((end - start).total_seconds() / 90))
    for row in db.session.scalars(statement.execution_options(yield_per=1000)):
        total.add(row)
        by_zone[row.zone_id].add(row)
        by_robot[row.robot_id].add(row)
        index = min(89, int((row.recorded_at - start).total_seconds() // seconds))
        buckets[(row.zone_id, index)].add(row)
    points = []
    # Include empty buckets as null to expose gaps instead of joining stale samples.
    for zid in sorted(by_zone):
        for index in range(90):
            at = start + timedelta(seconds=index * seconds)
            if at >= end:
                break
            acc = buckets.get((zid, index))
            stats = acc.summary() if acc else {}
            points.append({"zone_id": zid, "at": iso(at), "count": acc.count if acc else 0, **{key: stats[key]["avg"] if acc else None for key in METRICS}})
    result = {"from": iso(start), "to": iso(end), "period": period, "bucket_seconds": seconds, "record_count": total.count, "statistics": total.summary(), "points": points, "zones": [{"id": zid, "name": db.session.get(Zone, zid).name, "crop": db.session.get(Zone, zid).crop, "area_ha": db.session.get(Zone, zid).area_ha, "count": acc.count, "sources": dict(acc.sources), "statistics": acc.summary()} for zid, acc in sorted(by_zone.items())], "robots": [{"id": rid, "name": db.session.get(Robot, rid).name, "records": acc.count, "state_samples": dict(acc.states), "sources": dict(acc.sources)} for rid, acc in sorted(by_robot.items())], "sources": dict(total.sources)}
    result["demo"] = profile() if total.sources.get("demo") else None
    if report:
        alert_query = select(Alert).where(Alert.opened_at < end, db.or_(Alert.resolved_at.is_(None), Alert.resolved_at >= start))
        inspection_query = select(Inspection).where(Inspection.inspected_at >= start, Inspection.inspected_at < end)
        if zones:
            alert_query = alert_query.where(Alert.zone_id.in_(zones))
            inspection_query = inspection_query.where(Inspection.zone_id.in_(zones))
        if robot_id:
            alert_query = alert_query.where(Alert.robot_id == robot_id)
            inspection_query = inspection_query.where(Inspection.robot_id == robot_id)
        from .serialization import alert_dict
        result["alert_count"] = db.session.scalar(select(db.func.count()).select_from(alert_query.subquery()))
        result["alerts"] = [alert_dict(a) for a in db.session.scalars(alert_query.order_by(Alert.opened_at.desc()).limit(200))]
        result["inspection_count"] = db.session.scalar(select(db.func.count()).select_from(inspection_query.subquery()))
        result["inspections"] = [{"robot_id": i.robot_id, "zone_id": i.zone_id, "checkpoint_id": i.checkpoint_id, "at": iso(i.inspected_at)} for i in db.session.scalars(inspection_query.order_by(Inspection.inspected_at.desc()).limit(100))]
    return result

"""当班质量漂移追查：异常归因、留样复测计划与出货处置建议。

只读交接资料，不改写既成事实（实际冻结/放行决定、留样销毁记录）。
上限按参考规程（old）口径解释；新规程读数须同一样本配对或在共同支持内
换算后再比较，超出共同支持不换算。交接资料的规程编号为 old / new。
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from inspect_inputs import load_tables

REF_METHOD = "old"   # 上限的参考口径
ALT_METHOD = "new"   # 逐步切换中的新规程

CLEAR = "未见异常"
EXPLAINED = "已定因：检测规程差异"
PENDING = "待复测：多解释并存"

STANDS = "成立"
RULED_OUT = "不成立"
OPEN = "不能排除"
UNKNOWN = "无法确认"

REASON_CALIBRATION = "检测口径站住了"
REASON_ATTRIBUTION = "来料归属变化"
REASON_BOUNDARY = "异常仍跨不过批次界限"

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_int(text):
    """解析阿拉伯或中文小整数（0–99），失败返回 None。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in CN_NUM:
        return CN_NUM[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = CN_NUM.get(left, 1) if left else 1
        ones = CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def fmt3(value):
    return f"{value:.3f}"


# ---------------------------------------------------------------- 配置

@dataclass
class Policy:
    routine_kind: str
    limits: dict
    baseline_rounds: int
    max_retests: int


def load_policy(folder):
    raw = json.loads((Path(folder) / "policy.json").read_text(encoding="utf-8"))
    window = raw.get("质量时间窗", "")
    match = re.search(r"近([0-9零一二两三四五六七八九十]+)轮", window)
    rounds = cn_int(match.group(1)) if match else None
    resource = raw.get("复测资源", "")
    match = re.search(r"最多选([0-9零一二两三四五六七八九十]+)份", resource)
    budget = cn_int(match.group(1)) if match else None
    return Policy(
        routine_kind=raw.get("常规抽样类型", "random"),
        limits={g: float(v) for g, v in raw.get("质量上限比例", {}).items()},
        baseline_rounds=rounds or 3,
        max_retests=budget or 0,
    )


# ---------------------------------------------------------------- 领域对象

@dataclass
class Reading:
    read: str
    method: str
    fraction: float
    recorded: date
    authorized: bool


@dataclass
class Sample:
    sample: str
    batch: str
    grade: str
    kind: str
    sampled: date
    retain_until: date | None
    disposal: date | None
    readings: list = field(default_factory=list)

    def authorized(self, method=None):
        return [r for r in self.readings
                if r.authorized and (method is None or r.method == method)]

    def value(self, method):
        """同一留样的重复测量取均值，仍算一份样本，不算两份独立来料。"""
        hits = self.authorized(method)
        if not hits:
            return None
        return sum(r.fraction for r in hits) / len(hits)

    def retained_on(self, day):
        return (self.disposal is None and self.retain_until is not None
                and self.retain_until >= day)


@dataclass
class MixRow:
    lot: str
    kg_min: float
    kg_max: float


@dataclass
class Batch:
    batch: str
    line: str
    processed: date
    inputs: list = field(default_factory=list)


@dataclass
class Lot:
    lot: str
    supplier: str
    grade: str
    in_kg: float
    intake: date


@dataclass
class GradeShift:
    grade: str
    pairs: list          # (sample, old_value, new_value)
    diff: float          # 新规程均值 − 参考规程均值
    tol: float           # 配对残差上限，作测量误差代理
    old_range: tuple
    new_range: tuple


def build_domain(tables, as_of):
    """按 as_of 截断：之后的读数、留样、决定一律当作尚未发生。"""
    lots = {r["lot"]: Lot(r["lot"], r["supplier"], r["grade"],
                          float(r["in_kg"]), date.fromisoformat(r["intake"]))
            for r in tables["feed_lots"]}
    batches = {}
    for r in tables["mix"]:
        batch = batches.setdefault(
            r["batch"], Batch(r["batch"], r["line"],
                              date.fromisoformat(r["processed"])))
        batch.inputs.append(MixRow(r["input"], float(r["kg_min"]),
                                   float(r["kg_max"])))
    samples = {}
    for r in tables["retains"]:
        sampled = date.fromisoformat(r["sampled"])
        if sampled > as_of:
            continue
        samples[r["sample"]] = Sample(
            r["sample"], r["batch"], r["grade"], r["kind"], sampled,
            date.fromisoformat(r["retain_until"]) if r["retain_until"] else None,
            date.fromisoformat(r["disposal"]) if r["disposal"] else None)
    for r in tables["assays"]:
        recorded = date.fromisoformat(r["recorded"])
        if recorded > as_of or r["sample"] not in samples:
            continue
        samples[r["sample"]].readings.append(Reading(
            r["read"], r["method"], float(r["fraction"]), recorded,
            r["authorized"] == "yes"))
    shipments = {}
    for r in tables["shipments"]:
        shipments.setdefault(r["shipment"], []).append(
            {"batch": r["batch"], "outlet": r["outlet"],
             "separable": r["separable"] == "yes"})
    holds = [h for h in tables["holds"]
             if date.fromisoformat(h["effective"]) <= as_of]
    return lots, batches, samples, shipments, holds


def default_as_of(tables):
    days = []
    for name, key in (("assays", "recorded"), ("holds", "effective"),
                      ("retains", "sampled"), ("mix", "processed")):
        days += [date.fromisoformat(r[key]) for r in tables[name] if r[key]]
    return max(days)


# ---------------------------------------------------------------- 规程对比

def method_shifts(samples):
    """同一留样的新旧规程读数配对；重复测量先按样本合并，不算独立证据。"""
    by_grade = {}
    for s in samples.values():
        old, new = s.value(REF_METHOD), s.value(ALT_METHOD)
        if old is not None and new is not None:
            by_grade.setdefault(s.grade, []).append((s.sample, old, new))
    shifts = {}
    residuals_all = []
    for grade, pairs in by_grade.items():
        diffs = [new - old for _, old, new in pairs]
        diff = sum(diffs) / len(diffs)
        residuals = [abs(d - diff) for d in diffs]
        residuals_all.extend(residuals)
        shifts[grade] = GradeShift(
            grade, pairs, diff, max(residuals, default=0.0),
            (min(o for _, o, _ in pairs), max(o for _, o, _ in pairs)),
            (min(n for _, _, n in pairs), max(n for _, _, n in pairs)))
    fallback = max(residuals_all, default=0.0)
    for shift in shifts.values():
        if len(shift.pairs) < 2:
            shift.tol = fallback
    return shifts


def convert(shifts, grade, value, from_method, to_method):
    """共同支持内换算；超出该等级配对覆盖范围返回 None，不硬算。"""
    if from_method == to_method:
        return value
    shift = shifts.get(grade)
    if shift is None:
        return None
    if from_method == REF_METHOD and to_method == ALT_METHOD:
        lo, hi, delta = *shift.old_range, shift.diff
    elif from_method == ALT_METHOD and to_method == REF_METHOD:
        lo, hi, delta = *shift.new_range, -shift.diff
    else:
        return None
    if value < lo - 1e-9 or value > hi + 1e-9:
        return None
    return value + delta


def methods_on(sample):
    return sorted({r.method for r in sample.readings if r.authorized})


# ---------------------------------------------------------------- 归因

@dataclass
class Explanation:
    kind: str
    status: str
    detail: str
    evidence: list = field(default_factory=list)


@dataclass
class FlagAssessment:
    sample: Sample
    reading: Reading
    limit: float
    explanations: list = field(default_factory=list)


@dataclass
class BatchAssessment:
    batch: Batch
    flags: list = field(default_factory=list)
    targeted: list = field(default_factory=list)   # 追加抽样：仅该批处置证据
    status: str = CLEAR
    conclusion: str = ""


def baseline_samples(samples, batches, policy, batch, grade):
    """同线同级、早于当班的常规留样，按加工轮次取近 N 轮；追加抽样不进基线。"""
    rounds = sorted({b.processed for b in batches.values()
                     if b.line == batch.line and b.processed < batch.processed},
                    reverse=True)[:policy.baseline_rounds]
    picked = [s for s in samples.values()
              if s.kind == policy.routine_kind and s.grade == grade
              and batches[s.batch].line == batch.line
              and batches[s.batch].processed in rounds]
    picked.sort(key=lambda s: (batches[s.batch].processed, s.sample))
    return picked


def method_explanation(flag, shifts):
    """检测规程差异：同一样本跨规程读数优先，其次共同支持内换算。"""
    s, r, limit = flag.sample, flag.reading, flag.limit
    others = [x for x in s.readings
              if x.authorized and x.method != r.method]
    if others:
        best = min(x.fraction for x in others)
        ids = [x.read for x in others]
        if best <= limit:
            return Explanation(
                "检测规程差异", STANDS,
                f"同一留样 {s.sample} 的 {REF_METHOD} 尺度读数 {fmt3(best)} "
                f"未超上限 {fmt3(limit)}，超限来自规程口径而非实物",
                ids), True
        return Explanation(
            "检测规程差异", RULED_OUT,
            f"同一留样 {s.sample} 另一规程读数 {fmt3(best)} 同样超限",
            ids), False
    converted = convert(shifts, s.grade, r.fraction, r.method, REF_METHOD)
    if converted is not None:
        if converted <= limit:
            return Explanation(
                "检测规程差异", STANDS,
                f"共同支持内换算为参考尺度 {fmt3(converted)}，未超上限 "
                f"{fmt3(limit)}（换算证据弱于同一样本配对）",
                [r.read]), True
        return Explanation(
            "检测规程差异", RULED_OUT,
            f"共同支持内换算为参考尺度 {fmt3(converted)} 仍超上限 "
            f"{fmt3(limit)}", [r.read]), False
    shift = shifts.get(s.grade)
    span = (f"{s.grade} 级共同支持 {REF_METHOD} "
            f"{fmt3(shift.old_range[0])}–{fmt3(shift.old_range[1])} / "
            f"{ALT_METHOD} {fmt3(shift.new_range[0])}–{fmt3(shift.new_range[1])}"
            if shift else f"{s.grade} 级无跨规程配对")
    return Explanation(
        "检测规程差异", UNKNOWN,
        f"{r.method} 读数 {fmt3(r.fraction)} 超出共同支持（{span}），"
        f"不能换算；需同一留样跨规程复测才能判定", [r.read]), False


def within_line_explanation(flag, batch, base_samples, shifts):
    """线内变化：只用能落到共同尺度的基线；不可比就明说。"""
    s, limit = flag.sample, flag.limit
    tol = shifts[s.grade].tol if s.grade in shifts else 0.0
    notes, comparable = [], False
    order = [m for m in (REF_METHOD, ALT_METHOD) if m in methods_on(s)]
    for method in order:
        current = s.value(method)
        vals, incomparable = [], []
        for bs in base_samples:
            value = bs.value(method)
            if value is None:
                for other in methods_on(bs):
                    value = convert(shifts, s.grade, bs.value(other),
                                    other, method)
                    if value is not None:
                        break
            if value is None:
                incomparable.append(bs)
            else:
                vals.append((bs.sample, value))
        if incomparable:
            names = "、".join(
                f"{bs.sample}（已销毁）" if bs.disposal else bs.sample
                for bs in incomparable)
            notes.append(f"{names}的读数超出共同支持，不可换算到 "
                         f"{method} 尺度")
        if not vals:
            continue
        comparable = True
        base_txt = "、".join(f"{sid} {fmt3(v)}" for sid, v in vals)
        ceiling = max(v for _, v in vals) + tol
        if current <= limit and current <= ceiling + 1e-9:
            return Explanation(
                "线内变化", RULED_OUT,
                f"{method} 尺度基线 {base_txt}，本样 {fmt3(current)} 未超上限 "
                f"{fmt3(limit)}，且在既往范围加测量误差 {fmt3(tol)} 内",
                [sid for sid, _ in vals])
        if current <= limit:
            notes.append(f"{method} 尺度基线 {base_txt}，本样 "
                         f"{fmt3(current)} 未超上限 {fmt3(limit)}，但超出既往"
                         f"范围加误差 {fmt3(tol)}")
        else:
            notes.append(f"{method} 尺度基线 {base_txt}，本样 "
                         f"{fmt3(current)} 超上限 {fmt3(limit)} 且超出既往"
                         f"范围加误差 {fmt3(tol)}")
    if comparable:
        return Explanation("线内变化", OPEN, "；".join(notes), [])
    return Explanation("线内变化", UNKNOWN,
                       "；".join(notes) or "同线同级无常规留样基线", [])


def incoming_explanation(flag, batch, lots, batches, samples_by_batch, policy):
    """来料构成：只用单据能证实的重量范围，不把整批到料算给每条线。"""
    s, limit = flag.sample, flag.limit
    candidates, others = [], []
    for mx in batch.inputs:
        lot = lots[mx.lot]
        (candidates if lot.grade == s.grade else others).append((mx, lot))
    parts, evidence, feasible = [], [], False
    for mx, lot in candidates:
        seg = (f"{lot.lot}（{lot.supplier}，{lot.grade} 级）单据范围 "
               f"{mx.kg_min:.0f}–{mx.kg_max:.0f} kg，到料 {lot.in_kg:.0f} kg "
               f"不整批归属本线")
        prior = []
        for other in batches.values():
            if other.processed >= batch.processed:
                continue
            if not any(i.lot == lot.lot for i in other.inputs):
                continue
            for os_ in samples_by_batch.get(other.batch, []):
                if os_.kind == policy.routine_kind and os_.grade == s.grade:
                    prior += [rd for rd in os_.readings if rd.authorized]
        if not prior:
            feasible = True
            parts.append(f"{seg}：{lot.intake.isoformat()} 到料后无更早同级"
                         f"常规留样，无基线可比")
        elif all(rd.fraction <= limit for rd in prior):
            parts.append(f"{seg}：既往同级常规读数均在上限内")
            evidence += [rd.read for rd in prior]
        else:
            feasible = True
            parts.append(f"{seg}：既往同级常规读数曾超限")
            evidence += [rd.read for rd in prior]
    for mx, lot in others:
        parts.append(f"{lot.lot}（{lot.grade} 级）在本批占 {mx.kg_min:.0f}–"
                     f"{mx.kg_max:.0f} kg，{s.grade} 级试样不指向该部分")
    if not candidates:
        return Explanation("来料构成", OPEN,
                           "单据中无同级来料可归属；" + "；".join(parts), [])
    if feasible:
        head = "存在无基线或曾超限的同级来料"
        status = OPEN
    else:
        head = "同级来料既往常规证据均在上限内"
        status = RULED_OUT
    return Explanation("来料构成", status, "；".join([head] + parts), evidence)


def assess_batches(lots, batches, samples, policy, shifts):
    samples_by_batch = {}
    for s in samples.values():
        samples_by_batch.setdefault(s.batch, []).append(s)
    assessments = {}
    for key in sorted(batches, key=lambda k: (batches[k].processed, k)):
        batch = batches[key]
        found, targeted = [], []
        for s in sorted(samples_by_batch.get(key, []),
                        key=lambda s: s.sample):
            limit = policy.limits.get(s.grade)
            if limit is None:
                continue
            if s.kind != policy.routine_kind:
                hits = [r for r in s.readings
                        if r.authorized and r.fraction > limit]
                if hits:
                    targeted.append((s, hits))
                continue
            for r in s.readings:
                if r.authorized and r.fraction > limit:
                    found.append(FlagAssessment(s, r, limit))
        assessment = BatchAssessment(batch, found, targeted)
        for flag in found:
            method_expl, stands = method_explanation(flag, shifts)
            base = baseline_samples(samples, batches, policy, batch,
                                    flag.sample.grade)
            line_expl = within_line_explanation(flag, batch, base, shifts)
            feed_expl = incoming_explanation(flag, batch, lots, batches,
                                             samples_by_batch, policy)
            if stands:  # 同一留样跨规程读数站住，其余解释被覆盖
                for expl in (line_expl, feed_expl):
                    if expl.status != RULED_OUT:
                        expl.status = RULED_OUT
                        expl.detail = (f"同一留样跨规程读数已在上限内"
                                       f"（追溯号 {'、'.join(method_expl.evidence)}），"
                                       f"该解释被覆盖；原评估：{expl.detail}")
            flag.explanations = [method_expl, line_expl, feed_expl]
            flag.baseline = base
        if not found:
            assessment.status = CLEAR
            assessment.conclusion = "常规留样授权读数均在同级上限内"
        elif all(any(e.kind == "检测规程差异" and e.status == STANDS
                     for e in f.explanations)
                 and all(e.status == RULED_OUT for e in f.explanations
                         if e.kind != "检测规程差异")
                 for f in found):
            assessment.status = EXPLAINED
            assessment.conclusion = (
                "唯一解释站得住：检测规程差异；不建议仅凭此次超限停线，"
                "亦无需复测定因")
        else:
            assessment.status = PENDING
            assessment.conclusion = (
                "多解释并存：安排留样复测定因；统计相关不当作线故障证明，"
                "不建议仅凭本次超限停线")
        assessments[key] = assessment
    return assessments


# ---------------------------------------------------------------- 复测计划

@dataclass
class RetestItem:
    sample: str
    batch: str
    grade: str
    kind: str
    methods: list
    purpose: str


def plan_retests(assessments, samples, policy, as_of):
    """多解释并存时安排能区分原因的留样复测；受保留期与复测资源约束。"""
    items, notes = [], []
    pending = [a for a in assessments.values() if a.status == PENDING]
    pending.sort(key=lambda a: (a.batch.processed, a.batch.batch))
    seen = set()

    def offer(sample, purpose):
        if sample.sample in seen:
            return
        missing = [m for m in (REF_METHOD, ALT_METHOD)
                   if sample.value(m) is None]
        if not missing:
            return
        if sample.retained_on(as_of):
            seen.add(sample.sample)
            items.append(RetestItem(sample.sample, sample.batch, sample.grade,
                                    sample.kind, missing, purpose))
        else:
            why = (f"已于 {sample.disposal.isoformat()} 销毁"
                   if sample.disposal else
                   f"保留期至 {sample.retain_until} 已过")
            notes.append(f"{sample.sample}（{sample.batch}）{why}，"
                         f"无法复测；既成事实不重写")

    for a in pending:
        for flag in a.flags:
            offer(flag.sample, "同一留样补测缺失规程，判定检测差异是否成立")
        for sample, _ in a.targeted:
            offer(sample, "追加抽样处置证据的跨规程确认；结果不进常规基线")
    budget = policy.max_retests
    if budget and len(items) > budget:
        dropped = items[budget:]
        items = items[:budget]
        notes.append("复测资源上限 "
                     f"{budget} 份，暂缓：{'、'.join(i.sample for i in dropped)}")
    return items, notes


# ---------------------------------------------------------------- 出货处置

@dataclass
class SegmentAdvice:
    batch: str
    status: str
    separable: bool
    note: str


@dataclass
class ShipmentAdvice:
    shipment: str
    outlet: str
    actual: str
    action: str
    reason: str
    segments: list = field(default_factory=list)


def shipment_advices(shipments, assessments, holds):
    latest = {}
    for h in holds:
        key = h["shipment"]
        if key not in latest or h["effective"] >= latest[key]["effective"]:
            latest[key] = h
    advices = []
    for name in sorted(shipments):
        rows = shipments[name]
        outlet = rows[0]["outlet"]
        actual = latest[name]["state"] if name in latest else "无记录"
        segments, pending, evidence = [], [], []
        for row in rows:
            a = assessments.get(row["batch"])
            status = a.status if a else "无检测资料"
            if status == PENDING:
                pending.append(row["batch"])
            if a:
                for flag in a.flags:
                    for expl in flag.explanations:
                        if expl.kind == "检测规程差异":
                            evidence += expl.evidence
            segments.append(SegmentAdvice(row["batch"], status,
                                          row["separable"], ""))
        if pending:
            action = "继续暂缓"
            reason = (f"{REASON_BOUNDARY}（{'、'.join(pending)} 未定因）")
            if all(not s.separable for s in segments):
                reason += "；货批不可分割"
            for seg in segments:
                if seg.status == PENDING:
                    seg.note = "异常未定因，待留样复测"
                elif seg.separable:
                    seg.note = "该段自身证据已清，可分别拣回、独立放行"
                else:
                    seg.note = "该段自身证据已清，但货批不可分割，不能独立放行，随整批暂缓"
        elif all(s.status in (CLEAR, EXPLAINED) for s in segments):
            if actual == "released":
                action = "维持实际放行"
                reason = "实际决定为放行且资料未见需推翻的异常；实际决定不被重写"
            elif actual == "hold":
                action = "交回正常复核"
                trace = "、".join(dict.fromkeys(evidence))
                reason = (f"{REASON_CALIBRATION}（追溯号 {trace}）"
                          if trace else REASON_CALIBRATION)
            else:
                action = "正常复核"
                reason = "无实际冻结记录且未见异常"
        else:
            action = "人工复核"
            reason = "检测资料不足，不出自动建议"
        advices.append(ShipmentAdvice(name, outlet, actual, action, reason,
                                      segments))
    return advices


# ---------------------------------------------------------------- 汇总

@dataclass
class Report:
    as_of: date
    policy: Policy
    shifts: dict
    assessments: dict
    retests: list
    retest_notes: list
    advices: list
    holds: list
    samples: list
    lots: dict
    batches: dict
    shipments: dict
    notes: list


def analyze(tables, policy, as_of=None):
    as_of = as_of or default_as_of(tables)
    lots, batches, samples, shipments, holds = build_domain(tables, as_of)
    shifts = method_shifts(samples)
    assessments = assess_batches(lots, batches, samples, policy, shifts)
    retests, retest_notes = plan_retests(assessments, samples, policy, as_of)
    advices = shipment_advices(shipments, assessments, holds)
    notes = []
    for lot in lots.values():
        rows = [(b, mx) for b in batches.values() for mx in b.inputs
                if mx.lot == lot.lot]
        lo = sum(mx.kg_min for _, mx in rows)
        hi = sum(mx.kg_max for _, mx in rows)
        if hi < lot.in_kg:
            notes.append(
                f"{lot.lot} 到料 {lot.in_kg:.0f} kg，单据仅证实 "
                f"{lo:.0f}–{hi:.0f} kg 的去向；其余无单据，不并入任何线归属")
        if lo > lot.in_kg:
            notes.append(
                f"{lot.lot} 单据下限合计 {lo:.0f} kg 超过到料 "
                f"{lot.in_kg:.0f} kg，请核对过磅资料")
    return Report(as_of, policy, shifts, assessments, retests, retest_notes,
                  advices, holds, sorted(samples.values(),
                                         key=lambda s: s.sample),
                  lots, batches, shipments, notes)


# ---------------------------------------------------------------- 呈现

def render(report):
    lines = []
    add = lines.append
    add(f"质量漂移追查报告（资料截至 {report.as_of.isoformat()}）")
    add("上限按参考规程（old）口径解释；新规程读数经同一样本配对或共同支持"
        "内换算后再比较。实际冻结/放行决定与留样销毁为既成事实，本报告不重写。")
    add("")
    add("一、出货处置总览")
    for adv in report.advices:
        add(f"  {adv.shipment}（去向 {adv.outlet}）：建议{adv.action} —— "
            f"{adv.reason}；实际决定：{adv.actual}")
    add("")
    add("二、批次异常与可行解释")
    for key in sorted(report.assessments,
                      key=lambda k: (report.assessments[k].batch.processed, k)):
        a = report.assessments[key]
        b = a.batch
        inputs = "、".join(
            f"{mx.lot} {mx.kg_min:.0f}–{mx.kg_max:.0f} kg" for mx in b.inputs)
        add(f"  {key}（{b.line}，{b.processed.isoformat()} 加工；入料 {inputs}）"
            f"：{a.status}")
        for flag in a.flags:
            s, r = flag.sample, flag.reading
            add(f"    超限读数：{r.read}（留样 {s.sample}，{s.grade} 级，"
                f"{r.method} 规程 {fmt3(r.fraction)} > 上限 {fmt3(flag.limit)}）")
            for expl in flag.explanations:
                add(f"    {expl.kind}：{expl.status} —— {expl.detail}")
        for s, hits in a.targeted:
            for r in hits:
                add(f"    追加抽样：{r.read}（留样 {s.sample}，{r.method} 规程 "
                    f"{fmt3(r.fraction)}）仅作该批处置证据，不进常规基线")
        add(f"    结论：{a.conclusion}")
    add("")
    add("三、检测规程对比（授权读数；同一留样重复测量只算一份样本）")
    for grade in sorted(report.shifts):
        sh = report.shifts[grade]
        pairs = "、".join(f"{sid} {n - o:+.3f}" for sid, o, n in sh.pairs)
        thin = "；配对单薄，覆盖有限" if len(sh.pairs) < 2 else ""
        add(f"  {grade} 级 {len(sh.pairs)} 对同测留样（{pairs}）：新规程平均高 "
            f"{fmt3(sh.diff)}，配对残差 {fmt3(sh.tol)}；共同支持 "
            f"{REF_METHOD} {fmt3(sh.old_range[0])}–{fmt3(sh.old_range[1])} / "
            f"{ALT_METHOD} {fmt3(sh.new_range[0])}–{fmt3(sh.new_range[1])}{thin}")
    outside = []
    for s in report.samples:
        for r in s.readings:
            if not r.authorized:
                continue
            if convert(report.shifts, s.grade, r.fraction, r.method,
                       REF_METHOD if r.method == ALT_METHOD else ALT_METHOD) \
                    is None and r.method in (REF_METHOD, ALT_METHOD):
                outside.append(f"{r.read}（{s.sample}，{r.method} "
                               f"{fmt3(r.fraction)}）")
    if outside:
        add("  超出共同支持、不予换算的读数：" + "、".join(outside))
    add("")
    add(f"四、留样复测计划（截至 {report.as_of.isoformat()} 仍在保留期且未销毁；"
        f"本期资源最多 {report.policy.max_retests} 份，已排 "
        f"{len(report.retests)} 份）")
    if report.retests:
        for item in report.retests:
            kind = "追加抽样" if item.kind != report.policy.routine_kind \
                else "常规留样"
            add(f"  {item.sample}（{item.batch}，{item.grade} 级，{kind}）：补 "
                f"{'、'.join(item.methods)} 规程 —— {item.purpose}")
    else:
        add("  无需复测定因。")
    for note in report.retest_notes:
        add(f"  注：{note}")
    add("")
    add("五、出货分段与可分割性")
    for adv in report.advices:
        sep = "可分割" if all(s.separable for s in adv.segments) \
            else "不可分割"
        add(f"  {adv.shipment}（{adv.outlet}，{sep}）：建议{adv.action}")
        for seg in adv.segments:
            note = f" —— {seg.note}" if seg.note else ""
            add(f"    {seg.batch}：{seg.status}{note}")
    add("")
    add("六、过磅归属（只用单据能证实的范围）")
    for lot in report.lots.values():
        uses = []
        for key in sorted(report.batches):
            for mx in report.batches[key].inputs:
                if mx.lot == lot.lot:
                    uses.append(f"{key} {mx.kg_min:.0f}–{mx.kg_max:.0f} kg")
        add(f"  {lot.lot}（{lot.supplier}，{lot.grade} 级，到料 "
            f"{lot.in_kg:.0f} kg，{lot.intake.isoformat()}）→ "
            + "、".join(uses))
    for note in report.notes:
        add(f"  数据附注：{note}")
    add("")
    add("七、审计轨迹")
    for h in report.holds:
        add(f"  实际决定 {h['decision']}：{h['shipment']} {h['state']}"
            f"（{h['effective']}）——既成事实，保留不改写")
    for s in report.samples:
        if s.disposal:
            add(f"  留样销毁：{s.sample}（{s.disposal.isoformat()}）"
                f"——既成事实，保留不改写")
    add("  重新分流原因（检测口径站住了 / 来料归属变化 / 异常仍跨不过批次界限）：")
    for adv in report.advices:
        if adv.action in ("交回正常复核", "继续暂缓"):
            add(f"    {adv.shipment}：{adv.action} —— {adv.reason}")
    add(f"    来料归属变化：本次无出货因此重新分流")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).parent / "data")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="资料截止日期（YYYY-MM-DD），默认取数据中最晚活动日")
    args = parser.parse_args()
    tables = load_tables(args.data)
    report = analyze(tables, load_policy(args.data), args.as_of)
    print(render(report))


if __name__ == "__main__":
    main()

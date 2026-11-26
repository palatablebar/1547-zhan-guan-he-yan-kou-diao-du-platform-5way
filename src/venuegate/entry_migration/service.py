"""Temporary entrance closures and batch migration.

A closure event (show changeover or booth verification) produces exactly one
signable migration sheet.  Affected live batches are routed to entrances that
are still open, measured in people per half-hour window so the receiving
entrance knows how much it is taking on.  The closed entrance stops counting
those batches, so the same people are never counted twice.  Every receiving
entrance keeps one gate free as an emergency lane.

Only batches with mature calibration and no training-range excursion migrate
automatically; missing evidence or an unresolved hard rule goes to manual
review.  When a closure is released the original entrance resumes.  Pending
migrated batches return in full when the restored headroom covers the peak,
otherwise they return in waves.  Batches whose window already completed do not
return.  Re-posting the same event never adds a migration sheet.
"""

from collections import Counter, defaultdict
from dataclasses import replace
from hashlib import sha256
from json import dumps
from statistics import median

from .contracts import ClosureEvent, MigrationLine, MigrationPlan, MigrationSheet, ReturnLine

CLOSURE_KEYS = {'event', 'cohort', 'start_day', 'end_day', 'reason', 'released', 'released_day', 'capacity_after'}
MAX_EVENTS = 50
MAX_REASON = 64
SCAN_SECONDS = 8
HALF_HOUR = 30 * 60
EMERGENCY_LANES = 1  # one gate kept free at every receiving / restored entrance


def _digest(value):
    encoded = dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(encoded.encode('utf-8')).hexdigest()


def _integer(raw, label):
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f'{label}应为整数')
    return raw


def parse_closures(raw):
    """Validate closure events and fold exact duplicates.

    Re-posting the same event must not create another migration sheet, so an
    identical event is folded once.  Two different events sharing one id are a
    contradiction and refuse to be signed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError('关停事件应为一个数组')
    if len(raw) > MAX_EVENTS:
        raise ValueError(f'单次最多处理 {MAX_EVENTS} 个关停事件')
    seen = {}
    ordered = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index} 个关停事件应为对象')
        extra = set(item) - CLOSURE_KEYS
        if extra:
            raise ValueError(f'第 {index} 个关停事件包含无法识别的字段：{"、".join(sorted(extra))}')
        event_id = item.get('event')
        cohort = item.get('cohort')
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id.strip()) > 64:
            raise ValueError(f'第 {index} 个关停事件缺少有效编号')
        if not isinstance(cohort, str) or not cohort.strip() or len(cohort.strip()) > 64:
            raise ValueError(f'第 {index} 个关停事件缺少有效入口')
        reason = item.get('reason', '临时关停')
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > MAX_REASON:
            raise ValueError(f'第 {index} 个关停事件的关停原因无效')
        released = item.get('released', False)
        if type(released) is not bool:
            raise ValueError(f'第 {index} 个关停事件的解除标记应为布尔值')
        released_day = item.get('released_day')
        if released_day is not None:
            released_day = _integer(released_day, '解除日')
        capacity_after = item.get('capacity_after')
        if capacity_after is not None:
            if isinstance(capacity_after, bool) or not isinstance(capacity_after, (int, float)):
                raise ValueError('恢复后通行能力应为数值')
        event = ClosureEvent(
            event_id=event_id.strip(), cohort=cohort.strip(),
            start_day=_integer(item.get('start_day'), '关停开始日'),
            end_day=_integer(item.get('end_day'), '关停结束日'),
            reason=reason.strip(), released=released, released_day=released_day,
            capacity_after=float(capacity_after) if capacity_after is not None else None,
        )
        if event.event_id in seen:
            if seen[event.event_id] != event:
                raise ValueError(f'关停事件 {event.event_id} 重复出现且内容不一致')
            continue
        seen[event.event_id] = event
        ordered.append(event)
    ordered.sort(key=lambda event: (event.start_day, event.end_day, event.cohort, event.event_id))
    _reject_overlapping(ordered)
    return tuple(ordered)


def derive_closures(batches, declared=()):
    """Derive closure events from batches paused by an unresolved hard rule.

    Today a booth that needs verification (``BOOTH_BREAK``) only flips its batch
    to 暂停 and leaves the queue at the closed entrance.  Each contiguous run of
    paused days at one entrance becomes an implicit closure event so a migration
    sheet is still produced.  Declared events win: an implicit run that lands in
    a declared window is absorbed instead of producing a second sheet.
    """
    declared = tuple(declared)
    paused = defaultdict(list)
    for row in batches:
        if row.get('state') == '暂停' and row.get('cohort'):
            paused[row['cohort']].append(int(row['day']))
    implicit = []
    for cohort in sorted(paused):
        days = sorted(set(paused[cohort]))
        start = previous = days[0]
        runs = []
        for day in days[1:]:
            if day == previous + 1:
                previous = day
                continue
            runs.append((start, previous + 1))
            start = previous = day
        runs.append((start, previous + 1))
        for begin, end in runs:
            if any(event.cohort == cohort and event.start_day < end and begin < event.end_day
                   for event in declared):
                continue
            event_id = 'CL-AUTO-' + _digest(('paused', cohort, begin, end))[:10]
            implicit.append(ClosureEvent(
                event_id=event_id, cohort=cohort, start_day=begin, end_day=end,
                reason='硬规则未解除·备用核验口验电'))
    return tuple(sorted(tuple(declared) + tuple(implicit),
                        key=lambda event: (event.start_day, event.end_day, event.cohort, event.event_id)))


def _reject_overlapping(events):
    by_cohort = defaultdict(list)
    for event in events:
        by_cohort[event.cohort].append(event)
    for cohort, group in by_cohort.items():
        group.sort(key=lambda item: (item.start_day, item.end_day, item.event_id))
        for left, right in zip(group, group[1:]):
            if right.start_day < left.end_day:
                raise ValueError(f'入口 {cohort} 的关停事件 {left.event_id} 与 {right.event_id} 区间重叠')


def _turnstiles(rows, cohort, entrances=None):
    if entrances and cohort in entrances:
        return int(entrances[cohort])
    values = [row['turnstiles'] for row in rows
              if row['cohort'] == cohort and row.get('turnstiles') is not None]
    return int(median(values)) if values else 0


def _window_capacity(turnstiles):
    """People per half-hour window, keeping one emergency gate free.

    An entrance with no recorded gates has no confirmed throughput, so it takes
    nobody rather than a made-up default.
    """
    gates = int(turnstiles or 0)
    if gates <= EMERGENCY_LANES:
        return 0
    return (gates - EMERGENCY_LANES) * (HALF_HOUR // SCAN_SECONDS)


def _restored_capacity(event, rows, entrances=None):
    """Restored half-hour throughput at the released entrance.

    ``capacity_after`` is the usable gate count reported at release; when it is
    missing the entrance's last recorded gate count is used.
    """
    restored = event.capacity_after if event.capacity_after is not None else _turnstiles(
        rows, event.cohort, entrances)
    return int(restored or 1) * (HALF_HOUR // SCAN_SECONDS)


def _migration_allowed(batch):
    if batch['state'] == '暂停':
        return False, 'HARD_RULE_ACTIVE', '硬规则未解除，停在原口等待人工判读'
    if batch['state'] == '人工判读':
        code = batch.get('reason') or 'MANUAL_REVIEW'
        return False, code, batch.get('evidence') or '需人工判读，暂不自动迁入'
    if batch.get('missing'):
        return False, 'MISSING_EVIDENCE', '缺测证据不足，转人工核对原始单据'
    if batch.get('uncertain'):
        return False, 'IMMATURE_CALIBRATION', '校准样本未成熟，转人工判读'
    if batch.get('excursions'):
        return False, 'OUTSIDE_TRAINING_RANGE', '超出训练范围，转人工判读'
    return True, '', ''


def _sheet_id(event):
    return 'MS-' + _digest(event.identity())[:12]


def _batch(row, event_id):
    return {
        'key': row['key'], 'event_id': event_id, 'cohort': row['cohort'], 'day': row['day'],
        'estimate': float(row['estimate']), 'upper': float(row['upper']), 'cost': int(row['cost']),
        'state': row['state'], 'reason': row.get('reason', ''), 'evidence': row.get('evidence', ''),
        'missing': int(row.get('missing') or 0),
        'excursions': bool(row.get('excursions')), 'uncertain': bool(row.get('uncertain')),
    }


def _assign(lines, batch, open_targets, target_capacity, load):
    allowed, code, detail = _migration_allowed(batch)
    if not allowed:
        lines.append(MigrationLine(
            key=batch['key'], event_id=batch['event_id'], source=batch['cohort'], target=None,
            day=batch['day'], estimate=batch['estimate'], upper=batch['upper'], cost=batch['cost'],
            state='人工判读', reason=code, reserve_lane=False,
            dispatched=batch['state'] == '已分派', detail=detail))
        return
    required = batch['upper']
    choices = []
    for zone in open_targets:
        ceiling = target_capacity[zone]
        spare = ceiling - load[(zone, batch['day'])]
        if spare >= required:
            # Spread across open entrances: prefer the one with the largest remaining
            # share of its own throughput, so every entrance knows its share.
            choices.append(((spare / ceiling if ceiling else 0), spare, zone))
    if not choices:
        lines.append(MigrationLine(
            key=batch['key'], event_id=batch['event_id'], source=batch['cohort'], target=None,
            day=batch['day'], estimate=batch['estimate'], upper=batch['upper'], cost=batch['cost'],
            state='候补迁入', reason='NO_RECEIVING_HEADROOM', reserve_lane=False,
            dispatched=batch['state'] == '已分派',
            detail='开放入口余量不足，保留在原口排队并继续巡查'))
        return
    share, spare, zone = max(choices, key=lambda pair: (round(pair[0], 6), pair[1], pair[2]))
    load[(zone, batch['day'])] += required
    lines.append(MigrationLine(
        key=batch['key'], event_id=batch['event_id'], source=batch['cohort'], target=zone,
        day=batch['day'], estimate=batch['estimate'], upper=batch['upper'], cost=batch['cost'],
        state='迁入', reason='CALIBRATED_IN_RANGE', reserve_lane=True,
        dispatched=batch['state'] == '已分派',
        detail=(f'校准成熟且未越训练域，迁入 {zone} 承接 {required:.0f} 人/半小时窗'
                f'（余量 {spare:.0f} 人），接收口预留 1 条应急通道')))


def _schedule_returns(pending, retained, cohort, capacity):
    """Re-home pending batches window by window.

    Each batch belongs to one half-hour window.  A window can absorb
    ``capacity - retained`` people (the retained load is batches that could not
    migrate and are still queueing at the original entrance).  A batch that
    overflows its own window is deferred to the next later window with room; the
    windows that receive batches become the successive return waves.  A batch
    larger than a whole empty window returns alone under on-site flow limiting.

    Returns a list of ``(wave_no, line, oversized)`` in return order.
    """
    by_day = defaultdict(list)
    for line in pending:
        by_day[line.day].append(line)
    room = {}

    def available(window):
        if window not in room:
            room[window] = max(0.0, capacity - float(retained.get((cohort, window), 0.0)))
        return room[window]

    usage = []
    placed = []
    for day in sorted(by_day):
        group = sorted(by_day[day], key=lambda item: (-item.upper, item.key))
        for line in group:
            window = day
            while available(window) < line.upper and available(window) < capacity:
                window += 1
            spare = available(window)
            if line.upper <= spare:
                room[window] = spare - line.upper
                oversized = line.upper > capacity
            else:
                # Even an empty window cannot hold this batch: it goes alone and
                # the whole window is reserved for it under flow limiting.
                room[window] = 0.0
                oversized = True
            if window not in usage:
                usage.append(window)
            placed.append((window, line, oversized))
    order = {window: index for index, window in enumerate(sorted(usage), 1)}
    return [(order[window], line, oversized) for window, line, oversized in placed]


def _return_decision(event, moved, as_of, retained, capacity):
    """Full versus staged return from restored headroom and pending peak.

    Batches whose half-hour window finished before the release have already
    been handled and never return.  The pending peak window is compared with the
    restored headroom (restored throughput minus the retained, non-migrated
    load).  When the peak fits, the original entrance takes everything back at
    once; otherwise returns are scheduled window by window in waves.
    """
    if not event.released or not event.is_released_by(as_of):
        return (), '待解除', '关停尚未解除，原口不恢复承接', 0
    completed = [line for line in moved if line.day < event.released_day]
    pending = [line for line in moved if line.day >= event.released_day]
    tail = f'，{len(completed)} 条已完成不回迁' if completed else ''
    if not pending:
        return (), '无需迁回', '迁入批次均已过完成时窗，已完成的不回迁' + tail, 0
    by_day = defaultdict(list)
    for line in pending:
        by_day[line.day].append(line)
    retained_peak = max((float(retained.get((event.cohort, day), 0.0)) for day in by_day), default=0.0)
    headroom = max(0.0, capacity - retained_peak)
    note = f'，已扣除原口留存的未迁批次 {retained_peak:.0f} 人/半小时窗' if retained_peak else ''
    peak = max(sum(line.upper for line in group) for group in by_day.values())
    if peak <= headroom:
        detail = (f'恢复后余量 {headroom:.0f} 人/半小时窗覆盖待迁回峰值 {peak:.0f} 人{note}，'
                  f'立即全量迁回' + tail)
        ordered = sorted(pending, key=lambda item: (item.day, -item.upper, item.key))
        return tuple(ReturnLine(
            key=line.key, event_id=event.event_id, source=line.target or event.cohort,
            target=event.cohort, day=line.day, estimate=line.estimate, upper=line.upper,
            cost=line.cost, dispatched=line.dispatched, mode='全量迁回', wave=1,
            reason=detail) for line in ordered), '全量迁回', detail, int(peak)
    scheduled = _schedule_returns(sorted(pending, key=lambda item: (item.day, -item.upper, item.key)),
                                  retained, event.cohort, capacity)
    total = max(wave for wave, _, _ in scheduled)
    lines = []
    for wave, line, oversized in scheduled:
        if oversized:
            reason = (f'单批预计 {line.upper:.0f} 人超过恢复后余量 {headroom:.0f} 人/半小时窗{note}，'
                      f'单列一批并现场限流，第 {wave} 批迁回')
        elif wave == 1:
            reason = (f'恢复后余量 {headroom:.0f} 人/半小时窗不足以一次承接待迁回峰值 {peak:.0f} 人{note}，'
                      f'按恢复后的高峰客流分批迁回，第 {wave} 批（第 {line.day} 日窗）')
        else:
            reason = (f'第 {line.day} 日窗承接不下，顺延到后续窗，按恢复后的高峰客流第 {wave} 批迁回')
        lines.append(ReturnLine(
            key=line.key, event_id=event.event_id, source=line.target or event.cohort,
            target=event.cohort, day=line.day, estimate=line.estimate, upper=line.upper,
            cost=line.cost, dispatched=line.dispatched, mode='分批迁回', wave=wave,
            reason=reason))
    detail = (f'恢复后余量 {headroom:.0f} 人/半小时窗不足以一次承接待迁回峰值 {peak:.0f} 人{note}，'
              f'按恢复后的高峰客流分 {total} 批迁回' + tail)
    return tuple(lines), '分批迁回', detail, int(peak)


def plan_migrations(events, batches, *, as_of, entrances=None):
    """Build one migration sheet per closure event.

    ``batches`` is the live work list with ``key, cohort, day, estimate, upper,
    cost, state, missing, excursions, uncertain`` and optionally ``turnstiles``.
    ``as_of`` is the day the return ruling is made.  ``entrances`` optionally
    declares open entrances (cohort -> gate count) that have no live batch yet,
    so they can still be chosen as receivers.
    """
    if isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of <= 3650:
        raise ValueError('关停裁决日应为 0–3650 之间的整数')
    events = _normalise_events(events)
    rows = tuple(dict(row) for row in batches)
    for row in rows:
        for field in ('key', 'cohort', 'day', 'estimate', 'upper', 'cost', 'state'):
            if field not in row:
                raise ValueError(f'批次缺少 {field}，无法生成迁移单')
    declared = {str(zone): int(gates) for zone, gates in (entrances or {}).items()}
    for gates in declared.values():
        if gates <= 0:
            raise ValueError('入口通行口数应为正整数')
    zones = sorted({row['cohort'] for row in rows} | set(declared))
    target_capacity = {zone: _window_capacity(_turnstiles(rows, zone, declared)) for zone in zones}
    load = Counter()
    for row in rows:
        load[(row['cohort'], row['day'])] += float(row['upper'])
    sheets = []
    for event in events:
        affected = [_batch(row, event.event_id) for row in rows
                    if row['cohort'] == event.cohort and event.covers(row['day'])]
        affected.sort(key=lambda item: (item['day'], item['key']))
        lines = []
        for batch in affected:
            closed = {other.cohort for other in events if other.covers(batch['day'])}
            open_targets = [zone for zone in zones
                            if zone != event.cohort and zone not in closed]
            _assign(lines, batch, open_targets, target_capacity, load)
        moved = tuple(line for line in lines if line.moved)
        moved_keys = {line.key for line in moved}
        retained = Counter()
        for row in rows:
            if row['cohort'] == event.cohort and row['key'] not in moved_keys:
                retained[(row['cohort'], row['day'])] += float(row['upper'])
        restored = _restored_capacity(event, rows, declared)
        returns, return_mode, detail, peak = _return_decision(
            event, moved, as_of, retained, restored)
        targets = tuple(sorted({line.target for line in moved if line.target}))
        if not lines:
            status = '无需迁移'
        elif not (event.released and event.is_released_by(as_of)):
            status = '迁移待解除'
        elif return_mode == '无需迁回':
            status = '已解除·无需迁回'
        else:
            status = '已解除·' + return_mode
        checksum = _digest({
            'sheet': _sheet_id(event), 'event': event.as_dict(), 'targets': targets,
            'status': status, 'return_mode': return_mode, 'return_detail': detail,
            'restored_capacity': restored, 'pending_peak': peak,
            'lines': [_line_dict(line) for line in lines],
            'returns': [_line_dict(line) for line in returns],
        })[:16]
        sheets.append(MigrationSheet(
            sheet_id=_sheet_id(event), event=event, lines=tuple(lines), returns=returns,
            targets=targets, status=status, return_mode=return_mode, checksum=checksum,
            return_detail=detail, restored_capacity=restored, pending_peak=peak,
            receiving=_receiving_summary(lines, rows=rows, entrances=declared)))
    plan = MigrationPlan(events=events, sheets=tuple(sheets), reconciliation=())
    return replace(plan, reconciliation=_reconcile(plan, rows))


def _receiving_summary(lines, *, rows, entrances=None):
    """How much each receiving entrance takes on, in people per half-hour window."""
    by_target = defaultdict(list)
    for line in lines:
        if line.moved and line.target:
            by_target[line.target].append(line)
    summary = []
    for zone in sorted(by_target):
        group = by_target[zone]
        days = sorted({line.day for line in group})
        peak = max(sum(line.upper for line in group if line.day == day) for day in days)
        capacity = _window_capacity(_turnstiles(rows, zone, entrances))
        summary.append({
            '接收口': zone, '接收批次': len(group), '承接人/半小时窗': round(peak, 1),
            '可用通行人/半小时窗': capacity, '剩余余量': round(capacity - peak, 1),
            '应急通道': '预留 1 条',
        })
    return tuple(summary)


def _normalise_events(events):
    seen = {}
    for event in events:
        if not isinstance(event, ClosureEvent):
            raise ValueError('关停事件须为已解析的事件对象')
        if event.event_id in seen and seen[event.event_id] != event:
            raise ValueError(f'关停事件 {event.event_id} 重复出现且内容不一致')
        seen[event.event_id] = event
    ordered = sorted(seen.values(), key=lambda item: (item.start_day, item.end_day, item.cohort, item.event_id))
    _reject_overlapping(ordered)
    return tuple(ordered)


def _line_dict(line):
    return {field: getattr(line, field) for field in line.__dataclass_fields__}


def _reconcile(plan, batches):
    zones = sorted({row['cohort'] for row in batches} |
                   {sheet.event.cohort for sheet in plan.sheets} |
                   {line.target for line in plan.lines if line.target} |
                   {line.target for line in plan.returns})
    table = []
    totals = {'pre_batches': 0, 'pre_used': 0, 'post_batches': 0, 'post_used': 0,
              'pre_people': 0.0, 'post_people': 0.0}
    for zone in zones:
        pre_rows = [row for row in batches if row['cohort'] == zone]
        pre_batches = len(pre_rows)
        pre_used = sum(int(row['cost']) for row in pre_rows if row['state'] == '已分派')
        pre_people = sum(float(row['estimate']) for row in pre_rows)
        out_lines = [line for line in plan.lines if line.moved and line.source == zone]
        in_lines = [line for line in plan.lines if line.moved and line.target == zone]
        returns_in = [line for line in plan.returns if line.returned and line.target == zone]
        returns_out = [line for line in plan.returns if line.returned and line.source == zone]
        post_batches = pre_batches - len(out_lines) + len(in_lines) + len(returns_in) - len(returns_out)
        post_used = (pre_used
                     - sum(line.cost for line in out_lines if line.dispatched)
                     + sum(line.cost for line in in_lines if line.dispatched)
                     + sum(line.cost for line in returns_in if line.dispatched)
                     - sum(line.cost for line in returns_out if line.dispatched))
        post_people = (pre_people - sum(line.estimate for line in out_lines)
                       + sum(line.estimate for line in in_lines)
                       + sum(line.estimate for line in returns_in)
                       - sum(line.estimate for line in returns_out))
        if post_batches < 0 or post_used < 0 or post_people < -1e-6:
            raise ArithmeticError('入口承接与工位对账出现负数')
        table.append({
            '入口': zone, '关停前批次': pre_batches, '关停前承接人': round(pre_people, 1),
            '关停前工位': pre_used, '迁出批次': len(out_lines), '迁入批次': len(in_lines),
            '迁回批次': len(returns_in), '关停后批次': post_batches,
            '关停后承接人': round(post_people, 1), '关停后工位': post_used,
            '应急通道': '预留 1 条' if (in_lines or returns_in) else '—',
        })
        totals['pre_batches'] += pre_batches
        totals['pre_used'] += pre_used
        totals['post_batches'] += post_batches
        totals['post_used'] += post_used
        totals['pre_people'] += pre_people
        totals['post_people'] += post_people
    if (totals['pre_batches'] != totals['post_batches'] or totals['pre_used'] != totals['post_used']
            or abs(totals['pre_people'] - totals['post_people']) > 1e-6):
        raise ArithmeticError('关停前后各入口承接或工位未守恒')
    table.append({
        '入口': '合计', '关停前批次': totals['pre_batches'], '关停前承接人': round(totals['pre_people'], 1),
        '关停前工位': totals['pre_used'],
        '迁出批次': sum(1 for line in plan.lines if line.moved),
        '迁入批次': sum(1 for line in plan.lines if line.moved),
        '迁回批次': len(plan.returns),
        '关停后批次': totals['post_batches'], '关停后承接人': round(totals['post_people'], 1),
        '关停后工位': totals['post_used'], '应急通道': '—',
    })
    return tuple(table)


def migration_evidence(plan):
    """Workbench tables: sheet register, migration lines, return rulings, reconciliation."""
    sheet_of = {sheet.event.event_id: sheet.sheet_id for sheet in plan.sheets}
    sheets = [{
        '迁移单号': sheet.sheet_id, '关停事件': sheet.event.event_id, '入口': sheet.event.cohort,
        '关停区间': f'{sheet.event.start_day}–{sheet.event.end_day}',
        '解除': f'第 {sheet.event.released_day} 日' if sheet.event.released else '未解除',
        '迁入批次': len(sheet.moved), '人工判读': len(sheet.manual),
        '候补迁入': len(sheet.waiting), '迁回批次': len(sheet.returns),
        '迁回裁决': sheet.return_mode, '状态': sheet.status, '签收码': sheet.checksum,
    } for sheet in plan.sheets]
    receiving = [dict(item, 迁移单号=sheet.sheet_id) for sheet in plan.sheets
                 for item in sheet.receiving]
    lines = [{
        '迁移单号': sheet_of[line.event_id], '单号': line.key, '原入口': line.source,
        '接收口': line.target or '—', '处置': line.state, '原因': line.reason,
        '应急通道': '预留 1 条' if line.reserve_lane else '—', '说明': line.detail,
    } for line in plan.lines]
    returns = [{
        '迁移单号': sheet_of[line.event_id], '单号': line.key, '当前入口': line.source,
        '回迁至': line.target, '方式': line.mode, '批次': line.wave, '依据': line.reason,
    } for line in plan.returns]
    return {
        '迁移单': sheets,
        '迁移明细': lines,
        '迁回裁决': returns,
        '接收口承接': receiving,
        '关停对账': list(plan.reconciliation),
    }


def migration_payload(plan):
    """Serializable payload attached to the run result."""
    return {
        'events': [sheet.event.as_dict() for sheet in plan.sheets],
        'sheets': [{
            'sheet_id': sheet.sheet_id, 'event': sheet.event.as_dict(), 'status': sheet.status,
            'return_mode': sheet.return_mode, 'return_detail': sheet.return_detail,
            'targets': list(sheet.targets), 'checksum': sheet.checksum,
            'restored_capacity': sheet.restored_capacity, 'pending_peak': sheet.pending_peak,
            'receiving': list(sheet.receiving),
            'lines': [_line_dict(line) for line in sheet.lines],
            'returns': [_line_dict(line) for line in sheet.returns],
        } for sheet in plan.sheets],
        'reconciliation': list(plan.reconciliation),
        'summary': {
            'sheets': len(plan.sheets), 'moved': len(plan.moved),
            'manual': sum(1 for line in plan.lines if line.state == '人工判读'),
            'waiting': sum(1 for line in plan.lines if line.state == '候补迁入'),
            'returned': len(plan.returns),
        },
    }



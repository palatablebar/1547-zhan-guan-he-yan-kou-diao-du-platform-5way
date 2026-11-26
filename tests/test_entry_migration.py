import pytest

from venuegate.entry_migration.contracts import ClosureEvent
from venuegate.entry_migration.service import (migration_evidence, migration_payload,
                                               parse_closures, plan_migrations)
from venuegate.pipeline import execute, open_fixture, preview


def batch(key, cohort, day, upper, *, cost=1, state='已分派', missing=0,
          excursions=False, uncertain=False, turnstiles=6, reason='ABOVE_TRIGGER', evidence=''):
    return {'key': key, 'cohort': cohort, 'day': day, 'estimate': upper * .8, 'upper': upper,
            'cost': cost, 'state': state, 'reason': reason, 'evidence': evidence,
            'missing': missing, 'excursions': excursions, 'uncertain': uncertain,
            'turnstiles': turnstiles}


def event(event_id='E1', cohort='北门', start=10, end=20, **kwargs):
    return ClosureEvent(event_id=event_id, cohort=cohort, start_day=start, end_day=end, **kwargs)


# --- contract / parsing -----------------------------------------------------

def test_repeated_event_does_not_add_another_sheet_or_move_people_twice():
    rows = [batch('A', '北门', 12, 120), batch('B', '南门', 12, 90)]
    raw = [{'event': 'CL-1', 'cohort': '北门', 'start_day': 10, 'end_day': 20}]
    once = plan_migrations(parse_closures(raw), rows, as_of=12)
    twice = plan_migrations(parse_closures(raw + raw), rows, as_of=12)
    assert once == twice
    assert len(once.sheets) == 1 and len(once.moved) == 1


def test_conflicting_duplicate_event_is_rejected():
    raw = [{'event': 'CL-1', 'cohort': '北门', 'start_day': 10, 'end_day': 20},
           {'event': 'CL-1', 'cohort': '北门', 'start_day': 11, 'end_day': 20}]
    with pytest.raises(ValueError, match='不一致'):
        parse_closures(raw)


def test_overlapping_closures_at_one_entrance_are_rejected():
    raw = [{'event': 'CL-1', 'cohort': '北门', 'start_day': 10, 'end_day': 20},
           {'event': 'CL-2', 'cohort': '北门', 'start_day': 15, 'end_day': 25}]
    with pytest.raises(ValueError, match='重叠'):
        parse_closures(raw)


@pytest.mark.parametrize('declaration', [
    {'event': '', 'cohort': '北门', 'start_day': 10, 'end_day': 20},
    {'event': 'X', 'cohort': '北门', 'start_day': 20, 'end_day': 10},
    {'event': 'X', 'cohort': '北门', 'start_day': 10, 'end_day': 20, 'released': True},
    {'event': 'X', 'cohort': '北门', 'start_day': 10, 'end_day': 20, 'released_day': 5},
    {'event': 'X', 'cohort': '北门', 'start_day': 10, 'end_day': 20, 'capacity_after': 0},
    {'event': 'X', 'cohort': '北门', 'start_day': 10, 'end_day': 20, 'unexpected': 1},
])
def test_invalid_closure_declarations_are_refused(declaration):
    with pytest.raises(ValueError):
        parse_closures([declaration])


def test_sheet_requires_supported_batch_fields():
    with pytest.raises(ValueError, match='批次缺少'):
        plan_migrations((event(),), [{'key': 'A', 'cohort': '北门'}], as_of=12)


# --- migration rules --------------------------------------------------------

def test_only_calibrated_in_range_batches_migrate_automatically():
    rows = [
        batch('OK', '北门', 12, 120),
        batch('MISSING', '北门', 12, 120, missing=1),
        batch('OUTSIDE', '北门', 12, 120, excursions=True),
        batch('UNSURE', '北门', 12, 120, uncertain=True),
        batch('HARD', '北门', 12, 120, state='暂停', reason='BOOTH_BREAK'),
    ]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 8})
    states = {line.key: (line.state, line.reason) for line in plan.lines}
    assert states['OK'] == ('迁入', 'CALIBRATED_IN_RANGE')
    assert states['MISSING'] == ('人工判读', 'MISSING_EVIDENCE')
    assert states['OUTSIDE'] == ('人工判读', 'OUTSIDE_TRAINING_RANGE')
    assert states['UNSURE'] == ('人工判读', 'IMMATURE_CALIBRATION')
    assert states['HARD'] == ('人工判读', 'HARD_RULE_ACTIVE')
    assert len(plan.moved) == 1


def test_closed_entrance_no_longer_counts_migrated_people():
    rows = [batch('A', '北门', 12, 120, cost=2), batch('B', '南门', 12, 90, cost=1)]
    plan = plan_migrations((event(),), rows, as_of=12)
    moved = next(line for line in plan.lines if line.key == 'A')
    assert moved.target == '南门'
    source = next(row for row in plan.reconciliation if row['入口'] == '北门')
    target = next(row for row in plan.reconciliation if row['入口'] == '南门')
    assert source['关停前批次'] == 1 and source['关停后批次'] == 0
    assert target['迁入批次'] == 1 and target['关停后承接人'] > target['关停前承接人']
    total = next(row for row in plan.reconciliation if row['入口'] == '合计')
    assert total['关停前批次'] == total['关停后批次'] == 2
    assert total['关停前承接人'] == total['关停后承接人']


def test_receiving_entrance_keeps_an_emergency_lane():
    rows = [batch('A', '北门', 12, 100, turnstiles=6)]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 6})
    moved = plan.moved[0]
    assert moved.reserve_lane is True
    receiving = plan.sheets[0].receiving[0]
    assert receiving['应急通道'] == '预留 1 条'
    # One gate is kept free: 5 lanes * 4 windows = 20 windows per half-hour.
    assert receiving['可用通行人/半小时窗'] == 5 * (30 * 60 // 8)


def test_batches_without_receiving_headroom_wait_at_the_closed_entrance():
    rows = [batch('A', '北门', 12, 5000, cost=2, turnstiles=6),
            batch('B', '南门', 12, 10, cost=1, turnstiles=3)]
    plan = plan_migrations((event(),), rows, as_of=12)
    waiting = [line for line in plan.lines if line.key == 'A'][0]
    assert waiting.state == '候补迁入' and waiting.target is None
    assert waiting.reason == 'NO_RECEIVING_HEADROOM'


def test_each_sheet_is_isolated_by_event_and_entrance():
    rows = [batch('A', '北门', 12, 100), batch('B', '东门', 12, 100), batch('C', '南门', 25, 100)]
    closures = (event('CL-1', '北门', 10, 15), event('CL-2', '东门', 10, 15))
    plan = plan_migrations(closures, rows, as_of=25)
    assert len(plan.sheets) == 2
    by_event = {sheet.event.event_id: [line.key for line in sheet.lines] for sheet in plan.sheets}
    assert by_event['CL-1'] == ['A'] and by_event['CL-2'] == ['B']


def test_other_closed_entrance_is_not_used_as_a_receiver():
    rows = [batch('A', '北门', 12, 100), batch('B', '东门', 12, 100)]
    closures = (event('CL-1', '北门', 10, 15), event('CL-2', '东门', 10, 15))
    plan = plan_migrations(closures, rows, as_of=12)
    assert all(line.target is None for line in plan.lines)
    assert {line.state for line in plan.lines} == {'候补迁入'}


# --- return rulings ---------------------------------------------------------

def test_full_return_when_restored_headroom_covers_the_peak():
    rows = [batch('A', '北门', 11, 100), batch('B', '北门', 12, 150)]
    plan = plan_migrations((event(released=True, released_day=14, capacity_after=10),), rows,
                           as_of=20, entrances={'南门': 10})
    # Release day is after the migrated windows: those finished, nothing returns.
    assert plan.returns == ()
    assert plan.sheets[0].return_mode == '无需迁回'


def test_pending_batches_return_in_full_when_headroom_allows():
    rows = [batch('A', '北门', 15, 100), batch('B', '北门', 15, 120)]
    plan = plan_migrations((event(released=True, released_day=14, capacity_after=10),), rows,
                           as_of=16, entrances={'南门': 10})
    modes = {line.mode for line in plan.returns}
    waves = {line.wave for line in plan.returns}
    assert modes == {'全量迁回'} and waves == {1}
    assert len(plan.returns) == 2


def test_pending_batches_return_in_waves_when_peak_exceeds_restored_headroom():
    rows = [batch('A', '北门', 15, 200, cost=1), batch('B', '北门', 15, 190, cost=1),
            batch('C', '北门', 16, 200, cost=1)]
    # capacity_after=1 gate -> 225 people per half-hour window; day 15 peaks at 390.
    plan = plan_migrations((event(released=True, released_day=14, capacity_after=1),), rows,
                           as_of=18, entrances={'南门': 10})
    sheet = plan.sheets[0]
    assert sheet.return_mode == '分批迁回'
    assert max(line.wave for line in plan.returns) >= 2
    assert {line.mode for line in plan.returns} == {'分批迁回'}
    assert '高峰客流' in sheet.return_detail


def test_completed_batches_never_return():
    rows = [batch('OLD', '北门', 11, 100), batch('NEW', '北门', 16, 100)]
    plan = plan_migrations((event(released=True, released_day=15, capacity_after=10),), rows,
                           as_of=16, entrances={'南门': 10})
    returned = {line.key for line in plan.returns}
    assert returned == {'NEW'}
    assert '已完成不回迁' in plan.sheets[0].return_detail


def test_unreleased_closure_does_not_resume_the_original_entrance():
    rows = [batch('A', '北门', 12, 100)]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 6})
    assert plan.returns == ()
    assert plan.sheets[0].status == '迁移待解除'
    assert plan.sheets[0].return_mode == '待解除'


def test_release_after_decision_day_is_not_applied_yet():
    rows = [batch('A', '北门', 12, 100)]
    plan = plan_migrations((event(released=True, released_day=19, capacity_after=10),), rows,
                           as_of=12, entrances={'南门': 6})
    assert plan.returns == ()
    assert plan.sheets[0].return_mode == '待解除'


# --- integration -----------------------------------------------------------

def test_sample_run_exposes_signable_sheets_and_reconciliation():
    result = preview()
    payload = result['migration']
    assert payload['summary']['sheets'] >= 1
    assert all(sheet['checksum'] for sheet in payload['sheets'])
    total = next(row for row in payload['reconciliation'] if row['入口'] == '合计')
    assert total['关停前批次'] == total['关停后批次']
    assert total['关停前工位'] == total['关停后工位']
    stage = result['sections'][7]
    assert stage['migration_title'] == '关停迁移单'
    assert stage['migration_rows'] and stage['migration_reconciliation']


def test_posting_same_closures_twice_is_idempotent():
    dataset = open_fixture()
    once = execute(dataset['records'], closures=dataset['closures'], as_of=dataset['as_of'])
    twice = execute(dataset['records'], closures=list(dataset['closures']) * 2, as_of=dataset['as_of'])
    assert once['migration'] == twice['migration']
    assert once['rows'] == twice['rows']


def test_closures_do_not_alter_base_disposition_or_estimates():
    dataset = open_fixture()
    plain = execute(dataset['records'], as_of=dataset['as_of'])
    closed = execute(dataset['records'], as_of=dataset['as_of'], closures=dataset['closures'])
    assert closed['rows'] == plain['rows']
    assert closed['feature_stamp'] == plain['feature_stamp']
    # Without declarations only the paused hard-rule batch yields an implicit sheet.
    assert plain['migration']['summary']['sheets'] == 1
    assert plain['migration']['sheets'][0]['event']['event'].startswith('CL-AUTO-')
    assert closed['migration']['summary']['sheets'] >= 2


def test_api_accepts_closures_and_rejects_bad_event():
    from venuegate.entry import app
    dataset = open_fixture()
    client = app.test_client()
    ok = client.post('/api/venuegate/run', json={**dataset})
    assert ok.status_code == 200 and ok.get_json()['migration']['summary']['sheets'] >= 1
    bad = client.post('/api/venuegate/run', json={**dataset, 'closures': [{'event': 'X'}]})
    assert bad.status_code == 422
    unknown = client.post('/api/venuegate/run', json={**dataset, 'not_a_field': 1})
    assert unknown.status_code == 422


def test_evidence_tables_are_consistent_with_payload():
    rows = [batch('A', '北门', 12, 120, cost=2)]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 8})
    evidence = migration_evidence(plan)
    payload = migration_payload(plan)
    assert set(evidence) == {'迁移单', '迁移明细', '迁回裁决', '接收口承接', '关停对账'}
    assert len(evidence['迁移明细']) == len(payload['sheets'][0]['lines'])
    assert evidence['迁移单'][0]['签收码'] == payload['sheets'][0]['checksum']
    assert evidence['迁移明细'][0]['接收口'] == payload['sheets'][0]['lines'][0]['target']
    assert migration_payload(plan_migrations((), [], as_of=96))['sheets'] == []


def test_paused_hard_rule_batch_yields_an_implicit_migration_sheet():
    from venuegate.entry_migration.service import derive_closures
    rows = [batch('HALT', '北门', 12, 120, cost=2, state='暂停', reason='BOOTH_BREAK'),
            batch('OK', '北门', 12, 90, cost=1)]
    events = derive_closures(rows)
    assert len(events) == 1
    assert events[0].cohort == '北门' and events[0].start_day == 12 and events[0].end_day == 13
    plan = plan_migrations(events, rows, as_of=12, entrances={'南门': 8})
    states = {line.key: line.state for line in plan.lines}
    assert states['HALT'] == '人工判读' and states['OK'] == '迁入'


def test_declared_event_absorbs_overlapping_implicit_run():
    from venuegate.entry_migration.service import derive_closures
    rows = [batch('HALT', '北门', 12, 120, cost=2, state='暂停', reason='BOOTH_BREAK')]
    declared = (event('CL-OPS', '北门', 10, 15),)
    events = derive_closures(rows, declared)
    assert [item.event_id for item in events] == ['CL-OPS']


def test_reversing_input_order_keeps_migration_identical():
    dataset = open_fixture()
    forward = execute(dataset['records'], closures=dataset['closures'], as_of=dataset['as_of'])
    backward = execute(list(reversed(dataset['records'])),
                       closures=list(reversed(dataset['closures'])), as_of=dataset['as_of'])
    assert forward['migration'] == backward['migration']


def test_receiving_entrances_never_exceed_their_capacity():
    rows = [batch(f'B{i}', '北门', 12, 300, cost=1, turnstiles=6) for i in range(8)]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 2, '东门': 3})
    # 2-gate receiver: (2-1)*225 = 225 per window; 3-gate: (3-1)*225 = 450.
    for line in plan.moved:
        assert line.upper <= 225 if line.target == '南门' else line.upper <= 450
    loads = {}
    for line in plan.moved:
        loads[line.target] = loads.get(line.target, 0) + line.upper
    assert loads.get('南门', 0) <= 225 and loads.get('东门', 0) <= 450


def test_entrance_with_a_single_gate_leaves_no_receiving_headroom():
    rows = [batch('A', '北门', 12, 100)]
    plan = plan_migrations((event(),), rows, as_of=12, entrances={'南门': 1})
    # One gate is reserved as the emergency lane, so nothing can be received.
    assert plan.moved == ()
    assert plan.lines[0].state == '候补迁入'


def test_non_positive_receiving_gates_are_rejected():
    rows = [batch('A', '北门', 12, 100)]
    with pytest.raises(ValueError, match='正整数'):
        plan_migrations((event(),), rows, as_of=12, entrances={'南门': 0})


def test_sample_closures_cover_all_return_modes():
    payload = preview()['migration']
    modes = {sheet['return_mode'] for sheet in payload['sheets']}
    assert {'全量迁回', '分批迁回', '待解除'} <= modes
    for sheet in payload['sheets']:
        if sheet['return_mode'] == '无需迁回':
            continue
        if sheet['return_mode'] == '全量迁回':
            assert all(line['wave'] == 1 and line['mode'] == '全量迁回' for line in sheet['returns'])
        if sheet['return_mode'] == '分批迁回':
            assert max(line['wave'] for line in sheet['returns']) >= 2
            assert {line['mode'] for line in sheet['returns']} == {'分批迁回'}
        if sheet['return_mode'] == '待解除':
            assert sheet['returns'] == []


def test_receiving_entrances_never_exceed_declared_capacity_in_sample():
    for sheet in preview()['migration']['sheets']:
        for item in sheet['receiving']:
            assert item['剩余余量'] >= 0
            assert item['承接人/半小时窗'] <= item['可用通行人/半小时窗']
            assert item['应急通道'] == '预留 1 条'

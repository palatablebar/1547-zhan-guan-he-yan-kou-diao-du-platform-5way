from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClosureEvent:
    """A temporary entrance closure during a show changeover or booth verification."""

    event_id: str
    cohort: str
    start_day: int
    end_day: int
    reason: str = '临时关停'
    released: bool = False
    released_day: int | None = None
    capacity_after: float | None = None

    def __post_init__(self):
        if not self.event_id or not self.cohort or len(self.event_id) > 64 or len(self.cohort) > 64:
            raise ValueError('关停事件缺少有效的编号或入口')
        if type(self.start_day) is not int or type(self.end_day) is not int:
            raise ValueError('关停起止日应为整数')
        if not 0 <= self.start_day < self.end_day <= 3651:
            raise ValueError('关停起止日应递增且在 0–3651 日之间')
        if type(self.released) is not bool:
            raise ValueError('关停解除标记应为布尔值')
        if self.released_day is not None:
            if type(self.released_day) is not int:
                raise ValueError('解除日应为整数')
            if not self.start_day <= self.released_day < self.end_day:
                raise ValueError('解除日应落在关停区间内')
            if not self.released:
                raise ValueError('填写解除日时必须标记为已解除')
        if self.released and self.released_day is None:
            raise ValueError('已解除的关停事件需要填写解除日')
        if self.capacity_after is not None:
            if isinstance(self.capacity_after, bool) or not isinstance(self.capacity_after, (int, float)):
                raise ValueError('恢复后通行能力应为数值')
            if not self.capacity_after > 0:
                raise ValueError('恢复后通行能力应为正数')

    def release_boundary(self):
        return self.released_day if self.released_day is not None else self.end_day

    def covers(self, day):
        # Migration is planned against the whole declared closure; an early
        # release is handled afterwards by returning the still-pending batches.
        return self.start_day <= day < self.end_day

    def is_released_by(self, as_of):
        return self.release_boundary() <= as_of

    def identity(self):
        return (self.event_id, self.cohort, self.start_day, self.end_day, self.reason,
                self.released, self.released_day, self.capacity_after)

    def as_dict(self):
        return {
            'event': self.event_id,
            'cohort': self.cohort,
            'start_day': self.start_day,
            'end_day': self.end_day,
            'reason': self.reason,
            'released': self.released,
            'released_day': self.released_day,
            'capacity_after': self.capacity_after,
        }


@dataclass(frozen=True, slots=True)
class MigrationLine:
    key: str
    event_id: str
    source: str
    target: str | None
    day: int
    estimate: float
    upper: float
    cost: int
    state: str
    reason: str
    reserve_lane: bool
    dispatched: bool
    detail: str

    @property
    def moved(self):
        return self.state == '迁入'


@dataclass(frozen=True, slots=True)
class ReturnLine:
    key: str
    event_id: str
    source: str
    target: str
    day: int
    estimate: float
    upper: float
    cost: int
    mode: str
    wave: int | None
    reason: str
    dispatched: bool = False

    @property
    def returned(self):
        return self.mode in {'全量迁回', '分批迁回'}


@dataclass(frozen=True, slots=True)
class MigrationSheet:
    sheet_id: str
    event: ClosureEvent
    lines: tuple[MigrationLine, ...]
    returns: tuple[ReturnLine, ...]
    targets: tuple[str, ...]
    status: str
    return_mode: str
    checksum: str
    return_detail: str = ''
    restored_capacity: int = 0
    pending_peak: int = 0
    receiving: tuple[dict, ...] = ()

    @property
    def moved(self):
        return tuple(line for line in self.lines if line.moved)

    @property
    def manual(self):
        return tuple(line for line in self.lines if line.state == '人工判读')

    @property
    def waiting(self):
        return tuple(line for line in self.lines if line.state == '候补迁入')


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    events: tuple[ClosureEvent, ...]
    sheets: tuple[MigrationSheet, ...]
    reconciliation: tuple[dict, ...]

    @property
    def lines(self):
        return tuple(line for sheet in self.sheets for line in sheet.lines)

    @property
    def returns(self):
        return tuple(line for sheet in self.sheets for line in sheet.returns)

    @property
    def moved(self):
        return tuple(line for line in self.lines if line.moved)

    def by_key(self):
        status = {}
        for sheet in self.sheets:
            for line in sheet.lines:
                status[line.key] = {'sheet': sheet.sheet_id, 'event': sheet.event.event_id,
                                    'state': line.state, 'source': line.source,
                                    'target': line.target, 'reason': line.reason,
                                    'reserve_lane': line.reserve_lane}
        for line in self.returns:
            status.setdefault(line.key, {'sheet': None, 'event': line.event_id, 'state': line.mode,
                                         'source': line.source, 'target': line.target,
                                         'reason': line.reason})
            status[line.key]['return'] = line.mode
            status[line.key]['wave'] = line.wave
        return status


@dataclass(frozen=True, slots=True)
class ReturnDecision:
    mode: str
    detail: str
    capacity: int
    peak: int
    waves: tuple[ReturnLine, ...]

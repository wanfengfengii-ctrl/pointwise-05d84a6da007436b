from dataclasses import dataclass
from enum import Enum
import json

from database import read_integer
from evidence.intervals import Interval
from registry.channels import normalize_channels, require_name


class RunStatus(str, Enum):
    ACTIVE = 'active'
    SEALED = 'sealed'


@dataclass(frozen=True)
class Run:
    run_id: int
    station: str
    revision_id: int
    start_seq: int
    condition: tuple[str, ...]
    observer: str
    first_seq: int
    position: int
    status: RunStatus
    sealed_end: int | None
    result_id: str | None


@dataclass(frozen=True)
class Switch:
    run_id: int
    cut_seq: int
    new_condition: tuple[str, ...]
    new_observer: str
    new_first_seq: int
    new_position: int


def validate_position(position, label='source sequence'):
    if type(position) is not int or position < 0:
        raise ValueError(f'invalid {label}: {position!r}')
    return position


class RunStore:
    def __init__(self, db):
        self.db = db

    def insert(self, station, revision_id, start_seq, condition, observer, first_seq):
        cursor = self.db.execute(
            'INSERT INTO runs(station, revision_id, start_seq, condition, observer, first_seq) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (station, revision_id, str(start_seq), json.dumps(condition), observer, str(first_seq)),
        )
        return cursor.lastrowid

    def load(self, run_id):
        if type(run_id) is not int or run_id <= 0:
            raise KeyError(run_id)
        row = self.db.execute('SELECT * FROM runs WHERE run_id = ?', (str(run_id),)).fetchone()
        if row is None:
            raise KeyError(run_id)
        condition = json.loads(row['condition'])
        if type(condition) is not list or tuple(condition) != normalize_channels(condition):
            raise ValueError('invalid stored run condition')
        return Run(
            run_id, require_name(row['station'], 'station'), row['revision_id'],
            validate_position(read_integer(row['start_seq'])), tuple(condition),
            require_name(row['observer'], 'observer'), validate_position(read_integer(row['first_seq'])),
            validate_position(read_integer(row['position']), 'delivery position'),
            RunStatus(row['status']),
            None if row['sealed_end'] is None else validate_position(read_integer(row['sealed_end'])),
            row['result_id'],
        )

    def advance(self, run_id, previous, position):
        if position != previous + 1:
            raise ValueError('delivery position must advance one place')
        cursor = self.db.execute(
            "UPDATE runs SET position = ? WHERE run_id = ? AND status = 'active' AND position = ?",
            (str(position), run_id, str(previous)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('run changed before delivery could be acknowledged')

    def mark_sealed(self, run_id, end, result_id):
        cursor = self.db.execute(
            "UPDATE runs SET status = 'sealed', sealed_end = ?, result_id = ? "
            "WHERE run_id = ? AND status = 'active'",
            (str(end), result_id, run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('run changed before sealing')


class SwitchStore:
    def __init__(self, db):
        self.db = db

    def insert(self, run_id, cut, new_condition, observer, first_seq):
        self.db.execute(
            'INSERT INTO run_switches(run_id, cut_seq, new_condition, new_observer, '
            'new_first_seq) VALUES (?, ?, ?, ?, ?)',
            (run_id, str(cut), json.dumps(new_condition), observer, str(first_seq)),
        )

    def for_run(self, run_id):
        row = self.db.execute(
            'SELECT * FROM run_switches WHERE run_id = ?', (str(run_id),)
        ).fetchone()
        if row is None:
            return None
        condition = json.loads(row['new_condition'])
        if type(condition) is not list or tuple(condition) != normalize_channels(condition):
            raise ValueError('invalid stored switch condition')
        return Switch(
            run_id, validate_position(read_integer(row['cut_seq']), 'cut sequence'),
            tuple(condition), require_name(row['new_observer'], 'observer'),
            validate_position(read_integer(row['new_first_seq']), 'first eligible sequence'),
            validate_position(read_integer(row['new_position']), 'delivery position'),
        )

    def advance(self, run_id, previous, position):
        if position != previous + 1:
            raise ValueError('delivery position must advance one place')
        cursor = self.db.execute(
            "UPDATE run_switches SET new_position = ? "
            "WHERE run_id = ? AND new_position = ?",
            (str(position), run_id, str(previous)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('run changed before delivery could be acknowledged')


class GapStore:
    def __init__(self, db):
        self.db = db

    def remember(self, run_id, interval):
        self.db.execute(
            'INSERT OR IGNORE INTO history_gaps(run_id, lo, hi) VALUES (?, ?, ?)',
            (run_id, str(interval.start), str(interval.end)),
        )

    def all_for(self, run_id):
        rows = self.db.execute(
            'SELECT lo, hi FROM history_gaps WHERE run_id = ? ORDER BY lo', (str(run_id),)
        )
        return tuple(
            Interval(read_integer(row['lo']), read_integer(row['hi'])) for row in rows
        )

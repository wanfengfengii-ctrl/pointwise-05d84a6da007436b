"""In-shift cutover from one channel condition to another."""
from dataclasses import dataclass

from database import transaction
from evidence.frames import FrameLedger, validate_frame
from evidence.intervals import Interval, validate_interval
from evidence.receipts import ReceiptLedger, validate_receipt
from registry.calibrations import RevisionCatalog
from registry.channels import ChannelCatalog, require_channels
from sessions.portal import check_registration
from sessions.runs import RunStatus, RunStore, SwitchStore, GapStore, validate_position


@dataclass(frozen=True)
class HistoryIntake:
    new_frames: int
    new_receipts: int
    unavailable: tuple[Interval, ...]
    pending: tuple[Interval, ...]


def switch(db, portal, run_id, new_filter, cut):
    validate_position(cut, 'cut sequence')
    with transaction(db, write=True):
        runs = RunStore(db)
        run = runs.load(run_id)
        if run.status is RunStatus.SEALED:
            raise ValueError('cannot switch a sealed run')
        switches = SwitchStore(db)
        if switches.for_run(run_id) is not None:
            raise ValueError('run already switched condition once')
        if cut < run.start_seq:
            raise ValueError('cut sequence precedes the run start')
        available = ChannelCatalog(db).for_station(run.station)
        new_condition = require_channels(new_filter, available)
        revision = RevisionCatalog(db).resolve(run.station, run.revision_id)
        new_condition = require_channels(
            new_condition, (item.channel for item in revision.calibrations)
        )
    registration = portal.register(run.station, new_condition)
    try:
        check_registration(registration)
        with transaction(db, write=True):
            current = RunStore(db).load(run_id)
            if current.status is RunStatus.SEALED or current.run_id != run_id:
                raise RuntimeError('run changed before the switch could be registered')
            if SwitchStore(db).for_run(run_id) is not None:
                raise ValueError('run already switched condition once')
            SwitchStore(db).insert(
                run_id, cut, new_condition,
                registration.observer, registration.first_seq,
            )
    except BaseException as exc:
        try:
            portal.release(registration.observer)
        except Exception as release_error:
            exc.add_note(f'switch cleanup failed: {release_error}')
        raise
    return None


def _history_binding(station, condition):
    history_observer = f'{station}/history'

    class _HistoryBinding:
        @staticmethod
        def check_frame(frame):
            return validate_frame(
                frame, observer=history_observer, condition=condition, first_seq=0
            )

        @staticmethod
        def check_receipt(receipt):
            return validate_receipt(
                receipt, observer=history_observer, condition=condition, first_seq=0
            )

    return _HistoryBinding()


def _store_history_read(db, run_id, condition, station, read):
    binding = _history_binding(station, condition)
    frames = FrameLedger(db)
    receipts = ReceiptLedger(db)
    gaps = GapStore(db)
    new_frames = 0
    for frame in read.frames:
        binding.check_frame(frame)
        if frames.record(run_id, frame):
            new_frames += 1
    new_receipts = 0
    for receipt in read.receipts:
        binding.check_receipt(receipt)
        if receipts.record(run_id, receipt):
            new_receipts += 1
    unavailable = tuple(
        validate_interval(item) for item in read.unavailable if item.start < item.end
    )
    pending = tuple(
        validate_interval(item) for item in read.pending if item.start < item.end
    )
    for gap in unavailable:
        gaps.remember(run_id, gap)
    return HistoryIntake(new_frames, new_receipts, unavailable, pending)


def fetch_history(db, portal, run_id, start, end):
    """Pull retained history into the switched run's ledgers.

    History evidence keeps its own observer identity and provenance. Expired
    stretches are remembered as confirmed-unavailable for sealing.
    """
    interval = validate_interval(Interval(start, end))
    with transaction(db, write=True):
        run = RunStore(db).load(run_id)
        if run.status is RunStatus.SEALED:
            raise ValueError('cannot fetch history for a sealed run')
        switch_record = SwitchStore(db).for_run(run_id)
        if switch_record is None:
            raise ValueError('history fetch requires a switched run')
        if interval.start < run.start_seq:
            raise ValueError('history interval precedes the run start')
        read = portal.read_history(
            run.station, switch_record.new_condition, start, end
        )
        return _store_history_read(
            db, run_id, switch_record.new_condition, run.station, read
        )

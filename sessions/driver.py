from dataclasses import dataclass

from database import transaction
from evidence.intake import CaptureContext, IntakeOutcome, accept_event
from sessions.portal import check_delivery
from sessions.runs import RunStatus, RunStore, SwitchStore


@dataclass(frozen=True)
class StepReport:
    acknowledged: int
    new_frames: int
    new_receipts: int
    position: int
    status: RunStatus


def _drain(db, portal, run_id, runs, binding, position):
    """binding = (observer, condition, first_seq, position_loader, advance)."""
    observer, condition, first_seq, load_position, advance = binding
    acknowledged = new_frames = new_receipts = 0
    for delivery in portal.poll(observer, position):
        with transaction(db, write=True):
            current = runs.load(run_id)
            if current.status is not RunStatus.ACTIVE or load_position(current, db) != position:
                raise RuntimeError('run changed during portal poll')
            check_delivery(delivery, observer, condition, position)
            context = CaptureContext(run_id, current.start_seq, condition, observer, first_seq)
            outcome = accept_event(db, context, delivery.event)
            advance(run_id, position, delivery.position)
        position = delivery.position
        acknowledged += 1
        new_frames += outcome is IntakeOutcome.NEW_FRAME
        new_receipts += outcome is IntakeOutcome.NEW_RECEIPT
    return acknowledged, new_frames, new_receipts, position


def step(db, portal, run_id):
    runs = RunStore(db)
    run = runs.load(run_id)
    if run.status is RunStatus.SEALED:
        return StepReport(0, 0, 0, run.position, run.status)
    # The original real-time observation remains the run's delivery position.
    acknowledged, new_frames, new_receipts, position = _drain(
        db, portal, run_id, runs,
        (run.observer, run.condition, run.first_seq,
         lambda current, _db: current.position, runs.advance),
        run.position,
    )
    switch_record = SwitchStore(db).for_run(run_id)
    if switch_record is not None:
        switches = SwitchStore(db)
        extra = _drain(
            db, portal, run_id, runs,
            (switch_record.new_observer, switch_record.new_condition,
             switch_record.new_first_seq,
             lambda _current, database: SwitchStore(database).for_run(run_id).new_position,
             switches.advance),
            switch_record.new_position,
        )
        acknowledged += extra[0]
        new_frames += extra[1]
        new_receipts += extra[2]
    return StepReport(acknowledged, new_frames, new_receipts, position, run.status)

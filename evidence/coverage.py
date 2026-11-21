from database import transaction
from evidence.frames import FrameLedger
from evidence.intake import CaptureContext
from evidence.intervals import Interval, intersect_interval, normalize_intervals, validate_interval
from evidence.receipts import ReceiptLedger, receipt_order
from evidence.switching import (
    CoverageReport, assess_side, inspect_coverage, windowed_side_views,
)
from sessions.runs import RunStore, SwitchStore


def validate_selection(context, interval, frames, receipts):
    validate_interval(interval)
    if interval.start < context.start_seq:
        raise ValueError('target interval precedes run start')
    previous = interval.start - 1
    for frame in frames:
        context.check_frame(frame)
        if not previous < frame.seq < interval.end:
            raise ValueError('frames must be unique, ordered and within the target interval')
        previous = frame.seq
    seen_receipts = set()
    last_order = None
    for receipt in receipts:
        context.check_receipt(receipt)
        identity = receipt.observer, receipt.receipt_id
        order = receipt_order(receipt)
        if (identity in seen_receipts or (last_order is not None and order < last_order)
                or intersect_interval(interval, receipt.interval) is None):
            raise ValueError('invalid receipt selection')
        seen_receipts.add(identity)
        last_order = order


def coverage(db, run_id, start, end):
    interval = validate_interval(Interval(start, end))
    with transaction(db):
        run = RunStore(db).load(run_id)
        switch_record = SwitchStore(db).for_run(run_id)
        frames = FrameLedger(db).in_range(run_id, interval)
        receipts = ReceiptLedger(db).intersecting(run_id, interval)
        if switch_record is None:
            validate_selection(CaptureContext.from_run(run), interval, frames, receipts)
            return inspect_coverage(interval, frames, receipts)
        if interval.start < run.start_seq:
            raise ValueError('target interval precedes run start')
        limited = windowed_side_views(run, switch_record, start, end)
        sides = tuple(assess_side(view, frames, receipts) for view in limited)
        proven = normalize_intervals(tuple(
            fragment for side in sides
            for fragment in (intersect_interval(side.view.interval, receipt.interval)
                             for receipt in side.receipts) if fragment is not None
        ))
        missing = normalize_intervals(tuple(
            piece for side in sides for piece in side.missing
        ))
        no_match = normalize_intervals(tuple(
            piece for side in sides for piece in side.no_match
        ))
        return CoverageReport(proven, missing, no_match)

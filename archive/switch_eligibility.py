"""Seal-time eligibility for runs that switched condition mid-shift."""
from dataclasses import dataclass

from evidence.frames import FrameLedger
from evidence.intervals import (
    Interval, normalize_intervals, subtract_intervals, validate_interval,
)
from evidence.receipts import ReceiptLedger
from evidence.switching import (
    SideSelection, assess_side, side_views, split_by_eligibility,
)
from sessions.runs import GapStore, SwitchStore
from sessions.switching import _store_history_read


@dataclass(frozen=True)
class SwitchSealSelection:
    interval: Interval
    cut: int
    sides: tuple[SideSelection, ...]
    frames: tuple
    receipts: tuple
    no_match: tuple


@dataclass(frozen=True)
class SwitchSealDecision:
    missing: tuple[Interval, ...]
    unavailable: tuple[Interval, ...]
    selection: SwitchSealSelection | None


def _assess(db, run, switch_record, end):
    interval = validate_interval(Interval(run.start_seq, end))
    frames = FrameLedger(db).in_range(run.run_id, interval)
    receipts = ReceiptLedger(db).intersecting(run.run_id, interval)
    views = side_views(run, switch_record, end)
    sides = tuple(assess_side(view, frames, receipts) for view in views)
    return interval, sides


def assess_switch_seal(db, portal, run, end):
    switch_record = SwitchStore(db).for_run(run.run_id)
    if switch_record is None:
        raise ValueError('run did not switch condition')
    interval, sides = _assess(db, run, switch_record, end)
    # Positions that never had a real-time observation qualification may still
    # be recovered from retained history. Ask the portal once per such piece.
    for side in sides:
        _, ineligible = split_by_eligibility(side.view, side.missing)
        for piece in ineligible:
            read = portal.read_history(
                run.station, side.view.condition, piece.start, piece.end
            )
            _store_history_read(
                db, run.run_id, side.view.condition, run.station, read
            )
    if any(split_by_eligibility(side.view, side.missing)[1] for side in sides):
        interval, sides = _assess(db, run, switch_record, end)

    gaps = GapStore(db).all_for(run.run_id)
    missing_pieces = []
    unavailable_pieces = []
    for side in sides:
        eligible, ineligible = split_by_eligibility(side.view, side.missing)
        # History-confirmed gaps only retire positions that never had a
        # real-time observation qualification.
        covered_pieces = normalize_intervals(tuple(
            fragment for piece in ineligible for gap in gaps
            for fragment in (_overlap(piece, gap),) if fragment is not None
        ))
        unavailable_pieces.extend(covered_pieces)
        missing_pieces.extend(eligible)
        missing_pieces.extend(subtract_intervals(ineligible, covered_pieces))
    missing = normalize_intervals(missing_pieces)
    unavailable = normalize_intervals(unavailable_pieces)
    if missing or unavailable:
        return SwitchSealDecision(missing, unavailable, None)
    frames = tuple(sorted(
        (frame for side in sides for frame in side.frames),
        key=lambda frame: frame.seq,
    ))
    receipts = tuple(sorted(
        {identity: receipt for side in sides for receipt in side.receipts
         for identity in ((receipt.observer, receipt.receipt_id),)}.values(),
        key=lambda receipt: (receipt.interval, receipt.observer, receipt.receipt_id),
    ))
    no_match = normalize_intervals(tuple(
        interval for side in sides for interval in side.no_match
    ))
    selection = SwitchSealSelection(
        interval, switch_record.cut_seq, sides, frames, receipts, no_match,
    )
    return SwitchSealDecision((), (), selection)


def _overlap(left, right):
    start = max(left.start, right.start)
    end = min(left.end, right.end)
    return Interval(start, end) if start < end else None

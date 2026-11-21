"""Per-side coverage accounting for in-shift cutover runs."""
from dataclasses import dataclass

from evidence.frames import FrameConflict, RawFrame
from evidence.intervals import (
    Interval, intersect_interval, normalize_intervals, subtract_intervals, validate_interval,
)
from evidence.receipts import CoverageReceipt, receipt_order


def inspect_coverage(interval, frames, receipts):
    """Proven/missing/no-match spans from frames and receipts over one interval."""
    validate_interval(interval)
    fragments = (intersect_interval(interval, receipt.interval) for receipt in receipts)
    proven = normalize_intervals(fragment for fragment in fragments if fragment is not None)
    occupied = (Interval(frame.seq, frame.seq + 1) for frame in frames)
    return CoverageReport(
        proven, subtract_intervals((interval,), proven), subtract_intervals(proven, occupied),
    )


@dataclass(frozen=True)
class CoverageReport:
    proven: tuple[Interval, ...]
    missing: tuple[Interval, ...]
    no_match: tuple[Interval, ...]


@dataclass(frozen=True)
class SideView:
    label: str
    condition: tuple[str, ...]
    observer: str
    first_seq: int
    interval: Interval
    station: str | None = None


@dataclass(frozen=True)
class SideSelection:
    view: SideView
    frames: tuple[RawFrame, ...]
    receipts: tuple[CoverageReceipt, ...]
    no_match: tuple[Interval, ...]
    missing: tuple[Interval, ...]


def side_views(run, switch_record, end):
    cut = switch_record.cut_seq
    return (
        SideView(
            'old', run.condition, run.observer, run.first_seq,
            validate_interval(Interval(run.start_seq, max(run.start_seq, min(cut, end)))),
            station=run.station,
        ),
        SideView(
            'new', switch_record.new_condition, switch_record.new_observer,
            switch_record.new_first_seq,
            validate_interval(Interval(max(run.start_seq, min(cut, end)), end)),
            station=run.station,
        ),
    )


def _authoritative_frames(side, stored_frames):
    candidates = {}
    for frame in stored_frames:
        if not (side.interval.start <= frame.seq < side.interval.end):
            continue
        if frame.channel not in side.condition:
            continue
        previous = candidates.get(frame.seq)
        if previous is not None:
            if (previous.channel, previous.value) != (frame.channel, frame.value):
                raise FrameConflict(
                    f'conflicting original frame at sequence {frame.seq} within {side.label} side'
                )
            # Same seq/channel/value redelivered from another source: keep the first piece.
            if (frame.observer, frame.provenance) < (previous.observer, previous.provenance):
                candidates[frame.seq] = frame
        else:
            candidates[frame.seq] = frame
    return tuple(sorted(candidates.values(), key=lambda frame: frame.seq))


def _side_receipts(side, stored_receipts):
    history_observer = None if side.station is None else f'{side.station}/history'
    selected = []
    for receipt in stored_receipts:
        if receipt.condition != side.condition:
            continue
        if side.observer:
            if receipt.observer != side.observer and receipt.observer != history_observer:
                # Receipts only count under the observation identity that certified them.
                continue
        if intersect_interval(receipt.interval, side.interval) is None:
            continue
        selected.append(receipt)
    return tuple(sorted(selected, key=receipt_order))


def assess_side(side, stored_frames, stored_receipts):
    validate_interval(side.interval)
    frames = _authoritative_frames(side, stored_frames)
    receipts = _side_receipts(side, stored_receipts)
    if side.interval.start == side.interval.end:
        return SideSelection(side, (), (), (), ())
    report = inspect_coverage(side.interval, frames, receipts)
    return SideSelection(side, frames, receipts, report.no_match, report.missing)


def split_by_eligibility(side, missing):
    """Split missing positions into real-time eligible and never-eligible pieces."""
    eligible = []
    ineligible = []
    for interval in normalize_intervals(missing):
        if side.first_seq > interval.start:
            cut = min(interval.end, side.first_seq)
            if interval.start < cut:
                ineligible.append(Interval(interval.start, cut))
            if cut < interval.end:
                eligible.append(Interval(cut, interval.end))
        else:
            eligible.append(interval)
    return tuple(eligible), tuple(ineligible)


def windowed_side_views(run, switch_record, start, end):
    """Side views clipped to a queried window while keeping each side's binding."""
    window = validate_interval(Interval(start, end))
    limited = []
    for view in side_views(run, switch_record, max(end, run.start_seq)):
        fragment = intersect_interval(view.interval, window)
        if fragment is None:
            continue
        limited.append(SideView(
            view.label, view.condition, view.observer, view.first_seq,
            validate_interval(fragment), station=view.station,
        ))
    return tuple(limited)

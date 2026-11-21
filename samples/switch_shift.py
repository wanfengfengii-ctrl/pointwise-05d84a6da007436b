"""Cutover shift demo: salinity -> turbidity within one sealed shift.

Shows the operational story from SWITCH.md:
  1. open a salinity shift and switch to turbidity at sequence 100;
  2. the late turbidity head leaves a pending gap at the first seal attempt;
  3. delayed real-time evidence completes the shift and publishes one result;
  4. the source is stopped and the frozen artifact is reopened by result id,
     showing original frames with their observer identity and provenance;
  5. a second shift with a late registration demonstrates retained-history
     completion and, when history capacity is zero, confirmed-unavailable gaps.
"""
from contextlib import closing
from fractions import Fraction
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive import get, seal
from database import open_database
from evidence.intervals import Interval
from registry import initialize_defaults
from samples.portal_fixture import STATION, ScriptedPortal
from samples.switch_inputs import (
    BEFORE_CUT, BEFORE_LATE_START, CUT, END, LATE_START, NEW, OLD, START, TAIL,
)
from sessions.driver import step
from sessions.opening import start
from sessions.switching import fetch_history, switch


def _print_stage(title, **fields):
    print('SWITCH_STAGE ' + json.dumps({'stage': title, **fields},
                                       ensure_ascii=False, sort_keys=True))


def _joint_shift(path):
    portal = ScriptedPortal(first_seq=START, history_limit=4)
    with closing(open_database(path)) as db:
        initialize_defaults(db)
        run_id = start(db, portal, STATION, 1, OLD, START)
        old_handle = portal.active_handles[0]
        portal.advance(*BEFORE_CUT)
        switch(db, portal, run_id, NEW, CUT)
        new_handle = portal.active_handles[1]
        portal.advance(*BEFORE_LATE_START, *TAIL)

        # Old side fully proven; new side only delivers the tail at first.
        for seq in (99, 101, 102):
            portal.emit_frame(old_handle, seq)
        portal.emit_receipt(old_handle, START, END)
        portal.emit_frame(new_handle, 103)
        portal.emit_receipt(new_handle, LATE_START, END)
        step(db, portal, run_id)
        pending = seal(db, run_id, END, portal=portal)
        _print_stage(
            'pending_before_late_evidence',
            result_id=pending.result_id,
            missing=[[span.start, span.end] for span in pending.missing],
            unavailable=[[span.start, span.end] for span in pending.unavailable],
        )

        # Delayed turbidity frame and the head coverage receipt arrive.
        portal.emit_frame(new_handle, CUT)
        portal.emit_receipt(new_handle, CUT, LATE_START)
        step(db, portal, run_id)
        fixed = seal(db, run_id, END, portal=portal)
        _print_stage(
            'sealed_after_completion',
            result_id=fixed.result_id,
            release_error=fixed.release_error,
            active_handles=list(portal.active_handles),
        )
    portal.shutdown()
    return fixed.result_id


def _reopen_and_show(path, result_id):
    with closing(open_database(path)) as db:
        artifact = get(db, result_id)
        _print_stage(
            'reopen_after_source_stop',
            result_id=artifact.result_id,
            run_id=artifact.run_id,
            station=artifact.station,
            interval=[artifact.interval.start, artifact.interval.end],
            cut=artifact.cut,
            old_condition=list(artifact.old_condition),
            new_condition=list(artifact.new_condition),
            frames=[
                {'seq': frame.seq, 'channel': frame.channel, 'value': frame.value,
                 'observer': frame.observer, 'provenance': frame.provenance}
                for frame in artifact.frames
            ],
            receipts=[
                {'observer': receipt.observer,
                 'interval': [receipt.interval.start, receipt.interval.end],
                 'condition': list(receipt.condition), 'provenance': receipt.provenance}
                for receipt in artifact.receipts
            ],
            no_match=[[span.start, span.end] for span in artifact.no_match],
            quality=[
                {'channel': summary.channel, 'unit': summary.unit.symbol,
                 'valid_count': summary.valid_count,
                 'mean': None if summary.mean is None else str(summary.mean)}
                for summary in artifact.quality.summaries
            ],
        )
        means = {summary.channel: summary.mean for summary in artifact.quality.summaries}
        assert means['salinity'] == Fraction(7)
        assert means['turbidity'] == Fraction(19, 2)
        assert artifact.no_match == (Interval(98, 99), Interval(101, 103))
        print(f'{artifact.result_id} 停源重开领取：切点 {artifact.cut}，'
              f'盐度均值 {means["salinity"]} g/L，浊度均值 {means["turbidity"]} NTU')


def _late_registration_shift(path, *, history_limit):
    portal = ScriptedPortal(first_seq=START, history_limit=history_limit)
    with closing(open_database(path)) as db:
        initialize_defaults(db)
        run_id = start(db, portal, STATION, 1, OLD, START)
        old_handle = portal.active_handles[0]
        portal.advance(*BEFORE_CUT, *BEFORE_LATE_START)  # next eligible seq is 102
        switch(db, portal, run_id, NEW, CUT)
        new_handle = portal.active_handles[1]
        intake = fetch_history(db, portal, run_id, CUT, LATE_START)
        portal.advance(*TAIL)
        for seq in (99, 101, 102):
            portal.emit_frame(old_handle, seq)
        portal.emit_receipt(old_handle, START, END)
        portal.emit_frame(new_handle, 103)
        portal.emit_receipt(new_handle, LATE_START, END)
        step(db, portal, run_id)
        outcome = seal(db, run_id, END, portal=portal)
        _print_stage(
            f'late_registration_history_limit_{history_limit}',
            history_frames=intake.new_frames,
            history_unavailable=[[span.start, span.end] for span in intake.unavailable],
            result_id=outcome.result_id,
            missing=[[span.start, span.end] for span in outcome.missing],
            unavailable=[[span.start, span.end] for span in outcome.unavailable],
        )
        if outcome.result_id is not None:
            artifact = get(db, outcome.result_id)
            hundred, = (frame for frame in artifact.frames if frame.seq == CUT)
            print(f'{outcome.result_id} 历史补齐：100 号帧出处 {hundred.observer} '
                  f'{hundred.provenance}')
        else:
            print(f'历史容量 {history_limit}：{outcome.unavailable} 明确不可取，'
                  f'{outcome.missing} 仍待实时取证')
    portal.shutdown()


def main():
    with TemporaryDirectory() as directory:
        joint_path = Path(directory) / 'joint.sqlite'
        result_id = _joint_shift(joint_path)
        _reopen_and_show(joint_path, result_id)
        _late_registration_shift(Path(directory) / 'retained.sqlite', history_limit=2)
        _late_registration_shift(Path(directory) / 'empty-history.sqlite', history_limit=0)


if __name__ == '__main__':
    main()

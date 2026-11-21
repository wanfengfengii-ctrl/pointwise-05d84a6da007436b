import sqlite3
from contextlib import closing
from fractions import Fraction

from archive import StoredEvidenceError, get, seal
from archive.results import ResultStore
from database import open_database
from evidence.coverage import coverage
from evidence.frames import FrameConflict
from evidence.intervals import Interval
from registry import initialize_defaults
from samples.portal_fixture import STATION, ScriptedPortal
from samples.switch_inputs import (
    BEFORE_CUT, BEFORE_LATE_START, CUT, END, LATE_START, NEW, OLD, START, TAIL,
)
from sessions.driver import step
from sessions.opening import start
from sessions.runs import RunStatus, RunStore, SwitchStore
from sessions.switching import fetch_history, switch
from tests import ShiftCase


class SwitchCase(ShiftCase):
    def _portal(self, *, history_limit=4):
        return ScriptedPortal(first_seq=START, history_limit=history_limit)

    def _open_switched(self, portal, *, cut=CUT, advance_after=True):
        run_id = start(self.db, portal, STATION, self.default_id, OLD, START)
        old_handle = portal.active_handles[0]
        portal.advance(*BEFORE_CUT)
        self.assertIsNone(switch(self.db, portal, run_id, NEW, cut))
        new_handle = portal.active_handles[1]
        self.assertEqual(SwitchStore(self.db).for_run(run_id).new_first_seq, CUT)
        if advance_after:
            portal.advance(*BEFORE_LATE_START, *TAIL)
        return run_id, old_handle, new_handle

    def _prove_old(self, portal, handle):
        for seq in (99, 101, 102):
            portal.emit_frame(handle, seq)
        portal.emit_receipt(handle, START, END)

    def _prove_new_tail(self, portal, handle):
        portal.emit_frame(handle, 103)
        portal.emit_receipt(handle, LATE_START, END)

    def _prove_new_head(self, portal, handle):
        portal.emit_frame(handle, CUT)
        portal.emit_receipt(handle, CUT, LATE_START)


class TestJointScenario(SwitchCase):
    def test_pending_then_complete_seal_and_reopen(self):
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal)
        self._prove_old(portal, old_handle)
        self._prove_new_tail(portal, new_handle)
        report = step(self.db, portal, run_id)
        with self.subTest(case_id='switch.position_remains_original'):
            self.assertEqual(report.position, RunStore(self.db).load(run_id).position)
            self.assertEqual(report.status, RunStatus.ACTIVE)
        pending = seal(self.db, run_id, END, portal=portal)
        with self.subTest(case_id='switch.pending_head'):
            self.assertIsNone(pending.result_id)
            self.assertEqual(pending.missing, (Interval(CUT, LATE_START),))
            self.assertEqual(pending.unavailable, ())
            self.assertIsNone(ResultStore(self.db).find_by_run(run_id))
            self.assertEqual(RunStore(self.db).load(run_id).status, RunStatus.ACTIVE)
        self._prove_new_head(portal, new_handle)
        step(self.db, portal, run_id)
        fixed = seal(self.db, run_id, END, portal=portal)
        self.assertIsNotNone(fixed.result_id)
        self.assertIsNone(fixed.release_error)
        self.assertEqual(portal.active_handles, ())
        # Same cutoff retry returns the same result id without re-registering.
        again = seal(self.db, run_id, END, portal=portal)
        self.assertEqual(again.result_id, fixed.result_id)
        with self.assertRaisesRegex(ValueError, 'different cutoff'):
            seal(self.db, run_id, END + 1, portal=portal)
        # Stop the source and reopen with fixed evidence only.
        portal.shutdown()
        artifact = get(self.reopen(), fixed.result_id)
        with self.subTest(case_id='switch.frozen_frames'):
            self.assertEqual(
                [(f.seq, f.channel, f.value) for f in artifact.frames],
                [(99, 'salinity', 7), (100, 'turbidity', 8), (103, 'turbidity', 11)],
            )
            self.assertEqual(artifact.no_match, (Interval(98, 99), Interval(101, 103)))
            self.assertEqual(artifact.cut, CUT)
            self.assertEqual((artifact.old_condition, artifact.new_condition), (OLD, NEW))
            self.assertEqual(artifact.interval, Interval(START, END))
        means = {summary.channel: summary.mean for summary in artifact.quality.summaries}
        with self.subTest(case_id='switch.fixed_metrology'):
            self.assertEqual(means['salinity'], Fraction(7))
            self.assertEqual(means['turbidity'], Fraction(19, 2))
        # Every frozen frame keeps its original observer and provenance.
        self.assertTrue(all(frame.observer and frame.provenance for frame in artifact.frames))
        self.assertEqual(
            [frame.observer for frame in artifact.frames],
            ['qingwan/observer-1', 'qingwan/observer-2', 'qingwan/observer-2'],
        )

    def test_coverage_reports_side_aware_spans(self):
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal)
        self._prove_old(portal, old_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        report = coverage(self.db, run_id, START, END)
        self.assertEqual(report.missing, (Interval(CUT, LATE_START),))
        self.assertIn(Interval(START, CUT), report.proven)
        self.assertIn(Interval(LATE_START, END), report.proven)


class TestSwitchValidation(SwitchCase):
    def test_illegal_arguments(self):
        portal = self._portal()
        run_id = start(self.db, portal, STATION, self.default_id, OLD, START)
        portal.advance(*BEFORE_CUT)
        with self.assertRaises(KeyError):
            switch(self.db, portal, run_id + 100, NEW, CUT)
        for bad in (-1, START - 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    switch(self.db, portal, run_id, NEW, bad)
        with self.assertRaises(ValueError):
            switch(self.db, portal, run_id, ('unknown-channel',), CUT)
        with self.assertRaises(ValueError):
            switch(self.db, portal, run_id, (), CUT)
        # Failed switch must not leave an observer behind.
        handles_before = portal.active_handles
        with self.assertRaises(ValueError):
            switch(self.db, portal, run_id, ('unknown-channel',), CUT)
        self.assertEqual(portal.active_handles, handles_before)

    def test_one_switch_per_shift_and_no_switch_after_seal(self):
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal)
        with self.assertRaisesRegex(ValueError, 'already switched'):
            switch(self.db, portal, run_id, OLD, CUT)
        self._prove_old(portal, old_handle)
        portal.emit_frame(new_handle, 100)
        portal.emit_frame(new_handle, 103)
        portal.emit_receipt(new_handle, CUT, LATE_START)
        portal.emit_receipt(new_handle, LATE_START, END)
        step(self.db, portal, run_id)
        self.assertIsNotNone(seal(self.db, run_id, END, portal=portal).result_id)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            switch(self.db, portal, run_id, NEW, CUT)


class TestRedeliveryAndConflict(SwitchCase):
    def test_redelivery_keeps_first_piece_conflict_keeps_prefix(self):
        portal = self._portal()
        run_id, _, new_handle = self._open_switched(portal)
        portal.emit_frame(new_handle, 100)
        step(self.db, portal, run_id)
        # Identical redelivery from another provenance: first piece retained.
        portal.emit_frame(new_handle, 100, provenance='redelivery/100')
        self.assertEqual(step(self.db, portal, run_id).new_frames, 0)
        rows = self.db.execute(
            'SELECT provenance FROM frames WHERE run_id = ? AND seq = ?',
            (run_id, '100'),
        ).fetchall()
        self.assertEqual([row['provenance'] for row in rows], ['qingwan/source/100'])
        # Conflicting content: FrameConflict, accepted prefix position preserved.
        from evidence.frames import RawFrame
        portal.append_event(
            new_handle, RawFrame(100, 'turbidity', 999, new_handle, 'conflict/100')
        )
        portal.emit_frame(new_handle, 103)
        with self.assertRaises(FrameConflict):
            step(self.db, portal, run_id)
        self.assertEqual(SwitchStore(self.db).for_run(run_id).new_position, 2)
        # The valid later frame is still waiting; after the bad event is skipped
        # it can be acknowledged (portal poll replays until acknowledged).
        with self.assertRaises(FrameConflict):
            step(self.db, portal, run_id)

    def test_bad_new_event_preserves_both_accepted_prefixes(self):
        from evidence.frames import RawFrame
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal, advance_after=False)
        portal.advance(*BEFORE_LATE_START)
        portal.emit_frame(old_handle, 99)
        portal.append_event(
            new_handle, RawFrame(100, 'salinity', 8, new_handle, 'bad/100')
        )
        with self.assertRaises(ValueError):
            step(self.db, portal, run_id)
        # Old side accepted its frame; the new side accepted nothing.
        self.assertEqual(RunStore(self.db).load(run_id).position, 1)
        self.assertEqual(SwitchStore(self.db).for_run(run_id).new_position, 0)
        # The bad event remains at the head of the new observation stream.
        with self.assertRaises(ValueError):
            step(self.db, portal, run_id)
        self.assertEqual(RunStore(self.db).load(run_id).position, 1)


class TestOutOfCutEvidence(SwitchCase):
    def test_out_of_cut_frame_and_crossing_receipt(self):
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal)
        # Old observer holds an out-of-cut frame (seq 102): stored but never authoritative.
        self._prove_old(portal, old_handle)
        self._prove_new_head(portal, new_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        fixed = seal(self.db, run_id, END, portal=portal)
        artifact = get(self.db, fixed.result_id)
        self.assertEqual([frame.seq for frame in artifact.frames], [99, 100, 103])
        # The old [98,104) receipt only certifies the old side [98,100).
        self.assertEqual(artifact.no_match, (Interval(98, 99), Interval(101, 103)))
        old_receipt, = (r for r in artifact.receipts if r.observer == old_handle)
        self.assertEqual(old_receipt.interval, Interval(START, END))

    def test_cross_observer_conflict_blocks_seal_identical_redelivery_keeps_first(self):
        from evidence.frames import FrameLedger, RawFrame
        for differing in (True, False):
            with self.subTest(differing=differing):
                portal = self._portal(history_limit=2)
                run_id = start(self.db, portal, STATION, self.default_id, OLD, START)
                old_handle = portal.active_handles[0]
                portal.advance(*BEFORE_CUT, *BEFORE_LATE_START)
                switch(self.db, portal, run_id, NEW, CUT)
                new_handle = portal.active_handles[1]
                fetch_history(self.db, portal, run_id, CUT, LATE_START)
                portal.advance(*TAIL)
                value = 888 if differing else 8
                FrameLedger(self.db).record(
                    run_id, RawFrame(100, 'turbidity', value, new_handle, 'replica/100')
                )
                for seq in (99, 101, 102):
                    portal.emit_frame(old_handle, seq)
                portal.emit_receipt(old_handle, START, END)
                portal.emit_frame(new_handle, 103)
                portal.emit_receipt(new_handle, LATE_START, END)
                step(self.db, portal, run_id)
                if differing:
                    with self.assertRaises(FrameConflict):
                        seal(self.db, run_id, END, portal=portal)
                    self.assertIsNone(ResultStore(self.db).find_by_run(run_id))
                else:
                    fixed = seal(self.db, run_id, END, portal=portal)
                    artifact = get(self.db, fixed.result_id)
                    hundred, = (f for f in artifact.frames if f.seq == 100)
                    self.assertEqual(hundred.observer, 'qingwan/history')
                    self.assertEqual(hundred.provenance, 'qingwan/source/100')


class TestLateRegistrationHistory(SwitchCase):
    def _late_portal_run(self, portal, *, fetch=True):
        run_id = start(self.db, portal, STATION, self.default_id, OLD, START)
        old_handle = portal.active_handles[0]
        portal.advance(*BEFORE_CUT, *BEFORE_LATE_START)  # next_seq now 102
        switch(self.db, portal, run_id, NEW, CUT)
        new_handle = portal.active_handles[1]
        self.assertEqual(SwitchStore(self.db).for_run(run_id).new_first_seq, LATE_START)
        return run_id, old_handle, new_handle

    def test_retained_history_completes_with_history_identity(self):
        portal = self._portal(history_limit=2)
        run_id, old_handle, new_handle = self._late_portal_run(portal)
        intake = fetch_history(self.db, portal, run_id, CUT, LATE_START)
        self.assertEqual(intake.new_frames, 1)
        self.assertEqual(intake.unavailable, ())
        # Repeated history intake is idempotent and keeps the first provenance.
        again = fetch_history(self.db, portal, run_id, CUT, LATE_START)
        self.assertEqual(again.new_frames, 0)
        portal.advance(*TAIL)
        self._prove_old(portal, old_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        fixed = seal(self.db, run_id, END, portal=portal)
        self.assertIsNotNone(fixed.result_id)
        artifact = get(self.db, fixed.result_id)
        hundred, = (frame for frame in artifact.frames if frame.seq == 100)
        self.assertEqual(hundred.observer, 'qingwan/history')
        self.assertEqual(hundred.provenance, 'qingwan/source/100')

    def test_zero_history_capacity_is_unavailable(self):
        portal = self._portal(history_limit=0)
        run_id, old_handle, new_handle = self._late_portal_run(portal)
        portal.advance(*TAIL)
        self._prove_old(portal, old_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        outcome = seal(self.db, run_id, END, portal=portal)
        self.assertIsNone(outcome.result_id)
        self.assertEqual(outcome.unavailable, (Interval(CUT, LATE_START),))
        self.assertEqual(outcome.missing, ())

    def test_expired_history_blocks_but_early_late_receipt_still_pending(self):
        portal = self._portal(history_limit=2)
        run_id, old_handle, new_handle = self._late_portal_run(portal)
        portal.advance(*TAIL)  # history [100,102) now expired
        self._prove_old(portal, old_handle)
        step(self.db, portal, run_id)
        outcome = seal(self.db, run_id, END, portal=portal)
        # Never-eligible head confirmed unavailable; eligible tail still missing.
        self.assertEqual(outcome.unavailable, (Interval(CUT, LATE_START),))
        self.assertEqual(outcome.missing, (Interval(LATE_START, END),))

    def test_early_registration_survives_later_history_expiry(self):
        portal = self._portal(history_limit=2)
        run_id, old_handle, new_handle = self._open_switched(portal)
        # History expires, but the real-time new observation was eligible at 100.
        self._prove_old(portal, old_handle)
        self._prove_new_head(portal, new_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        fixed = seal(self.db, run_id, END, portal=portal)
        self.assertIsNotNone(fixed.result_id)
        self.assertEqual(
            [frame.seq for frame in get(self.db, fixed.result_id).frames], [99, 100, 103]
        )


class TestCommitFailure(SwitchCase):
    def test_failed_commit_preserves_retry_state(self):
        portal = self._portal()
        run_id, old_handle, new_handle = self._open_switched(portal)
        self._prove_old(portal, old_handle)
        portal.emit_frame(new_handle, 100)
        portal.emit_frame(new_handle, 103)
        portal.emit_receipt(new_handle, CUT, LATE_START)
        portal.emit_receipt(new_handle, LATE_START, END)
        step(self.db, portal, run_id)
        self.db.executescript('''
            CREATE TABLE allowed_commit(run_id INTEGER PRIMARY KEY);
            CREATE TABLE commit_guard (
                run_id INTEGER REFERENCES allowed_commit(run_id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER reject_commit AFTER UPDATE OF status ON runs
            WHEN NEW.status = 'sealed'
            BEGIN INSERT INTO commit_guard(run_id) VALUES (NEW.run_id); END;
        ''')
        with self.assertRaises(sqlite3.IntegrityError):
            seal(self.db, run_id, END, portal=portal)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(RunStore(self.db).load(run_id).status, RunStatus.ACTIVE)
        self.assertIsNone(ResultStore(self.db).find_by_run(run_id))
        # Observations stay available before a successful publish.
        self.assertEqual(set(portal.active_handles), {old_handle, new_handle})
        self.db.execute('DROP TRIGGER reject_commit')
        retried = seal(self.db, run_id, END, portal=portal)
        self.assertIsNotNone(retried.result_id)
        self.assertEqual(portal.active_handles, ())
        self.assertEqual(
            seal(self.db, run_id, END, portal=portal).result_id, retried.result_id
        )


class TestDamagedSwitchEvidence(SwitchCase):
    def _sealed_switch(self, portal):
        run_id, old_handle, new_handle = self._open_switched(portal)
        self._prove_old(portal, old_handle)
        self._prove_new_head(portal, new_handle)
        self._prove_new_tail(portal, new_handle)
        step(self.db, portal, run_id)
        return seal(self.db, run_id, END, portal=portal).result_id

    def test_tampered_payload_is_rejected(self):
        import json
        from archive.switch_artifacts import switch_artifact_digest
        portal = self._portal()
        result_id = self._sealed_switch(portal)
        portal.shutdown()
        damaged_path = self.path.with_name('damaged-switch.sqlite')
        with closing(open_database(damaged_path)) as damaged:
            self.db.backup(damaged)
            stored = ResultStore(damaged).load(result_id)
            data = json.loads(stored.payload)
            data['cut'] = CUT + 1
            modified = json.dumps(data, sort_keys=True, separators=(',', ':'))
            damaged.execute(
                'UPDATE results SET payload = ? WHERE result_id = ?',
                (modified, result_id),
            )
            with self.assertRaisesRegex(StoredEvidenceError, 'digest mismatch'):
                get(damaged, result_id)
            damaged.execute(
                'UPDATE results SET digest = ? WHERE result_id = ?',
                (switch_artifact_digest(modified), result_id),
            )
            with self.assertRaises(StoredEvidenceError):
                get(damaged, result_id)
        # The intact database still verifies.
        self.assertEqual(get(self.reopen(), result_id).cut, CUT)


class TestEmptySides(SwitchCase):
    def test_cut_equals_start_and_end(self):
        portal = ScriptedPortal(first_seq=10)
        run_id = start(self.db, portal, STATION, self.default_id, ('salinity',), 10)
        old = portal.active_handles[0]
        switch(self.db, portal, run_id, ('turbidity',), 10)  # empty old side
        new = portal.active_handles[1]
        portal.advance(('turbidity', 5), ('salinity', 6))
        portal.emit_frame(new, 10)
        portal.emit_receipt(new, 10, 12)
        portal.emit_frame(old, 11)
        portal.emit_receipt(old, 10, 12)
        step(self.db, portal, run_id)
        fixed = seal(self.db, run_id, 12, portal=portal)
        artifact = get(self.db, fixed.result_id)
        self.assertEqual(artifact.no_match, (Interval(11, 12),))
        self.assertEqual([frame.seq for frame in artifact.frames], [10])
        means = {item.channel: item.mean for item in artifact.quality.summaries}
        self.assertIsNone(means['salinity'])
        self.assertEqual(means['turbidity'], Fraction(5))

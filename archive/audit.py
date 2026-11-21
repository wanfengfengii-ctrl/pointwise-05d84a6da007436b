import json

from archive.artifacts import artifact_digest, decode_artifact, encode_artifact
from archive.eligibility import validate_evidence_binding
from archive.results import ResultStore, result_id_for
from archive.switch_artifacts import (
    decode_switch_artifact, encode_switch_artifact,
    expected_switch_quality, switch_artifact_digest,
)
from evidence.intake import CaptureContext
from evidence.intervals import Interval, normalize_intervals, validate_interval
from evidence.switching import assess_side, side_views
from quality.evaluation import evaluate_shift
from registry.channels import normalize_channels, require_name
from sessions.runs import validate_position


class StoredEvidenceError(ValueError):
    pass


def verify_artifact(stored):
    try:
        if type(stored.payload) is not str or artifact_digest(stored.payload) != stored.digest:
            raise ValueError('artifact digest mismatch')
        version = json.loads(stored.payload).get('version')
        if version == 2:
            return _verify_switch(stored)
        artifact = decode_artifact(stored.payload)
        if encode_artifact(artifact) != stored.payload:
            raise ValueError('noncanonical artifact')
        validate_position(artifact.run_id, 'run identity')
        validate_position(artifact.first_seq, 'first eligible sequence')
        validate_interval(artifact.interval)
        require_name(artifact.observer, 'observer')
        for interval in artifact.no_match:
            validate_interval(interval, allow_empty=False)
        if (artifact.run_id == 0 or stored.run_id != artifact.run_id
                or artifact.result_id != stored.result_id
                or artifact.result_id != result_id_for(artifact.run_id)
                or stored.end != artifact.interval.end
                or artifact.station != artifact.revision.station
                or artifact.condition != normalize_channels(artifact.condition)):
            raise ValueError('artifact identity or fixed binding mismatch')
        context = CaptureContext(
            artifact.run_id, artifact.interval.start, artifact.condition,
            artifact.observer, artifact.first_seq,
        )
        report = validate_evidence_binding(context, artifact.interval, artifact.frames, artifact.receipts)
        if report.missing or report.no_match != artifact.no_match:
            raise ValueError('frozen coverage does not match original evidence')
        expected = evaluate_shift(artifact.frames, artifact.revision, artifact.condition)
        if expected != artifact.quality:
            raise ValueError('frozen quality does not match the original evidence')
        return artifact
    except (TypeError, AttributeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StoredEvidenceError(f'damaged evidence for {stored.result_id!r}: {exc}') from exc


def _verify_switch(stored):
    if switch_artifact_digest(stored.payload) != stored.digest:
        raise ValueError('artifact digest mismatch')
    artifact = decode_switch_artifact(stored.payload)
    if encode_switch_artifact(artifact) != stored.payload:
        raise ValueError('noncanonical artifact')
    validate_position(artifact.run_id, 'run identity')
    validate_position(artifact.cut, 'cut sequence')
    validate_interval(artifact.interval)
    if (artifact.run_id == 0 or stored.run_id != artifact.run_id
            or artifact.result_id != stored.result_id
            or artifact.result_id != result_id_for(artifact.run_id)
            or stored.end != artifact.interval.end
            or artifact.station != artifact.revision.station):
        raise ValueError('artifact identity or fixed binding mismatch')
    if artifact.old_condition != normalize_channels(artifact.old_condition):
        raise ValueError('invalid old condition')
    if artifact.new_condition != normalize_channels(artifact.new_condition):
        raise ValueError('invalid new condition')
    if not (artifact.interval.start <= artifact.cut <= artifact.interval.end):
        raise ValueError('cut lies outside the frozen interval')
    if tuple(frame.seq for frame in artifact.frames) != tuple(sorted(
            frame.seq for frame in artifact.frames)) or len({
                frame.seq for frame in artifact.frames
            }) != len(artifact.frames):
        raise ValueError('frames must be unique and ordered by source sequence')
    for frame in artifact.frames:
        side_condition = (artifact.old_condition if frame.seq < artifact.cut
                          else artifact.new_condition)
        if frame.channel not in side_condition:
            raise ValueError(f'frame {frame.seq} is outside its side condition')
        require_name(frame.observer, 'observer')
        require_name(frame.provenance, 'frame provenance')
    for interval in artifact.no_match:
        validate_interval(interval, allow_empty=False)
    # Re-derive per-side coverage from the frozen receipts and frames.
    switch_record = _SwitchRecord(artifact)
    run = _FrozenRun(artifact)
    views = side_views(run, switch_record, artifact.interval.end)
    history_observer = f'{artifact.station}/history'
    for receipt in artifact.receipts:
        require_name(receipt.observer, 'observer')
        require_name(receipt.provenance, 'receipt provenance')
        if not (receipt.observer == history_observer
                or receipt.observer.startswith(f'{artifact.station}/observer-')):
            raise ValueError('receipt observer is outside the station observation scope')
    sides = tuple(assess_side(view, artifact.frames, artifact.receipts) for view in views)
    for side in sides:
        if side.missing:
            raise ValueError('frozen coverage is incomplete')
        for receipt in side.receipts:
            if receipt.condition != side.view.condition:
                raise ValueError('receipt does not certify the side condition')
    expected_no_match = normalize_intervals(tuple(
        interval for side in sides for interval in side.no_match
    ))
    if expected_no_match != artifact.no_match:
        raise ValueError('frozen no-match spans do not match the evidence')
    expected_frames = tuple(sorted(
        (frame for side in sides for frame in side.frames),
        key=lambda frame: frame.seq,
    ))
    if expected_frames != artifact.frames:
        raise ValueError('frozen frames do not match the authoritative selection')
    if expected_switch_quality(artifact) != artifact.quality:
        raise ValueError('frozen quality does not match the original evidence')
    return artifact


class _SwitchRecord:
    def __init__(self, artifact):
        self.cut_seq = artifact.cut
        self.new_condition = artifact.new_condition
        self.new_observer = ''
        self.new_first_seq = 0
        self.new_position = 0


class _FrozenRun:
    def __init__(self, artifact):
        self.run_id = artifact.run_id
        self.station = artifact.station
        self.revision_id = artifact.revision.revision_id
        self.start_seq = artifact.interval.start
        self.condition = artifact.old_condition
        self.observer = ''
        self.first_seq = 0


def get(db, result_id):
    try:
        stored = ResultStore(db).load(result_id)
    except (TypeError, ValueError) as exc:
        raise StoredEvidenceError(f'damaged result index for {result_id!r}') from exc
    return verify_artifact(stored)

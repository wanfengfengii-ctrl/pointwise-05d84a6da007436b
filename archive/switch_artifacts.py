"""Frozen artifact for a run that switched condition once mid-shift."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from evidence.frames import RawFrame
from evidence.intervals import Interval
from evidence.receipts import CoverageReceipt
from quality.evaluation import QualityReport, evaluate_shift
from registry.calibrations import StationRevision
from sessions.runs import validate_position
from archive.artifacts import _quality_data, _read_quality


@dataclass(frozen=True)
class SwitchArchiveArtifact:
    result_id: str
    run_id: int
    station: str
    interval: Interval
    cut: int
    old_condition: tuple[str, ...]
    new_condition: tuple[str, ...]
    revision: StationRevision
    frames: tuple[RawFrame, ...]
    receipts: tuple[CoverageReceipt, ...]
    no_match: tuple[Interval, ...]
    quality: QualityReport


def encode_switch_artifact(artifact):
    data = {
        'version': 2,
        'result_id': artifact.result_id,
        'run_id': artifact.run_id,
        'station': artifact.station,
        'interval': asdict(artifact.interval),
        'cut': artifact.cut,
        'old_condition': artifact.old_condition,
        'new_condition': artifact.new_condition,
        'revision': artifact.revision.to_data(),
        'frames': [asdict(frame) for frame in artifact.frames],
        'receipts': [asdict(receipt) for receipt in artifact.receipts],
        'no_match': [asdict(interval) for interval in artifact.no_match],
        'quality': _quality_data(artifact.quality),
    }
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False)


def decode_switch_artifact(payload):
    data = json.loads(payload)
    receipts = tuple(CoverageReceipt(
        item['receipt_id'], Interval(**item['interval']), tuple(item['condition']),
        item['observer'], item['provenance'],
    ) for item in data['receipts'])
    return SwitchArchiveArtifact(
        data['result_id'], data['run_id'], data['station'], Interval(**data['interval']),
        validate_position(data['cut'], 'cut sequence'),
        tuple(data['old_condition']), tuple(data['new_condition']),
        StationRevision.from_data(data['revision']),
        tuple(RawFrame(**item) for item in data['frames']), receipts,
        tuple(Interval(**item) for item in data['no_match']),
        _read_quality(data['quality']),
    )


def switch_artifact_digest(payload):
    return sha256(payload.encode('utf-8')).hexdigest()


def expected_switch_quality(artifact):
    """Recompute statistics separately for the channels of each side condition."""
    return switch_quality(
        artifact.frames, artifact.revision, artifact.cut,
        artifact.old_condition, artifact.new_condition,
    )


def switch_quality(frames, revision, cut, old_condition, new_condition):
    """Per-side conversion, limits and channel means for a switched shift."""
    old_frames = tuple(frame for frame in frames if frame.seq < cut)
    new_frames = tuple(frame for frame in frames if frame.seq >= cut)
    old_report = evaluate_shift(old_frames, revision, old_condition)
    new_report = evaluate_shift(new_frames, revision, new_condition)
    readings = tuple(sorted(
        old_report.readings + new_report.readings,
        key=lambda assessed: assessed.reading.seq,
    ))
    summaries = tuple(sorted(
        old_report.summaries + new_report.summaries,
        key=lambda summary: summary.channel,
    ))
    return QualityReport(readings, summaries)

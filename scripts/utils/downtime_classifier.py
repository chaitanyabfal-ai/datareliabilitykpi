"""
Downtime classifier -- rule-based root-cause analysis for sensor downtime.

This implements the ILDS downtime decision tree from the reliability design
discourse. Downtime is multi-dimensional; the classifier walks the categories
in priority order and assigns the first cause for which the corresponding
detector returns a positive signal:

  1. Network            (Airtel LTE outage, Wi-Fi/VPN, DNS/firewall, S3 throttling)
  2. Hardware           (sensor malfunction, power failure, battery, SIM, cable, clock drift)
  3. Software           (firmware bug, EC2/memory, schema validation, misconfigured thresholds)
  4. Operational        (planned maintenance, pigging, valve ops, recalibration, ESD)
  5. Environmental      (extreme weather, temperature extremes, dust, earthquake)
  6. Cybersecurity       (unauthorized access, DDoS, API rate limits, credential leaks)
  7. Data pipeline      (S3 upload, SNS, SQS backlog, EC2 processing delays)
  8. Human error        (incorrect placement, manual override, mislabelled data, forgotten power-off)
  9. Unknown            (no detector matched)

All detectors are injectable so this module stays pure and unit-testable:
each detector is a callable (sensor_id, start, end, context) -> Detection
or None. External detectors that need network/credentials are wrapped in
the scripts/utils/detectors package and disabled (returning None) when not
configured, so the pipeline runs offline by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from config.aws_config import RELIABILITY
from scripts.utils.uptime_engine import parse_timestamp


@dataclass
class Detection:
    cause: str
    category: str
    confidence: float = 1.0
    evidence: dict = field(default_factory=dict)


Detector = Callable[[str, datetime, datetime, dict], "Detection | None"]


# Ordered categories -> the canonical cause labels for each. The classifier
# stops at the first category that yields a Detection, mirroring the design
# decision tree (network before hardware before software, etc.).
CATEGORY_ORDER = [
    "network",
    "hardware",
    "software",
    "operational",
    "environmental",
    "cybersecurity",
    "pipeline",
    "human",
]


class DowntimeClassifier:
    """Holds an ordered list of (category, detector) pairs and classifies downtime."""

    def __init__(self, detectors: list[tuple[str, Detector]] | None = None):
        # Detectors are grouped by category but evaluated in category order.
        self._detectors: list[tuple[str, Detector]] = list(detectors or [])

    def register(self, category: str, detector: Detector) -> None:
        self._detectors.append((category, detector))

    def classify(
        self,
        sensor_id: str,
        start: datetime,
        end: datetime,
        context: dict | None = None,
    ) -> Detection:
        context = context or {}
        for category in CATEGORY_ORDER:
            for cat, detector in self._detectors:
                if cat != category:
                    continue
                try:
                    detection = detector(sensor_id, start, end, context)
                except Exception:
                    detection = None
                if detection is not None:
                    if not detection.category:
                        detection = Detection(
                            cause=detection.cause, category=category,
                            confidence=detection.confidence, evidence=detection.evidence,
                        )
                    return detection
        return Detection(cause="Unknown Downtime", category="unknown", confidence=0.0)


def classify_downtime_event(
    sensor_id: str,
    downtime_start,
    downtime_end,
    context: dict | None = None,
    classifier: DowntimeClassifier | None = None,
) -> Detection:
    """Convenience wrapper used by the poller/aggregator.

    `downtime_start`/`downtime_end` may be ISO strings, epoch seconds, or datetimes.
    If no classifier is supplied, a default one is built from the configured
    (possibly disabled) detectors in scripts.utils.detectors.
    """
    start = parse_timestamp(downtime_start)
    end = parse_timestamp(downtime_end)
    if start is None or end is None:
        return Detection(cause="Unknown Downtime", category="unknown", confidence=0.0)
    if end < start:
        start, end = end, start
    if classifier is None:
        classifier = build_default_classifier()
    return classifier.classify(sensor_id, start, end, context)


def build_default_classifier() -> DowntimeClassifier:
    """Assemble the classifier from all available detectors.

    Importing here (not at module top) keeps the classifier unit-testable in
    isolation and avoids importing boto3/network clients unless needed.
    """
    from scripts.utils.detectors import (
        airtel_outage_detector,
        connectivity_detector,
        hardware_detector,
        software_detector,
        operational_detector,
        environmental_detector,
        cybersecurity_detector,
        pipeline_detector,
        human_error_detector,
    )

    classifier = DowntimeClassifier()
    classifier.register("network", airtel_outage_detector)
    classifier.register("network", connectivity_detector)
    classifier.register("hardware", hardware_detector)
    classifier.register("software", software_detector)
    classifier.register("operational", operational_detector)
    classifier.register("environmental", environmental_detector)
    classifier.register("cybersecurity", cybersecurity_detector)
    classifier.register("pipeline", pipeline_detector)
    classifier.register("human", human_error_detector)
    return classifier


def cause_bucket(cause: str) -> str:
    """Map a cause label back to its category for reporting/dashboard grouping."""
    if cause in RELIABILITY.planned_causes:
        return "planned"
    if cause in RELIABILITY.uncontrollable_causes:
        return "uncontrollable"
    if cause == "Unknown Downtime":
        return "unknown"
    return "true_downtime"

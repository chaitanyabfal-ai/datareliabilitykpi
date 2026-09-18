"""Cause-detector plugins for downtime root-cause analysis (rule-based).

The detectors live in ``detectors.py`` and are re-exported here so the
classifier can import them as ``from scripts.utils.detectors import ...``.
All logic is rule-based -- no machine learning.
"""
from scripts.utils.detectors.detectors import (
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

__all__ = [
    "airtel_outage_detector",
    "connectivity_detector",
    "hardware_detector",
    "software_detector",
    "operational_detector",
    "environmental_detector",
    "cybersecurity_detector",
    "pipeline_detector",
    "human_error_detector",
]

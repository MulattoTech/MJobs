"""Evidence-based ATS detection, separate from ATS application support."""

from services.ats_detection.detector import ATSDetectionResult, detect_ats

__all__ = ["ATSDetectionResult", "detect_ats"]

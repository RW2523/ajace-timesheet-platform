"""Format-specific extractors. Each ``extract(path) -> RawExtraction`` turns one
file into the common raw layer. The orchestrator picks which one to run based on
``detect.detect_file``."""
from .detect import detect_file, DetectedFile

__all__ = ["detect_file", "DetectedFile"]

"""
CSD FARS -- Liveness Detection
------------------------------
Provides anti-spoofing capabilities by detecting natural eye blinks
using the Eye Aspect Ratio (EAR) metric over consecutive frames.
"""

import math
import time
from typing import List, Tuple, Dict
try:
    import face_recognition
except ImportError:
    face_recognition = None

import config
from core import get_logger

logger = get_logger("liveness")


def _euclidean_distance(pt1: Tuple[int, int], pt2: Tuple[int, int]) -> float:
    """Calculates the Euclidean distance between two 2D points."""
    return math.dist(pt1, pt2)


def calculate_ear(eye_points: List[Tuple[int, int]]) -> float:
    """
    Calculates the Eye Aspect Ratio (EAR) given 6 facial landmarks for an eye.
    
    The EAR formula: (||p2 - p6|| + ||p3 - p5||) / (2 * ||p1 - p4||)
    where p1..p6 are the eye landmarks starting from the outer corner clockwise.
    """
    if len(eye_points) != 6:
        return 1.0 # Invalid points length, default to open

    # Vertical eye distances
    v1 = _euclidean_distance(eye_points[1], eye_points[5])
    v2 = _euclidean_distance(eye_points[2], eye_points[4])
    
    # Horizontal eye distance
    h = _euclidean_distance(eye_points[0], eye_points[3])
    
    if h == 0:
        return 1.0 # Avoid division by zero
        
    ear = (v1 + v2) / (2.0 * h)
    return ear


class LivenessTracker:
    """
    Tracks the blink state of a face across consecutive frames to verify liveness.
    """
    def __init__(self, tracker_id: int):
        self.tracker_id = tracker_id
        self.consecutive_frames_below_threshold = 0
        self.is_live = False
        self.confirmed_at = None
        self.frame_count = 0

    def reset(self):
        """Resets the liveness state."""
        self.is_live = False
        self.confirmed_at = None
        self.consecutive_frames_below_threshold = 0
        self.frame_count = 0

    def update(self, rgb_frame, bounding_box: Tuple[int, int, int, int], session_context: dict = None, require_liveness: bool = None) -> bool:
        """
        Updates the liveness state using the current frame and tracked bounding box.
        
        Args:
            rgb_frame: The current video frame in RGB format.
            bounding_box: A tuple (top, right, bottom, left) of the face location.
            session_context: Optional dict containing parameter overrides.
            require_liveness: When false, blink verification is advisory and does
                not block recognition.
            
        Returns:
            True if liveness has been confirmed (now or previously), False otherwise.
        """
        if require_liveness is None:
            require_liveness = getattr(config, "REQUIRE_LIVENESS", False)
        if not require_liveness:
            return True

        ttl = getattr(config, "LIVENESS_TTL_SECONDS", 10.0)
        if session_context is not None:
            overrides = session_context.get("overrides", {})
            if isinstance(overrides, dict):
                ttl = overrides.get("LIVENESS_TTL_SECONDS", ttl)
        try:
            ttl = min(30.0, max(5.0, float(ttl)))
        except (TypeError, ValueError):
            ttl = 10.0
        if self.is_live:
            if self.confirmed_at is not None and time.monotonic() - self.confirmed_at <= ttl:
                return True
            self.reset()

        self.frame_count += 1

        if self.frame_count <= 30:
            interval = 2
        elif self.frame_count <= 120:
            interval = 10
        else:
            interval = 30

        if self.frame_count % interval != 0:
            return self.is_live

        landmarks = face_recognition.face_landmarks(rgb_frame, face_locations=[bounding_box])
        
        if not landmarks:
            self.reset()
            return False
            
        face_marks = landmarks[0]
        left_eye = face_marks.get("left_eye")
        right_eye = face_marks.get("right_eye")
        
        if not left_eye or not right_eye:
            self.reset()
            return False
            
        left_ear = calculate_ear(left_eye)
        right_ear = calculate_ear(right_eye)
        
        # Average the EAR of both eyes
        avg_ear = (left_ear + right_ear) / 2.0
        
        if avg_ear < config.EAR_BLINK_THRESHOLD:
            self.consecutive_frames_below_threshold += 1
        else:
            # If the EAR goes back above the threshold, check if we had enough
            # consecutive closed frames to register a full blink.
            if self.consecutive_frames_below_threshold >= config.EAR_CONSEC_FRAMES:
                self.is_live = True
                self.confirmed_at = time.monotonic()
                logger.info(f"Liveness confirmed for tracker ID {self.tracker_id}")
            
            # Reset the counter
            self.consecutive_frames_below_threshold = 0
            
        return self.is_live

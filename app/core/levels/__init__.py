"""
Waterfall Levels for the Predator Browser.

Level 1 (Shadow API): Network interception - 0 cost, max speed
Level 2 (Blind Map): Accessibility Tree - Low cost
Level 3 (Eagle Eye): Vision with Set-of-Marks - High cost
"""

from app.core.levels.sniffer import Sniffer
from app.core.levels.navigator import Navigator
from app.core.levels.vision import VisionEngine

__all__ = ["Sniffer", "Navigator", "VisionEngine"]

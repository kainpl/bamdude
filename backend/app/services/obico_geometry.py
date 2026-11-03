"""Validate server-returned tuples; derive display geometry only on reads.

ONNX/RKNN upstream use frame width for box height. /p/ does not identify the
backend, so correcting that by guessing would also corrupt valid Darknet boxes.
"""

import math

PROFILE = "obico_center_xywh_server_returned_v1"
MAX_BOXES = 256


def _number(value) -> bool:
    try:
        return type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        return False


def validate_boxes(detections) -> tuple[list, int]:
    if not isinstance(detections, list):
        return [], 1
    valid = []
    invalid = max(0, len(detections) - MAX_BOXES)
    for detection in detections[:MAX_BOXES]:
        if not isinstance(detection, (list, tuple)) or len(detection) != 3:
            invalid += 1
            continue
        label, confidence, box = detection
        if (
            not isinstance(label, str)
            or not 0 < len(label) <= 128
            or not _number(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(box, (list, tuple))
            or len(box) != 4
            or not all(_number(n) for n in box)
            or box[2] <= 0
            or box[3] <= 0
            or any(abs(n) > 1e9 for n in box)
        ):
            invalid += 1
            continue
        valid.append([label, confidence, list(box)])
    return valid, invalid


def project_boxes(raw_boxes: list, width: int, height: int) -> list[dict]:
    if width <= 0 or height <= 0:
        return []
    valid, _ = validate_boxes(raw_boxes)
    boxes = []
    for label, confidence, (cx, cy, w, h) in valid:
        left, top = max(0, cx - w / 2), max(0, cy - h / 2)
        right, bottom = min(width, cx + w / 2), min(height, cy + h / 2)
        if right > left and bottom > top:
            boxes.append(
                {
                    "label": label,
                    "confidence": confidence,
                    "xyxy": [left / width, top / height, right / width, bottom / height],
                }
            )
    return boxes

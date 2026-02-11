"""
Native OpenCV Tracking Backend for ScriptCompiler Desktop

Uses CSRT tracker (primary) with LK optical flow (fallback) for robust point tracking.
CSRT = Discriminative Correlation Filter with Channel and Spatial Reliability.

Communicates via stdin/stdout JSON protocol.
Each message is a single JSON line terminated by newline.
Frame data is base64-encoded grayscale pixels.

Commands: ping, start_tracking, process_frame, stop_tracking, cleanup
"""

import sys
import json
import base64
import math

try:
    import numpy as np
    import cv2
except ImportError as e:
    error_msg = json.dumps({
        'success': False,
        'error': f'Missing dependency: {e}. Install with: pip install opencv-python numpy',
        'command': 'startup_error'
    })
    sys.stdout.write(error_msg + '\n')
    sys.stdout.flush()
    sys.exit(1)


class NativeTracker:
    def __init__(self):
        self.is_tracking = False
        self.frame_count = 0
        self.frame_width = 0
        self.frame_height = 0
        self.template_size = 40
        self.tracking_scale = 1.0

        # CSRT tracker (primary - robust appearance-based tracking)
        self.csrt_tracker = None
        self.csrt_confidence = 0.0

        # LK optical flow (secondary/fallback)
        self.prev_gray = None
        self.tracking_points = None
        self.good_points_indices = []
        self.max_points = 15
        self.min_points = 3
        self.quality_level = 0.01
        self.min_distance = 7

        self.lk_params = {
            'winSize': (21, 21),
            'maxLevel': 3,
            'criteria': (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01)
        }

        # Shared state
        self.last_valid_center = None
        self.out_of_bounds_count = 0
        self.max_out_of_bounds = 10

    def _get_roi_size(self):
        """ROI size for CSRT init and LK feature search."""
        return max(40, self.template_size)

    def decode_frame(self, frame_data, width, height):
        """Decode base64 grayscale frame to numpy array."""
        frame_bytes = base64.b64decode(frame_data)
        return np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width))

    def _make_roi_bbox(self, cx, cy, w, h):
        """Create (x, y, w, h) ROI tuple centered on (cx, cy) within frame bounds."""
        roi_size = self._get_roi_size()
        rx = int(max(0, cx - roi_size))
        ry = int(max(0, cy - roi_size))
        rw = int(min(w - rx, roi_size * 2))
        rh = int(min(h - ry, roi_size * 2))
        return (rx, ry, rw, rh)

    def _init_csrt(self, gray, cx, cy):
        """Initialize CSRT tracker with a ROI around the center point."""
        h, w = gray.shape
        bbox = self._make_roi_bbox(cx, cy, w, h)

        # CSRT needs a color or grayscale image in standard format
        # Convert grayscale to BGR for tracker (some trackers work better with color)
        frame_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

        self.csrt_tracker = cv2.TrackerCSRT_create()
        self.csrt_tracker.init(frame_bgr, bbox)

    def _init_lk_points(self, gray, cx, cy):
        """Find good features for LK optical flow around the center point."""
        h, w = gray.shape
        roi_size = self._get_roi_size()
        rx = int(max(0, cx - roi_size))
        ry = int(max(0, cy - roi_size))
        rw = int(min(w - rx, roi_size * 2))
        rh = int(min(h - ry, roi_size * 2))

        if rw <= 0 or rh <= 0:
            self.tracking_points = np.array(
                [[[cx, cy]]], dtype=np.float32
            )
            self.good_points_indices = [0]
            return

        roi = gray[ry:ry + rh, rx:rx + rw]

        corners = cv2.goodFeaturesToTrack(
            roi,
            maxCorners=self.max_points,
            qualityLevel=self.quality_level,
            minDistance=self.min_distance,
            blockSize=3,
            useHarrisDetector=False,
            k=0.04
        )

        if corners is not None and len(corners) > 0:
            self.tracking_points = corners.reshape(-1, 1, 2).astype(np.float32)
            self.tracking_points[:, 0, 0] += rx
            self.tracking_points[:, 0, 1] += ry
            self.good_points_indices = list(range(len(self.tracking_points)))
        else:
            self.tracking_points = np.array(
                [[[cx, cy]]], dtype=np.float32
            )
            self.good_points_indices = [0]

    def _lk_center(self, points):
        """Calculate median center from a list of [x, y] points."""
        if len(points) == 0:
            return None
        if len(points) == 1:
            return {'x': float(points[0][0]), 'y': float(points[0][1])}

        x_coords = sorted([p[0] for p in points])
        y_coords = sorted([p[1] for p in points])
        return {
            'x': float(x_coords[len(x_coords) // 2]),
            'y': float(y_coords[len(y_coords) // 2])
        }

    def start_tracking(self, msg):
        """Initialize tracking with first frame and selected point."""
        frame_data = msg['frameData']
        width = msg['width']
        height = msg['height']
        tracking_point = msg['trackingPoint']

        gray = self.decode_frame(frame_data, width, height)

        self.is_tracking = True
        self.frame_count = 0
        self.out_of_bounds_count = 0
        self.frame_width = width
        self.frame_height = height
        self.tracking_scale = msg.get('trackingScale', 1.0)
        self.template_size = msg.get('templateSize', 40)

        # Update LK window size
        win = max(15, min(31, int(self.template_size / 2)))
        self.lk_params['winSize'] = (win, win)

        px, py = float(tracking_point['x']), float(tracking_point['y'])

        # Initialize CSRT tracker (primary)
        self._init_csrt(gray, px, py)

        # Initialize LK optical flow points (secondary)
        self._init_lk_points(gray, px, py)
        self.prev_gray = gray

        self.last_valid_center = {'x': px, 'y': py}

        return {
            'success': True,
            'method': 'csrt',
            'pointCount': len(self.tracking_points) if self.tracking_points is not None else 0
        }

    def process_frame(self, msg):
        """Process a frame using CSRT (primary) with LK optical flow (fallback)."""
        if not self.is_tracking:
            return {'success': False, 'error': 'Not tracking'}

        frame_data = msg['frameData']
        width = msg['width']
        height = msg['height']

        next_gray = self.decode_frame(frame_data, width, height)

        # --- Primary: CSRT tracker ---
        csrt_center = None
        csrt_ok = False

        if self.csrt_tracker is not None:
            frame_bgr = cv2.cvtColor(next_gray, cv2.COLOR_GRAY2BGR)
            csrt_ok, bbox = self.csrt_tracker.update(frame_bgr)

            if csrt_ok:
                bx, by, bw, bh = bbox
                csrt_center = {
                    'x': float(bx + bw / 2),
                    'y': float(by + bh / 2)
                }

        # --- Secondary: LK optical flow ---
        lk_center = None

        if (self.prev_gray is not None and
                self.tracking_points is not None and
                len(self.good_points_indices) >= self.min_points and
                next_gray.shape == self.prev_gray.shape):

            next_points, status, err = cv2.calcOpticalFlowPyrLK(
                self.prev_gray,
                next_gray,
                self.tracking_points,
                None,
                **self.lk_params
            )

            good_points = []
            new_good_indices = []

            if status is not None and next_points is not None:
                for idx in self.good_points_indices:
                    if idx >= len(status) or status[idx][0] != 1:
                        continue

                    new_x = float(next_points[idx][0][0])
                    new_y = float(next_points[idx][0][1])

                    if 0 <= new_x < width and 0 <= new_y < height:
                        good_points.append([new_x, new_y])
                        new_good_indices.append(idx)
                        self.tracking_points[idx][0][0] = new_x
                        self.tracking_points[idx][0][1] = new_y

            if len(good_points) >= self.min_points:
                lk_center = self._lk_center(good_points)
                self.good_points_indices = new_good_indices

        # --- Fuse results ---
        result = None

        if csrt_ok and csrt_center:
            # CSRT succeeded - use it as primary
            center = csrt_center
            confidence = 0.9
            method = 'csrt'

            # If LK also available, average for extra stability
            if lk_center:
                # Weighted average: CSRT 70%, LK 30%
                center = {
                    'x': csrt_center['x'] * 0.7 + lk_center['x'] * 0.3,
                    'y': csrt_center['y'] * 0.7 + lk_center['y'] * 0.3
                }
                confidence = 0.95
                method = 'csrt_lk_fused'

            self.last_valid_center = center
            self.out_of_bounds_count = 0

            result = {
                'success': True,
                'trackingSpacePoint': center,
                'method': method,
                'confidence': round(confidence, 3)
            }

        elif lk_center:
            # CSRT failed but LK succeeded - use LK
            self.last_valid_center = lk_center
            self.out_of_bounds_count = 0

            result = {
                'success': True,
                'trackingSpacePoint': lk_center,
                'method': 'lk_fallback',
                'confidence': 0.6
            }

            # Re-init CSRT at the new position
            self._init_csrt(next_gray, lk_center['x'], lk_center['y'])

        else:
            # Both failed - return last known position
            self.out_of_bounds_count += 1

            result = {
                'success': True,
                'trackingSpacePoint': self.last_valid_center or {'x': width / 2, 'y': height / 2},
                'method': 'fallback_last_valid',
                'confidence': max(0.1, 0.5 - self.out_of_bounds_count * 0.05)
            }

            # Re-init both trackers at last known position
            if self.last_valid_center:
                self._init_csrt(next_gray, self.last_valid_center['x'], self.last_valid_center['y'])
                self._init_lk_points(next_gray, self.last_valid_center['x'], self.last_valid_center['y'])

        # Refresh LK points periodically (every 30 frames) to avoid drift
        if self.frame_count > 0 and self.frame_count % 30 == 0 and self.last_valid_center:
            self._init_lk_points(next_gray, self.last_valid_center['x'], self.last_valid_center['y'])

        # Update state
        self.prev_gray = next_gray
        self.frame_count += 1

        return result

    def stop_tracking(self):
        """Stop tracking and reset state."""
        self.is_tracking = False
        self.csrt_tracker = None
        self.prev_gray = None
        self.tracking_points = None
        self.good_points_indices = []
        self.last_valid_center = None
        self.out_of_bounds_count = 0
        self.frame_count = 0

    def cleanup(self):
        """Full cleanup."""
        self.stop_tracking()


def main():
    """Main loop: read JSON commands from stdin, write JSON responses to stdout."""
    tracker = NativeTracker()

    # Check CSRT availability
    has_csrt = hasattr(cv2, 'TrackerCSRT_create')

    startup_msg = json.dumps({
        'success': True,
        'command': 'startup',
        'opencv_version': cv2.__version__,
        'numpy_version': np.__version__,
        'has_csrt': has_csrt
    })
    sys.stdout.write(startup_msg + '\n')
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
            command = msg.get('command')
            request_id = msg.get('_requestId')

            if command == 'ping':
                result = {'success': True, 'pong': True, 'has_csrt': has_csrt}

            elif command == 'start_tracking':
                result = tracker.start_tracking(msg)

            elif command == 'process_frame':
                result = tracker.process_frame(msg)

            elif command == 'stop_tracking':
                tracker.stop_tracking()
                result = {'success': True}

            elif command == 'cleanup':
                tracker.cleanup()
                result = {'success': True}

            else:
                result = {'success': False, 'error': f'Unknown command: {command}'}

            result['command'] = command
            if request_id is not None:
                result['_requestId'] = request_id

            sys.stdout.write(json.dumps(result) + '\n')
            sys.stdout.flush()

        except json.JSONDecodeError as e:
            error_response = {
                'success': False,
                'error': f'Invalid JSON: {str(e)}',
                'command': 'parse_error'
            }
            if 'request_id' in dir() and request_id is not None:
                error_response['_requestId'] = request_id
            sys.stdout.write(json.dumps(error_response) + '\n')
            sys.stdout.flush()

        except Exception as e:
            error_response = {
                'success': False,
                'error': str(e),
                'command': msg.get('command', 'unknown') if isinstance(msg, dict) else 'unknown'
            }
            if 'request_id' in dir() and request_id is not None:
                error_response['_requestId'] = request_id
            sys.stdout.write(json.dumps(error_response) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()

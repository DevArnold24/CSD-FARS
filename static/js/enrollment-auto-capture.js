(function (root, factory) {
    const autoCapture = factory();
    if (typeof module === 'object' && module.exports) module.exports = autoCapture;
    else root.EnrollmentAutoCapture = autoCapture;
}(typeof window === 'undefined' ? globalThis : window, function () {
    'use strict';

    const defaults = Object.freeze({
        holdMs: 2000,
        maxObservationGapMs: 750,
        maxFrameAgeMs: 1500,
        minScore: 0.75,
        centerMaxYaw: 0.12,
        sideMinYaw: 0.14,
        sideMaxYaw: 0.45,
        maxRollDegrees: 15,
        maxCenterShift: 0.07,
        maxScaleChange: 0.08,
        maxYawChange: 0.07,
        maxRollChangeDegrees: 4
    });

    function settings(options) {
        const result = {...defaults};
        Object.keys(defaults).forEach(key => {
            if (Number.isFinite(options[key]) && options[key] > 0) result[key] = options[key];
        });
        return result;
    }

    function isPoint(point) {
        return point && Number.isFinite(point.x) && Number.isFinite(point.y);
    }

    function average(points) {
        return {x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
            y: points.reduce((sum, point) => sum + point.y, 0) / points.length};
    }

    // This is an approximate guide, not a calibrated head-angle measurement.
    // The detector normally receives an upright, unmirrored source image. A
    // nose offset to image-right then means the participant's own left. Display
    // mirroring must not change this sign; sourceMirrored is only for inputs
    // whose pixels were actually mirrored before landmark detection.
    function estimateOrientation(landmarks, options = {}) {
        const limits = settings(options);
        const points = Array.isArray(landmarks) ? landmarks : landmarks && landmarks.positions;
        if (!points || points.length !== 68) return {available: false};
        const relevant = [points[30], ...points.slice(36, 48)];
        if (!relevant.every(isPoint)) return {available: false};
        const eyes = [average(points.slice(36, 42)), average(points.slice(42, 48))]
            .sort((first, second) => first.x - second.x);
        const dx = eyes[1].x - eyes[0].x;
        const dy = eyes[1].y - eyes[0].y;
        const span = Math.hypot(dx, dy);
        if (span < 1 || dx <= 0) return {available: false};
        const midpoint = average(eyes);
        const nose = points[30];
        let yaw = ((nose.x - midpoint.x) * dx + (nose.y - midpoint.y) * dy) / (span * span);
        if (options.sourceMirrored === true) yaw = -yaw;
        const rollDegrees = Math.atan2(dy, dx) * 180 / Math.PI;
        const magnitude = Math.abs(yaw);
        const orientation = magnitude <= limits.centerMaxYaw ? 'center'
            : magnitude < limits.sideMinYaw ? 'between'
            : magnitude <= limits.sideMaxYaw ? (yaw > 0 ? 'left' : 'right') : 'too_far';
        return {available: true, yaw, rollDegrees, orientation,
            level: Math.abs(rollDegrees) <= limits.maxRollDegrees};
    }

    function poseName(pose) {
        if (pose === 0 || pose === 'center') return 'center';
        if (pose === 1 || pose === 'left') return 'left';
        if (pose === 2 || pose === 'right') return 'right';
        return null;
    }

    function validBox(box) {
        return box && ['x', 'y', 'width', 'height'].every(key => Number.isFinite(box[key]))
            && box.width > 0 && box.height > 0;
    }

    function moved(reference, sample, limits) {
        const first = reference.box;
        const second = sample.box;
        const shift = Math.hypot(
            (second.x + second.width / 2 - first.x - first.width / 2) / first.width,
            (second.y + second.height / 2 - first.y - first.height / 2) / first.height);
        return shift > limits.maxCenterShift
            || Math.abs(second.width / first.width - 1) > limits.maxScaleChange
            || Math.abs(second.height / first.height - 1) > limits.maxScaleChange
            || Math.abs(sample.yaw - reference.yaw) > limits.maxYawChange
            || Math.abs(sample.rollDegrees - reference.rollDegrees) > limits.maxRollChangeDegrees;
    }

    function messageFor(reason, pose, orientation) {
        if (reason === 'disabled') return 'Automatic capture is off.';
        if (reason === 'no_face') return 'Keep your whole face in view.';
        if (reason === 'multiple_faces') return 'Only one person should be in view.';
        if (reason === 'quality') return 'Follow the framing and lighting guidance.';
        if (reason === 'landmarks_unavailable') return 'Head angle cannot be checked. Wait for the pose guide.';
        if (reason === 'head_tilt') return 'Keep your head upright.';
        if (reason === 'pose') {
            if (orientation.orientation === 'too_far') return 'Turn back a little toward the camera.';
            return pose === 'center' ? 'Look directly at the camera lens.'
                : `Turn your head slightly to your ${pose}.`;
        }
        if (reason === 'movement') return 'Hold still to restart the countdown.';
        if (reason === 'hold_still') return 'Hold still.';
        if (reason === 'ready') return 'Ready to capture.';
        if (reason === 'complete') return 'Hold complete.';
        return 'Wait for a fresh preview check.';
    }

    function createTracker(options = {}) {
        const limits = settings(options);
        let anchor = null;
        let previous = null;
        let startedAt = null;
        let completed = false;

        function reset() {
            anchor = null;
            previous = null;
            startedAt = null;
            completed = false;
        }

        function reject(reason, orientation = null, pose = null) {
            reset();
            return {eligible: false, ready: false, countdownRemainingMs: limits.holdMs,
                progress: 0, reason, message: messageFor(reason, pose, orientation), orientation};
        }

        // Each call represents a new analyzed frame. There are no timers,
        // image retention, network requests, or capture actions in this helper.
        function update(input) {
            if (!input || input.enabled !== true) return reject('disabled');
            const now = input.now;
            const capturedAt = input.capturedAt === undefined ? now : input.capturedAt;
            if (!Number.isFinite(now) || !Number.isFinite(capturedAt)
                    || capturedAt > now || now - capturedAt > limits.maxFrameAgeMs) {
                return reject('stale_frame');
            }
            const pose = poseName(input.pose);
            if (!pose) return reject('invalid_pose');
            if (input.faceCount !== 1) return reject(input.faceCount > 1 ? 'multiple_faces' : 'no_face');
            if (input.qualityReady !== true || !Number.isFinite(input.detectionScore)
                    || input.detectionScore < limits.minScore || !validBox(input.faceBox)) {
                return reject('quality');
            }
            const orientation = estimateOrientation(input.landmarks, {...limits,
                sourceMirrored: input.sourceMirrored === true});
            if (!orientation.available) return reject('landmarks_unavailable', orientation);
            if (!orientation.level) return reject('head_tilt', orientation);
            if (orientation.orientation !== pose) return reject('pose', orientation, pose);

            const sample = {box: {...input.faceBox}, yaw: orientation.yaw,
                rollDegrees: orientation.rollDegrees, now, capturedAt, pose, sourceKey: input.sourceKey};
            if (previous && (pose !== previous.pose || input.sourceKey !== previous.sourceKey)) reset();
            if (previous && (now <= previous.now || capturedAt <= previous.capturedAt
                    || now - previous.now > limits.maxObservationGapMs)) {
                return reject('frame_gap', orientation);
            }
            if (anchor && (moved(anchor, sample, limits) || moved(previous, sample, limits))) {
                return reject('movement', orientation);
            }
            if (!anchor) {
                anchor = sample;
                startedAt = now;
            }
            previous = sample;
            if (completed) return {eligible: false, ready: false, countdownRemainingMs: 0,
                progress: 1, reason: 'complete', message: messageFor('complete'), orientation};
            const elapsed = Math.max(0, now - startedAt);
            const remainingMs = Math.max(0, limits.holdMs - elapsed);
            const ready = remainingMs === 0;
            if (ready) completed = true;
            const reason = ready ? 'ready' : 'hold_still';
            return {eligible: true, ready, countdownRemainingMs: remainingMs,
                progress: Math.min(1, elapsed / limits.holdMs),
                reason, message: messageFor(reason), orientation};
        }

        return {update, reset};
    }

    return {defaults, estimateOrientation, createTracker};
}));

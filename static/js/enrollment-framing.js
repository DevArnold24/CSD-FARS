(function (root, factory) {
    const framing = factory();
    if (typeof module === 'object' && module.exports) module.exports = framing;
    else root.EnrollmentFraming = framing;
}(typeof window === 'undefined' ? globalThis : window, function () {
    'use strict';

    function evaluate(face, sourceWidth, sourceHeight, viewportWidth, viewportHeight, options = {}) {
        const scale = Math.max(viewportWidth / sourceWidth, viewportHeight / sourceHeight);
        const left = (viewportWidth - sourceWidth * scale) / 2;
        const top = (viewportHeight - sourceHeight * scale) / 2;
        // Face coordinates already include any capture rotation. Only webcam display is mirrored.
        const sourceX = options.mirror ? sourceWidth - face.x - face.width : face.x;
        const box = {x: left + sourceX * scale, y: top + face.y * scale,
            width: face.width * scale, height: face.height * scale};
        const dx = (box.x + box.width / 2 - viewportWidth / 2) / (viewportWidth * 0.15);
        const dy = (box.y + box.height / 2 - viewportHeight / 2) / (viewportHeight * 0.20);
        const positionOk = dx * dx + dy * dy <= 1;
        const direction = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'left' : 'right') : (dy > 0 ? 'top' : 'bottom');
        const minArea = Number.isFinite(options.minFaceAreaRatio) ? options.minFaceAreaRatio : 0.03;
        const margin = Number.isFinite(options.minEdgeMarginRatio) ? options.minEdgeMarginRatio : 0.02;
        const tooSmall = face.width * face.height / (sourceWidth * sourceHeight) < minArea;
        const clipped = face.x <= sourceWidth * margin || face.y <= sourceHeight * margin
            || face.x + face.width >= sourceWidth * (1 - margin)
            || face.y + face.height >= sourceHeight * (1 - margin)
            || box.x < 0 || box.y < 0 || box.x + box.width > viewportWidth || box.y + box.height > viewportHeight;
        return {box, positionOk, sizeOk: !tooSmall && !clipped,
            positionText: positionOk ? 'Centered' : `Move toward the ${direction} of the preview`,
            sizeText: clipped ? 'Move back until your whole face is visible' : tooSmall ? 'Move closer to the camera' : 'Good distance'};
    }
    return {evaluate};
}));

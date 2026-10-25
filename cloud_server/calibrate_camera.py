"""Fit measured lens distortion from a checkerboard photographed by one camera.

python calibrate_camera.py master "calibration_photos/master/*.jpg" --columns 9 --rows 6
Columns/rows are INNER corners. Take >=10 views covering center, edges and tilts.
Do not mix mirrored/oriented/resized streams: capture with the deployed firmware.
"""
import argparse
import glob
import json
from pathlib import Path

import cv2
import numpy as np


def calibrate(pattern, columns, rows):
    grid = np.zeros((columns * rows, 3), np.float32)
    grid[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2)
    objects, images, image_size = [], [], None
    for name in glob.glob(pattern):
        image = cv2.imdecode(np.fromfile(name, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            continue
        size = (image.shape[1], image.shape[0])
        if image_size is not None and image_size != size:
            raise ValueError("All calibration photos must use the same resolution")
        image_size = size
        found, corners = cv2.findChessboardCornersSB(image, (columns, rows))
        if found:
            objects.append(grid.copy())
            images.append(corners)
    if len(images) < 10:
        raise ValueError("Need at least 10 valid checkerboard views")
    error, matrix, distortion, _, _ = cv2.calibrateCamera(objects, images, image_size, None, None)
    return {"rms_reprojection_error": error, "views": len(images),
            "image_width": image_size[0], "image_height": image_size[1],
            "camera_matrix": matrix.tolist(), "dist_coeffs": distortion.flatten().tolist()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera", choices=["master", "slave"])
    parser.add_argument("pattern")
    parser.add_argument("--columns", type=int, default=9)
    parser.add_argument("--rows", type=int, default=6)
    args = parser.parse_args()
    output = Path(__file__).parent / f"calibration_{args.camera}.json"
    if output.exists():
        raise SystemExit(f"{output} already exists. Back it up before recalibrating.")
    report = calibrate(args.pattern, args.columns, args.rows)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Saved", output, "RMS pixel error:", report["rms_reprojection_error"])

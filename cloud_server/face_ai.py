"""YuNet/SFace with input quality and supplementary eye-region HOG.

HOG is not an iris/learned periocular identity model. By default it is diagnostic;
it never authorizes independently of SFace and temporal agreement.
"""
import json
import math
import os
import threading

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_ID = "sface-2021dec-v1"
EYE_ID = "eye-hog-128x64-v1"
detector = recognizer = None
_lock = threading.RLock()


def settings():
    with open(os.path.join(BASE_DIR, "recognition_config.json"), encoding="utf-8") as stream:
        return json.load(stream)


def init_face_ai():
    global detector, recognizer
    with _lock:
        if detector is not None and recognizer is not None:
            return
        detector = cv2.FaceDetectorYN.create(
            os.path.join(BASE_DIR, "models", "face_detection_yunet_2023mar.onnx"),
            "", (320, 320), settings()["detection_threshold"], 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(
            os.path.join(BASE_DIR, "models", "face_recognition_sface_2021dec.onnx"), "")


def _load_image(value):
    if isinstance(value, (bytes, bytearray)):
        if not value:
            return None
        try:
            return cv2.imdecode(np.frombuffer(value, np.uint8), cv2.IMREAD_COLOR)
        except cv2.error:
            return None
    if isinstance(value, np.ndarray):
        return value
    try:
        # imread on Windows may fail for Vietnamese filenames.
        return _load_image(np.fromfile(str(value), dtype=np.uint8).tobytes())
    except (OSError, ValueError):
        return None


def _normalize(value, dimension=None):
    vector = np.asarray(value, np.float32).reshape(-1)
    if (dimension is not None and vector.size != dimension) or not np.all(np.isfinite(vector)):
        raise ValueError("Invalid feature dimension or non-finite values")
    norm = float(np.linalg.norm(vector))
    if norm < 1e-8:
        raise ValueError("Empty feature")
    return vector / norm


def compare_faces(known_feature, test_feature):
    if known_feature is None or test_feature is None:
        return 0.0
    try:
        a = _normalize(known_feature)
        b = _normalize(test_feature, a.size)
        return float(np.clip(np.dot(a, b), -1, 1))
    except (TypeError, ValueError):
        return 0.0


def _eye_crop(image, face):
    eyes = sorted([face[4:6], face[6:8]], key=lambda p: float(p[0]))
    left, right = np.array(eyes, np.float32)
    delta = right - left
    third = left + np.array([-delta[1], delta[0]], np.float32)
    matrix = cv2.getAffineTransform(
        np.array([left, right, third], np.float32),
        np.array([[36, 34], [92, 34], [36, 90]], np.float32))
    crop = cv2.warpAffine(image, matrix, (128, 64), borderMode=cv2.BORDER_REPLICATE)
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)


def _eye_feature(crop):
    normalized = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(crop)
    hog = cv2.HOGDescriptor((128, 64), (16, 16), (8, 8), (8, 8), 9)
    return _normalize(hog.compute(normalized))


def correct_lens(image, camera):
    """Optional measured calibration, never invent lens coefficients."""
    if camera not in ("master", "slave"):
        return image
    path = os.path.join(BASE_DIR, f"calibration_{camera}.json")
    if not os.path.exists(path):
        return image
    with open(path, encoding="utf-8") as stream:
        calibration = json.load(stream)
    matrix = np.array(calibration["camera_matrix"], np.float64).reshape(3, 3)
    height, width = image.shape[:2]
    # VGA JPEG and QVGA RGB565 may use different sensor crops, not mere resize.
    # A calibration from one mode must not warp the other with scaled coefficients.
    if (width, height) != (calibration["image_width"], calibration["image_height"]):
        return image
    distortion = np.array(calibration["dist_coeffs"], np.float64)
    return cv2.undistort(image, matrix, distortion)


def measure_quality(image, face, config):
    """Measure face/eyes, not a sharp background or mask edge.

    A still image cannot reliably separate motion blur from defocus. Pose is a
    landmark proxy, not calibrated 3D pose; nose landmarks under a mask may drift.
    """
    h, w = image.shape[:2]
    x, y, fw, fh = map(float, face[:4])
    eyes = _eye_crop(image, face)
    eye_points = sorted([face[4:6], face[6:8]], key=lambda p: float(p[0]))
    delta = np.subtract(eye_points[1], eye_points[0])
    distance = float(np.linalg.norm(delta))
    roll = math.degrees(math.atan2(float(delta[1]), float(delta[0])))
    brightness = float(np.mean(eyes))
    contrast = float(np.std(eyes))
    sharpness = float(cv2.Laplacian(eyes, cv2.CV_32F).var())
    dark = float(np.mean(eyes < 15))
    clipped = float(np.mean(eyes > 245))
    noise = float(np.median(np.abs(eyes.astype(np.float32) - cv2.medianBlur(eyes, 3))))
    yaw_proxy = abs(float(face[8]) - (float(face[4]) + float(face[6])) / 2) / max(distance, 1)
    problems = []
    if min(fw, fh) < config["min_face_pixels"] or distance < config["min_eye_distance"]:
        problems.append("Khuôn mặt quá nhỏ: tiến gần camera hơn")
    visible_width = max(0.0, min(w, x + fw) - max(0.0, x))
    visible_height = max(0.0, min(h, y + fh) - max(0.0, y))
    visible_fraction = visible_width * visible_height / max(fw * fh, 1)
    # Detector boxes can extend a few pixels past a visible masked chin. Reject
    # substantial clipping, rather than every subpixel error at an image edge.
    if visible_fraction < config.get("min_visible_face_fraction", 0.95):
        problems.append("Khuôn mặt bị cắt: đưa mặt vào giữa khung")
    if brightness < config["min_brightness"] or dark > 0.65:
        problems.append("Vùng mắt thiếu sáng: bổ sung ánh sáng phía trước")
    if brightness > config["max_brightness"] or clipped > 0.45:
        problems.append("Vùng mắt cháy sáng: tránh ánh đèn chiếu trực tiếp")
    if contrast < config["min_contrast"]:
        problems.append("Vùng mắt ít chi tiết hoặc bị che")
    if sharpness < config["min_sharpness"]:
        problems.append("Ảnh mờ/rung: đứng yên và kiểm tra tiêu cự")
    if abs(roll) > config["max_roll_degrees"] or yaw_proxy > config["max_yaw_proxy"]:
        problems.append("Mặt nghiêng quá nhiều: nhìn gần thẳng camera")
    eye_margin = max(2, distance * 0.15)
    for px, py in eye_points:
        if not (eye_margin <= px < w - eye_margin and eye_margin <= py < h - eye_margin):
            problems.append("Không thấy rõ cả hai mắt")
            break
    roi = image[max(0, int(y)):min(h, int(y + fh)), max(0, int(x)):min(w, int(x + fw))]
    means = np.mean(roi, axis=(0, 1)) if roi.size else np.zeros(3)
    return {
        "ok": not problems, "reasons": problems,
        "face_width": round(fw, 1), "face_height": round(fh, 1),
        "eye_distance": round(distance, 1), "sharpness": round(sharpness, 1),
        "brightness": round(brightness, 1), "contrast": round(contrast, 1),
        "dark_fraction": round(dark, 3), "clipped_fraction": round(clipped, 3),
        "noise_proxy": round(noise, 1), "roll_degrees": round(roll, 1),
        "yaw_proxy": round(yaw_proxy, 3), "mean_bgr": np.round(means, 1).tolist(),
        "visible_face_fraction": round(visible_fraction, 3),
        "occlusion": "unknown; mask labels come from supervised enrollment",
    }, eyes


def _detect_candidates(image, config):
    """Retry missed detections at bounded scales without lowering confidence.

    Only detection sees resized/contrast-adjusted images. Return landmarks in
    original pixels so enlargement cannot bypass face-size or blur checks.
    Called under _lock because OpenCV's detector has mutable input dimensions.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    sizes = list(dict.fromkeys([min(longest, 960), 640, 320, 960]))
    detector.setScoreThreshold(config["detection_threshold"])
    attempts = [(size, False) for size in sizes] + [(640, True)]
    for size, contrast_retry in attempts:
        scale = size / longest
        resized_width = max(1, round(width * scale))
        resized_height = max(1, round(height * scale))
        frame = cv2.resize(image, (resized_width, resized_height),
                           interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        if contrast_retry:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(lab[:, :, 0])
            frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        detector.setInputSize((resized_width, resized_height))
        _, faces = detector.detect(frame)
        if faces is None or len(faces) == 0:
            continue
        faces = faces.copy()
        faces[:, [0, 2, 4, 6, 8, 10, 12]] *= width / resized_width
        faces[:, [1, 3, 5, 7, 9, 11, 13]] *= height / resized_height
        return faces, {"detection_method": f"{'contrast' if contrast_retry else 'original'}_scale_{size}"}
    return None, {"detection_method": "no_candidate_after_retries"}


def analyze_face(value, camera=None):
    result = {"feature": None, "eye_feature": None,
              "quality": {"ok": False, "reasons": ["Không đọc được ảnh"]}}
    image = _load_image(value)
    if image is None or image.size == 0:
        return result
    if image.shape[0] * image.shape[1] > settings()["max_image_pixels"]:
        result["quality"]["reasons"] = ["Ảnh quá lớn; dùng ảnh không quá 12 megapixel"]
        return result
    image = correct_lens(image, camera)
    config = settings()
    with _lock:
        init_face_ai()
        faces, detection_info = _detect_candidates(image, config)
        if faces is None or len(faces) == 0:
            result["quality"].update(detection_info)
            result["quality"]["reasons"] = [
                "Bộ phát hiện chưa tìm thấy khuôn mặt trong ảnh này; có thể do khẩu trang, "
                "góc chụp hoặc ánh sáng. Thử ảnh chính diện, gần camera và đủ sáng."]
            return result
        substantial = [f for f in faces if min(f[2], f[3]) >= config["min_face_pixels"]]
        if len(substantial) > 1:
            result["quality"]["reasons"] = ["Có nhiều khuôn mặt: mỗi lần chỉ một người đứng trước cửa"]
            return result
        face = max(faces, key=lambda f: float(f[2] * f[3]))
        quality, eyes = measure_quality(image, face, config)
        quality.update(detection_info, detection_score=round(float(face[-1]), 3))
        result["quality"] = quality
        if not quality["ok"]:
            return result
        try:
            aligned = recognizer.alignCrop(image, face)
            result["feature"] = _normalize(recognizer.feature(aligned), 128)
            result["eye_feature"] = _eye_feature(eyes)
        except (cv2.error, ValueError) as exc:
            result["quality"].update(ok=False, reasons=[f"Không trích xuất được đặc trưng: {exc}"])
            result["feature"] = result["eye_feature"] = None
    return result


def extract_face_features(value):
    return analyze_face(value)["feature"], _load_image(value)


def make_template(analysis, mode):
    return {"model": MODEL_ID, "eye_model": EYE_ID, "mode": mode,
            "feature": analysis["feature"].tolist(),
            "eye_feature": analysis["eye_feature"].tolist(), "quality": analysis["quality"]}


def read_templates(serialized):
    """Read old SFace vectors without modifying existing profiles."""
    try:
        value = json.loads(serialized) if isinstance(serialized, str) else serialized
        if isinstance(value, list) and value and isinstance(value[0], (float, int)):
            _normalize(value, 128)
            return [{"model": MODEL_ID, "mode": "legacy", "feature": value}]
        if not isinstance(value, dict) or value.get("version") != 2:
            return []
        return [t for t in value.get("templates", []) if isinstance(t, dict) and t.get("model") == MODEL_ID]
    except (TypeError, ValueError, KeyError):
        return []

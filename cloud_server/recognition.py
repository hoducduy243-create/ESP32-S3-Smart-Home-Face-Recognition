"""Identity matching and session voting, kept independent of HTTP and GPIO."""
import statistics

from face_ai import EYE_ID, compare_faces, read_templates, settings


def _registered_identities(users, aliases):
    """Group explicitly confirmed aliases, without editing profiles/history.

    Unknown targets and cycles never invent an authorized identity. Similar
    embeddings alone are not evidence that two profiles belong to one person.
    """
    profiles = {row["id"]: dict(row) for row in users}
    identities = {}
    for profile_id, profile in profiles.items():
        canonical = profile_id
        visited = set()
        while str(canonical) in aliases:
            if canonical in visited:
                canonical = profile_id
                break
            visited.add(canonical)
            try:
                target = int(aliases[str(canonical)])
            except (TypeError, ValueError):
                canonical = profile_id
                break
            if target not in profiles:
                canonical = profile_id
                break
            canonical = target
        identity = identities.setdefault(canonical, {
            "id": canonical, "name": profiles[canonical]["name"], "templates": []})
        identity["templates"].extend((template, profile_id)
                                     for template in read_templates(profile.get("face_features")))
    return identities.values()


def rank_face(analysis, users, config=None):
    config = settings() if config is None else config
    result = {"accepted": False, "user_id": None, "name": None, "score": 0.0,
              "margin": 0.0, "eye_score": None, "mode": None,
              "eye_check_policy": config.get("eye_check_policy", "diagnostic"),
              "warnings": [], "runner_up_user_id": None, "runner_up_score": 0.0,
              "quality": analysis["quality"], "reasons": list(analysis["quality"]["reasons"])}
    if analysis["feature"] is None or not analysis["quality"]["ok"]:
        return result
    candidates = []
    for user in _registered_identities(users, config.get("identity_aliases", {})):
        groups = {}
        for template, profile_id in user["templates"]:
            groups.setdefault(template.get("mode", "legacy"), []).append((template, profile_id))
        if any(mode != "legacy" for mode in groups):
            groups.pop("legacy", None)
        for mode, templates in groups.items():
            # Match the closest enrolled view. Averaging a matching side view
            # with a less similar frontal sample can reject a valid slight turn.
            score, template, profile_id = max(
                ((compare_faces(t.get("feature"), analysis["feature"]), t, source)
                 for t, source in templates), key=lambda item: item[0])
            # Corroboration only. Missing eye data in old profiles is explicitly
            # marked legacy, not invented from an existing full-face vector.
            eye_score = (compare_faces(template.get("eye_feature"), analysis.get("eye_feature"))
                         if template.get("eye_model") == EYE_ID else None)
            candidates.append({"user_id": user["id"], "name": user["name"],
                               "score": score, "eye_score": eye_score, "mode": mode,
                               "matched_profile_id": profile_id})
    if not candidates:
        result["reasons"] = ["Chưa có mẫu khuôn mặt hợp lệ"]
        return result
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]
    runner = next((c for c in candidates if c["user_id"] != best["user_id"]), None)
    runner_score = max(0.0, runner["score"]) if runner else 0.0
    result.update(best, margin=best["score"] - runner_score,
                  runner_up_user_id=runner["user_id"] if runner else None,
                  runner_up_score=runner_score)
    reasons = []
    if best["score"] < config["similarity_threshold"]:
        reasons.append("Khuôn mặt chưa đủ giống mẫu; bổ sung ảnh chính diện và nghiêng nhẹ của đúng người")
    if runner is not None and result["margin"] < config["identity_margin"]:
        reasons.append("Kết quả gần giống nhiều người; cần xác thực bổ sung")
    if best["mode"] != "legacy" and (
            best["eye_score"] is None or best["eye_score"] < config["eye_similarity_threshold"]):
        message = "HOG vùng mắt khác mẫu; cần kiểm tra ánh sáng/góc chụp"
        if result["eye_check_policy"] == "enforce":
            reasons.append(message)
        else:
            result["warnings"].append(message)
    result.update(accepted=not reasons, reasons=reasons)
    return result


def decide_camera(frames, camera, config):
    """Require temporal agreement within ONE camera; never pool single votes."""
    frames = list(frames)
    accepted = [f for f in frames if f["accepted"]]
    identities = {f["user_id"] for f in accepted}
    best = max(frames, key=lambda f: f["score"], default={})
    decision = {"status": "DENIED", "user_name": "Không xác thực được",
                "user_id": None, "confidence": float(best.get("score", 0)),
                "message": f"{camera}: chưa có ảnh đạt chất lượng", "accepted_frames": len(accepted),
                "frame_count": len(frames)}
    if len(identities) > 1:
        decision["message"] = f"{camera}: các ảnh nhận ra người khác nhau; camera này không cấp quyền"
    elif len(accepted) >= config["min_consistent_frames"]:
        decision.update(status="UNLOCK", user_id=accepted[0]["user_id"],
                        user_name=accepted[0]["name"],
                        confidence=float(statistics.median(f["score"] for f in accepted)),
                        message=f"{camera}: {len(accepted)}/{len(frames)} ảnh cùng xác nhận một người")
    elif accepted:
        decision["message"] = f"{camera}: chỉ có một ảnh khớp; cần ít nhất hai khung hình nhất quán"
    elif frames:
        reasons = list(dict.fromkeys(reason for f in frames for reason in f["reasons"]))
        decision["message"] = "; ".join(reasons) or "Không tìm thấy người đã đăng ký"
    return decision


def decide_session(master_frames, slave_frames, config=None, prefer_master=False):
    """OR fusion of independent camera decisions. Only Master drives the relay."""
    config = settings() if config is None else config
    cameras = {"master": decide_camera(master_frames, "Master", config),
               "slave": decide_camera(slave_frames, "Slave", config)}
    verified = [name for name, result in cameras.items() if result["status"] == "UNLOCK"]
    source = ("master" if prefer_master and "master" in verified else
              max(verified, key=lambda name: cameras[name]["confidence"]) if verified else None)
    best = cameras[source] if source else max(cameras.values(), key=lambda result: result["confidence"])
    decision = dict(best)
    decision.update(authorization_camera=source, verified_cameras=verified,
                    decision_policy="either_camera", camera_results=cameras,
                    master_frame_count=cameras["master"]["frame_count"],
                    slave_frame_count=cameras["slave"]["frame_count"])
    if not verified:
        decision.update(user_id=None, user_name="Không xác thực được",
                        message="; ".join(result["message"] for result in cameras.values()))
    elif len(verified) == 2:
        same_person = cameras["master"]["user_id"] == cameras["slave"]["user_id"]
        decision["message"] += ("; cả hai camera xác nhận cùng người" if same_person else
                                 "; hai camera xác nhận người đăng ký khác nhau, áp dụng chính sách OR")
    return decision

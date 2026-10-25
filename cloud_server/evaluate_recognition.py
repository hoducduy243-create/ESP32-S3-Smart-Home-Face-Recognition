"""Offline session evaluation. Never reads/writes the production database.

Usage: python evaluate_recognition.py evaluation/manifest.json --output report.json
Use different capture sessions for enrollment/probes. Include unknown people.
"""
import argparse
import json
from pathlib import Path

from face_ai import analyze_face, make_template, settings
from recognition import decide_session, rank_face


def evaluate(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    gallery = []
    enrollment_paths = set()
    for person in manifest["gallery"]:
        templates = []
        for mode in ("unmasked", "masked"):
            for name in person.get(mode, []):
                path = (root / name).resolve()
                enrollment_paths.add(path)
                analysis = analyze_face(str(path))
                if analysis["feature"] is None:
                    raise ValueError(f"Enrollment rejected: {name}: {analysis['quality']['reasons']}")
                templates.append(make_template(analysis, mode))
        if not templates:
            raise ValueError("Gallery entry has no usable photos")
        gallery.append({"id": person["id"], "name": person["name"],
                        "face_features": {"version": 2, "templates": templates}})
    observations = []
    for probe in manifest["probes"]:
        path = (root / probe["path"]).resolve()
        if path in enrollment_paths:
            raise ValueError("Do not reuse enrollment photos as probes")
        if probe["camera"] not in ("master", "slave"):
            raise ValueError("Probe camera must be master or slave")
        observations.append((probe, analyze_face(str(path), probe["camera"])))
    reports = []
    for threshold in (0.35, 0.4, 0.45, 0.5, 0.55, 0.6):
        config = dict(settings(), similarity_threshold=threshold)
        sessions = {}
        for probe, analysis in observations:
            slot = sessions.setdefault(probe["session"], {
                "expected": probe.get("expected_user_id"), "master": {}, "slave": {}})
            if slot["expected"] != probe.get("expected_user_id"):
                raise ValueError("A session must have one expected identity")
            index = probe["frame_index"]
            if index not in (0, 1, 2) or index in slot[probe["camera"]]:
                raise ValueError("Use unique frame indices 0..2 per camera/session")
            slot[probe["camera"]][index] = rank_face(analysis, gallery, config)
        genuine = impostor = rejected_genuine = accepted_impostor = misidentified = 0
        details = []
        for session_id, slot in sessions.items():
            decision = decide_session(slot["master"].values(), slot["slave"].values(), config)
            expected = slot["expected"]
            if expected is None:
                impostor += 1
                accepted_impostor += decision["status"] == "UNLOCK"
            else:
                genuine += 1
                correct = decision["status"] == "UNLOCK" and decision["user_id"] == expected
                rejected_genuine += not correct
                misidentified += decision["status"] == "UNLOCK" and decision["user_id"] != expected
            details.append({"session": session_id, "expected": expected, **decision})
        reports.append({"threshold": threshold, "genuine_sessions": genuine,
                        "unknown_sessions": impostor, "wrong_identity_unlocks": misidentified,
                        "false_reject_rate": rejected_genuine / genuine if genuine else None,
                        "unknown_accept_rate": accepted_impostor / impostor if impostor else None,
                        "sessions": details})
    return {"note": "Master-only authorization. Small datasets cannot establish production false-accept rates. HOG is diagnostic, not liveness.",
            "quality_rejections": [{"path": p["path"], "quality": a["quality"]}
                                   for p, a in observations if not a["quality"]["ok"]],
            "thresholds": reports}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--output", default="recognition_report.json")
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(evaluate(args.manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved", args.output)

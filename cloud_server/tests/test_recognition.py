"""Recognition policy and real HTTP handlers against a temporary database.

These verify behavior and failure handling, not real masked-face accuracy.
"""
import ast
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
from typing import Annotated, Optional
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import db
import face_ai
import evaluate_recognition
from face_ai import analyze_face, make_template, measure_quality, read_templates, settings
from recognition import decide_session, rank_face


def analysis(feature=None, quality=True):
    vector = np.zeros(128, np.float32)
    vector[0] = 1
    return {"feature": vector if feature is None else feature,
            "eye_feature": vector.copy(),
            "quality": {"ok": quality, "reasons": [] if quality else ["Ảnh mờ"], "sharpness": 100}}


def user(identity=1, vector=None, mode="masked"):
    template = make_template(analysis(vector), mode)
    return {"id": identity, "name": f"Person {identity}",
            "face_features": json.dumps({"version": 2, "templates": [template]})}


class RecognitionPolicyTests(unittest.TestCase):
    def test_detection_retry_maps_landmarks_back_to_original_pixels(self):
        image = np.zeros((100, 200, 3), np.uint8)
        # Second pass has width 640, so coordinates are enlarged by 3.2.
        face = np.array([[32, 16, 160, 192, 64, 64, 160, 64, 112, 100,
                          80, 144, 144, 144, .8]], np.float32)
        fake_detector = Mock()
        fake_detector.detect.side_effect = [(1, None), (1, face)]
        with patch.object(face_ai, 'detector', fake_detector):
            candidates, info = face_ai._detect_candidates(image, settings())
        self.assertEqual(info['detection_method'], 'original_scale_640')
        self.assertAlmostEqual(float(candidates[0, 0]), 10)
        self.assertAlmostEqual(float(candidates[0, 2]), 50)
        self.assertAlmostEqual(float(candidates[0, -1]), .8)
        fake_detector.setScoreThreshold.assert_called_once_with(settings()['detection_threshold'])

    def test_no_face_message_does_not_claim_eyes_are_covered(self):
        fake_detector = Mock()
        fake_detector.detect.return_value = (1, None)
        with patch.object(face_ai, 'detector', fake_detector), patch.object(face_ai, 'recognizer', Mock()):
            result = analyze_face(np.zeros((240, 320, 3), np.uint8))
        self.assertFalse(result['quality']['ok'])
        self.assertEqual(result['quality']['detection_method'], 'no_candidate_after_retries')
        self.assertNotIn('để lộ', result['quality']['reasons'][0])

    def test_old_embeddings_remain_readable(self):
        vector = analysis()["feature"].tolist()
        self.assertEqual(read_templates(json.dumps(vector))[0]["mode"], "legacy")
        self.assertEqual(read_templates("[1,2]"), [])

    def test_masked_template_requires_face_and_eye_agreement(self):
        config = dict(settings(), eye_check_policy='enforce')
        sample = analysis()
        self.assertTrue(rank_face(sample, [user()], config)["accepted"])
        sample["eye_feature"] = -sample["eye_feature"]
        self.assertFalse(rank_face(sample, [user()], config)["accepted"])
        sample["eye_feature"] *= -1
        sample["feature"] *= -1
        self.assertFalse(rank_face(sample, [user()], config)["accepted"])

    def test_diagnostic_eye_score_cannot_veto_or_authorize_identity(self):
        sample = analysis()
        sample['eye_feature'] *= -1
        result = rank_face(sample, [user()])
        self.assertTrue(result['accepted'])
        self.assertTrue(result['warnings'])
        sample['feature'] *= -1
        self.assertFalse(rank_face(sample, [user()])['accepted'])

    def test_bad_image_cannot_open_even_when_features_match(self):
        result = rank_face(analysis(quality=False), [user()])
        self.assertFalse(result["accepted"])
        self.assertEqual(decide_session([result, result], [])["status"], "DENIED")

    def test_ambiguous_identity_is_rejected(self):
        result = rank_face(analysis(), [user(1), user(2)])
        self.assertFalse(result["accepted"])
        self.assertLess(result["margin"], settings()["identity_margin"])

    def test_best_enrolled_view_matches_without_averaging_away_the_turn(self):
        frontal = np.zeros(128, np.float32)
        frontal[:2] = [-.2, np.sqrt(.96)]
        side = np.zeros(128, np.float32)
        side[:2] = [.8, .6]
        person = user(1, frontal, 'unmasked')
        templates = json.loads(person['face_features'])
        templates['templates'].append(make_template(analysis(side), 'unmasked'))
        person['face_features'] = json.dumps(templates)
        result = rank_face(analysis(), [person])
        self.assertTrue(result['accepted'])
        self.assertAlmostEqual(result['score'], .8, places=5)

    def test_confirmed_aliases_are_one_identity_without_editing_profiles(self):
        people = [user(1), user(4)]
        before = json.dumps(people)
        config = dict(settings(), identity_aliases={'4': 1})
        result = rank_face(analysis(), people, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['user_id'], 1)
        self.assertIsNone(result['runner_up_user_id'])
        self.assertEqual(json.dumps(people), before)
        distinct = rank_face(analysis(), people, dict(config, identity_aliases={}))
        self.assertFalse(distinct['accepted'])

    def test_alias_missing_target_or_cycle_cannot_hide_ambiguous_people(self):
        people = [user(1), user(4)]
        for aliases in ({'4': 99}, {'1': 4, '4': 1}, {'4': 'invalid'}):
            result = rank_face(analysis(), people, dict(settings(), identity_aliases=aliases))
            self.assertFalse(result['accepted'])
        remaining = rank_face(analysis(), [user(4)], dict(settings(), identity_aliases={'4': 1}))
        self.assertEqual(remaining['user_id'], 4)

    def test_mild_roll_and_turn_are_allowed_while_large_pose_is_rejected(self):
        image = np.clip(np.random.default_rng(1).normal(120, 35, (240, 320, 3)), 0, 255).astype(np.uint8)
        face = np.array([40, 20, 160, 180, 80, 80, 160, 125, 164, 115, 90, 150, 150, 150, .99], np.float32)
        quality, _ = measure_quality(image, face, settings())
        self.assertTrue(quality['ok'])
        self.assertGreater(abs(quality['roll_degrees']), 25)
        face[8] = 220
        self.assertFalse(measure_quality(image, face, settings())[0]['ok'])

    def test_either_camera_requires_its_own_consistent_frames(self):
        good = rank_face(analysis(), [user()])
        other = dict(good, user_id=2, name="Other")
        self.assertEqual(decide_session([good], [])["status"], "DENIED")
        self.assertEqual(decide_session([good, good], [])["status"], "UNLOCK")
        self.assertEqual(decide_session([good], [good])["status"], "DENIED")
        slave = decide_session([], [good, good])
        self.assertEqual(slave["status"], "UNLOCK")
        self.assertEqual(slave['authorization_camera'], 'slave')
        self.assertEqual(decide_session([good, good], [other])["status"], "UNLOCK")
        self.assertEqual(decide_session([good, other], [good])["status"], "DENIED")
        self.assertEqual(decide_session([good, other], [good, good])["status"], "UNLOCK")
        both = decide_session([good, good], [other, other])
        self.assertEqual(both['status'], 'UNLOCK')
        self.assertEqual(both['verified_cameras'], ['master', 'slave'])

    def test_both_cameras_fail_quality_or_similarity(self):
        bad = rank_face(analysis(quality=False), [user()])
        unknown = analysis()
        unknown['feature'] *= -1
        unknown = rank_face(unknown, [user()])
        self.assertEqual(decide_session([bad, bad], [unknown, unknown])['status'], 'DENIED')

    def test_upgraded_profile_cannot_bypass_eye_check_via_legacy(self):
        item = user()
        value = json.loads(item["face_features"])
        value["templates"].append({"mode": "legacy", "model": face_ai.MODEL_ID,
                                    "feature": analysis()["feature"].tolist()})
        item["face_features"] = json.dumps(value)
        sample = analysis()
        sample["eye_feature"] *= -1
        self.assertFalse(rank_face(sample, [item], dict(settings(), eye_check_policy='enforce'))["accepted"])

    def test_minor_box_overflow_is_tolerated_but_cropped_face_or_eyes_rejected(self):
        image = np.clip(np.random.default_rng(1).normal(120, 35, (240, 320, 3)), 0, 255).astype(np.uint8)
        face = np.array([40, 20, 160, 225, 80, 80, 160, 80, 120, 115, 90, 150, 150, 150, .99], np.float32)
        quality, _ = measure_quality(image, face, settings())
        self.assertTrue(quality['ok'])
        self.assertGreater(quality['visible_face_fraction'], .95)
        face[3] = 300
        self.assertFalse(measure_quality(image, face, settings())[0]['ok'])
        face[3] = 180
        face[4:8] = [2, 80, 82, 80]
        self.assertFalse(measure_quality(image, face, settings())[0]['ok'])

    def test_quality_checks_eye_region_blur_size_exposure_and_pose(self):
        config = settings()
        image = np.clip(np.random.default_rng(1).normal(120, 35, (240, 320, 3)), 0, 255).astype(np.uint8)
        face = np.array([40, 20, 160, 180, 80, 80, 160, 80, 120, 115, 90, 150, 150, 150, .99], np.float32)
        self.assertTrue(measure_quality(image, face, config)[0]["ok"])
        blurred = cv2.GaussianBlur(image, (25, 25), 6)
        self.assertFalse(measure_quality(blurred, face, config)[0]["ok"])
        self.assertFalse(measure_quality(np.zeros_like(image), face, config)[0]["ok"])
        small = face.copy()
        small[2:4] = 40
        self.assertFalse(measure_quality(image, small, config)[0]["ok"])
        tilted = face.copy()
        tilted[7] = 150
        self.assertFalse(measure_quality(image, tilted, config)[0]["ok"])

    def test_decoder_handles_empty_corrupt_and_unicode_filename(self):
        self.assertIsNone(analyze_face(b"")["feature"])
        self.assertIsNone(analyze_face(b"not an image")["feature"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ảnh.png"
            image = np.zeros((32, 32, 3), np.uint8)
            path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
            self.assertEqual(face_ai._load_image(str(path)).shape, image.shape)

    def test_real_models_load_and_do_not_detect_a_face_in_blank_image(self):
        result = analyze_face(np.zeros((240, 320, 3), np.uint8))
        self.assertIsNone(result["feature"])
        self.assertFalse(result["quality"]["ok"])

    def test_evaluator_counts_unknown_and_known_sessions_without_database(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text(json.dumps({
                'gallery': [{'id': 1, 'name': 'Person', 'unmasked': ['enroll.jpg']}],
                'probes': [
                    {'session': 'known', 'camera': 'master', 'frame_index': i,
                     'path': f'known{i}.jpg', 'expected_user_id': 1} for i in (0, 1)
                ] + [
                    {'session': 'unknown', 'camera': 'master', 'frame_index': i,
                     'path': f'unknown{i}.jpg', 'expected_user_id': None} for i in (0, 1)
                ]}), encoding='utf-8')
            def fake_analyze(path, camera=None):
                sample = analysis()
                if 'unknown' in path:
                    sample['feature'] *= -1
                return sample
            with patch.object(evaluate_recognition, 'analyze_face', side_effect=fake_analyze):
                report = evaluate_recognition.evaluate(manifest)
            self.assertEqual(len(report['thresholds']), 6)
            for row in report['thresholds']:
                self.assertEqual(row['unknown_sessions'], 1)
                self.assertEqual(row['genuine_sessions'], 1)
                self.assertEqual(row['unknown_accept_rate'], 0)
                self.assertEqual(row['false_reject_rate'], 0)


class DatabaseMigrationTests(unittest.TestCase):
    def test_diagnostics_migration_preserves_old_history_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'old.db')
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE access_history (id INTEGER PRIMARY KEY, session_id TEXT, status TEXT)')
                conn.execute("INSERT INTO access_history VALUES (1,'OLD','DENIED')")
                conn.commit()
            with patch.object(db, 'DB_PATH', path):
                db.init_db()
                db.init_db()
            with contextlib.closing(sqlite3.connect(path)) as conn:
                row = conn.execute('SELECT session_id,status,quality_report FROM access_history').fetchone()
            self.assertEqual(row, ('OLD', 'DENIED', None))


class CaptureApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = str(root / "database.db")
        with patch.object(db, 'DB_PATH', self.database):
            db.init_db()
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO authorized_users (id,name,user_code,face_features) VALUES (1,?,?,?)",
                         ("Person 1", "ONE", user()["face_features"]))
            conn.commit()
        config = dict(settings())
        self.namespace = dict(
            app=FastAPI(), Request=Request, Form=Form, File=File, Query=Query,
            Annotated=Annotated, Optional=Optional, UploadFile=UploadFile,
            HTTPException=HTTPException, JSONResponse=JSONResponse, RedirectResponse=RedirectResponse,
            sqlite3=sqlite3, os=os, time=time, json=json, secrets=secrets, asyncio=asyncio,
            print=lambda *args, **kwargs: None,
            DB_PATH=self.database, CAPTURES_DIR=str(root), settings=lambda: config,
            sessions={}, camera_status={"master": {}, "slave": {}},
            camera_heartbeat={"master": None, "slave": None},
            analyze_face=Mock(return_value=analysis()), rank_face=rank_face, decide_session=decide_session,
            archive_history_if_needed=lambda: None, require_admin=lambda request: None,
            valid_csrf=lambda request, token: token == 'csrf',
            make_template=make_template, read_templates=read_templates,
        )
        wanted = {'get_db_connection', 'capture_session', 'read_capture', 'ingest_capture',
                  'settle_capture', 'start_capture', 'complete_capture', 'capture_result',
                  'upload_master', 'upload_slave', 'get_session_quality', 'enrollment_templates',
                  'add_face_samples', 'get_face_sample_summary', 'receive_heartbeat', 'health'}
        tree = ast.parse((SERVER / 'app.py').read_text(encoding='utf-8-sig'))
        tree.body = [item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in wanted]
        exec(compile(tree, str(SERVER / 'app.py'), 'exec'), self.namespace)

    def request(self, content=b"image"):
        async def stream():
            yield content
        return type('RequestStub', (), {'stream': staticmethod(stream)})()

    async def capture(self, camera, index, final=False, session='TEST'):
        with contextlib.redirect_stdout(io.StringIO()):
            return await self.namespace['upload_' + camera](self.request(), session, index, final)

    async def test_three_frame_protocol_caches_one_decision_and_history_row(self):
        await self.capture('master', 0)
        await self.capture('master', 1)
        result = await self.capture('master', 2, True)
        self.assertEqual(result['status'], 'UNLOCK')
        self.assertEqual(result['authorization_camera'], 'master')
        cached = await self.capture('master', 2, True)
        self.assertEqual(cached, result)
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_history').fetchone()[0], 1)

    async def test_slave_unlocks_and_late_master_photo_keeps_verdict(self):
        await self.capture('slave', 0)
        allowed = await self.capture('slave', 1, True)
        self.assertEqual(self.namespace['analyze_face'].call_count, 2)
        self.assertEqual(allowed['status'], 'UNLOCK')
        self.assertEqual(allowed['authorization_camera'], 'slave')
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        cached = await self.capture('master', 0, True)
        self.assertEqual(cached, allowed)
        self.assertEqual(self.namespace['analyze_face'].call_count, 2)
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            row = conn.execute('SELECT status,master_img FROM access_history WHERE session_id=?', ('TEST',)).fetchone()
        self.assertEqual(row[0], 'UNLOCK')
        self.assertIsNotNone(row[1])

    async def test_bad_slave_does_not_veto_good_master(self):
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('slave', 0)
        pending = await self.capture('slave', 1, True)
        self.assertEqual(pending['status'], 'PENDING')
        self.namespace['analyze_face'].return_value = analysis()
        await self.capture('master', 0)
        allowed = await self.capture('master', 1, True)
        self.assertEqual(allowed['status'], 'UNLOCK')

    async def test_failed_master_waits_for_slave_then_slave_unlocks(self):
        await self.namespace['start_capture']('TEST')
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        pending = await self.capture('master', 1, True)
        self.assertEqual(pending['status'], 'PENDING')
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_history').fetchone()[0], 0)
        self.namespace['analyze_face'].return_value = analysis()
        await self.capture('slave', 0)
        allowed = await self.capture('slave', 1, True)
        self.assertEqual(allowed['status'], 'UNLOCK')
        self.assertEqual(allowed['authorization_camera'], 'slave')
        report = await self.namespace['get_session_quality']('TEST', self.request())
        self.assertEqual(len(report['master']), 2)
        self.assertEqual(len(report['slave']), 2)

    async def test_no_master_frames_still_allows_slave_and_polling_retrieves_result(self):
        self.assertEqual((await self.namespace['start_capture']('TEST'))['status'], 'PENDING')
        self.assertEqual((await self.namespace['complete_capture']('TEST', 'master'))['status'], 'PENDING')
        await self.capture('slave', 0)
        await self.capture('slave', 1, True)
        result = await self.namespace['capture_result']('TEST')
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'slave'))

    async def test_votes_from_different_cameras_or_sessions_are_not_combined(self):
        await self.capture('master', 0, True)
        denied = await self.capture('slave', 0, True)
        self.assertEqual(denied['status'], 'DENIED')
        await self.capture('master', 0, True, 'OTHER')
        await self.capture('slave', 1, True, 'THIRD')
        self.assertEqual((await self.namespace['capture_result']('OTHER'))['status'], 'PENDING')
        self.assertEqual((await self.namespace['capture_result']('THIRD'))['status'], 'PENDING')

    async def test_deadline_denies_missing_camera_and_ignores_late_good_frames(self):
        await self.capture('master', 0, True)
        slot = self.namespace['sessions']['TEST']
        slot['deadline'] = time.monotonic() - 1
        denied = await self.namespace['capture_result']('TEST')
        self.assertEqual(denied['status'], 'DENIED')
        self.assertTrue(denied['timed_out'])
        await self.capture('slave', 0)
        late = await self.capture('slave', 1, True)
        self.assertEqual(late, denied)
        self.assertEqual(self.namespace['analyze_face'].call_count, 1)

    async def test_deadline_can_use_two_completed_votes_when_final_upload_was_lost(self):
        await self.capture('slave', 0)
        await self.capture('slave', 1)
        self.namespace['sessions']['TEST']['deadline'] = time.monotonic() - 1
        allowed = await self.namespace['capture_result']('TEST')
        self.assertEqual((allowed['status'], allowed['authorization_camera']), ('UNLOCK', 'slave'))

    async def test_cached_verdict_survives_restart_without_new_history_or_inference(self):
        await self.capture('slave', 0)
        allowed = await self.capture('slave', 1, True)
        self.namespace['sessions'].clear()
        self.assertEqual(await self.namespace['capture_result']('TEST'), allowed)
        self.assertEqual(await self.capture('master', 0, True), allowed)
        with self.assertRaises(HTTPException) as caught:
            await self.namespace['start_capture']('TEST')
        self.assertEqual(caught.exception.status_code, 409)
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_history').fetchone()[0], 1)
        self.assertEqual(self.namespace['analyze_face'].call_count, 2)

    async def test_concurrent_cameras_duplicates_and_polls_produce_one_history_row(self):
        await self.namespace['start_capture']('TEST')
        responses = await asyncio.gather(
            self.capture('master', 0), self.capture('slave', 0),
            self.capture('master', 0), self.capture('slave', 0),
            self.capture('master', 1, True), self.capture('slave', 1, True),
            self.namespace['capture_result']('TEST'))
        final = await self.namespace['capture_result']('TEST')
        self.assertEqual(final['status'], 'UNLOCK')
        for response in responses:
            if response['status'] != 'PENDING':
                self.assertEqual(response, final)
        slot = self.namespace['sessions']['TEST']
        self.assertLessEqual(len(slot['master']), 2)
        self.assertLessEqual(len(slot['slave']), 2)
        self.assertEqual(slot['inflight'], {'master': 0, 'slave': 0})
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_history').fetchone()[0], 1)

    async def test_slow_inference_cannot_authorize_after_timeout(self):
        await self.capture('slave', 0)
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_thread_call(function, *args):
            started.set()
            await release.wait()
            return analysis()
        with patch.object(asyncio, 'to_thread', side_effect=slow_thread_call):
            task = asyncio.create_task(self.capture('slave', 1, True))
            await started.wait()
            self.namespace['sessions']['TEST']['deadline'] = time.monotonic() - 1
            denied = await self.namespace['capture_result']('TEST')
            release.set()
            self.assertEqual(await task, denied)
        self.assertEqual(denied['status'], 'DENIED')

    async def test_poll_during_final_inference_cannot_deny_prematurely(self):
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0, True)
        self.namespace['analyze_face'].return_value = analysis()
        await self.capture('slave', 0)
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_thread_call(function, *args):
            started.set()
            await release.wait()
            return analysis()
        with patch.object(asyncio, 'to_thread', side_effect=slow_thread_call):
            task = asyncio.create_task(self.capture('slave', 1, True))
            await started.wait()
            self.assertEqual((await self.namespace['capture_result']('TEST'))['status'], 'PENDING')
            release.set()
            self.assertEqual((await task)['status'], 'UNLOCK')

    async def test_failed_last_frame_completion_and_bounded_reply(self):
        await self.capture('slave', 0)
        await self.capture('slave', 1)
        allowed = await self.namespace['complete_capture']('TEST', 'slave')
        self.assertEqual(allowed['status'], 'UNLOCK')
        self.assertLess(len(json.dumps(allowed, ensure_ascii=False).encode('utf-8')), 4096)

    async def test_frame_diagnostics_remain_available_after_session_cache_cleared(self):
        await self.capture('master', 0)
        await self.capture('master', 1, True)
        self.namespace['sessions'].clear()
        report = await self.namespace['get_session_quality']('TEST', self.request())
        self.assertEqual(report['decision']['authorization_camera'], 'master')
        self.assertEqual(len(report['master']), 2)
        self.assertNotIn('feature', report['master']['0'])

    async def test_retransmission_of_same_frame_does_not_add_vote(self):
        await self.capture('master', 0)
        result = await self.capture('master', 0, True)
        self.assertEqual(result['status'], 'PENDING')
        result = await self.namespace['complete_capture']('TEST', 'slave')
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(self.namespace['analyze_face'].call_count, 1)

    async def test_missing_slave_still_allows_two_good_master_frames(self):
        await self.capture('master', 0)
        self.assertEqual((await self.capture('master', 1, True))['status'], 'UNLOCK')

    async def test_primary_master_accepts_without_any_slave(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        await self.capture('master', 0)
        await self.capture('master', 1)
        result = await self.namespace['complete_capture']('TEST', 'master', False)
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'master'))
        self.assertEqual(result['slave_frame_count'], 0)
        self.assertFalse(result['timed_out'])

    async def test_primary_master_rejects_without_waiting_for_absent_slave(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        # Network heartbeat alone does not mean Slave joined this capture.
        await self.namespace['receive_heartbeat']('slave')
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        await self.capture('master', 1)
        result = await self.namespace['complete_capture']('TEST', 'master', False)
        self.assertEqual(result['status'], 'DENIED')
        self.assertFalse(result['timed_out'])
        self.assertIn('Ảnh mờ', result['message'])
        self.assertEqual(result['camera_results']['slave']['status'], 'SKIPPED')
        self.assertNotIn('Slave', result['message'])

    async def test_primary_good_master_never_waits_for_ready_but_stuck_slave(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        await self.capture('master', 0)
        await self.capture('master', 1)
        result = await self.namespace['complete_capture']('TEST', 'master', True)
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'master'))

    async def test_primary_failed_master_allows_participating_slave_to_authorize(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        pending = await self.namespace['complete_capture']('TEST', 'master', True)
        self.assertEqual(pending['status'], 'PENDING')
        self.namespace['analyze_face'].return_value = analysis()
        await self.capture('slave', 0)
        result = await self.capture('slave', 1, True)
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'slave'))

    async def test_primary_zero_master_frames_can_use_ready_slave(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        pending = await self.namespace['complete_capture']('TEST', 'master', True)
        self.assertEqual(pending['status'], 'PENDING')
        await self.capture('slave', 0)
        result = await self.capture('slave', 1, True)
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'slave'))

    async def test_primary_master_has_priority_even_with_higher_scoring_slave(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        await self.capture('slave', 0)
        pending = await self.capture('slave', 1, True)
        self.assertEqual(pending['status'], 'PENDING')
        await self.capture('master', 0)
        await self.capture('master', 1)
        slot = self.namespace['sessions']['TEST']
        for frame in slot['slave'].values():
            frame['score'] = .99
        for frame in slot['master'].values():
            frame['score'] = .70
        result = await self.namespace['complete_capture']('TEST', 'master', True)
        self.assertEqual(result['verified_cameras'], ['master', 'slave'])
        self.assertEqual(result['authorization_camera'], 'master')
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode('utf-8')), 4096)

    async def test_primary_actual_slave_upload_survives_lost_ready_ack(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        await self.capture('slave', 0)
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        result = await self.namespace['complete_capture']('TEST', 'master', False)
        self.assertEqual(result['status'], 'PENDING')
        self.namespace['analyze_face'].return_value = analysis()
        result = await self.capture('slave', 1, True)
        self.assertEqual((result['status'], result['authorization_camera']), ('UNLOCK', 'slave'))

    async def test_primary_stuck_slave_has_bounded_wait_and_retry_cannot_extend_it(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        await self.namespace['complete_capture']('TEST', 'master', True)
        slot = self.namespace['sessions']['TEST']
        first_deadline = slot['slave_deadline']
        self.assertLessEqual(first_deadline - time.monotonic(), 10)
        await self.namespace['complete_capture']('TEST', 'master', False)
        self.assertTrue(slot['slave_expected'])
        self.assertEqual(slot['slave_deadline'], first_deadline)
        slot['slave_deadline'] = time.monotonic() - 1
        result = await self.namespace['capture_result']('TEST')
        self.assertEqual(result['status'], 'DENIED')
        self.assertTrue(result['slave_timed_out'])
        self.assertFalse(result['timed_out'])
        self.namespace['analyze_face'].return_value = analysis()
        await self.capture('slave', 0)
        self.assertEqual(await self.capture('slave', 1, True), result)

    async def test_primary_mode_cannot_change_during_session(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        with self.assertRaises(HTTPException) as caught:
            await self.namespace['start_capture']('TEST', 'dual')
        self.assertEqual(caught.exception.status_code, 409)

    async def test_health_identifies_backend_with_master_primary_support(self):
        result = await self.namespace['health']()
        self.assertIn('master_primary', result['capture_modes'])
        self.assertEqual(result['protocol_version'], 3)
        self.assertEqual(result['backend_version'], 'master-primary-multiview-v1')

    async def test_heartbeat_is_processed_while_master_inference_is_pending(self):
        await self.namespace['start_capture']('TEST', 'master_primary')
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_thread_call(function, *args):
            started.set()
            await release.wait()
            return analysis()
        with patch.object(asyncio, 'to_thread', side_effect=slow_thread_call):
            task = asyncio.create_task(self.capture('master', 0))
            try:
                await started.wait()
                response = await self.namespace['receive_heartbeat']('master')
                self.assertEqual(response['status'], 'OK')
                self.assertIsNotNone(self.namespace['camera_heartbeat']['master'])
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_rejected_quality_never_unlocks(self):
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        await self.capture('master', 0)
        self.assertEqual((await self.capture('master', 1, True))['status'], 'PENDING')
        result = await self.namespace['complete_capture']('TEST', 'slave')
        self.assertEqual(result['status'], 'DENIED')
        self.assertIn('Ảnh mờ', result['message'])

    async def test_deleted_profile_cannot_unlock_from_earlier_votes(self):
        await self.capture('master', 0)
        await self.capture('master', 1)
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            conn.execute('DELETE FROM authorized_users WHERE id=1')
            conn.commit()
        await self.capture('master', 2, True)
        result = await self.namespace['complete_capture']('TEST', 'slave')
        self.assertEqual(result['status'], 'DENIED')

    async def test_oversize_upload_and_expired_session(self):
        with self.assertRaises(HTTPException) as caught:
            await self.namespace['read_capture'](self.request(b'x' * (4194304 + 1)))
        self.assertEqual(caught.exception.status_code, 413)
        slot = self.namespace['capture_session']('OLD')
        slot['created'] = time.monotonic() - 121
        self.namespace['capture_session']('NEW')
        self.assertNotIn('OLD', self.namespace['sessions'])

    async def test_add_masked_samples_atomic_on_quality_failure_and_requires_csrf(self):
        photo = lambda: UploadFile(filename='mask.jpg', file=io.BytesIO(b'image'))
        route = self.namespace['add_face_samples']
        with self.assertRaises(HTTPException) as caught:
            await route(1, self.request(), 'bad', 'masked', [photo()])
        self.assertEqual(caught.exception.status_code, 403)
        result = await route(1, self.request(), 'csrf', 'masked', [photo(), photo()])
        self.assertEqual(result['sample_count'], 3)
        self.namespace['analyze_face'].return_value = analysis(quality=False)
        with self.assertRaises(HTTPException):
            await route(1, self.request(), 'csrf', 'masked', [photo()])
        summary = await self.namespace['get_face_sample_summary'](1, self.request())
        self.assertEqual(summary['sample_count'], 3)
        self.assertNotIn('feature', summary)

    async def test_sample_modes_are_saved_and_counted_independently(self):
        route = self.namespace['add_face_samples']
        photo = lambda name: UploadFile(filename=name, file=io.BytesIO(b'image'))
        before = await self.namespace['get_face_sample_summary'](1, self.request())
        self.assertEqual((before['unmasked'], before['masked']), (0, 1))
        plain = await route(1, self.request(), 'csrf', 'unmasked', [photo('plain.jpg'), photo('plain2.jpg')])
        self.assertEqual((plain['unmasked'], plain['masked']), (2, 1))
        self.assertEqual(plain['mode'], 'unmasked')
        masked = await route(1, self.request(), 'csrf', 'masked', [photo('mask.jpg')])
        self.assertEqual((masked['unmasked'], masked['masked']), (2, 2))
        self.assertEqual(masked['mode'], 'masked')
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            templates = read_templates(conn.execute('SELECT face_features FROM authorized_users WHERE id=1').fetchone()[0])
        self.assertEqual([t['mode'] for t in templates], ['masked', 'unmasked', 'unmasked', 'masked'])

    async def test_http_query_validation_and_staged_response(self):
        async def post(query, path='/api/upload_master'):
            messages = []
            sent = False
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': b'image', 'more_body': False}
                return {'type': 'http.disconnect'}
            async def send(message):
                messages.append(message)
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                     'method': 'POST', 'scheme': 'http', 'path': path,
                     'query_string': query.encode(), 'root_path': '',
                     'headers': [(b'content-type', b'image/jpeg')],
                     'server': ('test', 80), 'client': ('test', 1)}
            with contextlib.redirect_stdout(io.StringIO()):
                await self.namespace['app'](scope, receive, send)
            return messages
        rejected = await post('session_id=TEST&frame_index=3&final=0')
        self.assertEqual(rejected[0]['status'], 422)
        invalid_session = await post('session_id=..%2Fbad&frame_index=0&final=0')
        self.assertEqual(invalid_session[0]['status'], 422)
        staged = await post('session_id=TEST&frame_index=0&final=0')
        self.assertEqual(staged[0]['status'], 200)
        response = json.loads(staged[1]['body'])
        self.assertEqual(response['status'], 'PENDING')
        self.assertEqual(response['protocol_version'], 3)
        bad_mode = await post('session_id=PRIMARY&mode=wrong', '/api/capture/start')
        self.assertEqual(bad_mode[0]['status'], 422)
        primary = await post('session_id=PRIMARY&mode=master_primary', '/api/capture/start')
        self.assertEqual(primary[0]['status'], 200)
        self.assertEqual(json.loads(primary[1]['body'])['capture_mode'], 'master_primary')
        invalid_flag = await post('session_id=PRIMARY&camera=master&slave_expected=maybe',
                                  '/api/capture/complete')
        self.assertEqual(invalid_flag[0]['status'], 422)
        completed = await post('session_id=PRIMARY&camera=master&slave_expected=0',
                               '/api/capture/complete')
        self.assertEqual(completed[0]['status'], 200)
        result = json.loads(completed[1]['body'])
        self.assertEqual(result['status'], 'DENIED')  # No images cannot authorize.
        self.assertEqual(result['camera_results']['slave']['status'], 'SKIPPED')
        self.assertFalse(result['timed_out'])


if __name__ == '__main__':
    unittest.main()

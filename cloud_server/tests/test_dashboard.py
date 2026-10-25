"""Run with: python -m unittest discover -s cloud_server/tests -v.

Load the real user handlers without running app.py's module-level initialization,
which creates production folders and opens the production database.
AI output is stubbed; these tests exercise the popup API contract, not recognition.
"""
import ast
import asyncio
import contextlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from typing import Annotated, Optional
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined


SERVER = Path(__file__).resolve().parents[1]


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        database = root / 'database.db'
        with contextlib.closing(sqlite3.connect(database)) as conn:
            conn.execute('''CREATE TABLE authorized_users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                user_code TEXT UNIQUE NOT NULL, role TEXT,
                avatar_path TEXT, face_features TEXT)''')
        self.namespace = dict(
            app=FastAPI(), Request=Request, Form=Form, File=File,
            UploadFile=UploadFile, HTTPException=HTTPException,
            JSONResponse=JSONResponse, RedirectResponse=RedirectResponse,
            sqlite3=sqlite3, os=os, time=time, json=json, hmac=hmac, secrets=secrets,
            Annotated=Annotated, Optional=Optional,
            DB_PATH=str(database), AVATARS_DIR=str(root), ADMIN_USERNAME='test-admin',
            analyze_face=Mock(return_value={'feature': [0.2, 0.4], 'quality': {'ok': True, 'reasons': []}}),
            make_template=lambda result, mode: {'feature': result['feature'], 'mode': mode},
            settings=lambda: {'max_upload_bytes': 4194304, 'max_templates_per_user': 20},
            asyncio=asyncio,
        )
        wanted = {'get_db_connection', 'is_admin', 'require_admin', 'valid_csrf',
                  'enrollment_templates', 'add_user', 'delete_user'}
        tree = ast.parse((SERVER / 'app.py').read_text(encoding='utf-8-sig'))
        tree.body = [item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in wanted]
        exec(compile(tree, str(SERVER / 'app.py'), 'exec'), self.namespace)

    def request(self, accept='application/json', admin=True):
        return SimpleNamespace(headers={'accept': accept}, session={
            'admin': 'test-admin' if admin else None, 'csrf_token': 'test-csrf',
        })

    def add_user(self, request=None, code='USER_001', csrf='test-csrf', filename='face.jpg'):
        upload = UploadFile(filename=filename, file=io.BytesIO(b'test image'))
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(self.namespace['add_user'](
                request or self.request(), name='Nguyễn Văn An', user_code=code,
                role='Thành viên', avatar=upload, csrf_token=csrf,
            ))

    def test_popup_register_returns_profile_without_biometric_features(self):
        response = self.add_user(filename='ảnh khuôn mặt.jpg')
        self.assertEqual(response.status_code, 201)
        user = json.loads(response.body)['user']
        self.assertEqual(user['name'], 'Nguyễn Văn An')
        self.assertEqual(user['user_code'], 'USER_001')
        self.assertNotIn('face_features', user)
        with contextlib.closing(sqlite3.connect(self.namespace['DB_PATH'])) as conn:
            self.assertEqual(conn.execute('SELECT name FROM authorized_users WHERE id=?', (user['id'],)).fetchone()[0], user['name'])

    def test_no_face_returns_json_error_and_cleans_temporary_photo(self):
        self.namespace['analyze_face'].return_value = {'feature': None, 'quality': {'ok': False, 'reasons': ['Không phát hiện khuôn mặt']}}
        response = self.add_user()
        self.assertEqual(response.status_code, 422)
        self.assertIn('khuôn mặt', json.loads(response.body)['detail'])
        self.assertFalse(list(Path(self.temp.name).glob('user_*.jpg')))

    def test_duplicate_code_returns_conflict_without_duplicate_row(self):
        first = json.loads(self.add_user(filename='face.jpg').body)['user']
        first_avatar = Path(self.temp.name) / Path(first['avatar_path']).name
        first_content = first_avatar.read_bytes()
        response = self.add_user(filename='face.jpg')
        self.assertEqual(response.status_code, 409)
        with contextlib.closing(sqlite3.connect(self.namespace['DB_PATH'])) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM authorized_users').fetchone()[0], 1)
        self.assertEqual(first_avatar.read_bytes(), first_content)
        self.assertEqual(len(list(Path(self.temp.name).glob('user_*.jpg'))), 1)

    def test_existing_form_redirects_still_work(self):
        response = self.add_user(self.request('text/html'))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/?register_success=1')
        self.namespace['analyze_face'].return_value = {'feature': None, 'quality': {'ok': False, 'reasons': ['Không phát hiện khuôn mặt']}}
        response = self.add_user(self.request('text/html'), code='OTHER', filename='other.jpg')
        self.assertEqual(response.headers['location'], '/?register_error=no_face')

    def test_popup_delete_updates_database_without_redirect(self):
        user_id = json.loads(self.add_user().body)['user']['id']
        response = asyncio.run(self.namespace['delete_user'](user_id, self.request(), 'test-csrf'))
        self.assertEqual(response, {'success': True, 'user_id': user_id})
        with contextlib.closing(sqlite3.connect(self.namespace['DB_PATH'])) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM authorized_users').fetchone()[0], 0)

    def test_existing_delete_form_redirects_still_work(self):
        response = asyncio.run(self.namespace['delete_user'](1, self.request('text/html'), 'test-csrf'))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/')

    def test_register_and_delete_require_admin_and_csrf(self):
        for admin, csrf, expected in [(False, 'test-csrf', 401), (True, 'invalid', 403)]:
            for handler in ['add', 'delete']:
                with self.subTest(handler=handler, admin=admin, csrf=csrf):
                    with self.assertRaises(HTTPException) as caught:
                        if handler == 'add':
                            self.add_user(self.request(admin=admin), csrf=csrf)
                        else:
                            asyncio.run(self.namespace['delete_user'](1, self.request(admin=admin), csrf))
                    self.assertEqual(caught.exception.status_code, expected)


class TemplateTests(unittest.TestCase):
    def test_asset_version_and_sample_picker_are_rendered_for_existing_users(self):
        env = Environment(loader=FileSystemLoader(SERVER / 'templates'), autoescape=True, undefined=StrictUndefined)
        template = env.get_template('index.html')
        context = dict(users=[{'id': 7, 'name': 'Người đã đăng ký', 'user_code': 'OLD',
                              'role': 'Thành viên', 'avatar_path': '/avatars/face.jpg'}],
                       total_access=0, unlocked_count=0, denied_count=0, csrf_token='test',
                       url_for=lambda _, path: '/static/' + path)
        first = template.render(**context, static_version='100')
        updated = template.render(**context, static_version='101')
        self.assertIn('/static/dashboard.js?v=100', first)
        self.assertIn('/static/dashboard.js?v=101', updated)
        self.assertNotIn('/static/dashboard.js?v=100', updated)
        self.assertIn('data-add-samples="7"', updated)
        self.assertIn('id="samples-dialog"', updated)
        self.assertIn('id="samples-form-unmasked"', updated)
        self.assertIn('id="samples-form-masked"', updated)
        self.assertIn('name="mode" value="unmasked"', updated)
        self.assertIn('name="mode" value="masked"', updated)
        self.assertIn('name="photos" accept="image/*" multiple required', updated)

    def test_dashboard_renders_empty_and_populated_states_and_escapes_user_text(self):
        env = Environment(loader=FileSystemLoader(SERVER / 'templates'), autoescape=True, undefined=StrictUndefined)
        template = env.get_template('index.html')
        for users in [[], [{'id': 1, 'name': '<script>alert(1)</script>', 'user_code': 'A"B', 'role': 'Thành viên', 'avatar_path': '/avatars/test.jpg'}]]:
            html = template.render(users=users, total_access=0, unlocked_count=0, denied_count=0,
                                   csrf_token='test', url_for=lambda _, path: '/static/' + path)
            self.assertIn('Nhật ký nhận diện', html)
            self.assertNotIn('Lịch sử truy cập', html)
            self.assertNotIn('<script>alert(1)</script>', html)
            self.assertIn('id="history-search"', html)
            self.assertIn('id="workspace-dialog"', html)


if __name__ == '__main__':
    unittest.main()

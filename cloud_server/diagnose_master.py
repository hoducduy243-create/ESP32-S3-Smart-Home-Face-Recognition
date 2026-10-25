"""Read-only inspection of enrollment coverage and recent Master failures."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from db import DB_PATH
from face_ai import analyze_face, compare_faces, read_templates
from recognition import rank_face


def diagnose(limit=8):
    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        users = [dict(row) for row in conn.execute('SELECT id,name,face_features FROM authorized_users')]
        history = [dict(row) for row in conn.execute(
            'SELECT session_id,status,confidence,message,master_img,created_at FROM access_history ORDER BY id DESC LIMIT ?',
            (limit,))]
    finally:
        conn.close()
    print('ENROLLMENT')
    for user in users:
        templates = read_templates(user['face_features'])
        print(json.dumps({'id': user['id'], 'name': user['name'],
                          'counts': {mode: sum(t.get('mode') == mode for t in templates)
                                     for mode in ('unmasked', 'masked', 'legacy')}}, ensure_ascii=False))
    print('CROSS_PROFILE_NEAR_DUPLICATES')
    for index, user in enumerate(users):
        for other in users[index + 1:]:
            scores = [compare_faces(a.get('feature'), b.get('feature'))
                      for a in read_templates(user['face_features'])
                      for b in read_templates(other['face_features'])]
            if scores and max(scores) >= .90:
                print(json.dumps({'user_ids': [user['id'], other['id']], 'max_score': max(scores)}))
    print('RECENT_MASTER')
    captures = Path(__file__).parent / 'captures'
    for row in history:
        image = row.get('master_img')
        if image:
            path = captures / Path(image).name
            if path.is_file():
                result = rank_face(analyze_face(str(path), 'master'), users)
                row['reanalysis'] = result
        print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    diagnose(args.limit)

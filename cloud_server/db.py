import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Bảng người dùng hợp lệ (được phép mở cửa)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS authorized_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user_code TEXT UNIQUE NOT NULL,
            role TEXT DEFAULT 'Member',
            avatar_path TEXT,
            face_features TEXT, -- Lưu SFace embedding dạng JSON
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Bảng lịch sử ra vào
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT DEFAULT 'Người lạ (Unknown)',
            status TEXT NOT NULL, -- 'UNLOCK' hoặc 'DENIED'
            confidence REAL DEFAULT 0.0,
            master_img TEXT,
            slave_img TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES authorized_users(id)
        )
    """)
    # Preserve per-frame diagnostics after the in-memory capture session expires.
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(access_history)")}
    if "quality_report" not in columns:
        cursor.execute("ALTER TABLE access_history ADD COLUMN quality_report TEXT")
    # Bảng lưu trữ tạm lịch sử cũ
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER,
            session_id TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT DEFAULT 'Người lạ (Unknown)',
            status TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            master_img TEXT,
            slave_img TEXT,
            message TEXT,
            original_created_at TIMESTAMP,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database SQLite đã khởi tạo thành công tại:", DB_PATH)

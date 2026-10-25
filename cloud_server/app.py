import os

import time

import json

import sqlite3

import shutil

import asyncio

import secrets

import hmac

from typing import Optional, Annotated



from fastapi import FastAPI, Request, Query, UploadFile, File, Form, HTTPException, Header

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse

from fastapi.staticfiles import StaticFiles

from fastapi.templating import Jinja2Templates

from starlette.middleware.sessions import SessionMiddleware

from dotenv import load_dotenv

from security import check_admin_password

from db import DB_PATH, init_db

from face_ai import analyze_face, make_template, read_templates, settings
from recognition import rank_face, decide_session





# ============================================================

# PATHS

# ============================================================



BASE_DIR = os.path.dirname(os.path.abspath(__file__))



load_dotenv(os.path.join(BASE_DIR, ".env"))



ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")



if not ADMIN_USERNAME or not SESSION_SECRET_KEY:

    raise RuntimeError("Missing Admin configuration")



if len(SESSION_SECRET_KEY) < 64:

    raise RuntimeError("Session secret key is too short")



CAPTURES_DIR = os.path.join(BASE_DIR, "captures")

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

STATIC_DIR = os.path.join(BASE_DIR, "static")

AVATARS_DIR = os.path.join(BASE_DIR, "avatars")

ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")



ARCHIVE_LIMIT = 100

ARCHIVE_KEEP_DAYS = 5



os.makedirs(CAPTURES_DIR, exist_ok=True)

os.makedirs(TEMPLATES_DIR, exist_ok=True)

os.makedirs(STATIC_DIR, exist_ok=True)

os.makedirs(AVATARS_DIR, exist_ok=True)

os.makedirs(ARCHIVE_DIR, exist_ok=True)

# ============================================================



init_db()





def get_db_connection():

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    return conn

def archive_history_if_needed():

    conn = get_db_connection()



    try:

        total = conn.execute(

            "SELECT COUNT(*) FROM access_history"

        ).fetchone()[0]



        if total < ARCHIVE_LIMIT:

            return False



        records = conn.execute(

            """

            SELECT *

            FROM access_history

            ORDER BY id ASC

            LIMIT ?

            """,

            (ARCHIVE_LIMIT,)

        ).fetchall()



        if len(records) < ARCHIVE_LIMIT:

            return False



        for record in records:

            master_img = record["master_img"]

            slave_img = record["slave_img"]



            archived_master_img = move_capture_to_archive(

                master_img

            )



            archived_slave_img = move_capture_to_archive(

                slave_img

            )



            conn.execute(

                """

                INSERT INTO access_archive

                (

                    original_id,

                    session_id,

                    user_id,

                    user_name,

                    status,

                    confidence,

                    master_img,

                    slave_img,

                    message,

                    original_created_at

                )

                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                """,

                (

                    record["id"],

                    record["session_id"],

                    record["user_id"],

                    record["user_name"],

                    record["status"],

                    record["confidence"],

                    archived_master_img,

                    archived_slave_img,

                    record["message"],

                    record["created_at"]

                )

            )



        record_ids = [

            record["id"]

            for record in records

        ]



        placeholders = ",".join(

            "?" for _ in record_ids

        )



        conn.execute(

            f"""

            DELETE FROM access_history

            WHERE id IN ({placeholders})

            """,

            record_ids

        )



        conn.commit()



        print(

            f"[ARCHIVE] Đã chuyển "

            f"{len(records)} bản ghi vào archive."

        )



        return True



    except Exception as e:

        conn.rollback()



        print(

            f"[ARCHIVE ERROR] {e}"

        )



        return False



    finally:

        conn.close()





def move_capture_to_archive(image_url):

    if not image_url:

        return None



    filename = os.path.basename(image_url)



    source_path = os.path.join(

        CAPTURES_DIR,

        filename

    )



    archive_path = os.path.join(

        ARCHIVE_DIR,

        filename

    )



    if os.path.exists(source_path):

        shutil.move(

            source_path,

            archive_path

        )



    return f"/archive/{filename}"

def cleanup_expired_archive():

    conn = get_db_connection()



    try:

        expired_records = conn.execute(

            """

            SELECT *

            FROM access_archive

            WHERE archived_at <= datetime('now', ?)

            """,

            (f"-{ARCHIVE_KEEP_DAYS} days",)

        ).fetchall()



        for record in expired_records:

            for image_url in (

                record["master_img"],

                record["slave_img"]

            ):

                if not image_url:

                    continue



                filename = os.path.basename(image_url)



                filepath = os.path.join(

                    ARCHIVE_DIR,

                    filename

                )



                if os.path.exists(filepath):

                    os.remove(filepath)



        conn.execute(

            """

            DELETE FROM access_archive

            WHERE archived_at <= datetime('now', ?)

            """,

            (f"-{ARCHIVE_KEEP_DAYS} days",)

        )



        conn.commit()



        if expired_records:

            print(

                f"[ARCHIVE CLEANUP] Đã xóa "

                f"{len(expired_records)} bản ghi quá "

                f"{ARCHIVE_KEEP_DAYS} ngày."

            )



    except Exception as e:

        conn.rollback()



        print(

            f"[ARCHIVE CLEANUP ERROR] {e}"

        )

    finally:

        conn.close()





async def archive_cleanup_loop():

    while True:

        cleanup_expired_archive()

        await asyncio.sleep(3600)







# ============================================================

# ============================================================
# PERSISTENT CAMERA CONNECTION LOG (MASTER / SLAVE)
# ============================================================

HEARTBEAT_OFFLINE_SECONDS = 15


def init_camera_connection_log():
    """A separate table so access / face-recognition history stays intact."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS camera_connection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_name TEXT NOT NULL CHECK(camera_name IN ('master', 'slave')),
                event_type TEXT NOT NULL CHECK(event_type IN ('ONLINE', 'OFFLINE')),
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)


# Only keep current process' state in memory; history is durable in SQLite.
camera_connection_last_status = {"master": None, "slave": None}


def record_camera_connection_event(camera_name: str, new_status: str, detail: str):
    if camera_connection_last_status[camera_name] == new_status:
        return
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO camera_connection_log
               (camera_name, event_type, detail) VALUES (?, ?, ?)""",
            (camera_name, new_status, detail)
        )
    camera_connection_last_status[camera_name] = new_status
    print(f"[CONNECTION LOG] {camera_name.upper()}: {new_status} - {detail}")


async def monitor_camera_connections():
    """Save ONLINE/OFFLINE transitions, not every heartbeat."""
    while True:
        now = time.time()
        for camera_name in ("master", "slave"):
            last = camera_heartbeat[camera_name]
            is_online = last is not None and now - last <= HEARTBEAT_OFFLINE_SECONDS
            new_status = "ONLINE" if is_online else "OFFLINE"
            detail = (
                "Đã nhận tín hiệu Heartbeat từ thiết bị"
                if is_online else
                "Chưa có Heartbeat hoặc đã quá thời gian chờ 15 giây"
            )
            try:
                record_camera_connection_event(camera_name, new_status, detail)
            except sqlite3.Error as exc:
                print(f"[CONNECTION LOG ERROR] {camera_name}: {exc}")
        await asyncio.sleep(2)


# FASTAPI

# ============================================================



app = FastAPI(

    title="Smart Dual Camera Access Control With Database"

)

app.add_middleware(

    SessionMiddleware,

    secret_key=SESSION_SECRET_KEY,

    session_cookie="access_admin_session",

    max_age=3600,

    same_site="lax",

    https_only=False

)

@app.on_event("startup")

async def start_archive_cleanup():

    init_camera_connection_log()
    asyncio.create_task(monitor_camera_connections())

    cleanup_expired_archive()

    asyncio.create_task(archive_cleanup_loop())







app.mount(

    "/static",

    StaticFiles(directory=STATIC_DIR),

    name="static"

)







templates = Jinja2Templates(

    directory=TEMPLATES_DIR

)





# Lưu tạm thông tin ảnh Master/Slave theo session

sessions = {}

# ============================================================

# CAMERA ACTIVITY MONITORING

# ============================================================



camera_status = {

    "master": {

        "last_seen": None,

        "last_session": None

    },

    "slave": {

        "last_seen": None,

        "last_session": None

    }

}

# ============================================================

# ADMIN AUTHENTICATION

# ============================================================





def is_admin(request: Request) -> bool:

    return request.session.get("admin") == ADMIN_USERNAME





def require_admin(request: Request):

    if not is_admin(request):

        raise HTTPException(

            status_code=401,

            detail="Bạn chưa đăng nhập Admin"

        )



# ============================================================

# PROTECTED IMAGE ACCESS

# ============================================================



@app.get("/avatars/{filename}")

async def get_avatar(request: Request, filename: str):



    require_admin(request)



    if filename in (".", "..") or filename != os.path.basename(filename):

        raise HTTPException(

            status_code=400,

            detail="Tên file không hợp lệ"

        )



    filepath = os.path.join(AVATARS_DIR, filename)



    if not os.path.isfile(filepath):

        raise HTTPException(

            status_code=404,

            detail="Không tìm thấy ảnh"

        )



    return FileResponse(filepath)





@app.get("/captures/{filename}")

async def get_capture(request: Request, filename: str):



    require_admin(request)



    if filename in (".", "..") or filename != os.path.basename(filename):

        raise HTTPException(

            status_code=400,

            detail="Tên file không hợp lệ"

        )



    filepath = os.path.join(CAPTURES_DIR, filename)



    if not os.path.isfile(filepath):

        raise HTTPException(

            status_code=404,

            detail="Không tìm thấy ảnh"

        )



    return FileResponse(filepath)



def require_device_key(provided_key: str, expected_key: str):

    if not provided_key or not hmac.compare_digest(

        provided_key,

        expected_key

    ):

        raise HTTPException(

            status_code=401,

            detail="Device authentication failed"

        )



def get_csrf_token(request: Request) -> str:

    token = request.session.get("csrf_token")



    if not token:

        token = secrets.token_urlsafe(32)

        request.session["csrf_token"] = token



    return token





def valid_csrf(request: Request, token: str) -> bool:

    expected = request.session.get("csrf_token", "")



    return bool(expected) and hmac.compare_digest(

        expected, token

    )





@app.get("/login", response_class=HTMLResponse)

async def login_page(request: Request):



    if is_admin(request):

        return RedirectResponse(url="/", status_code=303)



    return templates.TemplateResponse(

        request=request,

        name="login.html",

        context={

            "error": None,

            "csrf_token": get_csrf_token(request)

        }

    )





@app.post("/login")

async def login(

    request: Request,

    username: str = Form(...),

    password: str = Form(...),

    csrf_token: str = Form(...)

):



    if not valid_csrf(request, csrf_token):

        return JSONResponse(

            {"error": "Invalid CSRF token"},

            status_code=403

        )



    valid_username = hmac.compare_digest(

        username.encode("utf-8"),

        ADMIN_USERNAME.encode("utf-8")

    )

    valid_password = check_admin_password(password)



    if valid_username and valid_password:

        request.session.clear()

        request.session["admin"] = ADMIN_USERNAME

        request.session["csrf_token"] = secrets.token_urlsafe(32)



        return RedirectResponse(url="/", status_code=303)



    return templates.TemplateResponse(

        request=request,

        name="login.html",

        context={

            "error": "Sai tài khoản hoặc mật khẩu!",

            "csrf_token": get_csrf_token(request)

        },

        status_code=401

    )





@app.post("/logout")

async def logout(

    request: Request,

    csrf_token: str = Form(...)

):



    if not is_admin(request):

        return RedirectResponse(url="/login", status_code=303)



    if not valid_csrf(request, csrf_token):

        return JSONResponse(

            {"error": "Invalid CSRF token"},

            status_code=403

        )



    request.session.clear()



    return RedirectResponse(url="/login", status_code=303)





# ============================================================

# DASHBOARD

# ============================================================



@app.get("/", response_class=HTMLResponse)

async def home(request: Request):



    if not is_admin(request):

        return RedirectResponse(

            url="/login",

            status_code=303

        )



    conn = get_db_connection()



    users = conn.execute(

        "SELECT * FROM authorized_users ORDER BY id DESC"

    ).fetchall()



    logs = conn.execute(

        "SELECT * FROM access_history ORDER BY id DESC LIMIT 50"

    ).fetchall()



    total_access = conn.execute(

        "SELECT COUNT(*) FROM access_history"

    ).fetchone()[0]



    unlocked_count = conn.execute(

        "SELECT COUNT(*) FROM access_history "

        "WHERE status = 'UNLOCK'"

    ).fetchone()[0]



    denied_count = conn.execute(

        "SELECT COUNT(*) FROM access_history "

        "WHERE status = 'DENIED'"

    ).fetchone()[0]



    conn.close()



    context = {

        "request": request,

        "users": [dict(u) for u in users],

        "logs": [{key: l[key] for key in l.keys() if key != "quality_report"} for l in logs],

        "total_access": total_access,

        "unlocked_count": unlocked_count,

        "denied_count": denied_count,

        "csrf_token": get_csrf_token(request),

        # A changed asset gets a new URL, preventing old JS with new form HTML.
        "static_version": str(max(
            os.stat(os.path.join(STATIC_DIR, filename)).st_mtime_ns
            for filename in ("dashboard.js", "dashboard.css")
        ))



    }



    return templates.TemplateResponse(

        request=request,

        name="index.html",

        context=context,

        headers={"Cache-Control": "no-store"}

    )





# ============================================================

# ADD USER

# ============================================================



async def enrollment_templates(uploads, mode):
    templates = []
    for upload in uploads:
        if not upload.filename:
            continue
        data = await upload.read(settings()["max_upload_bytes"] + 1)
        if len(data) > settings()["max_upload_bytes"]:
            raise HTTPException(413, "Mỗi ảnh tối đa 4 MB")
        result = await asyncio.to_thread(analyze_face, data)
        if result["feature"] is None or not result["quality"]["ok"]:
            reason = "; ".join(result["quality"]["reasons"])
            raise HTTPException(422, f"Ảnh {upload.filename}: {reason}")
        templates.append(make_template(result, mode))
    return templates


@app.post("/api/users/add")
async def add_user(
    request: Request, name: str = Form(...), user_code: str = Form(...),
    role: str = Form("Thành viên"), avatar: UploadFile = File(...),
    csrf_token: str = Form(...),
    photos: Annotated[Optional[list[UploadFile]], File()] = None,
    masked_photos: Annotated[Optional[list[UploadFile]], File()] = None,
):
    require_admin(request)
    if not valid_csrf(request, csrf_token):
        raise HTTPException(403, "CSRF token không hợp lệ")
    wants_json = "application/json" in request.headers.get("accept", "")
    count = 1 + len(photos or []) + len(masked_photos or [])
    if count > settings()["max_templates_per_user"]:
        raise HTTPException(422, "Mỗi người tối đa 20 ảnh mẫu")
    try:
        # Keep avatar bytes for the dashboard; supplementary photos are not retained.
        avatar_content = await avatar.read(settings()["max_upload_bytes"] + 1)
        await avatar.seek(0)
        templates = await enrollment_templates([avatar] + (photos or []), "unmasked")
        templates += await enrollment_templates(masked_photos or [], "masked")
        if not templates:
            raise HTTPException(422, "Cần ít nhất một ảnh khuôn mặt không khẩu trang")
    except HTTPException as exc:
        if wants_json:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return RedirectResponse("/?register_error=no_face", status_code=303)
    suffix = os.path.splitext(avatar.filename or "")[1].lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        suffix = ".jpg"
    filename = f"user_{int(time.time())}_{secrets.token_hex(8)}{suffix}"
    filepath = os.path.join(AVATARS_DIR, filename)
    conn = get_db_connection()
    try:
        with open(filepath, "wb") as stream:
            stream.write(avatar_content)
        cursor = conn.execute(
            "INSERT INTO authorized_users (name,user_code,role,avatar_path,face_features) VALUES (?,?,?,?,?)",
            (name.strip(), user_code.strip(), role, f"/avatars/{filename}",
             json.dumps({"version": 2, "templates": templates})))
        conn.commit()
        user_id = cursor.lastrowid
    except Exception as exc:
        conn.rollback()
        if os.path.exists(filepath):
            os.remove(filepath)
        if wants_json:
            duplicate = isinstance(exc, sqlite3.IntegrityError)
            return JSONResponse({"detail": "Mã người dùng đã tồn tại." if duplicate else "Không lưu được hồ sơ."},
                                status_code=409 if duplicate else 500)
        return RedirectResponse("/?register_error=database", status_code=303)
    finally:
        conn.close()
    if wants_json:
        return JSONResponse({"user": {"id": user_id, "name": name.strip(), "user_code": user_code.strip(),
                             "role": role, "avatar_path": f"/avatars/{filename}",
                             "sample_count": len(templates)}}, status_code=201)
    return RedirectResponse("/?register_success=1", status_code=303)


@app.get("/api/users/{user_id}/samples")
async def get_face_sample_summary(user_id: int, request: Request):
    require_admin(request)
    conn = get_db_connection()
    try:
        user = conn.execute("SELECT face_features FROM authorized_users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(404, "Không tìm thấy người dùng")
        templates = read_templates(user["face_features"])
        return {"sample_count": len(templates),
                "unmasked": sum(t.get("mode") == "unmasked" for t in templates),
                "masked": sum(t.get("mode") == "masked" for t in templates),
                "legacy": sum(t.get("mode") == "legacy" for t in templates)}
    finally:
        conn.close()


@app.post("/api/users/{user_id}/samples")
async def add_face_samples(
    user_id: int, request: Request, csrf_token: str = Form(...),
    mode: str = Form(...), photos: list[UploadFile] = File(...),
):
    require_admin(request)
    if not valid_csrf(request, csrf_token):
        raise HTTPException(403, "CSRF token không hợp lệ")
    if mode not in ("masked", "unmasked"):
        raise HTTPException(422, "Chọn loại ảnh có hoặc không khẩu trang")
    if len(photos) > settings()["max_templates_per_user"]:
        raise HTTPException(422, "Mỗi người tối đa 20 ảnh mẫu")
    additions = await enrollment_templates(photos, mode)
    if not additions:
        raise HTTPException(422, "Chọn ít nhất một ảnh")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT face_features FROM authorized_users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(404, "Không tìm thấy người dùng")
        templates = read_templates(user["face_features"]) + additions
        if len(templates) > settings()["max_templates_per_user"]:
            raise HTTPException(422, "Hồ sơ đã đủ 20 ảnh mẫu; cần tạo lại hồ sơ để thay bộ mẫu")
        conn.execute("UPDATE authorized_users SET face_features=? WHERE id=?",
                     (json.dumps({"version": 2, "templates": templates}), user_id))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "user_id": user_id, "mode": mode,
            "sample_count": len(templates), "added": len(additions),
            "unmasked": sum(t.get("mode") == "unmasked" for t in templates),
            "masked": sum(t.get("mode") == "masked" for t in templates),
            "legacy": sum(t.get("mode") == "legacy" for t in templates)}


@app.post("/api/users/delete/{user_id}")

async def delete_user(

    user_id: int,

    request: Request,

    csrf_token: str = Form(...)

):



    require_admin(request)



    if not valid_csrf(request, csrf_token):

        raise HTTPException(

            status_code=403,

            detail="CSRF token không hợp lệ"

        )



    conn = get_db_connection()



    conn.execute(

        "DELETE FROM authorized_users WHERE id = ?",

        (user_id,)

    )



    conn.commit()

    conn.close()



    if "application/json" in request.headers.get("accept", ""):
        return {"success": True, "user_id": user_id}

    return RedirectResponse(

        url="/",

        status_code=303

    )





# ============================================================

# DASHBOARD LOG API

# ============================================================



@app.get("/api/logs")

async def get_logs(request: Request):



    require_admin(request)



    conn = get_db_connection()



    logs = conn.execute(

        "SELECT * FROM access_history "

        "ORDER BY id DESC LIMIT 50"

    ).fetchall()



    total = conn.execute(

        "SELECT COUNT(*) FROM access_history"

    ).fetchone()[0]



    unlocked = conn.execute(

        "SELECT COUNT(*) FROM access_history "

        "WHERE status = 'UNLOCK'"

    ).fetchone()[0]



    denied = conn.execute(

        "SELECT COUNT(*) FROM access_history "

        "WHERE status = 'DENIED'"

    ).fetchone()[0]



    conn.close()



    return {

        "logs": [{key: l[key] for key in l.keys() if key != "quality_report"} for l in logs],

        "total": total,

        "unlocked": unlocked,

        "denied": denied

    }





# ============================================================

# MASTER CAMERA

# ============================================================



# ============================================================

# MASTER CAMERA

# ============================================================





def capture_session(session_id):
    now = time.monotonic()
    for key, value in list(sessions.items()):
        if (now - value["created"] > settings()["session_ttl_seconds"]
                and not any(value["inflight"].values())):
            sessions.pop(key, None)
    if session_id not in sessions:
        if len(sessions) >= 256:
            raise HTTPException(503, "Quá nhiều phiên chụp; thử lại sau")
        slot = {"created": now, "deadline": now + settings().get("capture_timeout_seconds", 40),
                "master": {}, "slave": {}, "inflight": {"master": 0, "slave": 0},
                "camera_locks": {"master": asyncio.Lock(), "slave": asyncio.Lock()},
                "decision_lock": asyncio.Lock()}
        # A retry after a server restart/cache eviction must not create another verdict.
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT quality_report FROM access_history WHERE session_id=? ORDER BY id DESC LIMIT 1",
                               (session_id,)).fetchone()
        finally:
            conn.close()
        if row and row["quality_report"]:
            report = json.loads(row["quality_report"])
            slot["decision"] = report["decision"]
            slot["master"] = {int(k): v for k, v in report.get("master", {}).items()}
            slot["slave"] = {int(k): v for k, v in report.get("slave", {}).items()}
        elif row:
            raise HTTPException(409, "Phiên cũ đã kết thúc; tạo mã phiên mới")
        sessions[session_id] = slot
    return sessions[session_id]


async def read_capture(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > settings()["max_upload_bytes"]:
            raise HTTPException(413, "Ảnh tối đa 4 MB")
    if not data:
        raise HTTPException(400, "No image data")
    return bytes(data)


async def ingest_capture(camera, request, session_id, frame_index, final):
    slot = capture_session(session_id)
    slot["inflight"][camera] += 1
    try:
        image_data = await read_capture(request)
        # Retries from one camera are serialized; the two camera buffers stay separate.
        async with slot["camera_locks"][camera]:
            slot[camera + "_done"] = slot.get(camera + "_done", False) or final
            if frame_index in slot[camera]:
                return slot  # First completed upload owns this index; retries never add votes.
            result = None
            if "decision" not in slot and time.monotonic() < slot["deadline"]:
                # face_ai's thread lock protects the shared YuNet/SFace instances.
                analysis = await asyncio.to_thread(analyze_face, image_data, camera)
                conn = get_db_connection()
                try:
                    users = conn.execute("SELECT id,name,face_features FROM authorized_users").fetchall()
                    result = rank_face(analysis, users)
                finally:
                    conn.close()
                # A slow inference cannot change a verdict or count after the deadline.
                if "decision" not in slot and time.monotonic() < slot["deadline"]:
                    slot[camera][frame_index] = result
                camera_status[camera]["quality"] = result["quality"]
                print(f"[QUALITY {camera} frame={frame_index}] {json.dumps(result, ensure_ascii=False)}")
            sharpness = (result["quality"].get("sharpness", 0) if result and result["quality"]["ok"]
                         else -0.5)
            if sharpness >= slot.get(camera + "_sharpness", -1) and "decision" not in slot:
                filename = f"{camera}_{session_id}.jpg"
                with open(os.path.join(CAPTURES_DIR, filename), "wb") as stream:
                    stream.write(image_data)
                slot[camera + "_img"] = f"/captures/{filename}"
                slot[camera + "_sharpness"] = sharpness
            elif "decision" in slot and camera + "_img" not in slot:
                # Keep a missing late photo, without rerunning recognition or changing votes.
                filename = f"{camera}_{session_id}.jpg"
                with open(os.path.join(CAPTURES_DIR, filename), "wb") as stream:
                    stream.write(image_data)
                slot[camera + "_img"] = f"/captures/{filename}"
            camera_status[camera].update(last_seen=time.time(), last_session=session_id)
            if "decision" in slot:
                conn = get_db_connection()
                try:
                    conn.execute(f"UPDATE access_history SET {camera}_img=? WHERE session_id=?",
                                 (slot.get(camera + "_img"), session_id))
                    conn.commit()
                finally:
                    conn.close()
    finally:
        slot["inflight"][camera] -= 1
    return slot


async def settle_capture(session_id, slot):
    """One atomic, immutable decision; incomplete/absent cameras cannot veto a winner."""
    async with slot["decision_lock"]:
        if "decision" in slot:
            return slot["decision"]
        timed_out = time.monotonic() >= slot["deadline"]
        ready = {camera: timed_out or (slot.get(camera + "_done", False)
                                      and slot["inflight"][camera] == 0)
                 for camera in ("master", "slave")}
        master_primary = slot.get("capture_mode") == "master_primary"
        slave_timed_out = False
        slave_skipped = False
        if master_primary and ready["master"]:
            # Missing Slave is optional. Actual upload activity also proves it
            # joined, even if its READY acknowledgement was lost over ESP-NOW.
            slave_expected = (slot.get("slave_expected", False)
                              or bool(slot["slave"]) or slot["inflight"]["slave"] > 0)
            slave_timed_out = (not ready["slave"] and "slave_deadline" in slot
                               and time.monotonic() >= slot["slave_deadline"])
            slave_skipped = not slave_expected
            if not slave_expected or slave_timed_out:
                ready["slave"] = True
        frames = {camera: list(slot[camera].values()) if ready[camera] else [] for camera in ready}
        conn = get_db_connection()
        try:
            # Recheck earlier votes if an administrator deleted a profile mid-capture.
            valid_ids = {row["id"] for row in conn.execute("SELECT id FROM authorized_users")}
            for camera in frames:
                frames[camera] = [dict(f, accepted=False, reasons=["Hồ sơ đã bị xóa trong lúc xác thực"])
                                  if f["accepted"] and f["user_id"] not in valid_ids else f
                                  for f in frames[camera]]
            decision = decide_session(frames["master"], frames["slave"], prefer_master=master_primary)
            if ((master_primary and not ready["master"])
                    or (decision["status"] != "UNLOCK" and not all(ready.values()))):
                return {"status": "PENDING", "session_id": session_id, "protocol_version": 3,
                        "decision_policy": "either_camera",
                        "capture_mode": slot.get("capture_mode", "dual"),
                        "master_frames": len(slot["master"]), "slave_frames": len(slot["slave"]),
                        "remaining_ms": max(0, int((slot["deadline"] - time.monotonic()) * 1000))}
            decision.update(session_id=session_id, protocol_version=3, timed_out=timed_out,
                        capture_mode=slot.get("capture_mode", "dual"), slave_timed_out=slave_timed_out,
                        master_confidence=max((f["score"] for f in slot["master"].values()), default=0),
                        slave_confidence=max((f["score"] for f in slot["slave"].values()), default=0))
            if slave_skipped:
                if decision["status"] != "UNLOCK":
                    decision["message"] = decision["camera_results"]["master"]["message"]
                decision["camera_results"]["slave"].update(
                    status="SKIPPED", message="Slave không tham gia phiên chụp này")
            if timed_out and decision["status"] != "UNLOCK":
                decision["message"] += "; hết thời gian chờ hai camera"
            elif slave_timed_out and decision["status"] != "UNLOCK":
                decision["message"] += "; hết thời gian nhận ảnh bổ sung từ Slave"
            for camera in ready:
                if not ready[camera]:
                    decision["camera_results"][camera]["status"] = "PENDING"
            conn.execute("""INSERT INTO access_history
                (session_id,user_id,user_name,status,confidence,master_img,slave_img,message,created_at,quality_report)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, decision["user_id"], decision["user_name"], decision["status"],
                 decision["confidence"], slot.get("master_img"), slot.get("slave_img"),
                 decision["message"], time.strftime("%Y-%m-%d %H:%M:%S"),
                  json.dumps({"master": slot["master"], "slave": slot["slave"],
                              "decision": decision}, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        slot["decision"] = decision
        archive_history_if_needed()
        print(f"[ACCESS DECISION] {session_id}: {decision['status']} - {decision['message']}")
        return decision


@app.post("/api/capture/start")
async def start_capture(
    session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    mode: Annotated[str, Query(pattern=r"^(dual|master_primary)$")] = "dual",
):
    slot = capture_session(session_id)
    if "decision" in slot:
        raise HTTPException(409, "Phiên đã kết thúc; tạo mã phiên mới")
    if "capture_mode" in slot and slot["capture_mode"] != mode:
        raise HTTPException(409, "Không đổi chế độ của phiên đang chụp")
    slot["capture_mode"] = mode
    return await settle_capture(session_id, slot)


@app.post("/api/capture/complete")
async def complete_capture(
    session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    camera: str = Query(..., pattern=r"^(master|slave)$"),
    slave_expected: Annotated[Optional[bool], Query()] = None,
):
    slot = capture_session(session_id)
    async with slot["camera_locks"][camera]:
        slot[camera + "_done"] = True
        if camera == "master" and slot.get("capture_mode") == "master_primary":
            # Freeze participation and its deadline on the first completion;
            # an HTTP retry must never extend a stuck Slave's waiting period.
            slot.setdefault("slave_expected", bool(slave_expected))
            slot.setdefault("slave_deadline", min(slot["deadline"], time.monotonic()
                            + settings().get("slave_fallback_timeout_seconds", 10)))
    return await settle_capture(session_id, slot)


@app.get("/api/capture/result")
async def capture_result(session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")):
    return await settle_capture(session_id, capture_session(session_id))


@app.post("/api/upload_master")
async def upload_master(
    request: Request,
    session_id: Optional[str] = Query(None, pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    frame_index: int = Query(0, ge=0, le=2), final: bool = Query(True),
):
    session_id = session_id or "SES_" + secrets.token_hex(12)
    slot = await ingest_capture("master", request, session_id, frame_index, final)
    return await settle_capture(session_id, slot)


@app.post("/api/upload_slave")
async def upload_slave(
    request: Request,
    session_id: Optional[str] = Query(None, pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    frame_index: int = Query(0, ge=0, le=2), final: bool = Query(True),
):
    session_id = session_id or "SES_" + secrets.token_hex(12)
    slot = await ingest_capture("slave", request, session_id, frame_index, final)
    return await settle_capture(session_id, slot)


@app.get("/api/session-quality/{session_id}")
async def get_session_quality(session_id: str, request: Request):
    require_admin(request)
    slot = sessions.get(session_id)
    if slot is None:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT quality_report FROM access_history WHERE session_id=? ORDER BY id DESC LIMIT 1",
                               (session_id,)).fetchone()
        finally:
            conn.close()
        if row and row["quality_report"]:
            return json.loads(row["quality_report"])
        raise HTTPException(404, "Không còn báo cáo chi tiết cho phiên này")
    return {"master": slot["master"], "slave": slot["slave"], "decision": slot.get("decision")}


camera_heartbeat = {

    "master": None,

    "slave": None

}



@app.post("/api/heartbeat/{camera_name}")

async def receive_heartbeat(camera_name: str):

    if camera_name not in camera_heartbeat:

        raise HTTPException(

            status_code=400,

            detail="Camera không hợp lệ"

        )



    camera_heartbeat[camera_name] = time.time()



    return {

        "status": "OK",

        "camera": camera_name

    }

@app.get("/api/connection-logs")
async def get_connection_logs(
    request: Request,
    camera_name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500)
):
    require_admin(request)
    if camera_name not in (None, "master", "slave"):
        raise HTTPException(status_code=400, detail="Camera không hợp lệ")

    with get_db_connection() as conn:
        if camera_name is None:
            rows = conn.execute(
                """SELECT id, camera_name, event_type, detail, created_at
                   FROM camera_connection_log ORDER BY id DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, camera_name, event_type, detail, created_at
                   FROM camera_connection_log WHERE camera_name = ?
                   ORDER BY id DESC LIMIT ?""",
                (camera_name, limit)
            ).fetchall()

    return {"logs": [dict(row) for row in rows]}


@app.get("/api/camera-status")

async def get_camera_status(request: Request):

    require_admin(request)



    now = time.time()

    result = {}



    for camera_name in ("master", "slave"):

        info = camera_status[camera_name]



        last_seen = info["last_seen"]

        last_heartbeat = camera_heartbeat[camera_name]



        # Kiểm tra heartbeat trong 15 giây gần nhất

        if last_heartbeat is None:

            connection_status = "UNKNOWN"

        elif now - last_heartbeat <= HEARTBEAT_OFFLINE_SECONDS:

            connection_status = "ONLINE"

        else:

            connection_status = "OFFLINE"



        if last_seen is None:

            activity_status = "NO_DATA"

            seconds_ago = None

            last_seen_text = None

        else:

            seconds_ago = max(0, int(now - last_seen))

            activity_status = (

                "RECENT" if seconds_ago <= 60 else "INACTIVE"

            )

            last_seen_text = time.strftime(

                "%Y-%m-%d %H:%M:%S",

                time.localtime(last_seen)

            )



        result[camera_name] = {

            "status": activity_status,

            "connection_status": connection_status,

            "last_seen": last_seen_text,

            "seconds_ago": seconds_ago,

            "last_session": info["last_session"],

            "last_heartbeat": last_heartbeat,
            "quality": info.get("quality")

        }



    return result

@app.get("/health")

async def health():

    return {

        "status": "HEALTHY",

        "database": "CONNECTED",
        "backend_version": "master-primary-multiview-v1",
        "protocol_version": 3,
        "capture_modes": ["dual", "master_primary"]

    }






# ============================================================

# RUN SERVER

# ============================================================



if __name__ == "__main__":



    import uvicorn



    uvicorn.run(

        "app:app",

        host="0.0.0.0",

        port=8000,

        reload=False

    )

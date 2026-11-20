import aiosqlite
from datetime import datetime


class Database:
    def __init__(self, db_path: str = "mosy_tasks.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    mfy TEXT DEFAULT 'Umumiy',
                    role TEXT DEFAULT 'worker',
                    registered_at TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    instruction TEXT DEFAULT '',
                    deadline TEXT NOT NULL,
                    answer_type TEXT NOT NULL, -- 'photo', 'file', 'both', 'text'
                    call_message TEXT DEFAULT '',
                    template_file_id TEXT DEFAULT '',
                    template_file_name TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT DEFAULT 'sent', -- 'sent', 'read', 'submitted', 'rejected', 'approved'
                    read_at TEXT,
                    submitted_at TEXT,
                    answer_text TEXT DEFAULT '',
                    file_id TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    file_kind TEXT DEFAULT '',
                    last_reminded_at TEXT,
                    call_triggered INTEGER DEFAULT 0,
                    FOREIGN KEY (task_id) REFERENCES tasks(id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    rejected_date TEXT NOT NULL, -- YYYY-MM-DD
                    rejected_at TEXT NOT NULL,   -- YYYY-MM-DD HH:MM:SS
                    FOREIGN KEY (assignment_id) REFERENCES assignments(id)
                )
            """)
            await db.commit()

    async def upsert_user(self, user_id: int, full_name: str, mfy: str = "Umumiy", role: str = "worker"):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, full_name, mfy, role, registered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    mfy = excluded.mfy,
                    role = excluded.role
            """, (user_id, full_name, mfy, role, now))
            await db.commit()

    async def get_user(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_workers(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE role = 'worker' ORDER BY mfy, full_name")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def create_task_with_assignments(
        self,
        title: str,
        description: str,
        instruction: str,
        deadline: str,
        answer_type: str,
        call_message: str,
        user_ids: list[int],
        template_file_id: str = "",
        template_file_name: str = ""
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                INSERT INTO tasks (
                    title, description, instruction, deadline, answer_type,
                    call_message, template_file_id, template_file_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                title, description, instruction, deadline, answer_type,
                call_message, template_file_id, template_file_name, now
            ))
            task_id = cur.lastrowid
            for uid in user_ids:
                await db.execute("""
                    INSERT INTO assignments (task_id, user_id, status, last_reminded_at)
                    VALUES (?, ?, 'sent', ?)
                """, (task_id, uid, now))
            await db.commit()
            return task_id

    async def get_task(self, task_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_assignment(self, assignment_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT a.*, t.title, t.description, t.instruction, t.deadline,
                       t.answer_type, t.template_file_id, t.template_file_name,
                       u.full_name, u.mfy
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                JOIN users u ON a.user_id = u.user_id
                WHERE a.id = ?
            """, (assignment_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_read(self, assignment_id: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE assignments
                SET status = CASE WHEN status = 'sent' THEN 'read' ELSE status END,
                    read_at = COALESCE(read_at, ?)
                WHERE id = ?
            """, (now, assignment_id))
            await db.commit()

    async def submit_answer(
        self,
        assignment_id: int,
        answer_text: str = "",
        file_id: str = "",
        file_name: str = "",
        file_kind: str = ""
    ):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE assignments
                SET status = 'submitted',
                    submitted_at = ?,
                    answer_text = COALESCE(NULLIF(?, ''), answer_text),
                    file_id = COALESCE(NULLIF(?, ''), file_id),
                    file_name = COALESCE(NULLIF(?, ''), file_name),
                    file_kind = COALESCE(NULLIF(?, ''), file_kind)
                WHERE id = ?
            """, (now, answer_text, file_id, file_name, file_kind, assignment_id))
            await db.commit()

    async def approve_assignment(self, assignment_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE assignments SET status = 'approved' WHERE id = ?
            """, (assignment_id,))
            await db.commit()

    async def reject_assignment(self, assignment_id: int, reason: str):
        now_dt = datetime.now()
        rejected_date = now_dt.strftime("%Y-%m-%d")
        rejected_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT task_id, user_id FROM assignments WHERE id = ?", (assignment_id,))
            row = await cur.fetchone()
            if not row:
                return
            await db.execute("""
                UPDATE assignments SET status = 'rejected' WHERE id = ?
            """, (assignment_id,))
            await db.execute("""
                INSERT INTO rejections (assignment_id, task_id, user_id, reason, rejected_date, rejected_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (assignment_id, row["task_id"], row["user_id"], reason, rejected_date, rejected_at))
            await db.commit()

    async def get_user_assignments(self, user_id: int, only_rejected: bool = False):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT a.*, t.title, t.description, t.instruction, t.deadline,
                       t.answer_type, t.template_file_id, t.template_file_name,
                       (SELECT r.reason FROM rejections r WHERE r.assignment_id = a.id ORDER BY r.id DESC LIMIT 1) AS last_reason,
                       (SELECT r.rejected_at FROM rejections r WHERE r.assignment_id = a.id ORDER BY r.id DESC LIMIT 1) AS last_rejected_at
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                WHERE a.user_id = ?
            """
            params = [user_id]
            if only_rejected:
                query += " AND a.status = 'rejected'"
            query += " ORDER BY a.id DESC"
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_rejections_history(self, user_id: int | None = None):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT r.*, t.title AS task_title, u.full_name, u.mfy
                FROM rejections r
                JOIN tasks t ON r.task_id = t.id
                JOIN users u ON r.user_id = u.user_id
            """
            params = []
            if user_id is not None:
                query += " WHERE r.user_id = ?"
                params.append(user_id)
            query += " ORDER BY r.rejected_date DESC, r.id DESC"
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_monitoring_rows(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT a.id AS assignment_id, a.status, a.read_at, a.submitted_at,
                       a.file_name, a.file_kind, a.answer_text,
                       t.id AS task_id, t.title, t.deadline, t.answer_type,
                       u.user_id, u.full_name, u.mfy
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                JOIN users u ON a.user_id = u.user_id
                ORDER BY t.id DESC, u.mfy ASC, u.full_name ASC
            """)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_pending_hourly_reminders(self):
        # Bajarilmagan (approved yoki submitted bo'lmagan) topshiriqlar
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT a.id AS assignment_id, a.user_id, a.status, a.last_reminded_at,
                       t.title, t.deadline, t.created_at
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                WHERE a.status IN ('sent', 'read', 'rejected')
            """)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def mark_reminded(self, assignment_id: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE assignments SET last_reminded_at = ? WHERE id = ?
            """, (now, assignment_id))
            await db.commit()

    async def get_unread_over_10_min(self):
        # 10 daqiqadan oshib ham o'qilmagan ('sent') topshiriqlar
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT a.id AS assignment_id, a.user_id, a.call_triggered,
                       t.title, t.call_message, t.created_at, u.full_name, u.mfy
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                JOIN users u ON a.user_id = u.user_id
                WHERE a.status = 'sent' AND a.call_triggered = 0
            """)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def mark_call_triggered(self, assignment_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE assignments SET call_triggered = 1 WHERE id = ?
            """, (assignment_id,))
            await db.commit()
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

class Database:
    def __init__(self, db_path: str = None):
        self.app_dir = Path(__file__).parent.parent
        self.db_path = db_path or str(self.app_dir / "data" / "pdfbro.db")
        self.backup_dir = self.app_dir / "data" / "backups"
        
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        self._init_db()
    
    # init db
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_size INTEGER,
                    output_path TEXT NOT NULL,
                    output_name TEXT NOT NULL,
                    output_size INTEGER,
                    status TEXT DEFAULT 'success',
                    error_msg TEXT,
                    backup_path TEXT,
                    converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_converted_at ON history(converted_at)")
            self._ensure_history_sync_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL UNIQUE,
                    device_name TEXT NOT NULL,
                    address TEXT,
                    token_hash TEXT,
                    paired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'paired'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pairing_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    pairing_code TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    server_url TEXT,
                    state TEXT DEFAULT 'waiting',
                    device_id TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mobile_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_job_id TEXT,
                    device_id TEXT,
                    source_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    output_path TEXT,
                    status TEXT DEFAULT 'pending',
                    sync_status TEXT DEFAULT 'pending',
                    file_size INTEGER DEFAULT 0,
                    checksum TEXT,
                    error_msg TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mobile_jobs_status ON mobile_jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mobile_jobs_source ON mobile_jobs(source_path)")
            conn.commit()

    def _ensure_history_sync_columns(self, conn):
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(history)").fetchall()
        }
        columns = {
            "remote_job_id": "TEXT",
            "device_id": "TEXT",
            "pairing_session_id": "TEXT",
            "sync_status": "TEXT DEFAULT 'local'",
            "last_synced_at": "TIMESTAMP",
            "transfer_source": "TEXT DEFAULT 'desktop'",
        }
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE history ADD COLUMN {column} {definition}")
    
    def add_record(self, source_path: str, output_path: str, 
                   status: str = "success", error_msg: str = None,
                   backup_path: str = None, remote_job_id: str = None,
                   device_id: str = None, pairing_session_id: str = None,
                   sync_status: str = "local", transfer_source: str = "desktop") -> int:
        # conversion history
        source = Path(source_path)
        output = Path(output_path)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO history 
                (source_path, source_name, source_size, output_path, output_name, 
                 output_size, status, error_msg, backup_path, remote_job_id,
                 device_id, pairing_session_id, sync_status, transfer_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(source),
                source.name,
                source.stat().st_size if source.exists() else 0,
                str(output),
                output.name,
                output.stat().st_size if output.exists() else 0,
                status,
                error_msg,
                backup_path,
                remote_job_id,
                device_id,
                pairing_session_id,
                sync_status,
                transfer_source
            ))
            conn.commit()
            return cursor.lastrowid
    
    # get conversion history
    def get_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM history 
                ORDER BY converted_at DESC 
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    # ambil dengan ID 
    def get_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM history WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def clear_history(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM history")
            conn.commit()

    def create_pairing_session(self, session_id: str, pairing_code: str,
                               token_hash: str, server_url: str,
                               expires_at: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT OR REPLACE INTO pairing_sessions
                (session_id, pairing_code, token_hash, server_url, state,
                 expires_at, updated_at)
                VALUES (?, ?, ?, ?, 'waiting', ?, CURRENT_TIMESTAMP)
            """, (session_id, pairing_code, token_hash, server_url, expires_at))
            conn.commit()
            return cursor.lastrowid

    def update_pairing_session_state(self, session_id: str, state: str,
                                     device_id: str = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE pairing_sessions
                SET state = ?, device_id = COALESCE(?, device_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
            """, (state, device_id, session_id))
            conn.commit()

    def get_pairing_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM pairing_sessions
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def upsert_device(self, device_id: str, device_name: str, address: str = None,
                      token_hash: str = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO devices
                (device_id, device_name, address, token_hash, status)
                VALUES (?, ?, ?, ?, 'paired')
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name = excluded.device_name,
                    address = excluded.address,
                    token_hash = excluded.token_hash,
                    status = 'paired',
                    last_seen_at = CURRENT_TIMESTAMP
            """, (device_id, device_name, address, token_hash))
            conn.commit()

    def mark_device_seen(self, device_id: str):
        if not device_id:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE devices
                SET last_seen_at = CURRENT_TIMESTAMP
                WHERE device_id = ?
            """, (device_id,))
            conn.commit()

    def get_devices(self, limit: int = 20) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM devices
                ORDER BY last_seen_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def add_mobile_job(self, device_id: str, source_name: str, source_path: str,
                       status: str = "pending", file_size: int = 0,
                       checksum: str = None, remote_job_id: str = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO mobile_jobs
                (remote_job_id, device_id, source_name, source_path, status,
                 sync_status, file_size, checksum)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                remote_job_id,
                device_id,
                source_name,
                source_path,
                status,
                file_size,
                checksum,
            ))
            conn.commit()
            return cursor.lastrowid

    def get_mobile_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM mobile_jobs
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_mobile_job_by_source(self, source_path: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM mobile_jobs
                WHERE source_path = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (source_path,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_mobile_job_status(self, job_id: int, status: str,
                                 output_path: str = None, error_msg: str = None,
                                 sync_status: str = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE mobile_jobs
                SET status = ?,
                    output_path = COALESCE(?, output_path),
                    error_msg = ?,
                    sync_status = COALESCE(?, sync_status),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, output_path, error_msg, sync_status, job_id))
            conn.commit()
    
    def create_backup(self, pdf_path: str, max_size_mb: int = 100) -> Optional[str]:
        source = Path(pdf_path)
        if not source.exists():
            return None
        
        # filter file yang dibackup
        if source.suffix.lower() != '.pdf':
            return None
        
        # skip jika file terlalu besar
        if source.stat().st_size > max_size_mb * 1024 * 1024:
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{source.stem}_{timestamp}.pdf"
        backup_path = self.backup_dir / backup_name
        
        # cek apakah backup berhasil
        try:
            shutil.copy2(source, backup_path)
            if backup_path.exists() and backup_path.stat().st_size == source.stat().st_size:
                return str(backup_path)
            return None
        except Exception:
            return None
    
    # restore backup
    def restore_backup(self, record_id: int, dest_path: str = None) -> bool:
        record = self.get_record(record_id)
        if not record or not record.get('backup_path'):
            return False
        
        backup = Path(record['backup_path'])
        if not backup.exists():
            return False
        
        dest = Path(dest_path) if dest_path else Path(record['source_path'])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, dest)
        return True

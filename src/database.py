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
            conn.commit()
    
    def add_record(self, source_path: str, output_path: str, 
                   status: str = "success", error_msg: str = None,
                   backup_path: str = None) -> int:
        # conversion history
        source = Path(source_path)
        output = Path(output_path)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO history 
                (source_path, source_name, source_size, output_path, output_name, 
                 output_size, status, error_msg, backup_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(source),
                source.name,
                source.stat().st_size if source.exists() else 0,
                str(output),
                output.name,
                output.stat().st_size if output.exists() else 0,
                status,
                error_msg,
                backup_path
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

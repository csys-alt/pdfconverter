import html
import os
import platform
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QMutex, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from src.converter import ConversionResult, PDFConverter
from src.database import Database
from src.pairing import PairingService
from src.sync import SupabaseSyncService


class ConvertWorker(QThread):
    """Background worker for batch file conversion."""

    progress = Signal(int, int, str, bool, str)
    finished = Signal(list)

    def __init__(self, converter: PDFConverter, files: list, output_dir: str, db: Database):
        super().__init__()
        self.converter = converter
        self.files = files
        self.output_dir = output_dir
        self.db = db
        self._results = []
        self._results_lock = QMutex()

    def cancel(self):
        self.converter.cancel_all()

    def _on_progress(self, completed: int, total: int, filename: str, result: ConversionResult):
        mobile_job = self.db.get_mobile_job_by_source(result.source_path)
        backup_path = None
        if result.success and result.output_path:
            backup_path = self.db.create_backup(result.output_path)

        transfer_source = "mobile" if mobile_job else "desktop"
        remote_job_id = None
        device_id = None
        sync_status = "local"

        if mobile_job:
            remote_job_id = mobile_job.get("remote_job_id") or str(mobile_job["id"])
            device_id = mobile_job.get("device_id")
            sync_status = "pending"
            self.db.update_mobile_job_status(
                job_id=mobile_job["id"],
                status="completed" if result.success else "failed",
                output_path=result.output_path if result.success else None,
                error_msg=result.error if not result.success else None,
                sync_status="pending",
            )

        self.db.add_record(
            source_path=result.source_path,
            output_path=result.output_path if result.success else "",
            status="success" if result.success else "failed",
            error_msg=result.error if not result.success else None,
            backup_path=backup_path,
            remote_job_id=remote_job_id,
            device_id=device_id,
            sync_status=sync_status,
            transfer_source=transfer_source,
        )

        self._results_lock.lock()
        self._results.append((result.source_path, result))
        self._results_lock.unlock()

        self.progress.emit(completed, total, filename, result.success, result.error)

    def run(self):
        self._results = []

        for file_path in self.files:
            mobile_job = self.db.get_mobile_job_by_source(file_path)
            if mobile_job:
                self.db.update_mobile_job_status(mobile_job["id"], "converting")

        self.converter.convert_batch(
            files=self.files,
            output_dir=self.output_dir,
            progress_callback=self._on_progress,
            silent=True,
        )

        self._results_lock.lock()
        results = list(self._results)
        self._results_lock.unlock()

        self.finished.emit(results)


class DropListWidget(QListWidget):
    """List widget with drag and drop support."""

    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).is_file():
                files.append(path)
        if files:
            self.files_dropped.emit(files)
            event.acceptProposedAction()


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.converter = PDFConverter()
        self.sync_service = SupabaseSyncService()
        self.pairing = PairingService(
            db=self.db,
            converter=self.converter,
            supported_formats=self.converter.SUPPORTED_FORMATS,
        )
        self.selected_files = []
        self.output_dir = None
        self.current_session = None
        self._last_uploads_seen = 0

        self._setup_ui()
        self._start_pairing_session()
        self._load_history()
        self._load_mobile_jobs()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_pairing_state)
        self.refresh_timer.start(1000)

    def _get_icon_path(self) -> Path:
        if getattr(sys, "frozen", False):
            base = Path(sys._MEIPASS)
        else:
            base = Path(__file__).parent.parent.parent
        return base / "assets" / "icon.ico"

    def _setup_ui(self):
        self.setWindowTitle("PDFConverter Desktop")
        self.setMinimumSize(1080, 720)

        icon_path = self._get_icon_path()
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._apply_styles()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.pairing_tab = QWidget()
        self.tabs.addTab(self.pairing_tab, "Pairing")
        self._setup_pairing_tab(self.pairing_tab)

        self.convert_tab = QWidget()
        self.tabs.addTab(self.convert_tab, "Convert")
        self._setup_convert_tab(self.convert_tab)

        self.inbox_tab = QWidget()
        self.tabs.addTab(self.inbox_tab, "Mobile Inbox")
        self._setup_inbox_tab(self.inbox_tab)

        self.history_tab = QWidget()
        self.tabs.addTab(self.history_tab, "History")
        self._setup_history_tab(self.history_tab)

        self.statusBar().showMessage("Ready")

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #f5f7fa;
                color: #18212f;
                font-size: 13px;
            }
            QFrame#Header {
                background: #ffffff;
                border: 1px solid #d9e2ec;
                border-radius: 8px;
            }
            QLabel#Title {
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#Subtitle {
                color: #5b677a;
            }
            QLabel#Badge {
                padding: 5px 10px;
                border-radius: 6px;
                font-weight: 600;
            }
            QGroupBox {
                background: #ffffff;
                border: 1px solid #d9e2ec;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #c9d4e2;
                border-radius: 6px;
                padding: 8px 12px;
            }
            QPushButton:hover {
                background: #eef4fb;
            }
            QPushButton:disabled {
                color: #9aa6b5;
                background: #edf1f5;
            }
            QPushButton#PrimaryButton {
                background: #0f6bff;
                color: white;
                border-color: #0f6bff;
                font-weight: 700;
            }
            QPushButton#DangerButton {
                background: #b42318;
                color: white;
                border-color: #b42318;
                font-weight: 700;
            }
            QLineEdit, QTextEdit, QListWidget, QTableWidget {
                background: #ffffff;
                border: 1px solid #cfd8e3;
                border-radius: 6px;
                padding: 6px;
            }
            QProgressBar {
                border: 1px solid #cfd8e3;
                border-radius: 6px;
                text-align: center;
                height: 18px;
                background: #ffffff;
            }
            QProgressBar::chunk {
                background: #16a34a;
                border-radius: 5px;
            }
        """)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Header")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)

        title_area = QVBoxLayout()
        title = QLabel("PDFConverter Desktop")
        title.setObjectName("Title")
        subtitle = QLabel("Pair mobile devices over Wi-Fi and convert with the local LibreOffice engine.")
        subtitle.setObjectName("Subtitle")
        title_area.addWidget(title)
        title_area.addWidget(subtitle)
        layout.addLayout(title_area, 1)

        self.engine_badge = QLabel()
        self.engine_badge.setObjectName("Badge")
        self.pairing_badge = QLabel()
        self.pairing_badge.setObjectName("Badge")
        self.sync_badge = QLabel()
        self.sync_badge.setObjectName("Badge")

        layout.addWidget(self.engine_badge)
        layout.addWidget(self.pairing_badge)
        layout.addWidget(self.sync_badge)

        self._set_badge(
            self.engine_badge,
            f"Engine: {self.converter.get_engine_name()}",
            "ok" if self.converter.is_available() else "error",
        )
        self._set_badge(self.pairing_badge, "Pairing: starting", "warn")
        self._set_badge(
            self.sync_badge,
            self.sync_service.status_label(),
            "ok" if self.sync_service.can_sync() else "neutral",
        )

        return frame

    def _setup_pairing_tab(self, tab: QWidget):
        layout = QHBoxLayout(tab)
        layout.setSpacing(12)

        pairing_group = QGroupBox("Mobile Pairing")
        pairing_layout = QVBoxLayout(pairing_group)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(300, 300)
        self.qr_label.setStyleSheet("""
            QLabel {
                background: #ffffff;
                border: 1px solid #cfd8e3;
                border-radius: 8px;
            }
        """)
        pairing_layout.addWidget(self.qr_label, alignment=Qt.AlignmentFlag.AlignCenter)

        code_caption = QLabel("Manual code")
        code_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.manual_code_label = QLabel("------")
        self.manual_code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.manual_code_label.setStyleSheet("font-size: 30px; font-weight: 800; letter-spacing: 0;")
        pairing_layout.addWidget(code_caption)
        pairing_layout.addWidget(self.manual_code_label)

        self.pair_url_field = QLineEdit()
        self.pair_url_field.setReadOnly(True)
        pairing_layout.addWidget(self.pair_url_field)

        button_row = QHBoxLayout()
        self.btn_start_pairing = QPushButton("Start Pairing")
        self.btn_start_pairing.setObjectName("PrimaryButton")
        self.btn_start_pairing.clicked.connect(self._start_pairing_session)
        button_row.addWidget(self.btn_start_pairing)

        self.btn_regenerate_pairing = QPushButton("Regenerate")
        self.btn_regenerate_pairing.clicked.connect(self._start_pairing_session)
        button_row.addWidget(self.btn_regenerate_pairing)

        self.btn_stop_pairing = QPushButton("Stop")
        self.btn_stop_pairing.clicked.connect(self._stop_pairing)
        button_row.addWidget(self.btn_stop_pairing)
        pairing_layout.addLayout(button_row)

        layout.addWidget(pairing_group, 0)

        status_group = QGroupBox("Connection")
        status_layout = QVBoxLayout(status_group)

        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(18)
        status_grid.setVerticalSpacing(10)
        self.server_value = QLabel("-")
        self.state_value = QLabel("-")
        self.device_value = QLabel("-")
        self.uploads_value = QLabel("0")
        self.expires_value = QLabel("-")
        self.sync_value = QLabel(self.sync_service.status_label())

        self._add_status_row(status_grid, 0, "Server", self.server_value)
        self._add_status_row(status_grid, 1, "State", self.state_value)
        self._add_status_row(status_grid, 2, "Device", self.device_value)
        self._add_status_row(status_grid, 3, "Uploads", self.uploads_value)
        self._add_status_row(status_grid, 4, "Expires", self.expires_value)
        self._add_status_row(status_grid, 5, "Supabase", self.sync_value)
        status_layout.addLayout(status_grid)

        endpoint_group = QGroupBox("Mobile Endpoints")
        endpoint_layout = QGridLayout(endpoint_group)
        self.endpoint_pair = QLabel("-")
        self.endpoint_upload = QLabel("-")
        self.endpoint_pair.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.endpoint_upload.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._add_status_row(endpoint_layout, 0, "Pair", self.endpoint_pair)
        self._add_status_row(endpoint_layout, 1, "Upload", self.endpoint_upload)
        status_layout.addWidget(endpoint_group)

        self.pairing_log = QTextEdit()
        self.pairing_log.setReadOnly(True)
        self.pairing_log.setMinimumHeight(160)
        status_layout.addWidget(self.pairing_log, 1)

        layout.addWidget(status_group, 1)

    def _setup_convert_tab(self, tab: QWidget):
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        file_group = QGroupBox("Conversion Queue")
        file_layout = QVBoxLayout(file_group)

        self.file_list = DropListWidget()
        self.file_list.files_dropped.connect(self._add_files)
        file_layout.addWidget(self.file_list, 1)

        btn_layout = QHBoxLayout()
        self.btn_browse = QPushButton("Browse Files")
        self.btn_browse.clicked.connect(self._browse_files)
        btn_layout.addWidget(self.btn_browse)

        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        btn_layout.addWidget(self.btn_remove)

        self.btn_clear = QPushButton("Clear Queue")
        self.btn_clear.clicked.connect(self._clear_files)
        btn_layout.addWidget(self.btn_clear)
        btn_layout.addStretch()
        file_layout.addLayout(btn_layout)
        layout.addWidget(file_group, 1)

        output_group = QGroupBox("Output")
        output_layout = QHBoxLayout(output_group)
        self.chk_same_dir = QCheckBox("Same as source")
        self.chk_same_dir.setChecked(True)
        self.chk_same_dir.stateChanged.connect(self._toggle_output_dir)
        output_layout.addWidget(self.chk_same_dir)

        self.txt_output = QLineEdit()
        self.txt_output.setPlaceholderText("Output directory")
        self.txt_output.setEnabled(False)
        output_layout.addWidget(self.txt_output, 1)

        self.btn_output = QPushButton("Select")
        self.btn_output.setEnabled(False)
        self.btn_output.clicked.connect(self._browse_output)
        output_layout.addWidget(self.btn_output)
        layout.addWidget(output_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_progress = QLabel("")
        layout.addWidget(self.lbl_progress)

        convert_btn_layout = QHBoxLayout()
        self.btn_convert = QPushButton("Convert to PDF")
        self.btn_convert.setObjectName("PrimaryButton")
        self.btn_convert.setMinimumHeight(42)
        self.btn_convert.clicked.connect(self._start_conversion)
        convert_btn_layout.addWidget(self.btn_convert)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("DangerButton")
        self.btn_cancel.setMinimumHeight(42)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        convert_btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(convert_btn_layout)

    def _setup_inbox_tab(self, tab: QWidget):
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        inbox_group = QGroupBox("Mobile Inbox")
        inbox_layout = QVBoxLayout(inbox_group)

        self.mobile_jobs_table = QTableWidget(0, 6)
        self.mobile_jobs_table.setHorizontalHeaderLabels(
            ["File", "Status", "Device", "Size", "Received", "Sync"]
        )
        self.mobile_jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mobile_jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.mobile_jobs_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        inbox_layout.addWidget(self.mobile_jobs_table, 1)

        inbox_btns = QHBoxLayout()
        self.btn_refresh_inbox = QPushButton("Refresh")
        self.btn_refresh_inbox.clicked.connect(self._load_mobile_jobs)
        inbox_btns.addWidget(self.btn_refresh_inbox)

        self.btn_add_inbox = QPushButton("Add to Queue")
        self.btn_add_inbox.clicked.connect(self._add_selected_mobile_to_queue)
        inbox_btns.addWidget(self.btn_add_inbox)

        self.btn_convert_inbox = QPushButton("Convert Selected")
        self.btn_convert_inbox.setObjectName("PrimaryButton")
        self.btn_convert_inbox.clicked.connect(self._convert_selected_mobile_jobs)
        inbox_btns.addWidget(self.btn_convert_inbox)

        self.btn_open_inbox = QPushButton("Open Inbox")
        self.btn_open_inbox.clicked.connect(self._open_inbox_folder)
        inbox_btns.addWidget(self.btn_open_inbox)
        inbox_btns.addStretch()
        inbox_layout.addLayout(inbox_btns)

        layout.addWidget(inbox_group, 1)

    def _setup_history_tab(self, tab: QWidget):
        layout = QVBoxLayout(tab)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self.history_list = QListWidget()
        self.history_list.setAlternatingRowColors(True)
        self.history_list.currentRowChanged.connect(self._show_history_details)
        list_layout.addWidget(self.history_list, 1)

        hist_btn_layout = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._load_history)
        hist_btn_layout.addWidget(self.btn_refresh)

        self.btn_restore = QPushButton("Restore Backup")
        self.btn_restore.clicked.connect(self._restore_backup)
        hist_btn_layout.addWidget(self.btn_restore)

        self.btn_open_folder = QPushButton("Open Folder")
        self.btn_open_folder.clicked.connect(self._open_output_folder)
        hist_btn_layout.addWidget(self.btn_open_folder)

        self.btn_clear_hist = QPushButton("Clear History")
        self.btn_clear_hist.clicked.connect(self._clear_history)
        hist_btn_layout.addWidget(self.btn_clear_hist)
        list_layout.addLayout(hist_btn_layout)
        splitter.addWidget(list_widget)

        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        details_layout.addWidget(self.details_text)
        splitter.addWidget(details_widget)
        splitter.setSizes([360, 640])

        layout.addWidget(splitter)

    def _add_status_row(self, layout: QGridLayout, row: int, label: str, value: QLabel):
        name = QLabel(label)
        name.setStyleSheet("color: #5b677a; font-weight: 600;")
        value.setWordWrap(True)
        layout.addWidget(name, row, 0)
        layout.addWidget(value, row, 1)

    def _set_badge(self, label: QLabel, text: str, kind: str):
        colors = {
            "ok": ("#dcfce7", "#166534", "#86efac"),
            "warn": ("#fef3c7", "#92400e", "#fcd34d"),
            "error": ("#fee2e2", "#991b1b", "#fecaca"),
            "neutral": ("#e5e7eb", "#374151", "#d1d5db"),
        }
        bg, fg, border = colors.get(kind, colors["neutral"])
        label.setText(text)
        label.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {border};"
            "border-radius: 6px; padding: 5px 10px; font-weight: 600;"
        )

    def _start_pairing_session(self):
        try:
            self.current_session = self.pairing.create_session()
            self._render_qr(self.current_session.qr_payload())
            self._append_pairing_log("Pairing session started.")
            self._refresh_pairing_state()
        except Exception as exc:
            self._append_pairing_log(f"Pairing failed: {exc}")
            QMessageBox.critical(self, "Pairing Error", str(exc))

    def _stop_pairing(self):
        self.pairing.stop()
        self.current_session = None
        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("Pairing stopped")
        self.manual_code_label.setText("------")
        self.pair_url_field.clear()
        self._append_pairing_log("Pairing server stopped.")
        self._refresh_pairing_state()

    def _refresh_pairing_state(self):
        snapshot = self.pairing.snapshot()
        state = snapshot.state.title()
        self.server_value.setText(snapshot.server_url or "-")
        self.state_value.setText(state)
        self.device_value.setText(snapshot.device_name or "-")
        self.uploads_value.setText(str(snapshot.uploads_received))
        self.expires_value.setText(snapshot.expires_at or "-")
        self.sync_value.setText(self.sync_service.status_label())
        self.endpoint_pair.setText(snapshot.pair_url or "-")
        upload_url = f"{snapshot.server_url}/upload?filename=<name>" if snapshot.server_url else "-"
        self.endpoint_upload.setText(upload_url)
        self.manual_code_label.setText(snapshot.manual_code or "------")
        self.pair_url_field.setText(snapshot.pair_url or "")

        if snapshot.state == "connected":
            self._set_badge(self.pairing_badge, "Pairing: connected", "ok")
        elif snapshot.state in {"waiting", "scanned"}:
            self._set_badge(self.pairing_badge, "Pairing: waiting", "warn")
        elif snapshot.state == "expired":
            self._set_badge(self.pairing_badge, "Pairing: expired", "error")
        elif snapshot.running:
            self._set_badge(self.pairing_badge, f"Pairing: {snapshot.state}", "neutral")
        else:
            self._set_badge(self.pairing_badge, "Pairing: stopped", "neutral")

        if snapshot.last_error:
            self.statusBar().showMessage(snapshot.last_error)

        if snapshot.uploads_received != self._last_uploads_seen:
            self._last_uploads_seen = snapshot.uploads_received
            self._load_mobile_jobs()
            self.tabs.setCurrentWidget(self.inbox_tab)

    def _render_qr(self, payload: str):
        try:
            import qrcode

            qr = qrcode.QRCode(version=None, border=2)
            qr.add_data(payload)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            module_count = len(matrix)
            box_size = max(1, 280 // module_count)
            image_size = module_count * box_size
            qimage = QImage(image_size, image_size, QImage.Format.Format_RGB32)
            qimage.fill(QColor("white"))

            painter = QPainter(qimage)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("black"))
            for row_index, row in enumerate(matrix):
                for col_index, enabled in enumerate(row):
                    if enabled:
                        painter.drawRect(
                            col_index * box_size,
                            row_index * box_size,
                            box_size,
                            box_size,
                        )
            painter.end()

            pixmap = QPixmap.fromImage(qimage).scaled(
                280,
                280,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.qr_label.setText("")
            self.qr_label.setPixmap(pixmap)
        except Exception:
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText("QR package missing\nUse manual code or URL")

    def _append_pairing_log(self, text: str):
        if hasattr(self, "pairing_log"):
            self.pairing_log.append(text)

    def _add_files(self, files: list):
        for file_path in files:
            if file_path in self.selected_files:
                continue
            if self.converter.is_supported(file_path):
                self.selected_files.append(file_path)
                item = QListWidgetItem(Path(file_path).name)
                item.setToolTip(file_path)
                self.file_list.addItem(item)
            else:
                self.statusBar().showMessage(f"Unsupported: {Path(file_path).name}")
        self._update_file_count()

    def _browse_files(self):
        formats = " ".join(f"*{ext}" for ext in self.converter.SUPPORTED_FORMATS)
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Files",
            "",
            f"Supported Documents ({formats});;All Files (*.*)",
        )
        if files:
            self._add_files(files)

    def _clear_files(self):
        self.file_list.clear()
        self.selected_files.clear()
        self._update_file_count()

    def _remove_selected(self):
        for item in self.file_list.selectedItems():
            idx = self.file_list.row(item)
            self.file_list.takeItem(idx)
            if idx < len(self.selected_files):
                self.selected_files.pop(idx)
        self._update_file_count()

    def _toggle_output_dir(self, state=None):
        enabled = not self.chk_same_dir.isChecked()
        self.txt_output.setEnabled(enabled)
        self.btn_output.setEnabled(enabled)

    def _browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_path:
            self.txt_output.setText(dir_path)
            self.output_dir = dir_path

    def _update_file_count(self):
        count = len(self.selected_files)
        self.statusBar().showMessage(f"{count} file(s) queued")

    def _start_conversion(self):
        if not self.selected_files:
            QMessageBox.warning(self, "No Files", "Please add files to convert.")
            return

        if not self.converter.is_available():
            QMessageBox.critical(
                self,
                "No Engine",
                "LibreOffice not found. Please install LibreOffice.",
            )
            return

        output_dir = None
        if not self.chk_same_dir.isChecked():
            output_dir = self.txt_output.text().strip()
            if not output_dir:
                QMessageBox.warning(self, "No Output", "Please select an output directory.")
                return

        self.btn_convert.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.selected_files))

        self.worker = ConvertWorker(
            self.converter,
            self.selected_files.copy(),
            output_dir,
            self.db,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, current: int, total: int, filename: str, success: bool, error: str):
        self.progress_bar.setValue(current)
        status = "OK" if success else "FAIL"
        self.lbl_progress.setText(f"{status} {filename} ({current}/{total})")
        if error:
            self.statusBar().showMessage(error)

    def _cancel_conversion(self):
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_progress.setText("Cancelling...")

    def _on_finished(self, results: list):
        self.btn_convert.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setVisible(False)

        success = sum(1 for _, result in results if result.success)
        failed = len(results) - success
        self.lbl_progress.setText(f"Done. {success} succeeded, {failed} failed")
        self.statusBar().showMessage("Conversion complete")

        self._clear_files()
        self._load_history()
        self._load_mobile_jobs()

        if failed > 0:
            errors = "\n".join(
                f"- {Path(path).name}: {result.error}"
                for path, result in results
                if not result.success
            )
            QMessageBox.warning(
                self,
                "Conversion Complete",
                f"Converted {success} files.\n{failed} failed:\n\n{errors}",
            )
        else:
            QMessageBox.information(
                self,
                "Success",
                f"Successfully converted {success} file(s) to PDF.",
            )

    def _load_mobile_jobs(self):
        records = self.db.get_mobile_jobs(100)
        self.mobile_jobs_table.setRowCount(0)
        for row, record in enumerate(records):
            self.mobile_jobs_table.insertRow(row)
            values = [
                record.get("source_name", ""),
                record.get("status", ""),
                record.get("device_id", "") or "-",
                self._fmt_size(record.get("file_size") or 0),
                record.get("created_at", ""),
                record.get("sync_status", ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                    item.setToolTip(record.get("source_path", ""))
                self.mobile_jobs_table.setItem(row, col, item)

    def _selected_mobile_jobs(self) -> list:
        jobs = []
        for index in self.mobile_jobs_table.selectionModel().selectedRows():
            item = self.mobile_jobs_table.item(index.row(), 0)
            if item:
                jobs.append(item.data(Qt.ItemDataRole.UserRole))
        return jobs

    def _add_selected_mobile_to_queue(self):
        jobs = self._selected_mobile_jobs()
        if not jobs:
            QMessageBox.warning(self, "No Selection", "Please select mobile files.")
            return
        paths = [job["source_path"] for job in jobs if Path(job["source_path"]).exists()]
        self._add_files(paths)
        self.tabs.setCurrentWidget(self.convert_tab)

    def _convert_selected_mobile_jobs(self):
        self._add_selected_mobile_to_queue()
        if self.selected_files:
            self._start_conversion()

    def _open_inbox_folder(self):
        self._open_folder(self.pairing.inbox_dir)

    def _load_history(self):
        self.history_list.clear()
        self.details_text.clear()

        records = self.db.get_history(100)
        for record in records:
            source = record.get("transfer_source") or "desktop"
            prefix = "MOBILE" if source == "mobile" else "LOCAL"
            status_icon = "[OK]" if record["status"] == "success" else "[X]"
            text = f"{status_icon} {prefix} {record['source_name']}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, record["id"])
            self.history_list.addItem(item)

    def _show_history_details(self, row: int):
        if row < 0:
            return

        item = self.history_list.item(row)
        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)
        if not record:
            return

        status_color = "green" if record["status"] == "success" else "red"
        details = f"""
<h3>Conversion Details</h3>
<table>
<tr><td><b>Status:</b></td><td style="color: {status_color}">{html.escape(record['status'].upper())}</td></tr>
<tr><td><b>Source:</b></td><td>{html.escape(record['source_name'])}</td></tr>
<tr><td><b>Source Path:</b></td><td>{html.escape(record['source_path'])}</td></tr>
<tr><td><b>Source Size:</b></td><td>{self._fmt_size(record['source_size'] or 0)}</td></tr>
<tr><td><b>Output:</b></td><td>{html.escape(record['output_name'])}</td></tr>
<tr><td><b>Output Path:</b></td><td>{html.escape(record['output_path'])}</td></tr>
<tr><td><b>Output Size:</b></td><td>{self._fmt_size(record['output_size'] or 0)}</td></tr>
<tr><td><b>Converted:</b></td><td>{html.escape(str(record['converted_at']))}</td></tr>
<tr><td><b>Backup:</b></td><td>{'Yes' if record['backup_path'] else 'No'}</td></tr>
<tr><td><b>Source Type:</b></td><td>{html.escape(str(record.get('transfer_source') or 'desktop'))}</td></tr>
<tr><td><b>Device:</b></td><td>{html.escape(str(record.get('device_id') or '-'))}</td></tr>
<tr><td><b>Sync:</b></td><td>{html.escape(str(record.get('sync_status') or 'local'))}</td></tr>
</table>
"""
        if record["error_msg"]:
            details += (
                "<p><b>Error:</b> "
                f"<span style='color:red'>{html.escape(record['error_msg'])}</span></p>"
            )

        self.details_text.setHtml(details)

    def _restore_backup(self):
        item = self.history_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No Selection", "Please select a history item.")
            return

        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)
        if not record or not record.get("backup_path"):
            QMessageBox.warning(self, "No Backup", "No backup available for this file.")
            return

        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Restore To",
            record["source_path"],
            "All Files (*.*)",
        )

        if dest:
            if self.db.restore_backup(record_id, dest):
                QMessageBox.information(self, "Restored", f"File restored to:\n{dest}")
            else:
                QMessageBox.critical(self, "Error", "Failed to restore backup.")

    def _clear_history(self):
        reply = QMessageBox.question(
            self,
            "Clear History",
            "Are you sure you want to clear all history?\nBackups will NOT be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self._load_history()
            self.statusBar().showMessage("History cleared")

    def _open_output_folder(self):
        item = self.history_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No Selection", "Please select a history item.")
            return

        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)
        if not record or not record.get("output_path"):
            QMessageBox.warning(self, "No Output", "No output file for this record.")
            return

        output_path = Path(record["output_path"])
        folder = output_path.parent if output_path.exists() else Path(record["source_path"]).parent

        if folder and folder.exists():
            self._open_folder(folder)
        else:
            QMessageBox.warning(self, "Not Found", "Folder not found.")

    def _open_folder(self, folder: Path):
        if not folder.exists():
            QMessageBox.warning(self, "Not Found", "Folder not found.")
            return

        if platform.system() == "Windows":
            os.startfile(str(folder))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(folder)], check=False)

    @staticmethod
    def _fmt_size(size: int) -> str:
        if not size:
            return "0 B"
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.2f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

    def closeEvent(self, event):
        self.pairing.stop()
        super().closeEvent(event)

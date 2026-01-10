import os
import subprocess
import platform

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFileDialog,
    QProgressBar, QMessageBox, QAbstractItemView, QSplitter,
    QGroupBox, QTextEdit, QLineEdit, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QThread, QMutex, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon

from pathlib import Path
import sys

from src.database import Database
from src.converter import PDFConverter, ConversionResult


class ConvertWorker(QThread):
    """Background worker for batch file conversion with smart parallel processing"""
    progress = Signal(int, int, str, bool, str)  # current, total, filename, success, error
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
        """Request cancellation"""
        self.converter.cancel_all()

    def _on_progress(self, completed: int, total: int, filename: str, result: ConversionResult):
        """Callback for each file completion"""
        # Backup the converted PDF
        backup_path = None
        if result.success and result.output_path:
            backup_path = self.db.create_backup(result.output_path)
        
        # Record in database
        self.db.add_record(
            source_path=result.source_path,
            output_path=result.output_path if result.success else "",
            status="success" if result.success else "failed",
            error_msg=result.error if not result.success else None,
            backup_path=backup_path
        )
        
        # Track result
        self._results_lock.lock()
        self._results.append((result.source_path, result))
        self._results_lock.unlock()
        
        # Emit progress signal
        self.progress.emit(completed, total, filename, result.success, result.error)

    def run(self):
        self._results = []
        
        # Use smart batch processing
        self.converter.convert_batch(
            files=self.files,
            output_dir=self.output_dir,
            progress_callback=self._on_progress,
            silent=True
        )
        
        self._results_lock.lock()
        results = list(self._results)
        self._results_lock.unlock()
        
        self.finished.emit(results)


class DropListWidget(QListWidget):
    """List widget with drag & drop support"""
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
    """Main application window"""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.converter = PDFConverter()
        self.selected_files = []
        self.output_dir = None

        self._setup_ui()
        self._load_history()

    def _get_icon_path(self) -> Path:
        """Get icon path (works in dev and PyInstaller)"""
        if getattr(sys, 'frozen', False):
            # Running as compiled exe
            base = Path(sys._MEIPASS)
        else:
            # Running in dev
            base = Path(__file__).parent.parent.parent
        return base / "assets" / "icon.ico"

    def _setup_ui(self):
        self.setWindowTitle("PDFBro - PDF Converter")
        self.setMinimumSize(700, 500)
        
        # Set app icon
        icon_path = self._get_icon_path()
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        engine_label = QLabel(f"Engine: {self.converter.get_engine_name()}")
        engine_label.setStyleSheet(
            "color: green; font-weight: bold;" if self.converter.is_available()
            else "color: red; font-weight: bold;"
        )
        layout.addWidget(engine_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        convert_tab = QWidget()
        self.tabs.addTab(convert_tab, "Convert")
        self._setup_convert_tab(convert_tab)

        history_tab = QWidget()
        self.tabs.addTab(history_tab, "History")
        self._setup_history_tab(history_tab)

        self.statusBar().showMessage("Ready")

    def _setup_convert_tab(self, tab: QWidget):
        layout = QVBoxLayout(tab)

        file_group = QGroupBox("Files to Convert (Drag & Drop or Browse)")
        file_layout = QVBoxLayout(file_group)

        self.file_list = DropListWidget()
        self.file_list.files_dropped.connect(self._add_files)
        file_layout.addWidget(self.file_list)

        btn_layout = QHBoxLayout()

        self.btn_browse = QPushButton("Browse Files")
        self.btn_browse.clicked.connect(self._browse_files)
        btn_layout.addWidget(self.btn_browse)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear_files)
        btn_layout.addWidget(self.btn_clear)

        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        btn_layout.addWidget(self.btn_remove)

        btn_layout.addStretch()
        file_layout.addLayout(btn_layout)
        layout.addWidget(file_group)

        output_group = QGroupBox("Output")
        output_layout = QHBoxLayout(output_group)

        self.chk_same_dir = QCheckBox("Same as source")
        self.chk_same_dir.setChecked(True)
        self.chk_same_dir.stateChanged.connect(self._toggle_output_dir)
        output_layout.addWidget(self.chk_same_dir)

        self.txt_output = QLineEdit()
        self.txt_output.setPlaceholderText("Output directory...")
        self.txt_output.setEnabled(False)
        output_layout.addWidget(self.txt_output)

        self.btn_output = QPushButton("...")
        self.btn_output.setFixedWidth(40)
        self.btn_output.setEnabled(False)
        self.btn_output.clicked.connect(self._browse_output)
        output_layout.addWidget(self.btn_output)

        layout.addWidget(output_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_progress = QLabel("")
        layout.addWidget(self.lbl_progress)

        # Button row for Convert and Cancel
        convert_btn_layout = QHBoxLayout()

        self.btn_convert = QPushButton("Convert to PDF")
        self.btn_convert.setMinimumHeight(40)
        self.btn_convert.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #1084d8;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.btn_convert.clicked.connect(self._start_conversion)
        convert_btn_layout.addWidget(self.btn_convert)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #d83b01;
                color: white;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #ea4a1a;
            }
        """)
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        convert_btn_layout.addWidget(self.btn_cancel)

        layout.addLayout(convert_btn_layout)

    def _setup_history_tab(self, tab: QWidget):
        layout = QVBoxLayout(tab)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self.history_list = QListWidget()
        self.history_list.setAlternatingRowColors(True)
        self.history_list.currentRowChanged.connect(self._show_history_details)
        list_layout.addWidget(self.history_list)

        hist_btn_layout = QHBoxLayout()

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._load_history)
        hist_btn_layout.addWidget(self.btn_refresh)

        self.btn_restore = QPushButton("Restore Backup")
        self.btn_restore.clicked.connect(self._restore_backup)
        hist_btn_layout.addWidget(self.btn_restore)

        self.btn_clear_hist = QPushButton("Clear History")
        self.btn_clear_hist.clicked.connect(self._clear_history)
        hist_btn_layout.addWidget(self.btn_clear_hist)

        self.btn_open_folder = QPushButton("Open Folder")
        self.btn_open_folder.clicked.connect(self._open_output_folder)
        hist_btn_layout.addWidget(self.btn_open_folder)

        list_layout.addLayout(hist_btn_layout)
        splitter.addWidget(list_widget)

        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)

        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        details_layout.addWidget(self.details_text)

        splitter.addWidget(details_widget)
        splitter.setSizes([300, 400])

        layout.addWidget(splitter)

    def _add_files(self, files: list):
        for file_path in files:
            if file_path not in self.selected_files:
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
            self, "Select Files", "",
            f"Supported Documents ({formats});;All Files (*.*)"
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

    def _toggle_output_dir(self, state):
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
        self.statusBar().showMessage(f"{count} file(s) selected")

    def _start_conversion(self):
        if not self.selected_files:
            QMessageBox.warning(self, "No Files", "Please add files to convert.")
            return

        if not self.converter.is_available():
            QMessageBox.critical(
                self, "No Engine",
                "LibreOffice not found. Please install LibreOffice."
            )
            return

        output_dir = None
        if not self.chk_same_dir.isChecked():
            output_dir = self.txt_output.text()
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
            self.db
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, current: int, total: int, filename: str, success: bool, error: str):
        self.progress_bar.setValue(current)
        status = "✓" if success else "✗"
        self.lbl_progress.setText(f"{status} {filename} ({current}/{total})")

    def _cancel_conversion(self):
        """Cancel ongoing conversion - force terminate"""
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_progress.setText("Cancelling...")

    def _on_finished(self, results: list):
        self.btn_convert.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setVisible(False)

        success = sum(1 for _, r in results if r.success)
        failed = len(results) - success

        self.lbl_progress.setText(f"Done! {success} succeeded, {failed} failed")
        self.statusBar().showMessage("Conversion complete")

        self._clear_files()
        self._load_history()

        if failed > 0:
            errors = "\n".join(
                f"- {Path(p).name}: {r.error}"
                for p, r in results if not r.success
            )
            QMessageBox.warning(
                self, "Conversion Complete",
                f"Converted {success} files.\n{failed} failed:\n\n{errors}"
            )
        else:
            QMessageBox.information(
                self, "Success",
                f"Successfully converted {success} file(s) to PDF."
            )

    def _load_history(self):
        self.history_list.clear()
        self.details_text.clear()

        records = self.db.get_history(100)
        for record in records:
            status_icon = "[OK]" if record['status'] == 'success' else "[X]"
            text = f"{status_icon} {record['source_name']}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, record['id'])
            self.history_list.addItem(item)

    def _show_history_details(self, row: int):
        if row < 0:
            return

        item = self.history_list.item(row)
        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)

        if record:
            def fmt_size(size):
                if not size:
                    return "0 B"
                elif size < 1024:
                    return f"{size} B"
                elif size < 1024 * 1024:
                    return f"{size / 1024:.1f} KB"
                elif size < 1024 * 1024 * 1024:
                    return f"{size / (1024*1024):.2f} MB"
                else:
                    return f"{size / (1024*1024*1024):.2f} GB"

            details = f"""
<h3>Conversion Details</h3>
<table>
<tr><td><b>Status:</b></td><td style="color: {'green' if record['status'] == 'success' else 'red'}">
    {record['status'].upper()}</td></tr>
<tr><td><b>Source:</b></td><td>{record['source_name']}</td></tr>
<tr><td><b>Source Path:</b></td><td>{record['source_path']}</td></tr>
<tr><td><b>Source Size:</b></td><td>{fmt_size(record['source_size'] or 0)}</td></tr>
<tr><td><b>Output:</b></td><td>{record['output_name']}</td></tr>
<tr><td><b>Output Path:</b></td><td>{record['output_path']}</td></tr>
<tr><td><b>Output Size:</b></td><td>{fmt_size(record['output_size'] or 0)}</td></tr>
<tr><td><b>Converted:</b></td><td>{record['converted_at']}</td></tr>
<tr><td><b>Backup:</b></td><td>{'Yes' if record['backup_path'] else 'No'}</td></tr>
</table>
"""
            if record['error_msg']:
                details += f"<p><b>Error:</b> <span style='color:red'>{record['error_msg']}</span></p>"

            self.details_text.setHtml(details)

    def _restore_backup(self):
        item = self.history_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No Selection", "Please select a history item.")
            return

        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)

        if not record or not record.get('backup_path'):
            QMessageBox.warning(self, "No Backup", "No backup available for this file.")
            return

        dest, _ = QFileDialog.getSaveFileName(
            self, "Restore To",
            record['source_path'],
            "All Files (*.*)"
        )

        if dest:
            if self.db.restore_backup(record_id, dest):
                QMessageBox.information(self, "Restored", f"File restored to:\n{dest}")
            else:
                QMessageBox.critical(self, "Error", "Failed to restore backup.")

    def _clear_history(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "Are you sure you want to clear all history?\nBackups will NOT be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self._load_history()
            self.statusBar().showMessage("History cleared")

    def _open_output_folder(self):
        """Open the output folder of selected history item"""
        item = self.history_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No Selection", "Please select a history item.")
            return

        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.db.get_record(record_id)

        if not record or not record.get('output_path'):
            QMessageBox.warning(self, "No Output", "No output file for this record.")
            return

        output_path = Path(record['output_path'])
        folder = output_path.parent if output_path.exists() else None

        if not folder or not folder.exists():
            # Try source folder as fallback
            folder = Path(record['source_path']).parent

        if folder and folder.exists():
            # Cross-platform folder opening
            if platform.system() == "Windows":
                os.startfile(str(folder))
            elif platform.system() == "Darwin":  # macOS
                subprocess.run(["open", str(folder)])
            else:  # Linux
                subprocess.run(["xdg-open", str(folder)])
        else:
            QMessageBox.warning(self, "Not Found", "Folder not found.")

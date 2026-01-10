import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from src.ui.main_window import MainWindow
from src.database import Database

def main():
    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    
    app = QApplication(sys.argv)
    app.setApplicationName("PDFBro")
    app.setOrganizationName("PDFBro")
    
    # Initialize database
    db = Database()
    
    # Create and show main window
    window = MainWindow(db)
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

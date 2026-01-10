# PDFBro - Simple PDF Converter

A lightweight, portable PDF converter desktop application with backup & restore functionality.

## Features

- **High-Quality PDF Conversion** - Uses LibreOffice or MS Office engine
- **Multi-Select & Drag-Drop** - Easy file selection
- **Conversion History** - Detailed logs of all conversions
- **Backup & Restore** - Automatically backs up source files
- **Cross-Platform** - Works on Windows, macOS, and Linux
- **Portable** - All data stored in app directory
- **Silent Operation** - No console windows or popups during conversion

## Supported Formats

| Format | Extensions |
|--------|------------|
| Word | .doc, .docx, .rtf, .txt |
| Excel | .xls, .xlsx, .csv |
| PowerPoint | .ppt, .pptx |
| OpenDocument | .odt, .ods, .odp |
| Web | .html |

## Requirements

- Python 3.9+
- LibreOffice **OR** Microsoft Office
- PySide6

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
python pdfbro.py
```

## Usage

1. **Add Files**: Drag & drop files or click "Browse Files"
2. **Set Output**: Choose same directory or custom output folder
3. **Convert**: Click "Convert to PDF"
4. **History**: View past conversions in the History tab
5. **Restore**: Restore accidentally deleted source files from backup

## Project Structure

```
pdfbro/
├── pdfbro.py           # Entry point
├── requirements.txt    # Dependencies
├── README.md
├── src/
│   ├── __init__.py
│   ├── database.py     # SQLite storage
│   ├── converter.py    # PDF conversion engine
│   └── ui/
│       ├── __init__.py
│       └── main_window.py  # PySide6 UI
└── data/               # Created at runtime
    ├── pdfbro.db       # SQLite database
    └── backups/        # File backups
```

## Building Portable Executable

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PDFBro pdfbro.py
```

## Privacy

All data is stored locally:
- SQLite database in `data/pdfbro.db`
- Backups in `data/backups/`
- No network connections
- No telemetry

## License

MIT License

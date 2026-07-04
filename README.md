# PDFConverter Desktop

A desktop PDF conversion app with local LibreOffice conversion, mobile-to-laptop Wi-Fi pairing, QR onboarding, and local-first history.

## Features

- **High-Quality PDF Conversion** - Uses LibreOffice or MS Office engine
- **Multi-Select & Drag-Drop** - Easy file selection
- **Conversion History** - Detailed logs of all conversions
- **Backup & Restore** - Automatically backs up source files
- **QR Pairing** - Generates short-lived pairing sessions for mobile apps
- **Wi-Fi Mobile Inbox** - Receives paired mobile uploads over the local network
- **Sync-Ready Metadata** - Tracks device, job, and sync state for Supabase integration
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
- qrcode

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
python pdfbro.py
```

## Usage

1. **Pair Mobile**: Open the Pairing tab and scan the QR code from the mobile app.
2. **Receive Files**: Paired mobile uploads appear in the Mobile Inbox tab.
3. **Convert**: Convert mobile inbox files or local desktop files with the Convert tab.
4. **History**: View desktop and mobile-originated conversions in the History tab.
5. **Restore**: Restore accidentally deleted source files from backup.

## Mobile Pairing Contract

The desktop app starts a local HTTP server on the laptop and advertises it on the LAN as `_docxtor._tcp` when `zeroconf` is installed. TXT metadata includes `code`, `token`, `pairId`, `name`, `displayName`, and `baseUrl`.

The QR code contains the Docxtor pairing ticket JSON:

```json
{
  "protocolVersion": 1,
  "engineName": "Docxtor Engine",
  "deviceName": "Laptop Name",
  "endpoint": {
    "baseUrl": "http://<laptop-ip>:<port>",
    "displayName": "PDFConverter Desktop",
    "token": "<pairing-token>",
    "pairingCode": "<manual-code>",
    "pairId": "<session-id>"
  }
}
```

Legacy pairing URLs are still accepted by Docxtor:

```text
http://<laptop-ip>:<port>/pair?session_id=<id>&token=<token>
```

Pairing can use either endpoint:

```text
POST /v1/pair
GET /pair?session_id=<id>&token=<token>
```

A successful pairing response includes `device_token` and `server_url`. The mobile app uses the device token for conversion:

```text
POST /v1/docx/render
Authorization: Bearer <device_token>
Content-Type: application/octet-stream
X-Docxtor-Source-Name: document.docx
Body: raw DOCX bytes
```

The response is JSON:

```json
{
  "sourceName": "document.docx",
  "pdfBase64": "<base64-pdf>",
  "paragraphCount": 0,
  "tableCount": 0,
  "diagnostics": []
}
```

The older `/upload?filename=<name>` endpoint remains available for mobile inbox workflows, but Docxtor's direct preview flow uses `/v1/docx/render`.

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
│   ├── pairing.py      # Wi-Fi pairing and mobile upload service
│   ├── sync.py         # Supabase configuration/status boundary
│   └── ui/
│       ├── __init__.py
│       └── main_window.py  # PySide6 UI
└── data/               # Created at runtime
    ├── pdfbro.db       # SQLite database
    ├── backups/        # File backups
    └── mobile_inbox/   # Paired mobile uploads
```

## Building Portable Executable

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PDFBro pdfbro.py
```

## Supabase Configuration

The desktop app is local-first. Supabase readiness is detected through environment variables:

```bash
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_ANON_KEY="your-anon-key"
```

The current implementation stores sync-ready metadata locally. Actual Supabase table writes should be wired to the shared `docxtor` mobile schema once that contract is finalized.

## Privacy

By default, data is stored locally:
- SQLite database in `data/pdfbro.db`
- Backups in `data/backups/`
- Mobile uploads in `data/mobile_inbox/`
- Local network server only while pairing/upload is active
- No telemetry

## License

MIT License

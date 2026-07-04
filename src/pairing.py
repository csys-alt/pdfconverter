import base64
import hashlib
import hmac
import json
import secrets
import socket
import tempfile
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from zeroconf import ServiceInfo, Zeroconf
except Exception:  # pragma: no cover - optional runtime dependency
    ServiceInfo = None
    Zeroconf = None


def _utc_now() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_filename(filename: str) -> str:
    name = Path(filename or "mobile-upload").name.strip()
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    return safe[:160] or "mobile-upload"


def _mdns_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "- _" else "-" for ch in value)
    cleaned = "-".join(cleaned.replace("_", "-").split())
    return cleaned[:50].strip("-.") or "Docxtor-Engine"


def get_lan_ip() -> str:
    """Return the best local address a phone on the same Wi-Fi can reach."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass

    return "127.0.0.1"


@dataclass
class PairingSession:
    session_id: str
    token: str
    manual_code: str
    server_url: str
    pair_url: str
    expires_at: str
    state: str = "waiting"

    def qr_payload(self) -> str:
        return json.dumps({
            "protocolVersion": 1,
            "engineName": "Docxtor Engine",
            "deviceName": socket.gethostname(),
            "endpoint": {
                "baseUrl": self.server_url,
                "displayName": "PDFConverter Desktop",
                "token": self.token,
                "pairingCode": self.manual_code,
                "pairId": self.session_id,
            },
        })


@dataclass
class PairingSnapshot:
    running: bool
    host: str
    port: int
    server_url: str
    state: str
    manual_code: str
    pair_url: str
    expires_at: str
    device_name: str
    device_id: str
    last_error: str
    uploads_received: int


class PairingService:
    """Local Wi-Fi pairing and upload service for the desktop app."""

    def __init__(
        self,
        db,
        converter=None,
        inbox_dir: Optional[Path] = None,
        supported_formats: Optional[set] = None,
        preferred_port: int = 8765,
        max_upload_mb: int = 100,
    ):
        self.db = db
        self.converter = converter
        self.app_dir = Path(__file__).parent.parent
        self.inbox_dir = inbox_dir or self.app_dir / "data" / "mobile_inbox"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.supported_formats = {ext.lower() for ext in (supported_formats or set())}
        self.preferred_port = preferred_port
        self.max_upload_bytes = max_upload_mb * 1024 * 1024

        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[PairingSession] = None
        self._session_token_hash = ""
        self._paired_device: Dict[str, str] = {}
        self._device_token = ""
        self._device_token_hash = ""
        self._uploads_received = 0
        self._last_error = ""
        self._zeroconf = None
        self._zeroconf_info = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        with self._lock:
            if self._server:
                return

            handler = self._handler_factory()
            try:
                self._server = ThreadingHTTPServer(("0.0.0.0", self.preferred_port), handler)
            except OSError:
                self._server = ThreadingHTTPServer(("0.0.0.0", 0), handler)

            self._server.daemon_threads = True
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="pdfconverter-pairing-server",
                daemon=True,
            )
            self._thread.start()
            self._last_error = ""

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
            self._session = None
            self._session_token_hash = ""
            self._paired_device = {}
            self._device_token = ""
            self._device_token_hash = ""
            self._unregister_mdns()

        if server:
            server.shutdown()
            server.server_close()

    def create_session(self, ttl_seconds: int = 300) -> PairingSession:
        self.start()
        with self._lock:
            host, port = self._address()
            server_url = f"http://{host}:{port}"
            session_id = secrets.token_urlsafe(9)
            token = secrets.token_urlsafe(24)
            manual_code = f"{secrets.randbelow(1000000):06d}"
            expires_at = _utc_now() + timedelta(seconds=ttl_seconds)
            params = urlencode({"session_id": session_id, "token": token})
            pair_url = f"{server_url}/pair?{params}"

            session = PairingSession(
                session_id=session_id,
                token=token,
                manual_code=manual_code,
                server_url=server_url,
                pair_url=pair_url,
                expires_at=_iso(expires_at),
                state="waiting",
            )
            self._session = session
            self._session_token_hash = _hash_secret(token)
            self._paired_device = {}
            self._device_token = ""
            self._device_token_hash = ""
            self._last_error = ""
            self.db.create_pairing_session(
                session_id=session_id,
                pairing_code=manual_code,
                token_hash=self._session_token_hash,
                server_url=server_url,
                expires_at=session.expires_at,
            )
            self._register_mdns(session)
            return session

    def snapshot(self) -> PairingSnapshot:
        with self._lock:
            if self._session and self._session.state == "waiting" and self._is_expired(self._session):
                self._session.state = "expired"
                self.db.update_pairing_session_state(self._session.session_id, "expired")

            host, port = self._address()
            session = self._session
            device = self._paired_device
            return PairingSnapshot(
                running=self.running,
                host=host,
                port=port,
                server_url=f"http://{host}:{port}" if self.running else "",
                state=session.state if session else "stopped",
                manual_code=session.manual_code if session else "",
                pair_url=session.pair_url if session else "",
                expires_at=session.expires_at if session else "",
                device_name=device.get("device_name", ""),
                device_id=device.get("device_id", ""),
                last_error=self._last_error,
                uploads_received=self._uploads_received,
            )

    def pair_device(
        self,
        session_id: str,
        token: str = "",
        manual_code: str = "",
        device_id: str = "",
        device_name: str = "",
        client_ip: str = "",
    ) -> Dict[str, str]:
        with self._lock:
            session = self._session
            if not session:
                raise ValueError("No active pairing session")
            if self._is_expired(session):
                session.state = "expired"
                self.db.update_pairing_session_state(session.session_id, "expired")
                raise ValueError("Pairing session expired")
            if session.session_id != session_id:
                raise ValueError("Invalid pairing session")

            token_ok = bool(token) and hmac.compare_digest(
                _hash_secret(token), self._session_token_hash
            )
            code_ok = bool(manual_code) and hmac.compare_digest(manual_code, session.manual_code)
            if not token_ok and not code_ok:
                raise ValueError("Invalid pairing token")

            device_id = device_id or f"mobile-{secrets.token_hex(6)}"
            device_name = device_name or "Docxtor Mobile"
            self._device_token = secrets.token_urlsafe(32)
            self._device_token_hash = _hash_secret(self._device_token)
            self._paired_device = {
                "device_id": device_id,
                "device_name": device_name,
                "client_ip": client_ip,
            }
            session.state = "connected"
            self.db.upsert_device(
                device_id=device_id,
                device_name=device_name,
                address=client_ip,
                token_hash=self._device_token_hash,
            )
            self.db.update_pairing_session_state(session.session_id, "connected", device_id)

            return {
                "status": "connected",
                "device_id": device_id,
                "device_name": device_name,
                "device_token": self._device_token,
                "server_url": session.server_url,
            }

    def accept_upload(self, filename: str, payload: bytes, device_token: str) -> Dict[str, str]:
        with self._lock:
            if not self._is_authorized(device_token):
                raise PermissionError("Device is not paired")

            safe_name = _safe_filename(filename)
            suffix = Path(safe_name).suffix.lower()
            if self.supported_formats and suffix not in self.supported_formats:
                raise ValueError(f"Unsupported file type: {suffix or 'unknown'}")
            if len(payload) > self.max_upload_bytes:
                raise ValueError("Upload exceeds desktop size limit")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.inbox_dir / f"{stamp}_{safe_name}"
            counter = 2
            while target.exists():
                target = self.inbox_dir / f"{stamp}_{counter}_{safe_name}"
                counter += 1

            target.write_bytes(payload)
            checksum = hashlib.sha256(payload).hexdigest()
            device_id = self._paired_device.get("device_id", "")
            job_id = self.db.add_mobile_job(
                device_id=device_id,
                source_name=safe_name,
                source_path=str(target),
                status="transferred",
                file_size=len(payload),
                checksum=checksum,
            )
            self._uploads_received += 1
            self.db.mark_device_seen(device_id)

            return {
                "status": "transferred",
                "job_id": str(job_id),
                "filename": safe_name,
                "size": str(len(payload)),
                "checksum": checksum,
            }

    def _address(self) -> tuple[str, int]:
        if not self._server:
            return get_lan_ip(), self.preferred_port
        return get_lan_ip(), int(self._server.server_address[1])

    def _is_expired(self, session: PairingSession) -> bool:
        try:
            expires_at = datetime.fromisoformat(session.expires_at.replace("Z", ""))
        except ValueError:
            return True
        return _utc_now() >= expires_at

    def _is_authorized(self, device_token: str) -> bool:
        if not device_token or not self._device_token_hash:
            return False
        return hmac.compare_digest(_hash_secret(device_token), self._device_token_hash)

    def _is_pairing_token(self, token: str) -> bool:
        session = self._session
        if not token or not session or self._is_expired(session):
            return False
        return hmac.compare_digest(_hash_secret(token), self._session_token_hash)

    def _register_mdns(self, session: PairingSession) -> None:
        self._unregister_mdns()
        if Zeroconf is None or ServiceInfo is None:
            return

        try:
            host, port = self._address()
            service_type = "_docxtor._tcp.local."
            instance = _mdns_name(f"PDFConverter Desktop {session.manual_code}")
            properties = {
                "code": session.manual_code,
                "token": session.token,
                "pairId": session.session_id,
                "displayName": "PDFConverter Desktop",
                "name": "PDFConverter Desktop",
                "baseUrl": session.server_url,
                "path": "/",
                "protocol": "1",
            }
            info = ServiceInfo(
                service_type,
                f"{instance}.{service_type}",
                addresses=[socket.inet_aton(host)],
                port=port,
                properties=properties,
                server=f"{_mdns_name(socket.gethostname())}.local.",
            )
            zeroconf = Zeroconf()
            zeroconf.register_service(info, allow_name_change=True)
            self._zeroconf = zeroconf
            self._zeroconf_info = info
        except Exception as exc:
            self._last_error = f"mDNS registration failed: {exc}"
            self._unregister_mdns()

    def _unregister_mdns(self) -> None:
        zeroconf = self._zeroconf
        info = self._zeroconf_info
        self._zeroconf = None
        self._zeroconf_info = None

        if zeroconf and info:
            try:
                zeroconf.unregister_service(info)
            except Exception:
                pass
        if zeroconf:
            try:
                zeroconf.close()
            except Exception:
                pass

    def _handler_factory(self):
        service = self

        class PairingRequestHandler(BaseHTTPRequestHandler):
            server_version = "PDFConverterPairing/1.0"

            def do_OPTIONS(self):
                self.send_response(204)
                self._send_cors_headers()
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path in {"/health", "/v1/health"}:
                    self._send_json({"status": "ok", "service": "pdfconverter-pairing"})
                    return
                if parsed.path in {"/pair", "/v1/pair"}:
                    params = parse_qs(parsed.query)
                    self._handle_pair(params)
                    return
                if parsed.path == "/status":
                    self._send_json(asdict(service.snapshot()))
                    return
                self._send_json({"error": "Not found"}, status=404)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path in {"/pair", "/v1/pair"}:
                    data = self._read_json()
                    params = {key: [str(value)] for key, value in data.items()}
                    self._handle_pair(params)
                    return
                if parsed.path == "/upload":
                    self._handle_upload(parsed)
                    return
                if parsed.path == "/v1/docx/render":
                    self._handle_docx_render()
                    return
                self._send_json({"error": "Not found"}, status=404)

            def log_message(self, format, *args):
                return

            def _handle_pair(self, params):
                try:
                    result = service.pair_device(
                        session_id=(
                            self._first(params, "session_id")
                            or self._first(params, "sessionId")
                            or self._first(params, "pairId")
                            or self._first(params, "pair_id")
                        ),
                        token=(
                            self._first(params, "token")
                            or self._first(params, "authToken")
                            or self._first(params, "pairingToken")
                        ),
                        manual_code=(
                            self._first(params, "manual_code")
                            or self._first(params, "code")
                            or self._first(params, "pairingCode")
                            or self._first(params, "pairing_code")
                        ),
                        device_id=(
                            self._first(params, "device_id")
                            or self._first(params, "deviceId")
                        ),
                        device_name=(
                            self._first(params, "device_name")
                            or self._first(params, "deviceName")
                        ),
                        client_ip=self.client_address[0],
                    )
                    self._send_json(result)
                except Exception as exc:
                    service._last_error = str(exc)
                    self._send_json({"status": "failed", "error": str(exc)}, status=400)

            def _handle_upload(self, parsed):
                try:
                    params = parse_qs(parsed.query)
                    filename = self._first(params, "filename") or self.headers.get(
                        "X-Filename", "mobile-upload"
                    )
                    device_token = self.headers.get("X-Device-Token") or self._first(
                        params, "device_token"
                    )
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length <= 0:
                        raise ValueError("Upload body is empty")
                    if content_length > service.max_upload_bytes:
                        raise ValueError("Upload exceeds desktop size limit")
                    filename, payload = self._read_upload_body(filename, content_length)
                    result = service.accept_upload(filename, payload, device_token)
                    self._send_json(result, status=201)
                except PermissionError as exc:
                    service._last_error = str(exc)
                    self._send_json({"status": "failed", "error": str(exc)}, status=401)
                except Exception as exc:
                    service._last_error = str(exc)
                    self._send_json({"status": "failed", "error": str(exc)}, status=400)

            def _handle_docx_render(self):
                try:
                    auth_header = self.headers.get("Authorization", "")
                    device_token = self.headers.get("X-Device-Token", "")
                    if auth_header.lower().startswith("bearer "):
                        device_token = auth_header[7:].strip()
                    if not (
                        service._is_authorized(device_token)
                        or service._is_pairing_token(device_token)
                    ):
                        self._send_json({"status": "failed", "error": "Unauthorized"}, status=401)
                        return

                    if service.converter is None:
                        self._send_json(
                            {"status": "failed", "error": "Converter not available"},
                            status=503,
                        )
                        return

                    source_name = self.headers.get(
                        "X-Docxtor-Source-Name", "document.docx"
                    )
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length <= 0:
                        raise ValueError("Request body is empty")
                    if content_length > service.max_upload_bytes:
                        self._send_json(
                            {"status": "failed", "error": "Request exceeds desktop size limit"},
                            status=413,
                        )
                        return

                    docx_bytes = self.rfile.read(content_length)
                    suffix = Path(source_name).suffix or ".docx"
                    result = None
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(docx_bytes)
                        tmp_path = tmp.name

                    try:
                        result = service.converter.convert_to_pdf(tmp_path)
                        if not result.success:
                            raise RuntimeError(result.error)

                        pdf_bytes = Path(result.output_path).read_bytes()
                        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
                        response = {
                            "sourceName": source_name,
                            "pdfBase64": pdf_b64,
                            "paragraphCount": 0,
                            "tableCount": 0,
                            "diagnostics": [],
                        }
                        self._send_json(response)
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)
                        if result and result.output_path:
                            Path(result.output_path).unlink(missing_ok=True)
                except PermissionError as exc:
                    service._last_error = str(exc)
                    self._send_json({"status": "failed", "error": str(exc)}, status=401)
                except Exception as exc:
                    service._last_error = str(exc)
                    self._send_json({"status": "failed", "error": str(exc)}, status=400)

            def _read_json(self) -> Dict[str, str]:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    return {}
                raw = self.rfile.read(content_length)
                return json.loads(raw.decode("utf-8"))

            def _read_upload_body(self, fallback_filename: str, content_length: int):
                content_type = self.headers.get("Content-Type", "")
                payload = self.rfile.read(content_length)
                if not content_type.startswith("multipart/form-data"):
                    return fallback_filename, payload

                message = Message()
                message["content-type"] = content_type
                boundary = message.get_param("boundary", header="content-type")
                if not boundary:
                    raise ValueError("Multipart boundary missing")

                delimiter = b"--" + boundary.encode("utf-8")
                for part in payload.split(delimiter):
                    part = part.strip(b"\r\n")
                    if not part or part == b"--":
                        continue

                    header_blob, separator, data = part.partition(b"\r\n\r\n")
                    if not separator:
                        continue

                    headers = header_blob.decode("utf-8", errors="ignore")
                    if "filename=" not in headers and 'name="file"' not in headers:
                        continue

                    filename = fallback_filename
                    disposition = ""
                    for line in headers.splitlines():
                        if line.lower().startswith("content-disposition:"):
                            disposition = line
                            break

                    for section in disposition.split(";"):
                        section = section.strip()
                        if section.startswith("filename="):
                            filename = section.split("=", 1)[1].strip().strip('"')
                            break

                    if data.endswith(b"\r\n"):
                        data = data[:-2]
                    if data.endswith(b"--"):
                        data = data[:-2]
                    return filename, data

                raise ValueError("Multipart file field missing")

            def _send_json(self, payload: Dict[str, str], status: int = 200):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _send_cors_headers(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-Device-Token, X-Filename, X-Docxtor-Source-Name, Authorization",
                )

            @staticmethod
            def _first(params, key: str) -> str:
                values = params.get(key, [""])
                return values[0] if values else ""

        return PairingRequestHandler

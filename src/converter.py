import subprocess
import shutil
import platform
import threading
import tempfile
import time
import os
import psutil
from pathlib import Path
from typing import Optional, List, Callable
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue

@dataclass
class ConversionResult:
    success: bool
    output_path: str = ""
    error: str = ""
    source_path: str = ""


class ResourceMonitor:
    
    @staticmethod
    def get_optimal_workers() -> int:
        """Calculate optimal number of parallel workers based on system resources"""
        cpu_count = os.cpu_count() or 2
        
        try:
            # Get available memory in GB
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)
            
            # Get CPU usage
            cpu_percent = psutil.cpu_percent(interval=0.1)
            
            # Each LibreOffice instance uses ~500MB-1GB RAM
            # Be conservative: allow 1 worker per 1.5GB available RAM
            mem_based_workers = max(1, int(available_gb / 1.5))
            
            # CPU-based: use half of cores to leave room for LibreOffice overhead
            cpu_based_workers = max(1, cpu_count // 2)
            
            # If system is already under load, reduce workers
            if cpu_percent > 70:
                cpu_based_workers = max(1, cpu_based_workers // 2)
            
            # Take the minimum to avoid overloading either CPU or RAM
            optimal = min(mem_based_workers, cpu_based_workers)
            
            # Cap at reasonable maximum (4 parallel conversions is usually enough)
            return min(optimal, 4)
            
        except Exception:
            # Fallback: conservative single-threaded
            return 1
    
    @staticmethod
    def should_throttle() -> bool:
        """Check if we should slow down due to resource pressure"""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.05)
            mem = psutil.virtual_memory()
            
            # Throttle if CPU > 90% or memory < 500MB available
            return cpu_percent > 90 or mem.available < (500 * 1024 * 1024)
        except Exception:
            return False


class PDFConverter:
    """Cross-platform PDF converter using LibreOffice with smart parallel processing"""
    
    # Supported input formats
    SUPPORTED_FORMATS = {
        '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.odt', '.ods', '.odp', '.rtf', '.txt', '.csv', '.html'
    }
    
    def __init__(self):
        self.engine_path = self._find_libreoffice()
        self._process_lock = threading.Lock()
        self._active_processes: List[subprocess.Popen] = []
        self._cancelled = False
        self._cancel_lock = threading.Lock()
    
    def _find_libreoffice(self) -> Optional[str]:
        """Find LibreOffice installation"""
        paths = []
        
        # Windows
        if platform.system() == "Windows":
            paths = [
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            ]
        # MacOS
        elif platform.system() == "Darwin":
            paths = [
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            ]
        # Linux
        else:
            paths = [
                "/usr/bin/libreoffice",
                "/usr/bin/soffice",
                "/usr/local/bin/libreoffice",
            ]
        
        for path in paths:
            if Path(path).exists():
                return path
        
        # Try to find in PATH
        if shutil.which("libreoffice"):
            return shutil.which("libreoffice")
        if shutil.which("soffice"):
            return shutil.which("soffice")
        
        return None
    
    def is_available(self) -> bool:
        """Check if LibreOffice is available"""
        return self.engine_path is not None
    
    def get_engine_name(self) -> str:
        """Get display name of current engine"""
        return "LibreOffice" if self.is_available() else "LibreOffice (not found)"
    
    def is_supported(self, file_path: str) -> bool:
        """Check if file format is supported"""
        return Path(file_path).suffix.lower() in self.SUPPORTED_FORMATS
    
    def cancel_all(self):
        """Cancel all running conversions"""
        with self._cancel_lock:
            self._cancelled = True
        self.kill_all_processes()
    
    def reset_cancel(self):
        """Reset cancellation flag for new batch"""
        with self._cancel_lock:
            self._cancelled = False
    
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested"""
        with self._cancel_lock:
            return self._cancelled
    
    def kill_all_processes(self):
        """Kill all active conversion processes"""
        with self._process_lock:
            for proc in self._active_processes:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except:
                        try:
                            proc.kill()
                        except:
                            pass
            self._active_processes.clear()
    
    def convert_batch(self, files: List[str], output_dir: str = None,
                      progress_callback: Callable[[int, int, str, ConversionResult], None] = None,
                      silent: bool = True) -> List[ConversionResult]:
        """
        Convert multiple files with smart parallel processing.
        Automatically adapts to system resources for optimal performance.
        """
        if not files:
            return []
        
        self.reset_cancel()
        results = []
        total = len(files)
        completed = 0
        results_lock = threading.Lock()
        
        # Determine optimal worker count based on current system state
        max_workers = ResourceMonitor.get_optimal_workers()
        
        def convert_single(file_path: str) -> ConversionResult:
            """Convert a single file with resource awareness"""
            if self.is_cancelled():
                return ConversionResult(False, error="Cancelled", source_path=file_path)
            
            # Adaptive throttling - pause briefly if system is under pressure
            while ResourceMonitor.should_throttle() and not self.is_cancelled():
                time.sleep(0.5)
            
            result = self._convert_single_file(file_path, output_dir, silent)
            result.source_path = file_path
            return result
        
        # Use ThreadPoolExecutor for efficient parallel processing
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all files
            future_to_file = {
                executor.submit(convert_single, f): f for f in files
            }
            
            # Process results as they complete
            for future in as_completed(future_to_file):
                if self.is_cancelled():
                    # Cancel remaining futures
                    for f in future_to_file:
                        f.cancel()
                    break
                
                file_path = future_to_file[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = ConversionResult(False, error=str(e), source_path=file_path)
                
                with results_lock:
                    results.append(result)
                    completed += 1
                    
                    if progress_callback:
                        progress_callback(completed, total, Path(file_path).name, result)
        
        return results
    
    def _convert_single_file(self, input_path: str, output_dir: str = None,
                              silent: bool = True) -> ConversionResult:
        """Convert a single document to PDF with high quality settings"""
        input_file = Path(input_path)
        
        if not input_file.exists():
            return ConversionResult(False, error=f"File not found: {input_path}")
        
        if not self.is_supported(input_path):
            return ConversionResult(False, error=f"Unsupported format: {input_file.suffix}")
        
        if not self.is_available():
            return ConversionResult(False, error="LibreOffice not found")
        
        # Calculate timeout based on file size (min 90s, +30s per 10MB, max 20min)
        file_size_mb = input_file.stat().st_size / (1024 * 1024)
        timeout = min(1200, max(90, int(90 + (file_size_mb / 10) * 30)))
        
        # Set output directory
        out_dir = Path(output_dir) if output_dir else input_file.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            return self._convert_libreoffice(input_file, out_dir, silent, timeout)
        except Exception as e:
            return ConversionResult(False, error=str(e))
    
    # Legacy single-file method for compatibility
    def convert_to_pdf(self, input_path: str, output_dir: str = None,
                       silent: bool = True) -> ConversionResult:
        """Convert single document to PDF (legacy method)"""
        result = self._convert_single_file(input_path, output_dir, silent)
        result.source_path = input_path
        return result
    
    def _convert_libreoffice(self, input_file: Path, output_dir: Path,
                             silent: bool, timeout: int = 120) -> ConversionResult:
        """Convert using LibreOffice with high quality settings"""
        
        # Create a temporary user profile to avoid conflicts with running LibreOffice instances
        # This prevents blank PDF issues caused by LibreOffice profile locks
        temp_profile = tempfile.mkdtemp(prefix="libreoffice_pdfbro_")
        process = None
        
        try:
            # Build command with high-quality PDF export
            # Using -env:UserInstallation for isolated profile prevents conflicts
            cmd = [
                self.engine_path,
                "--headless",  # No GUI
                "--invisible",  # Extra invisibility
                "--nologo",  # Skip logo
                "--nofirststartwizard",  # Skip wizard
                f"-env:UserInstallation=file:///{temp_profile.replace(os.sep, '/')}",
                "--convert-to", "pdf:writer_pdf_Export",  # High quality PDF export
                "--outdir", str(output_dir),
                str(input_file)
            ]
            
            # Silent mode - hide console window on Windows
            startupinfo = None
            creationflags = 0
            if silent and platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW
            
            # Set environment for better compatibility
            env = os.environ.copy()
            env["LC_ALL"] = "C.UTF-8"  # Ensure UTF-8 handling
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo,
                creationflags=creationflags,
                env=env
            )
            
            # Track active process for cancellation
            with self._process_lock:
                self._active_processes.append(process)
            
            stdout, stderr = process.communicate(timeout=timeout)
            exit_code = process.returncode
            
            # Remove from active processes
            with self._process_lock:
                if process in self._active_processes:
                    self._active_processes.remove(process)
            
            # Small delay to ensure file is fully written
            time.sleep(0.2)
            
            expected_output = output_dir / f"{input_file.stem}.pdf"
            
            if expected_output.exists():
                # Verify the PDF is not empty (at least has PDF header)
                file_size = expected_output.stat().st_size
                if file_size > 100:  # Valid PDFs are at least a few hundred bytes
                    return ConversionResult(True, output_path=str(expected_output))
                else:
                    expected_output.unlink()  # Remove empty/corrupt file
                    return ConversionResult(False, error="Conversion produced empty PDF")
            else:
                error = ""
                if stderr:
                    error = stderr.decode(errors='ignore').strip()
                if not error and stdout:
                    error = stdout.decode(errors='ignore').strip()
                if not error:
                    error = f"PDF not created (exit code: {exit_code})"
                return ConversionResult(False, error=error)
                
        except subprocess.TimeoutExpired:
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except:
                    process.kill()
            return ConversionResult(False, error="Conversion timed out")
        except Exception as e:
            if process and process.poll() is None:
                try:
                    process.terminate()
                except:
                    pass
            return ConversionResult(False, error=str(e))
        finally:
            # Remove from active processes
            with self._process_lock:
                if process and process in self._active_processes:
                    self._active_processes.remove(process)
            # Clean up temporary profile
            try:
                shutil.rmtree(temp_profile, ignore_errors=True)
            except:
                pass

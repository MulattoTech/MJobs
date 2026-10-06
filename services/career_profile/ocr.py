"""Offline, resource-bounded scanned PDF extraction in the Linux app runtime."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image


MAX_OCR_PAGES = 10
MAX_DIMENSION = 1800
MAX_RENDER_BYTES = 15 * 1024 * 1024
MAX_PAGE_TEXT_BYTES = 64 * 1024
PROCESS_TIMEOUT = 10
TOTAL_TIMEOUT = 45
MAX_DIAGNOSTIC_BYTES = 16 * 1024
RESOURCE_LIMITS_AVAILABLE = sys.platform.startswith('linux')
_SLOT = threading.BoundedSemaphore(1)
# Exec replaces the helper process, so subprocess timeout kills the actual tool.
# Do not use preexec_fn in the threaded API process.
_LIMIT_AND_EXEC = (
    'import os,resource,sys; '
    'resource.setrlimit(resource.RLIMIT_AS,(805306368,805306368)); '
    'resource.setrlimit(resource.RLIMIT_FSIZE,(16777216,16777216)); '
    'resource.setrlimit(resource.RLIMIT_CPU,(12,12)); '
    'resource.setrlimit(resource.RLIMIT_CORE,(0,0)); '
    'os.execv(sys.argv[1],sys.argv[1:])'
)


class OCRToolFailure(subprocess.SubprocessError):
    """Sanitized nonzero child result with metadata, never diagnostic contents."""

    def __init__(self, returncode, diagnostic_bytes, diagnostics_truncated):
        self.returncode = returncode
        self.diagnostic_bytes = diagnostic_bytes
        self.diagnostics_truncated = diagnostics_truncated
        super().__init__(f'OCR tool exited with status {returncode}')


def _run_tool(executable, args, timeout):
    # Tool diagnostics can contain private file paths or document content. Keep
    # them out of memory, API results, and logs; the Linux child file-size limit
    # bounds the temporary capture even if a tool emits continuously.
    with tempfile.TemporaryFile(mode='w+b') as diagnostics:
        result = subprocess.run([sys.executable, '-c', _LIMIT_AND_EXEC, executable, *args],
            check=False, shell=False, timeout=timeout, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=diagnostics,
            env={**{key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ},
                 'OMP_THREAD_LIMIT': '1', 'OMP_NUM_THREADS': '1'})
        diagnostics.flush()
        diagnostics.seek(0, os.SEEK_END)
        diagnostic_size = diagnostics.tell()
    if result.returncode != 0:
        raise OCRToolFailure(result.returncode, min(diagnostic_size, MAX_DIAGNOSTIC_BYTES),
                             diagnostic_size > MAX_DIAGNOSTIC_BYTES)


def ocr_pdf_pages(data, page_numbers):
    """Return page-number/text pairs and public warnings, without raw diagnostics."""
    if len(data) > 10 * 1024 * 1024 or any(type(n) is not int or not 1 <= n <= 100 for n in page_numbers):
        raise ValueError('OCR input exceeds supported limits')
    pages = sorted(set(page_numbers))
    if not pages:
        return {}, []
    renderer, engine = shutil.which('pdftoppm'), shutil.which('tesseract')
    if not RESOURCE_LIMITS_AVAILABLE or not renderer or not engine:
        return {}, ['Local OCR is unavailable; the supported Docker runtime includes scanned-resume extraction.']
    if not _SLOT.acquire(blocking=False):
        return {}, ['Local OCR is busy. Review native text or retry extraction later.']
    texts, warnings = {}, []
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        if len(pages) > MAX_OCR_PAGES:
            warnings.append(f'OCR page limit reached; only the first {MAX_OCR_PAGES} scanned pages were processed.')
        with tempfile.TemporaryDirectory(prefix='mjobs-resume-ocr-') as directory:
            root = Path(directory)
            source = root/'source.pdf'
            source.write_bytes(data)
            for number in pages[:MAX_OCR_PAGES]:
                if time.monotonic() >= deadline:
                    warnings.append('OCR time limit reached; some scanned pages were not processed.')
                    break
                prefix, output = root/f'page-{number}', root/f'text-{number}'
                try:
                    _run_tool(renderer, ['-f', str(number), '-l', str(number), '-r', '150',
                        '-scale-to', str(MAX_DIMENSION), '-singlefile', '-png', str(source), str(prefix)],
                        min(PROCESS_TIMEOUT, max(0.01, deadline-time.monotonic())))
                    raster = prefix.with_suffix('.png')
                    if not raster.is_file():
                        warnings.append(f'OCR renderer did not create an image for page {number}. Native text remains available for review.')
                        continue
                    if raster.stat().st_size > MAX_RENDER_BYTES:
                        raise ValueError('render limit')
                    with Image.open(raster) as image:
                        if max(image.size) > MAX_DIMENSION or image.width * image.height > MAX_DIMENSION**2:
                            raise ValueError('pixel limit')
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired('ocr', TOTAL_TIMEOUT)
                    _run_tool(engine, [str(raster), str(output), '-l', 'eng', '--psm', '3'],
                              min(PROCESS_TIMEOUT, max(0.01, deadline-time.monotonic())))
                    text_output = output.with_suffix('.txt')
                    if not text_output.is_file():
                        warnings.append(f'OCR recognition did not create text for page {number}. Native text remains available for review.')
                        continue
                    with text_output.open('rb') as stream:
                        content = stream.read(MAX_PAGE_TEXT_BYTES+1)
                    if len(content) > MAX_PAGE_TEXT_BYTES:
                        warnings.append(f'OCR text limit reached on page {number}.')
                    text = content[:MAX_PAGE_TEXT_BYTES].decode('utf-8', errors='replace').strip()
                    if text:
                        texts[number] = text
                    else:
                        warnings.append(f'OCR found no readable text on page {number}.')
                except subprocess.TimeoutExpired:
                    warnings.append(f'OCR tool timed out on page {number}. Native text remains available for review.')
                except FileNotFoundError:
                    warnings.append(f'OCR tool could not start on page {number}. Native text remains available for review.')
                except OCRToolFailure as failure:
                    diagnostics = ' Diagnostic output was captured and omitted for privacy.' if failure.diagnostic_bytes else ''
                    warnings.append(f'OCR tool exited with status {failure.returncode} on page {number}.{diagnostics} Native text remains available for review.')
                except ValueError:
                    warnings.append(f'OCR output for page {number} exceeded its supported resource limits. Native text remains available for review.')
                except (OSError, subprocess.SubprocessError):
                    warnings.append(f'OCR could not read tool output for page {number}. Native text remains available for review.')
                finally:
                    prefix.with_suffix('.png').unlink(missing_ok=True)
                    output.with_suffix('.txt').unlink(missing_ok=True)
        return texts, warnings
    except OSError:
        return texts, warnings + ['Local OCR temporary storage is unavailable. Native text remains available for review.']
    finally:
        _SLOT.release()

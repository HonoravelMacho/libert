"""Download de arquivos com barra de progresso (rich)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://welib.org/",
}


def get_downloads_dir() -> Path:
    """Pasta de Downloads do sistema (Linux desktop e Android/Termux)."""
    home = Path.home()
    candidates = [
        home / "storage" / "downloads",
        home / "Downloads",
        Path("/sdcard/Download"),
    ]
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except OSError:
            continue
    try:
        (home / "Downloads").mkdir(parents=True, exist_ok=True)
        return home / "Downloads"
    except OSError:
        return Path.cwd()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    return name[:180] if len(name) > 180 else (name or "livro")


def sniff_ext(data: bytes) -> str | None:
    """Adivinha a extensão pelos magic bytes (evita salvar PDF como HTML)."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "epub"
    if head.startswith(b"BOOKMOBI") or head.startswith(b"TPZ"):
        return "mobi"
    if head.startswith(b"AT&TFORM"):
        return "djvu"
    if head.startswith(b"Rar!"):
        return "cbr"
    return None


def _looks_like_html(data: bytes) -> bool:
    head = data.lstrip()[:200].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<title"))


def filename_for(title: str, fmt: str, url: str) -> str:
    ext = fmt.lower().lstrip(".")
    parsed = urlparse(url)
    base = os.path.basename(unquote(parsed.path))
    if base and "." in base and len(base) < 120 and re.search(r"\.(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)$", base, re.I):
        return sanitize_filename(base)
    return sanitize_filename(f"{title}.{ext}")


def _fix_mojibake(name: str) -> str:
    """Content-Disposition às vezes chega como latin1 cortando UTF-8 (ANÃLISE)."""
    if any(c in name for c in ("Ã", "Â", "â€")):
        try:
            return name.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return name


def _filename_from_response(resp, fallback: str, first_chunk: bytes | None = None) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        cand = _fix_mojibake(unquote(m.group(1)))
        cand = sanitize_filename(cand)
        if cand and "." in cand:
            return cand
    parsed = urlparse(str(resp.url))
    base = os.path.basename(unquote(parsed.path))
    if base and re.search(r"\.(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)$", base, re.I) and len(base) < 150:
        return sanitize_filename(_fix_mojibake(base))
    if first_chunk:
        ext = sniff_ext(first_chunk)
        if ext:
            stem = fallback.rsplit(".", 1)[0] if "." in fallback else fallback
            return sanitize_filename(f"{stem}.{ext}")
    return fallback


def _final_path(dest: Path, final_name: str) -> Path:
    final_dest = dest.parent / final_name
    if final_dest.exists() and final_dest != dest:
        stem, suffix = final_dest.stem, final_dest.suffix
        i = 1
        while (dest.parent / f"{stem} ({i}){suffix}").exists():
            i += 1
        final_dest = dest.parent / f"{stem} ({i}){suffix}"
    return final_dest


def _stream_to_file(resp, first_chunk: bytes, dest: Path, downloaded: int = 0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = int(resp.headers.get("Content-Length", 0) or 0) + downloaded
    final_dest = _final_path(dest, _filename_from_response(resp, dest.name, first_chunk))
    existed_before = final_dest.exists()
    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task(f"[cyan]Baixando {final_dest.name}", total=total or None)
        try:
            with open(final_dest, "wb") as f:
                if first_chunk:
                    f.write(first_chunk)
                    downloaded += len(first_chunk)
                    progress.update(task, completed=downloaded)
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))
            if downloaded == 0:
                raise RuntimeError("Arquivo vazio do servidor.")
            return final_dest
        except Exception:
            if not existed_before and final_dest.exists():
                try:
                    final_dest.unlink()
                except OSError:
                    pass
            raise


CHUNK_SIZE = 1024 * 64


def download_file(url: str, dest: Path, referer: str | None = None, timeout: int = 60,
                  session=None) -> Path:
    """Baixa `url` para `dest` com barra de progresso. Valida não-HTML antes de salvar."""
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    opener = (session or requests).get
    resp = opener(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    return _validate_and_stream(resp, dest)


def _validate_and_stream(resp, dest: Path) -> Path:
    try:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Download bloqueado (HTTP {resp.status_code}). "
                "O welib/LibGen pode exigir login para esse arquivo."
            )
        if resp.status_code == 404:
            raise RuntimeError("Arquivo de download não encontrado (HTTP 404).")
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        if "text/html" in ctype:
            raise RuntimeError(
                "O servidor devolveu uma página HTML em vez do arquivo do livro "
                f"(Content-Type: {resp.headers.get('Content-Type')}). Nada foi salvo."
            )
        auto_iter = resp.iter_content(CHUNK_SIZE)
        first = next(auto_iter, b"")
        if not first:
            raise RuntimeError("Servidor devolveu arquivo vazio.")
        if _looks_like_html(first):
            raise RuntimeError(
                "O servidor devolveu uma página HTML em vez do arquivo do livro. Nada foi salvo."
            )
        return _stream_to_file(resp, first, dest)
    finally:
        pass


def download_response(resp, first_chunk: bytes, dest: Path) -> Path:
    """Continua a partir de uma resposta já aberta (o IPFS race ganha aqui)."""
    if _looks_like_html(first_chunk):
        resp.close()
        raise RuntimeError("Gateway IPFS devolveu HTML; gateway bloqueado.")
    return _stream_to_file(resp, first_chunk, dest)

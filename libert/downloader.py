"""Download de arquivos com barra de progresso (rich)."""

from __future__ import annotations

import os
import re
import time
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
    "Referer": "https://welib.st/",
}


def get_downloads_dir() -> Path:
    """Pasta de Downloads do sistema (Linux desktop e Android/Termux)."""
    home = Path.home()
    candidates = [
        home / "storage" / "downloads",  # Termux
        home / "Downloads",  # Linux desktop / Termux (novo)
        Path("/sdcard/Download"),  # Android alternativo
    ]
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except OSError:
            continue
    # Fallback: cria ~/Downloads; se impossível, usa o cwd.
    try:
        (home / "Downloads").mkdir(parents=True, exist_ok=True)
        return home / "Downloads"
    except OSError:
        cwd = Path.cwd()
        return cwd


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    return name[:180] if len(name) > 180 else (name or "livro")


def filename_for(title: str, fmt: str, url: str) -> str:
    # Tenta extrair nome do Content-Disposition/URL depois; aqui o palpite inicial.
    ext = fmt.lower().lstrip(".")
    parsed = urlparse(url)
    base = os.path.basename(unquote(parsed.path))
    if base and "." in base and len(base) < 120:
        return sanitize_filename(base)
    return sanitize_filename(f"{title}.{ext}")


def _filename_from_response(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        cand = sanitize_filename(unquote(m.group(1)))
        if cand:
            return cand
    # Tenta pela URL final (após redirects).
    parsed = urlparse(str(resp.url))
    base = os.path.basename(unquote(parsed.path))
    if base and "." in base and len(base) < 120 and "md5" not in base.lower():
        return sanitize_filename(base)
    return fallback


def _looks_like_html(data: bytes) -> bool:
    head = data.lstrip()[:200].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<title"))


def download_file(url: str, dest: Path, referer: str | None = None, timeout: int = 60) -> Path:
    """Baixa `url` para `dest` com barra de progresso rich.

    Mostra tamanho total, porcentagem e velocidade instantânea.
    Valida que o conteúdo é o arquivo real: se o servidor devolver
    HTML (página de login/espera/erro) em vez do livro, aborta SEM salvar
    nada e levanta RuntimeError explicativo.
    Retorna o caminho final (pode mudar via Content-Disposition).
    """
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    dest.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as resp:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Download bloqueado (HTTP {resp.status_code}). "
                "O welib pode exigir login/conta gratuita para downloads. "
                "Abra a URL no navegador e verifique."
            )
        if resp.status_code == 404:
            raise RuntimeError("Arquivo de download não encontrado (HTTP 404).")
        resp.raise_for_status()

        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        if "text/html" in ctype:
            raise RuntimeError(
                "O servidor devolveu uma página HTML em vez do arquivo do livro "
                f"(Content-Type: {resp.headers.get('Content-Type')}). "
                "Isso acontece quando o link é de uma página intermediária "
                "(espera/login) e não do arquivo direto. Nada foi salvo. "
                "Atualize o libert e tente de novo."
            )

        total = int(resp.headers.get("Content-Length", 0) or 0)
        final_name = _filename_from_response(resp, dest.name)
        final_dest = dest.parent / final_name
        # Evita sobrescrever: adiciona (1), (2)...
        if final_dest.exists() and final_dest != dest:
            stem, suffix = final_dest.stem, final_dest.suffix
            i = 1
            while (dest.parent / f"{stem} ({i}){suffix}").exists():
                i += 1
            final_dest = dest.parent / f"{stem} ({i}){suffix}"

        start = time.time()
        downloaded = 0
        first_chunk = True
        html_detected = False
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
                    for chunk in resp.iter_content(chunk_size=1024 * 64):
                        if not chunk:
                            continue
                        if first_chunk:
                            first_chunk = False
                            if _looks_like_html(chunk):
                                html_detected = True
                                raise RuntimeError(
                                    "O servidor devolveu uma página HTML em vez do arquivo do livro. "
                                    "Isso acontece quando o link é de uma página intermediária "
                                    "(espera/login) e não do arquivo direto. Nada foi salvo. "
                                    "Atualize o libert e tente de novo."
                                )
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(time.time() - start, 0.001)
                        progress.update(task, advance=len(chunk))
            except Exception:
                # Nunca deixa arquivo corrompido/HTML para trás (mas preserva
                # um arquivo pré-existente do usuário).
                if html_detected and not existed_before:
                    try:
                        if final_dest.exists():
                            final_dest.unlink()
                    except OSError:
                        pass
                raise
        return final_dest

"""Scraper das páginas do welib, com resolução instantânea (modo yt-dlp).

Estratégia:
1. Sessão cloudscraper aquecida (passa por Cloudflare sem esperar o browser).
2. Links `js-partner-download-link` (CDN direta) têm prioridade máxima.
3. Fallbacks: IPFS em gateways paralelos e libgen — quando o servidor
   principal não tem o arquivo.
4. Retry com backoff curto apenas quando Cloudflare devolve challenge;
   nenhum sleep fixo no caminho feliz.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

try:
    import cloudscraper as _cloudscraper
except ImportError:  # pragma: no cover
    _cloudscraper = None

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://welib.org/",
}

MD5_RE = re.compile(r"/md5/([a-fA-F0-9]{32})")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
PAGE_FMT_RE = re.compile(
    r"\(\s*([A-Za-z0-9]{2,5})\s*,\s*(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))\s*\)", re.I
)
VARIANT_RE = re.compile(r"href=[\"'](/(?:slow|fast)_download/[a-fA-F0-9]{32}/0/0(?:/convert/[a-z0-9]+)?)[\"']", re.I)
AUTO_RE = re.compile(r"href=[\"'](/auto_download/[a-fA-F0-9]{32}/0/0)[\"']", re.I)
IPFS_RE = re.compile(r"ipfs://([a-zA-Z0-9]+)")
LIBGEN_RE = re.compile(r"https?://[^\"'\s<>]*libgen[^\"'\s<>]*file\.php\?id=\d+", re.I)
PARTNER_RE = re.compile(r'<a[^>]+class=["\'][^"\']*js-partner-download-link[^"\']*["\'][^>]*>', re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
PARTNER_FMT_RE = re.compile(r"Download\s*\(?\s*\(?\s*(PDF|EPUB|MOBI|AZW3|DJVU|FB2|TXT)\s*\)", re.I)
CHALLENGE_MARKERS = ("just a moment", "challenge-platform", "_cf_chl_opt", "checking your browser")

KNOWN_EXTS = ["pdf", "epub", "mobi", "azw3", "djvu", "fb2", "txt", "doc", "docx", "cbr", "cbz"]

HOSTS = ["welib.org", "welib.st", "pt.welib.st", "en.welib.st", "es.welib.st"]


def is_challenge(html: str) -> bool:
    if not html:
        return False
    head = html[:8000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def new_session():
    """Sessão com bypass de Cloudflare quando cloudscraper está presente."""
    if _cloudscraper is not None:
        s = _cloudscraper.create_scraper()
    else:
        s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


@dataclass
class BookFormat:
    fmt: str
    size: str = ""
    url: str = ""


@dataclass
class BookInfo:
    title: str
    md5: str
    page_fmt: str = ""
    page_size: str = ""
    source_url: str = ""
    slow_paths: list[str] = field(default_factory=list)
    fast_paths: list[str] = field(default_factory=list)
    auto_paths: list[str] = field(default_factory=list)
    ipfs_cids: list[str] = field(default_factory=list)
    libgen_urls: list[str] = field(default_factory=list)
    base: str = "https://welib.org"
    extra: dict = field(default_factory=dict)


class Http:
    """GET com retry curto em challenge, trocando de espelho quando bloqueado."""

    def __init__(self, session, status_cb=None):
        self.s = session
        self.status_cb = status_cb
        self.challenges = 0
        self._lock = None
        try:
            import threading

            self._lock = threading.Lock()
        except Exception:
            self._lock = None

    def _cb(self, msg: str):
        if self.status_cb:
            self.status_cb(msg)

    def get(self, url: str, tries: int = 4, timeout: int = 30, referer: str | None = None, **kw):
        host = urlparse(url).netloc.lower()
        parsed = urlparse(url)
        last = None
        for attempt in range(tries):
            try:
                if self._lock:
                    with self._lock:
                        r = self.s.get(url, timeout=timeout, allow_redirects=True, headers=({"Referer": referer} if referer else None), **kw)
                else:
                    r = self.s.get(url, timeout=timeout, allow_redirects=True, headers=({"Referer": referer} if referer else None), **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(random.uniform(0.4, 1.0) * (attempt + 1))
                continue
            if is_challenge(r.text):
                self.challenges += 1
                self._cb("[!] Cloudflare: aguardando desafio passar...")
                time.sleep(random.uniform(0.8, 1.6) * (attempt + 1))
                # Troca de host para bater em outro painel do Cloudflare.
                for h in HOSTS:
                    if h != host:
                        url = f"{parsed.scheme}://{h}{parsed.path}"
                        host = h
                        break
                continue
            if r.status_code in (403, 429):
                self.challenges += 1
                time.sleep(random.uniform(1.0, 2.0) * (attempt + 1))
                continue
            if r.status_code < 500:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(random.uniform(0.6, 1.2))
        raise RuntimeError(str(last) or "Falha de rede ao acessar o welib")


def extract_md5(url: str) -> str | None:
    m = MD5_RE.search(url)
    return m.group(1).lower() if m else None


def fetch_page(http: Http, url: str, referer: str | None = None):
    r = http.get(url, referer=referer)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ao acessar {url}")
    return r.text, str(r.url)


def normalize_book_url(url: str) -> str:
    u = (url or "").strip()
    m = re.search(r"/(slow_download|fast_download|auto_download)/([a-fA-F0-9]{32})", u, re.IGNORECASE)
    if m:
        return f"https://welib.org/md5/{m.group(2).lower()}"
    return u


def _clean_title(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw or "").strip()
    raw = re.sub(r"\s*[-|–—]\s*WeLib.*$", "", raw, flags=re.IGNORECASE).strip()
    return raw


def parse_book_page(html: str, final_url: str) -> BookInfo:
    md5 = extract_md5(final_url) or (MD5_RE.search(html).group(1).lower() if MD5_RE.search(html) else "")
    title_m = TITLE_RE.search(html)
    title = _clean_title(title_m.group(1)) if title_m else "Livro sem título"

    fmt, size = "", ""
    fm = PAGE_FMT_RE.search(title)
    if fm and fm.group(1).lower() in KNOWN_EXTS:
        fmt, size = fm.group(1).upper(), fm.group(2).replace(",", ".").upper().strip()

    slow: list[str] = []
    fast: list[str] = []
    for uh in VARIANT_RE.findall(html):
        low = uh.lower()
        (fast if "/fast_download/" in low else slow).append(uh)

    auto = sorted(set(AUTO_RE.findall(html)))
    cids = sorted(set(IPFS_RE.findall(html)), key=lambda c: (not c.startswith("Qm"), c))[:4]
    libgen = sorted(set(LIBGEN_RE.findall(html)))

    md5_page_paths = {
        "slow": uh for uh in slow if md5 and md5 in uh
    }
    return BookInfo(
        title=title,
        md5=md5,
        page_fmt=fmt,
        page_size=size,
        source_url=final_url,
        slow_paths=slow,
        fast_paths=fast,
        auto_paths=auto,
        ipfs_cids=cids,
        libgen_urls=libgen,
        base=f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}",
        extra={"md5_page_paths": md5_page_paths},
    )


def get_book_info(url: str, http: Http) -> BookInfo:
    url = normalize_book_url(url)
    html, final_url = fetch_page(http, url)
    return parse_book_page(html, final_url)


def partner_link_from_page(html: str, base: str) -> tuple[str, str]:
    """Extrai (url_direta, formato) da página slow_download/fast_download."""
    m = PARTNER_RE.search(html)
    if not m:
        return "", ""
    tag = m.group(0)
    hm = HREF_RE.search(tag)
    if not hm:
        return "", ""
    url = urljoin(base, hm.group(1).strip())
    fm = PARTNER_FMT_RE.search(tag)
    fmt = fm.group(1) if fm else ""
    # O texto do link vem depois do <a>; procura no trecho seguinte do HTML.
    if not fmt:
        seg = html[m.end(): m.end() + 200]
        fm = PARTNER_FMT_RE.search(seg)
        fmt = fm.group(1) if fm else ""
    return url, fmt


def resolve_partner(http: Http, book: BookInfo, path: str) -> tuple[str, str, str]:
    """Abre uma página /slow_download|fast_download e devolve (url, formato, path_usada)."""
    page_url = f"{book.base}{path}"
    referer = f"{book.base}/md5/{book.md5}" if book.md5 else book.source_url
    r = http.get(page_url, referer=referer)
    if r.status_code != 200:
        return "", "", path
    url, fmt = partner_link_from_page(r.text, book.base)
    return url, fmt, path


def warmup(http: Http, first_url: str) -> None:
    """Aquece cookies do Cloudflare com a própria página pedida."""
    try:
        http.get(first_url)
    except RuntimeError:
        pass

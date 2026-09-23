"""Scraper para páginas de livros do welib (ex: https://pt.welib.st/md5/<hash>)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://welib.st/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "max-age=0",
}

MD5_RE = re.compile(r"/md5/([a-fA-F0-9]{32})")
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))", re.IGNORECASE)
# Formatos de e-book mais comuns no welib / Anna's Archive.
KNOWN_EXTS = ["pdf", "epub", "mobi", "azw3", "djvu", "fb2", "txt", "doc", "docx", "cbr", "cbz"]


@dataclass
class BookFormat:
    fmt: str  # ex: "PDF"
    size: str  # ex: "15.4 MB" (pode ser "" se desconhecido)
    url: str  # URL direta (ou intermediária) de download
    label: str = ""  # descrição extra (idioma, ano...)


@dataclass
class BookInfo:
    title: str
    formats: list[BookFormat]
    source_url: str
    md5: str = ""  # md5 do livro (extraído da URL /md5/<hash>)


def new_session() -> requests.Session:
    """Sessão HTTP com headers de navegador (mantém cookies entre páginas)."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def extract_md5(url: str) -> str | None:
    m = MD5_RE.search(url)
    return m.group(1).lower() if m else None


def candidate_urls(url: str) -> list[str]:
    """Gera URLs candidatas para contornar bloqueios/idiomas.

    Tenta a URL original primeiro e depois espelhos do mesmo md5.
    """
    md5 = extract_md5(url)
    if not md5:
        return [url]
    path = f"/md5/{md5}"
    cands = [url]
    for base in (
        "https://welib.st",
        "https://welib.org",
        "https://pt.welib.st",
        "https://en.welib.st",
        "https://es.welib.st",
    ):
        full = base + path
        if full not in cands:
            cands.append(full)
    return cands


def _http_get(url: str, session: requests.Session | None, timeout: int = 30, **kwargs):
    """GET via sessão (se dada) ou requests direto, sempre com headers de navegador."""
    if session is not None:
        return session.get(url, timeout=timeout, allow_redirects=True, **kwargs)
    return requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True, **kwargs)


def fetch_page(url: str, timeout: int = 30, session: requests.Session | None = None) -> tuple[str, str]:
    """Baixa o HTML da página do livro. Retorna (html, url_final).

    Tenta a URL original e espelhos. Levanta RuntimeError com mensagem
    amigável em caso de bloqueio (Cloudflare 403) ou 404.
    """
    last_err: Exception | None = None
    for cand in candidate_urls(url):
        try:
            resp = _http_get(cand, session, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code == 404:
            last_err = RuntimeError(f"Página não encontrada (404): {cand}")
            continue
        if resp.status_code == 403:
            # Provável desafio Cloudflare.
            last_err = RuntimeError(
                f"Acesso bloqueado (403) em {cand}. "
                "O welib usa proteção Cloudflare; tente novamente em alguns segundos, "
                "troque de rede ou use VPN."
            )
            continue
        if resp.status_code != 200:
            last_err = RuntimeError(f"HTTP {resp.status_code} ao acessar {cand}")
            continue
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and len(resp.text) < 500:
            last_err = RuntimeError(f"Resposta inesperada de {cand} (HTTP {resp.status_code})")
            continue
        return resp.text, str(resp.url)
    raise RuntimeError(str(last_err) if last_err else f"Não foi possível acessar {url}")


def _clean_title(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip()
    # Remove sufixos comuns: " - WeLib", " | WeLib", etc.
    raw = re.sub(r"\s*[-|–—]\s*WeLib.*$", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"\s*[-|–—]\s*welib.*$", "", raw, flags=re.IGNORECASE).strip()
    return raw


def extract_title(soup: BeautifulSoup) -> str:
    # 1. og:title
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        t = _clean_title(meta["content"])
        if t:
            return t
    # 2. h1
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        t = _clean_title(h1.get_text(" ", strip=True))
        if t and len(t) > 2:
            return t
    # 3. <title>
    if soup.title and soup.title.get_text(strip=True):
        t = _clean_title(soup.title.get_text(" ", strip=True))
        if t and t.lower() not in ("just a moment...", "welib", "welib.st"):
            return t
    # 4. primeiro elemento com cara de título
    for sel in ("div.text-2xl", "div.text-xl", "div.font-bold", "h2"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True) and len(el.get_text(strip=True)) > 3:
            return _clean_title(el.get_text(" ", strip=True))
    return "Livro sem título"


def _size_near(tag) -> str:
    """Procura tamanho (ex: 15.4 MB) no texto do link e no elemento pai."""
    texts = []
    try:
        texts.append(tag.get_text(" ", strip=True))
    except Exception:
        pass
    parent = getattr(tag, "parent", None)
    if parent is not None:
        try:
            texts.append(parent.get_text(" ", strip=True)[:500])
        except Exception:
            pass
    for t in texts:
        m = SIZE_RE.search(t or "")
        if m:
            return m.group(1).replace(",", ".").upper().strip()
    return ""


def _fmt_from_text(text: str) -> str | None:
    low = (text or "").lower()
    for ext in KNOWN_EXTS:
        if re.search(r"\b" + re.escape(ext) + r"\b", low):
            return ext.upper()
    return None


def _page_format_size(soup: BeautifulSoup) -> tuple[str, str]:
    """Infere (formato, tamanho) principal da página.

    Ex: <title>God's Festivals ... ( PDF, 1.2 MB ) - WeLib</title>
    ou filepath '.../livro.pdf' + 'pdf, 1.2 MB' no texto.
    """
    texts: list[str] = []
    if soup.title:
        texts.append(soup.title.get_text(" ", strip=True))
    h1 = soup.find("h1")
    if h1:
        texts.append(h1.get_text(" ", strip=True))
    # Cabeçalho com "( PDF, 1.2 MB )"
    for t in texts:
        m = re.search(
            r"\(\s*([A-Za-z0-9]{2,5})\s*,\s*(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))\s*\)",
            t,
            re.IGNORECASE,
        )
        if m and m.group(1).lower() in KNOWN_EXTS:
            return m.group(1).upper(), m.group(2).replace(",", ".").upper().strip()
    full = soup.get_text(" ", strip=True)
    # "pdf, 1.2 MB" perto do início (evita listas de livros relacionados).
    head = full[:4000]
    m = re.search(
        r"\b(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)\b\s*,\s*(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))",
        head,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), m.group(2).replace(",", ".").upper().strip()
    # filepath ... .pdf
    m = re.search(r"filepath:\S+\.(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)\b", full, re.IGNORECASE)
    if m:
        fmt = m.group(1).upper()
        ms = SIZE_RE.search(head)
        size = ms.group(1).replace(",", ".").upper().strip() if ms else ""
        return fmt, size
    return "", ""


def extract_formats(soup: BeautifulSoup, page_url: str) -> list[BookFormat]:
    """Extrai formatos de download da página do welib.

    Numa página /md5/* real, todas as opções de download ("All download
    options have the same file") correspondem ao MESMO arquivo: retorna
    1 formato. Se a página listar extensões distintas, retorna uma
    entrada por formato (para satisfazer o modo interativo da CLI).
    """
    IGNORE_PREFIX = (
        "/search", "/member_codes", "/datasets", "/account", "/donate",
        "/login", "/giftcards", "/torrents", "/member", "/db/",
    )

    def _ignored(href: str) -> bool:
        h = href.strip()
        if not h or h.startswith(("javascript:", "mailto:", "#")):
            return True
        low = h.lower()
        # .torrent não é formato de e-book.
        if low.endswith(".torrent") or "/torrents" in low:
            return True
        for p in IGNORE_PREFIX:
            if low.startswith(p):
                return True
        return False

    page_fmt, page_size = _page_format_size(soup)

    # Coleta links reais de download, na ordem de preferência.
    slow: list[str] = []
    auto: list[str] = []
    fast: list[str] = []
    libgen: list[str] = []
    ipfs: list[str] = []
    other: list[str] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if _ignored(href):
            continue
        low = href.lower()
        text = a.get_text(" ", strip=True)
        abs_url = urljoin(page_url, href)

        if low.startswith("ipfs://"):
            # Converte ipfs://CID -> https://ipfs.io/ipfs/CID
            cid = href[len("ipfs://"):].split("?")[0].split("#")[0]
            if cid:
                ipfs.append(f"https://ipfs.io/ipfs/{cid}")
            continue
        if "/slow_download/" in low:
            slow.append(abs_url)
            continue
        if "/auto_download/" in low:
            auto.append(abs_url)
            continue
        if "/fast_download/" in low:
            fast.append(abs_url)
            continue
        if "libgen" in low and "file.php" in low:
            libgen.append(abs_url)
            continue
        if "ipfs" in low and ("/ipfs/" in low or "gateway" in low):
            ipfs.append(abs_url)
            continue
        # Link direto para arquivo (.pdf/.epub/...), fora de /search.
        m = re.search(r"\.(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)(\?|#|$)", low)
        if m and ("/download" in low or "download" in text.lower() or low.startswith("https://")):
            other.append(abs_url)

    ordered_urls = slow + libgen + ipfs + auto + fast + other
    # Dedup preservando ordem.
    seen_u: set[str] = set()
    uniq_urls: list[str] = []
    for u in ordered_urls:
        if u not in seen_u:
            seen_u.add(u)
            uniq_urls.append(u)

    # Tenta detectar múltiplos formatos distintos na página
    # (ex: badges "PDF (15.4 MB)" / "EPUB (2.1 MB)" como links diferentes).
    multi: dict[str, tuple[str, str]] = {}  # fmt -> (size, url)
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if _ignored(href):
            continue
        low = href.lower()
        if not any(k in low for k in ("/slow_download/", "/auto_download/", "/fast_download/", "/ipfs/", "file.php", ".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".fb2")):
            continue
        text = a.get_text(" ", strip=True)
        fmt = _fmt_from_text(text)
        ext_in_href = re.search(r"\.(pdf|epub|mobi|azw3|djvu|fb2|txt|docx?|cbr|cbz)(\?|#|$)", low)
        if fmt and ("/download" in low or ext_in_href or "ipfs" in low or "file.php" in low):
            # Evita o falso positivo do link de busca "Search WeLib for filepath:..."
            # (esses já foram ignorados via IGNORE_PREFIX, mas garante aqui).
            size = _size_near(a)
            if fmt not in multi:
                multi[fmt] = (size, urljoin(page_url, href))

    if len(multi) > 1:
        out: list[BookFormat] = []
        for fmt, (size, u) in multi.items():
            out.append(BookFormat(fmt=fmt, size=size or page_size, url=u))
        return out

    # Caso comum: um único arquivo por página md5.
    if uniq_urls:
        fmt = page_fmt
        if not fmt:
            # Infere pela URL ou pelo texto geral (limitado ao head).
            for u in uniq_urls:
                f = _fmt_from_text(u)
                if f:
                    fmt = f
                    break
        if not fmt:
            head = soup.get_text(" ", strip=True)[:4000]
            fmt = _fmt_from_text(head) or "PDF"
        return [BookFormat(fmt=fmt.upper(), size=page_size, url=uniq_urls[0])]

    # Fallback sem links: menções textuais de formato + tamanho.
    full_text = soup.get_text(" ", strip=True)
    found: list[BookFormat] = []
    for m in re.finditer(
        r"([A-Za-z]{2,5}(?:\s*[·,|/]\s*[A-Za-z]{2,5})*)\s*[·,|]\s*[^·|]{0,60}?\s*(\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB))",
        full_text[:8000],
    ):
        fmts_raw = m.group(1)
        size = m.group(2).replace(",", ".").upper().strip()
        for part in re.split(r"[·,|/]", fmts_raw):
            f = part.strip().upper()
            if f.lower() in KNOWN_EXTS:
                found.append(BookFormat(fmt=f, size=size, url=page_url, label=m.group(0)[:120]))
        if found:
            break
    if found:
        dedup: dict[str, BookFormat] = {}
        for f in found:
            if f.fmt not in dedup:
                dedup[f.fmt] = f
        return list(dedup.values())

    if page_fmt:
        return [BookFormat(fmt=page_fmt, size=page_size, url=page_url)]
    return []


def get_book_info(url: str, session: requests.Session | None = None) -> BookInfo:
    html, final_url = fetch_page(url, session=session)
    soup = BeautifulSoup(html, "html.parser")

    # Detecta desafio Cloudflare que passou como 200 com "Just a moment".
    title_tag = (soup.title.get_text() if soup.title else "") or ""
    if "just a moment" in title_tag.lower() and len(html) < 30000 and "challenge-platform" in html:
        raise RuntimeError(
            "O welib bloqueou a requisição (desafio Cloudflare). "
            "Aguarde alguns segundos e tente novamente, ou acesse a URL no navegador primeiro."
        )

    title = extract_title(soup)
    formats = extract_formats(soup, final_url)
    if not formats:
        raise RuntimeError(
            f'Nenhum formato de download encontrado na página "{title}". '
            "A página pode exigir login ou estar protegida. "
            "Abra a URL no navegador para verificar os downloads disponíveis."
        )
    return BookInfo(title=title, formats=formats, source_url=final_url, md5=extract_md5(url) or "")


def _is_download_page_url(url: str) -> bool:
    low = (url or "").lower()
    return any(k in low for k in ("/slow_download/", "/fast_download/", "/auto_download/"))


# Sinais de bloqueio/rate-limit da Cloudflare (às vezes vêm com HTTP 200).
BLOCK_MARKERS = (
    "challenge-platform",
    "just a moment",
    "attention required",
    "captcha",
    "rate limit",
    "ratelimit",
    "too many requests",
    "error 1015",
    "error code: 1015",
    "please wait",
    "verifying you are human",
)


def _looks_blocked(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in BLOCK_MARKERS)


def _download_page_variants(url: str) -> list[str]:
    """Variações da mesma página de download em espelhos (para contornar 403)."""
    m = re.match(r"(https?://)([^/]+)(/.*)?$", url)
    if not m:
        return [url]
    scheme, host, path = m.group(1), m.group(2).lower(), m.group(3) or ""
    cands = [url]
    for h in ("welib.st", "welib.org", "pt.welib.st", "en.welib.st"):
        if h != host:
            cands.append(f"{scheme}{h}{path}")
    return cands


RATE_LIMIT_MSG = (
    "O welib limitou o ritmo das requisições (proteção anti-robôs). "
    "Aguarde 1-2 minutos e tente de novo — evite disparar vários downloads "
    "em sequência muito rápida. Se persistir, abra a URL no navegador primeiro."
)


def resolve_download_url(
    fmt: BookFormat,
    referer: str | None = None,
    session: requests.Session | None = None,
    expected_md5: str | None = None,
    timeout: int = 30,
) -> str:
    """Resolve a URL final e direta do arquivo.

    As páginas /slow_download/, /fast_download/ e /auto_download/ do welib
    são páginas HTML intermediárias — NÃO são o arquivo. Esta função abre a
    página e extrai o link direto do arquivo (ex: CDN assinado em
    ``a.js-partner-download-link``). URLs já diretas (CDN, libgen, IPFS)
    são devolvidas como estão.
    """
    import time as _time

    url = fmt.url
    if not _is_download_page_url(url):
        return url

    resp = None
    last_status: int | None = None
    for attempt, cand in enumerate(_download_page_variants(url)):
        headers = {"Referer": referer} if referer else None
        try:
            if session is not None:
                resp = session.get(cand, timeout=timeout, allow_redirects=True, headers=headers or {})
            else:
                resp = requests.get(cand, headers={**BROWSER_HEADERS, **(headers or {})},
                                    timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            if attempt == 0:
                raise RuntimeError(f"Falha ao abrir a página de download: {e}")
            continue  # tenta o próximo espelho
        last_status = resp.status_code
        if resp.status_code in (403, 429):
            _time.sleep(2 * (attempt + 1))  # backoff antes do próximo espelho
            continue
        break

    if resp is None:
        raise RuntimeError("Falha ao abrir a página de download (sem resposta).")
    if last_status == 404:
        raise RuntimeError("Página de download não encontrada (HTTP 404).")
    if last_status in (403, 429):
        raise RuntimeError(f"HTTP {last_status} na página de download. {RATE_LIMIT_MSG}")
    if last_status != 200:
        raise RuntimeError(f"HTTP {last_status} na página de download.")

    final = str(resp.url)
    if "/account" in final or "/login" in final:
        raise RuntimeError(
            "Este download exige login no welib (a página redirecionou para /account). "
            "Entre na sua conta gratuita pelo navegador e tente de novo, "
            "ou escolha a opção de download lento (slow)."
        )

    soup = BeautifulSoup(resp.text, "html.parser")

    # Garante que a página de download é do MESMO livro (evita baixar
    # outro título quando o link é de "livros relacionados").
    if expected_md5:
        m = re.search(r"data-resource=['\"]md5:([a-fA-F0-9]{32})", resp.text)
        if m and m.group(1).lower() != expected_md5.lower():
            raise RuntimeError(
                "O link de download não corresponde ao livro solicitado "
                f"(md5 {m.group(1)} ≠ {expected_md5}). Abortando para não baixar o arquivo errado."
            )

    # 1. Link direto oficial ("📚 Download now").
    a = soup.select_one("a.js-partner-download-link[href]")
    if a and a.get("href"):
        return urljoin(final, a["href"].strip())

    # 2. Fallback: primeiro link externo (fora do welib) com cara de arquivo.
    for cand in soup.find_all("a", href=True):
        href = (cand.get("href") or "").strip()
        if not href.lower().startswith("http"):
            continue
        if "welib" in href.lower():
            continue
        text = cand.get_text(" ", strip=True).lower()
        if any(k in href.lower() for k in (".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".fb2", "/ipfs/")) or "download" in text:
            return href

    # Sem link E com sinais de bloqueio: é rate-limit, não "layout mudou".
    if _looks_blocked(resp.text):
        raise RuntimeError(RATE_LIMIT_MSG)

    raise RuntimeError(
        "Não encontrei o link direto do arquivo na página de download. "
        "Se você baixou vários livros em sequência, pode ser o limite de ritmo "
        "do welib — aguarde 1-2 minutos e tente de novo. "
        "Se persistir, abra a página no navegador para baixar manualmente."
    )


def normalize_book_url(url: str, session: requests.Session | None = None, timeout: int = 30) -> str:
    """Normaliza a URL de entrada para a página do livro (/md5/<hash>).

    Aceita que o usuário cole direto uma URL de download
    (/slow_download/<md5>/..., /fast_download/<md5>/...): extrai o md5 e
    volta para a página do livro, onde título/formatos são detectados
    corretamente. Para /auto_download/ (sem md5 na URL), lê o md5 da página.
    URLs de livro passam intactas.
    """
    u = (url or "").strip()
    m = re.search(r"/(slow_download|fast_download)/([a-fA-F0-9]{32})", u, re.IGNORECASE)
    if m:
        host = re.match(r"(https?://[^/]+)", u)
        base = host.group(1) if host else "https://welib.st"
        return f"{base}/md5/{m.group(2).lower()}"
    if "/auto_download/" in u.lower():
        try:
            if session is not None:
                resp = session.get(u, timeout=timeout, allow_redirects=True)
            else:
                resp = requests.get(u, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                mm = re.search(r"data-resource=['\"]md5:([a-fA-F0-9]{32})", resp.text, re.IGNORECASE)
                if mm:
                    host = re.match(r"(https?://[^/]+)", u)
                    base = host.group(1) if host else "https://welib.st"
                    return f"{base}/md5/{mm.group(1).lower()}"
        except requests.RequestException:
            pass
    return u

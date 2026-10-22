"""Interface de linha de comando do libert — modo instantâneo (yt-dlp de livros).

Rotas de download, em ordem (a primeira que funcionar ganha):
  1. link partner (CDN direta) das páginas /slow_download|fast_download do
     md5 pedido — sempre validando no URL do CDN que o md5 é o do livro;
  2. race de gateways IPFS em paralelo;
  3. link direto do libgen.

Se o welib estiver com rate-limit, ciclos de espera automática (8s→20s)
re-resolvem tudo sem pedir ação do usuário.

Lote (batch) usa pipeline em background: enquanto o livro atual baixa,
o próximo já está sendo resolvido — nenhum segundo morto entre livros.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import sys
import time

from pathlib import Path

from libert import __version__
from libert.downloader import download_file, download_response, filename_for, get_downloads_dir
from libert.ipfs import race as ipfs_race
from libert.scraper import (
    BookInfo,
    Http,
    get_book_info,
    new_session,
    normalize_book_url,
    resolve_partner,
    warmup,
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="libert",
        description="Baixe livros do welib direto pelo terminal. Ex: libert <URL_DO_WELIB>",
    )
    p.add_argument("url", nargs="?", help="URL da página do livro no welib (ex: https://pt.welib.st/md5/...)")
    p.add_argument("-f", "--format", dest="fmt", default=None,
                   help="Formato desejado (ex: pdf, epub, mobi). Evita a pergunta interativa.")
    p.add_argument("-l", "--list", dest="list_file", default=None, metavar="ARQUIVO.txt",
                   help="Arquivo .txt com lista de URLs (uma por linha).")
    p.add_argument("-o", "--output-dir", dest="output_dir", default=None,
                   help="Pasta de destino (padrão: ~/Downloads).")
    p.add_argument("-V", "--version", action="store_true", help="Mostra a versão e sai.")
    return p


def is_valid_url(url: str) -> bool:
    u = url.strip()
    if not u or not u.startswith(("http://", "https://")):
        return False
    return "welib" in u or "/md5/" in u or bool(URL_RE.match(u))


# ----------------------------------------------------------- resolução


def _rank_slow_path(path: str, book: BookInfo, wanted_fmt: str | None) -> tuple:
    low = path.lower()
    md5_rank = 0 if (not book.md5 or book.md5 in low) else 2
    is_slow = 0 if "/slow_download/" in low else 1
    if "/convert/" in low:
        fmt_rank = 0 if wanted_fmt and f"/convert/{wanted_fmt}" in low else 2
    else:
        fmt_rank = 1 if wanted_fmt else 0
    return (md5_rank, is_slow, fmt_rank)


def _partner(book: BookInfo, path: str, http: Http):
    try:
        url, fmt, _ = resolve_partner(http, book, path)
    except Exception:
        return None
    return (url, fmt) if url else None


# ------------------------------------------------------- fluxo principal


def _attempt_routes(book: BookInfo, wanted: str | None, out_dir: Path,
                    http: Http, referer: str) -> Path | None:
    paths = sorted(set(book.slow_paths + book.fast_paths),
                   key=lambda p: _rank_slow_path(p, book, wanted))

    for path in paths:
        got = _partner(book, path, http)
        if not got:
            # Página 'public servers' sem partner = provável rate-limit:
            # não martela as outras rotas, deixa o ciclo externo esperar.
            break
        if book.md5 and book.md5 not in got[0]:
            # O CDN precisa referenciar o md5 pedido (protege de baixar
            # outro livro quando a página veio trocada/protegida).
            continue
        dest = out_dir / filename_for(book.title, book.page_fmt or got[1] or "pdf", got[0])
        print("[+] Link direto do arquivo resolvido.")
        try:
            return download_file(got[0], dest, referer=referer)
        except RuntimeError:
            pass
        # Token do CDN pode virar HTML/rate-limit: resolve um novo e tenta
        # mais uma vez sem referer antes de gastar a próxima rota.
        got = _partner(book, path, http)
        if got:
            try:
                return download_file(got[0], dest, referer=None)
            except RuntimeError:
                pass

    if book.ipfs_cids:
        winner = ipfs_race(book.ipfs_cids)
        if winner is not None:
            resp, first, gw = winner
            conv = next((p.rsplit("/", 1)[-1] for p in book.slow_paths if "/convert/" in p.lower()
                         and (not wanted or wanted in p.lower())),
                        book.page_fmt.lower() or "epub")
            name = filename_for(book.title, conv, "")
            print("[+] Gateway IPFS respondeu primeiro; baixando...")
            return download_response(resp, first, out_dir / name)

    for lurl in book.libgen_urls[:1]:
        try:
            return download_file(
                lurl,
                out_dir / filename_for(book.title, book.page_fmt or "pdf", lurl),
                referer=None,
            )
        except RuntimeError:
            continue

    return None


def download_book(url: str, fmt: str | None, out_dir: Path, http: Http) -> Path:
    book = get_book_info(url, http)
    print(f'[+] Livro: "{book.title}"')
    wanted = fmt.strip().lower().lstrip(".") if fmt else None
    referer = f"{book.base}/md5/{book.md5}" if book.md5 else book.source_url

    # Ciclo anti-espera: se todas as rotas vieram vazias (rate-limit do welib),
    # respira um pouco e resolve de novo — em vez de devolver erro ao usuário.
    cooldowns = (0, 8, 20)
    last_err = None
    for attempt, cd in enumerate(cooldowns):
        if cd:
            print(f"[~] Rate-limit: aguardando {cd}s antes de resolver de novo...")
            time.sleep(cd)
            try:
                # Novo fetch da página do livro: o estado do welib pode ter mudado.
                book = get_book_info(book.source_url, http)
                print(f'[+] Livro: "{book.title}"')
            except RuntimeError:
                pass
        try:
            final = _attempt_routes(book, wanted, out_dir, http, referer)
        except RuntimeError as e:
            final = None
            last_err = e
        if final:
            return final

    extra = []
    if book.ipfs_cids:
        extra.append("IPFS indisponível no momento")
    if book.libgen_urls:
        extra.append("libgen sem arquivo direto")
    if book.md5:
        extra.append("sem link partner")
    hint = "; ".join(extra) or "nenhuma rota"
    if last_err and "HTML" in str(last_err):
        raise RuntimeError(
            f'O servidor devolveu HTML em vez do livro: "{book.title}". '
            "O welib limitou o ritmo; rode de novo em alguns segundos."
        )
    raise RuntimeError(
        f'Nenhuma rota de download disponível para "{book.title}" ({hint}). '
        "O arquivo pode ter saído do ar; aguarde alguns minutos e rode de novo, "
        "ou abra a URL no navegador."
    )


def download_single(url: str, fmt: str | None = None,
                    output_dir: str | Path | None = None) -> Path:
    """Fluxo padrão de um único livro. Levanta exceção em falha."""
    url = url.strip()
    if not is_valid_url(url):
        raise ValueError(f"URL inválida: {url!r}. Esperado algo como: https://pt.welib.st/md5/<hash>")
    out_dir = Path(output_dir).expanduser() if output_dir else get_downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    http = Http(new_session())
    warmup(http, normalize_book_url(url))
    t0 = time.time()
    final = download_book(url, fmt, out_dir, http)
    print(f"[+] Concluído em {time.time() - t0:.1f}s → {final}")
    return final


# --------------------------------------------------------- lote (batch)


def process_batch(urls: list[str], fmt: str | None = None,
                  output_dir: str | Path | None = None) -> tuple[int, int]:
    """Pipeline: enquanto baixa o livro atual, o próximo já está sendo resolvido."""
    total = len(urls)
    ok = 0
    fail = 0
    out_dir = Path(output_dir).expanduser() if output_dir else get_downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    http = Http(new_session())

    def pre(u: str):
        u = normalize_book_url(u)
        return get_book_info(u, http)

    try:
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(pre, urls[0])
            next_fut = None
            for i, url in enumerate(urls, start=1):
                safe = url.strip()
                print(f"[{i}/{total}] {safe[:90]}{'...' if len(safe) > 90 else ''}")
                prefetch_err = None
                next_fut = None
                try:
                    book = fut.result()
                    if i < total:
                        next_fut = pool.submit(pre, urls[i].strip())
                except Exception as e:
                    prefetch_err = e
                    book = None
                if book is None:
                    msg = str(prefetch_err) or type(prefetch_err).__name__
                    print(f"[!] Prefetch falhou ({msg}); resolvendo sincronamente...", file=sys.stderr)
                    try:
                        book = pre(safe)
                        if i < total:
                            next_fut = pool.submit(pre, urls[i].strip())
                    except Exception as e2:
                        msg2 = str(e2) or type(e2).__name__
                        print(f"[!] Falha em {safe[:80]}: {msg2}", file=sys.stderr)
                        fail += 1
                        if i < total:
                            next_fut = pool.submit(pre, urls[i].strip())
                        continue
                challenges_before = http.challenges
                try:
                    t0 = time.time()
                    final = download_book(book.source_url, fmt, out_dir, http)
                    ok += 1
                    print(f"[+] Concluído em {time.time() - t0:.1f}s → {final.name}")
                except (KeyboardInterrupt, Exception) as e:
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    msg = str(e) or type(e).__name__
                    print(f"[!] Falha: {msg}", file=sys.stderr)
                    fail += 1
                if next_fut is not None:
                    fut = next_fut
                if http.challenges > challenges_before:
                    # Cloudflare reclamou neste livro: pausa curta para o próximo.
                    time.sleep(1.5)
            print("-" * 50)
    except KeyboardInterrupt:
        print("\n[!] Lote interrompido pelo usuário.", file=sys.stderr)
    print(f"[+] Resumo: {ok} baixado(s) com sucesso, {fail} falha(s) de {total}.")
    return ok, fail


# --------------------------------------------------------- modo texto


def parse_txt_file(path: str | Path) -> list[str]:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f'Arquivo não encontrado: {p} (rode no diretório do arquivo ou informe o caminho completo)')
    if not p.is_file():
        raise ValueError(f'Não é um arquivo: {p}')
    urls: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = URL_RE.search(s)
        cand = m.group(0).rstrip(".,;)'\"") if m else s
        if is_valid_url(cand):
            urls.append(cand)
        else:
            print(f"[!] Ignorando linha inválida em {p.name}: {s[:80]}", file=sys.stderr)
    return urls


def read_pasted_urls() -> list[str]:
    print("Cole os links do welib (um por linha).")
    print("Pressione Enter em uma linha vazia para iniciar o download.")
    print("-" * 50)
    urls: list[str] = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        s = line.strip()
        if not s:
            break
        m = URL_RE.search(s)
        cand = m.group(0).rstrip(".,;)'\"") if m else s
        if is_valid_url(cand):
            urls.append(cand)
        else:
            print(f"[!] Link ignorado (inválido): {s[:80]}", file=sys.stderr)
    return urls


# ----------------------------------------------------------- menu


MENU_TEXT = """=== libert - Gerenciador de Downloads do welib ===

  [1] Baixar um único livro (inserir URL)
  [2] Baixar múltiplos livros (colar lista de URLs)
  [3] Baixar a partir de arquivo de texto (ex: links.txt)
  [0] Sair
"""


def run_menu(fmt: str | None = None, output_dir: str | Path | None = None) -> int:
    while True:
        print(MENU_TEXT)
        try:
            choice = input("Escolha uma opção (0-3): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Operação cancelada.", file=sys.stderr)
            return 130
        if choice == "0":
            print("Até logo!")
            return 0
        elif choice == "1":
            try:
                url = input("Cole a URL do livro no welib: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[!] Operação cancelada.", file=sys.stderr)
                return 130
            if not url:
                continue
            try:
                download_single(url, fmt=fmt, output_dir=output_dir)
                return 0
            except KeyboardInterrupt:
                print("\n[!] Operação cancelada.", file=sys.stderr)
                return 130
            except Exception as e:
                print(f"[!] Erro: {e}", file=sys.stderr)
                return 1
        elif choice in ("2", "3"):
            try:
                if choice == "2":
                    urls = read_pasted_urls()
                else:
                    given = input("Arquivo .txt com os links [links.txt]: ").strip()
                    urls = parse_txt_file(given or "links.txt")
            except (FileNotFoundError, ValueError, EOFError, KeyboardInterrupt) as e:
                print(f"[!] Erro: {e}", file=sys.stderr)
                continue
            if not urls:
                print("[!] Nenhum link válido informado.", file=sys.stderr)
                continue
            ok, fail = process_batch(urls, fmt=fmt, output_dir=output_dir)
            return 0 if fail == 0 else 1
        else:
            print("[!] Opção inválida. Escolha entre 0 e 3.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"libert {__version__}")
        return 0

    if args.list_file:
        try:
            urls = parse_txt_file(args.list_file)
        except (FileNotFoundError, ValueError) as e:
            print(f"[!] Erro: {e}", file=sys.stderr)
            return 1
        if not urls:
            print(f"[!] Nenhuma URL válida encontrada em {args.list_file}.", file=sys.stderr)
            return 1
        ok, fail = process_batch(urls, fmt=args.fmt, output_dir=args.output_dir)
        return 0 if fail == 0 else 1

    if not args.url:
        return run_menu(fmt=args.fmt, output_dir=args.output_dir)

    try:
        download_single(args.url, fmt=args.fmt, output_dir=args.output_dir)
        return 0
    except KeyboardInterrupt:
        print("\n[!] Operação cancelada.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as e:
        print(f"[!] Erro: {e}", file=sys.stderr)
        if "Formato" in str(e):
            return 2
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

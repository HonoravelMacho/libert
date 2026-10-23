"""Interface de linha de comando do libert — modo instantâneo (yt-dlp de livros).

Rotas de download, em ordem (a primeira que funcionar ganha):
  1. link partner (CDN direta) das páginas /slow_download|fast_download do
     md5 pedido — validando no URL do CDN que o md5 é o do livro;
  2. race de gateways IPFS em paralelo;
  3. link direto do libgen.

O modo padrão é paciente: se o welib/Cloudflare limitar o ritmo, o libert
mostra uma barra "⏳ Aguardando" e cicla (5s→45s) até o site liberar
(--max-wait controla o teto; padrão 300s).

Lote (batch):
  - 1 worker (padrão): pipeline — enquanto baixa o livro atual, o próximo
    já está sendo resolvido em background.
  - --jobs N: baixa N livros ao mesmo tempo com barra compartilhada
    (RatePacer mantém as requisições bem-comportadas).
  - --cookies cookies.txt: resolve Cloudflare na rede do Termux.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import sys
import time
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from libert import __version__
from libert.downloader import download_file, download_response, filename_for, get_downloads_dir
from libert.ipfs import race as ipfs_race
from libert.scraper import (
    BookInfo,
    Http,
    RatePacer,
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
    p.add_argument("--cookies", dest="cookies", default=None, metavar="cookies.txt",
                   help="Cookie jar (Netscape ou nome=valor) do navegador com sessão "
                        "aberta no welib — resolve bloqueios de Cloudflare no Termux.")
    p.add_argument("--jobs", dest="jobs", default=1, type=int, metavar="N",
                   help="Downloads em paralelo no modo lote (padrão: 1). 2-3 é seguro; "
                        "mais pode acionar o limite do welib.")
    p.add_argument("-w", "--max-wait", dest="max_wait", default=300, type=int, metavar="SEG",
                   help="Quanto tempo o libert fica ciclando 'Aguardando' ante "
                        "rate-limit/Cloudflare (padrão: 300s; 0 = desiste já).")
    return p


def is_valid_url(url: str) -> bool:
    u = url.strip()
    if not u or not u.startswith(("http://", "https://")):
        return False
    return "welib" in u or "/md5/" in u or bool(URL_RE.match(u))


def _countdown(seconds: float, label: str = "Aguardando liberação...", progress=None):
    """Barra de espera '⏳ Aguardando ...' (a pedido: nada de 'eta' seco).

    Com `progress` compartilhado (modo --jobs), cria uma task temporária
    nele; sem, abre a própria barra. Thread-safe o suficiente via locks do
    rich; removida ao fim para não sujar o painel.
    """
    if seconds <= 0:
        return
    if progress is not None:
        try:
            tmp = progress.add_task(label, total=seconds)
        except Exception:
            tmp = None
        start = time.time()
        try:
            while True:
                waited = time.time() - start
                if waited >= seconds:
                    break
                time.sleep(0.5)
                if tmp is not None:
                    progress.update(tmp, completed=min(seconds, time.time() - start))
        finally:
            if tmp is not None:
                try:
                    progress.remove_task(tmp)
                except Exception:
                    pass
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[yellow]⏳ [bold]{task.description}[/bold]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    ) as prog:
        task2 = prog.add_task(label, total=seconds)
        start = time.time()
        while True:
            waited = time.time() - start
            if waited >= seconds:
                break
            time.sleep(0.5)
            prog.update(task2, completed=min(seconds, time.time() - start))


def _cooldown_schedule(max_wait: int) -> list[int]:
    """[0, 5, 10, 20, 30, 45, 45, ...] até consumir o orçamento total."""
    steps = [0, 5, 10, 20, 30, 45]
    out: list[int] = []
    elapsed = 0
    if max_wait <= 0:
        return [0]
    for step in steps:
        out.append(step)
        elapsed += step
        if elapsed >= max_wait:
            break
    while elapsed < max_wait:
        out.append(45)
        elapsed += 45
    return out


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
                    http: Http, referer: str,
                    report=None, progress=None, task=None) -> Path | None:
    def say(msg: str):
        if report:
            report(msg)
        else:
            print(msg)

    paths = sorted(set(book.slow_paths + book.fast_paths),
                   key=lambda p: _rank_slow_path(p, book, wanted))

    for path in paths:
        got = _partner(book, path, http)
        if not got:
            break
        if book.md5 and book.md5 not in got[0]:
            continue
        dest = out_dir / filename_for(book.title, book.page_fmt or got[1] or "pdf", got[0])
        say("[+] Link direto do arquivo resolvido.")
        try:
            return download_file(got[0], dest, referer=referer, progress=progress, task=task)
        except RuntimeError:
            pass
        got = _partner(book, path, http)
        if got:
            try:
                return download_file(got[0], dest, referer=None, progress=progress, task=task)
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
            say("[+] Gateway IPFS respondeu primeiro; baixando...")
            return download_response(resp, first, out_dir / name, progress=progress, task=task)

    for lurl in book.libgen_urls[:1]:
        try:
            return download_file(
                lurl,
                out_dir / filename_for(book.title, book.page_fmt or "pdf", lurl),
                referer=None,
                progress=progress,
                task=task,
            )
        except RuntimeError:
            continue

    return None


def download_book(url: str, fmt: str | None, out_dir: Path, http: Http,
                  max_wait: int = 300,
                  report=None, progress=None, task=None, book: BookInfo | None = None) -> Path:
    """Resolve+baixa com paciência: cicla 'Aguardando' até max_wait esgotar."""
    say = report if report else (lambda m: print(m))

    if book is None:
        book = get_book_info(normalize_book_url(url), http)
        say(f'[+] Livro: "{book.title}"')
    wanted = fmt.strip().lower().lstrip(".") if fmt else None
    referer = f"{book.base}/md5/{book.md5}" if book.md5 else book.source_url

    schedule = _cooldown_schedule(max_wait)
    last_err: Exception | None = None
    t_start = time.time()
    spent = 0.0
    for cd in schedule:
        remaining = max_wait - spent
        if cd > remaining:
            cd = int(max(0, remaining))
        if cd > 0:
            _countdown(cd, "Aguardando o welib liberar o download...", progress=progress)
            spent += cd
            try:
                # Novo fetch da página do livro: o estado do welib muda com o tempo.
                book = get_book_info(book.source_url, http)
                say(f'[+] Livro: "{book.title}"')
            except RuntimeError:
                pass
        try:
            final = _attempt_routes(book, wanted, out_dir, http, referer,
                                    report=say, progress=progress, task=task)
        except RuntimeError as e:
            final = None
            last_err = e
        if final:
            say(f"[+] Concluído em {int(time.time() - t_start)}s")
            return final
        spent = time.time() - t_start
        if spent >= max_wait:
            break
    return _raise_no_route(book, last_err)


def _raise_no_route(book: BookInfo, last_err: Exception | None):
    extra = []
    if last_err and "HTML" in str(last_err):
        extra.append("servidor devolveu HTML em vez do livro")
    if book.ipfs_cids:
        extra.append("IPFS indisponível no momento")
    if book.libgen_urls:
        extra.append("libgen sem arquivo direto")
    if book.md5:
        extra.append("sem link partner / Cloudflare desafiou todas")
    hint = "; ".join(extra) or "nenhuma rota"
    raise RuntimeError(
        f'Nenhuma rota de download para "{book.title}" ({hint}). '
        "Tente --cookies cookies.txt (sessão do navegador) ou rode mais tarde.",
    )


def download_single(url: str, fmt: str | None = None,
                    output_dir: str | Path | None = None,
                    cookies_file: str | None = None,
                    max_wait: int = 300) -> Path:
    url = url.strip()
    if not is_valid_url(url):
        raise ValueError(f"URL inválida: {url!r}. Esperado algo como: https://pt.welib.st/md5/<hash>")
    out_dir = Path(output_dir).expanduser() if output_dir else get_downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    http = Http(new_session(cookies_file))
    warmup(http, normalize_book_url(url))
    return download_book(url, fmt, out_dir, http, max_wait=max_wait)


# --------------------------------------------------------- lote (batch)


def _book_job(idx: int, url: str, fmt: str | None, out_dir: Path,
              cookies_file: str | None, max_wait: int, pace: RatePacer,
              progress: Progress):
    """Trabalhador do modo paralelo: resolve+baixa com barra compartilhada."""
    task = progress.add_task(f"#{idx} Resolvendo", total=None)

    def say(m):
        progress.console.print(f"#{idx} {m}")

    def phase(desc: str):
        progress.update(task, description=f"#{idx} {desc}")

    scope = normalize_book_url(url)
    http = Http(new_session(cookies_file), pacer=pace)

    deadline = time.time() + max(max_wait, 45)
    try:
        while True:
            try:
                book = get_book_info(scope, http)
                phase(book.title[:44])
                break
            except RuntimeError:
                if time.time() >= deadline:
                    raise
                phase("Aguardando welib liberar (Cloudflare)...")
                _countdown(min(30, max(10, int(deadline - time.time()))),
                           "Aguardando liberação...", progress=progress)
        final = download_book(scope, fmt, out_dir, http, max_wait=max_wait,
                              report=lambda m: say(m),
                              progress=progress, task=task, book=book)
        progress.update(task, description=f"#{idx} ✓ {Path(final).name}")
        return final
    except Exception as e:
        msg = str(e) or type(e).__name__
        phase(f"✗ Falha: {msg[:60]}")
        progress.console.print(f"[red]#{idx} Falha:[/red] {msg}")
        return None


def process_batch(urls: list[str], fmt: str | None = None,
                  output_dir: str | Path | None = None,
                  cookies_file: str | None = None,
                  jobs: int = 1, max_wait: int = 300) -> tuple[int, int]:
    total = len(urls)
    out_dir = Path(output_dir).expanduser() if output_dir else get_downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0

    if jobs > 1:
        pace = RatePacer(min_interval=3.0)
        columns = [
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        ]
        with Progress(*columns) as progress:
            with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = [pool.submit(_book_job, i, u.strip(), fmt, out_dir,
                                    cookies_file, max_wait, pace, progress)
                        for i, u in enumerate(urls, start=1)]
                results = [f.result() for f in futs]
        ok = sum(1 for r in results if r is not None)
        fail = total - ok
    else:
        http = Http(new_session(cookies_file))
        pace = None

        def pre(u: str):
            return get_book_info(normalize_book_url(u), http)

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
                        print(f"[!] Prefetch falhou ({msg}); resolvendo sincronamente...",
                              file=sys.stderr)
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
                        print(f'[+] Livro: "{book.title}"')
                        final = download_book(book.source_url, fmt, out_dir, http,
                                              max_wait=max_wait, book=book)
                        ok += 1
                        print(f"[+] Arquivo salvo: {final.name}")
                    except (KeyboardInterrupt, Exception) as e:
                        if isinstance(e, KeyboardInterrupt):
                            raise
                        msg = str(e) or type(e).__name__
                        print(f"[!] Falha: {msg}", file=sys.stderr)
                        fail += 1
                    if next_fut is not None:
                        fut = next_fut
                    if http.challenges > challenges_before:
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


def run_menu(fmt: str | None = None, output_dir: str | Path | None = None,
             cookies: str | None = None, jobs: int = 1, max_wait: int = 300) -> int:
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
                download_single(url, fmt=fmt, output_dir=output_dir,
                                cookies_file=cookies, max_wait=max_wait)
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
            ok, fail = process_batch(urls, fmt=fmt, output_dir=output_dir,
                                     cookies_file=cookies, jobs=jobs, max_wait=max_wait)
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
        ok, fail = process_batch(urls, fmt=args.fmt, output_dir=args.output_dir,
                                 cookies_file=args.cookies, jobs=args.jobs,
                                 max_wait=args.max_wait)
        return 0 if fail == 0 else 1

    if not args.url:
        return run_menu(fmt=args.fmt, output_dir=args.output_dir, cookies=args.cookies,
                        jobs=args.jobs, max_wait=args.max_wait)

    try:
        download_single(args.url, fmt=args.fmt, output_dir=args.output_dir,
                        cookies_file=args.cookies, max_wait=args.max_wait)
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

"""Interface de linha de comando do libert."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from libert import __version__
from libert.downloader import download_file, filename_for, get_downloads_dir
from libert.scraper import get_book_info, new_session, resolve_download_url

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="libert",
        description="Baixe livros do welib direto pelo terminal. Ex: libert <URL_DO_WELIB>",
    )
    p.add_argument("url", nargs="?", help="URL da página do livro no welib (ex: https://pt.welib.st/md5/...)")
    p.add_argument(
        "-f",
        "--format",
        dest="fmt",
        default=None,
        help="Formato desejado (ex: pdf, epub, mobi). Evita a pergunta interativa.",
    )
    p.add_argument(
        "-l",
        "--list",
        dest="list_file",
        default=None,
        metavar="ARQUIVO.txt",
        help="Arquivo .txt com lista de URLs (uma por linha). Processa em lote sem abrir o menu.",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        default=None,
        help="Pasta de destino (padrão: ~/Downloads).",
    )
    p.add_argument(
        "-V", "--version", action="store_true", help="Mostra a versão e sai."
    )
    return p


def is_valid_url(url: str) -> bool:
    u = url.strip()
    if not u or not u.startswith(("http://", "https://")):
        return False
    return "welib" in u or "/md5/" in u or bool(URL_RE.match(u))


def choose_format(formats, wanted: str | None):
    if wanted:
        w = wanted.strip().lower().lstrip(".")
        for f in formats:
            if f.fmt.lower() == w:
                return f
        validos = ", ".join(f.fmt for f in formats)
        raise ValueError(f'Formato "{wanted}" não disponível para este livro. Formatos válidos: {validos}')
    if len(formats) == 1:
        return formats[0]
    # Múltiplos formatos: pergunta interativa.
    n = len(formats)
    while True:
        try:
            ans = input(f"Escolha o formato desejado (1-{n}): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if ans.isdigit() and 1 <= int(ans) <= n:
            return formats[int(ans) - 1]
        print(f"[!] Escolha inválida. Digite um número entre 1 e {n}.", file=sys.stderr)


def download_single(
    url: str,
    fmt: str | None = None,
    output_dir: str | Path | None = None,
    session=None,
) -> Path:
    """Fluxo padrão de um único livro. Retorna o caminho baixado. Levanta exceção em falha."""
    url = url.strip()
    if not is_valid_url(url):
        raise ValueError(f"URL inválida: {url!r}. Esperado algo como: https://pt.welib.st/md5/<hash>")

    session = session or new_session()

    print("[+] Conectando ao welib...")
    book = get_book_info(url, session=session)

    print(f'[+] Livro encontrado: "{book.title}"')
    print("[+] Formatos disponíveis:")
    for i, f in enumerate(book.formats, start=1):
        if f.size:
            print(f"  [{i}] {f.fmt}  ({f.size})")
        else:
            print(f"  [{i}] {f.fmt}")

    try:
        chosen = choose_format(book.formats, fmt)
    except ValueError as e:
        print(f"[!] Erro: {e}", file=sys.stderr)
        raise

    print("[+] Resolvendo link direto do arquivo...")
    direct_url = resolve_download_url(
        chosen, referer=book.source_url, session=session, expected_md5=book.md5 or None
    )
    out_dir = Path(output_dir).expanduser() if output_dir else get_downloads_dir()
    dest = out_dir / filename_for(book.title, chosen.fmt, direct_url)
    final = download_file(direct_url, dest, referer=book.source_url)
    print(f"[+] Download concluído: {final}")
    return final


def parse_txt_file(path: str | Path) -> list[str]:
    """Lê URLs válidas de um .txt, ignorando vazias e comentários '#'."""
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
        # Permite linhas com texto extra: extrai a primeira URL http.
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


def process_batch(urls: list[str], fmt: str | None = None, output_dir: str | Path | None = None) -> tuple[int, int]:
    """Baixa N livros em sequência. Retorna (sucessos, falhas)."""
    total = len(urls)
    ok = 0
    fail = 0
    session = new_session()  # cookies reutilizados entre os livros do lote
    for i, url in enumerate(urls, start=1):
        print(f'[{i}/{total}] Processando: {url.strip()[:100]}...')
        try:
            download_single(url, fmt=fmt, output_dir=output_dir, session=session)
            ok += 1
        except KeyboardInterrupt:
            print("\n[!] Lote interrompido pelo usuário.", file=sys.stderr)
            fail += total - ok - (fail)
            break
        except SystemExit as e:
            # choose_format antigo usava sys.exit; compat: conta como falha.
            print(f"[!] Falha em {url}: escolha de formato inválida.", file=sys.stderr)
            fail += 1
        except Exception as e:
            print(f"[!] Falha em {url}: {e}", file=sys.stderr)
            fail += 1
    print("-" * 50)
    print(f"[+] Resumo: {ok} baixado(s) com sucesso, {fail} falha(s) de {total}.")
    return ok, fail


# ---------------- Menu interativo ----------------

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
                print("[!] URL vazia. Tente novamente.", file=sys.stderr)
                continue
            try:
                download_single(url, fmt=fmt, output_dir=output_dir)
            except KeyboardInterrupt:
                print("\n[!] Operação cancelada.", file=sys.stderr)
                return 130
            except SystemExit as e:
                return int(getattr(e, "code", 1) or 0)
            except Exception as e:
                print(f"[!] Erro: {e}", file=sys.stderr)
                return 1
            return 0
        elif choice == "2":
            urls = read_pasted_urls()
            if not urls:
                print("[!] Nenhum link válido informado.", file=sys.stderr)
                continue
            ok, fail = process_batch(urls, fmt=fmt, output_dir=output_dir)
            return 0 if fail == 0 else 1
        elif choice == "3":
            try:
                given = input("Arquivo .txt com os links [links.txt]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[!] Operação cancelada.", file=sys.stderr)
                return 130
            # Padrão: links.txt no diretório atual (onde o terminal está aberto).
            txt_path = given or "links.txt"
            try:
                urls = parse_txt_file(txt_path)
            except (FileNotFoundError, ValueError) as e:
                print(f"[!] Erro: {e}", file=sys.stderr)
                continue
            if not urls:
                print(f"[!] Nenhuma URL válida encontrada em {txt_path}.", file=sys.stderr)
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

    # Modo lote direto via arquivo .txt (sem menu).
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

    # Sem URL e sem flags: abre o menu principal.
    if not args.url:
        return run_menu(fmt=args.fmt, output_dir=args.output_dir)

    # Modo padrão: um único link (compatível com v0.1).
    try:
        download_single(args.url, fmt=args.fmt, output_dir=args.output_dir)
        return 0
    except KeyboardInterrupt:
        print("\n[!] Operação cancelada.", file=sys.stderr)
        return 130
    except SystemExit as e:
        return int(getattr(e, "code", 1) or 0)
    except (ValueError, RuntimeError) as e:
        print(f"[!] Erro: {e}", file=sys.stderr)
        # Formato inválido via -f deve sair com código 2 (compat v0.1).
        if "Formato" in str(e):
            return 2
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

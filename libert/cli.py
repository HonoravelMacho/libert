"""Interface de linha de comando do libert."""

from __future__ import annotations

import argparse
import sys

from libert import __version__
from libert.downloader import download_file, filename_for, get_downloads_dir
from libert.scraper import get_book_info, resolve_download_url


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


def choose_format(formats, wanted: str | None):
    if wanted:
        w = wanted.strip().lower().lstrip(".")
        for f in formats:
            if f.fmt.lower() == w:
                return f
        validos = ", ".join(f.fmt for f in formats)
        print(f'[!] Formato "{wanted}" não disponível para este livro. Formatos válidos: {validos}', file=sys.stderr)
        sys.exit(2)
    if len(formats) == 1:
        return formats[0]
    # Múltiplos formatos: pergunta interativa.
    n = len(formats)
    while True:
        try:
            ans = input(f"Escolha o formato desejado (1-{n}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Operação cancelada.", file=sys.stderr)
            sys.exit(130)
        if ans.isdigit() and 1 <= int(ans) <= n:
            return formats[int(ans) - 1]
        print(f"[!] Escolha inválida. Digite um número entre 1 e {n}.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"libert {__version__}")
        return 0

    if not args.url:
        parser.print_usage(sys.stderr)
        print("Erro: informe a URL do livro. Ex: libert https://pt.welib.st/md5/<hash>", file=sys.stderr)
        return 2

    url = args.url.strip()
    if "welib" not in url and "/md5/" not in url:
        print("[!] URL inválida. Esperado algo como: https://pt.welib.st/md5/<hash>", file=sys.stderr)
        return 2

    print("[+] Conectando ao welib...")
    try:
        book = get_book_info(url)
    except RuntimeError as e:
        print(f"[!] Erro: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[!] Operação cancelada.", file=sys.stderr)
        return 130

    print(f'[+] Livro encontrado: "{book.title}"')
    print("[+] Formatos disponíveis:")
    for i, f in enumerate(book.formats, start=1):
        if f.size:
            print(f"  [{i}] {f.fmt}  ({f.size})")
        else:
            print(f"  [{i}] {f.fmt}")

    chosen = choose_format(book.formats, args.fmt)

    from pathlib import Path

    out_dir = Path(args.output_dir).expanduser() if args.output_dir else get_downloads_dir()
    dest = out_dir / filename_for(book.title, chosen.fmt, resolve_download_url(chosen))

    try:
        final = download_file(resolve_download_url(chosen), dest, referer=book.source_url)
    except RuntimeError as e:
        print(f"[!] Erro no download: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[!] Download cancelado.", file=sys.stderr)
        return 130

    print(f"[+] Download concluído: {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

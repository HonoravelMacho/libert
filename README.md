# libert

CLI extremamente rápida e direta para download de livros do [welib](https://welib.st).

Instale via PyPI:

```bash
pip install libert
```

## Uso

```bash
libert <URL_DO_WELIB>
libert -f pdf <URL_DO_WELIB>
libert --format epub <URL_DO_WELIB>
```

Exemplo:

```bash
libert https://pt.welib.st/md5/55d053185d2b4871c7a0549b997135f9
```

Fluxo no terminal:

```
[+] Conectando ao welib...
[+] Livro encontrado: "Nome do Livro"
[+] Formatos disponíveis:
  [1] PDF  (15.4 MB)
  [2] EPUB (2.1 MB)
Escolha o formato desejado (1-2):
```

- Com apenas 1 formato disponível, o download começa automaticamente.
- Com `-f/--format`, o modo é não-interativo.
- Arquivos são salvos em `~/Downloads` (Linux) ou `~/storage/downloads` (Termux/Android).
- Barra de progresso com tamanho total, porcentagem e velocidade.

## Opções

```
libert [-f FORMATO] [-o PASTA] <URL>
libert -V
```

## Desenvolvimento

```bash
pip install -e .
python -m build
```

## Licença

MIT

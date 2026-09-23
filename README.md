# libert

CLI extremamente rápida e direta para download de livros do [welib](https://welib.st).

Instale via PyPI:

```bash
pip install libert
```

## Uso rápido

```bash
libert <URL_DO_WELIB>
libert -f pdf <URL_DO_WELIB>
libert --format epub <URL_DO_WELIB>
libert -l links.txt
libert --list ./meus-links.txt
libert            # abre o menu interativo
```

Exemplo:

```bash
libert https://pt.welib.st/md5/55d053185d2b4871c7a0549b997135f9
```

Fluxo no terminal (1 livro):

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
- O app resolve o **link direto do arquivo** a partir da página de
  download do welib (evita salvar HTML no lugar do PDF) e valida o
  conteúdo baixado — se o servidor devolver página em vez do livro,
  nada é salvo e o erro é mostrado.
- URLs de download coladas direto (`.../slow_download/<md5>/...`) são
  convertidas para a página do livro automaticamente.
- Em lote há uma pausa curta entre livros e retentativas em espelhos,
  pois o welib limita o ritmo de requisições.
- Arquivos são salvos em `~/Downloads` (Linux) ou `~/storage/downloads` (Termux/Android).
- Barra de progresso com tamanho total, porcentagem e velocidade.

## Menu interativo (`libert` sem argumentos)

```
=== libert - Gerenciador de Downloads do welib ===

  [1] Baixar um único livro (inserir URL)
  [2] Baixar múltiplos livros (colar lista de URLs)
  [3] Baixar a partir de arquivo de texto (ex: links.txt)
  [0] Sair

Escolha uma opção (0-3):
```

- **Opção 1:** pede a URL e segue o fluxo padrão (auto se 1 formato, pergunta se N).
- **Opção 2:** cole um link por linha; uma linha em branco (Enter vazio) inicia o lote.
- **Opção 3:** informa o `.txt` (Enter = `links.txt` do diretório atual, ou seja, da pasta onde o terminal está aberto). Linhas vazias e comentários `#` são ignorados.

## Lote (batch)

```
[1/3] Processando: https://pt.welib.st/md5/aaa...
[+] Conectando ao welib...
...
[2/3] Processando: https://pt.welib.st/md5/bbb...
...
[+] Resumo: 2 baixado(s) com sucesso, 1 falha(s) de 3.
```

- 1 formato → baixa sem pausar; N formatos → pergunta o formato daquele livro.
- Falhas de um link (URL inválida, 404/403, rede) não abortam o lote.

Arquivo `.txt` de exemplo (`links.txt`):

```txt
# meus livros
https://pt.welib.st/md5/55d053185d2b4871c7a0549b997135f9
https://welib.st/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

## Opções

```
libert [-f FORMATO] [-o PASTA] [-l ARQUIVO.txt] [URL]
libert -V
```

## Desenvolvimento

```bash
pip install -e .
python -m build
```

## Licença

MIT

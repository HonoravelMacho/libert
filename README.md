# libert

CLI **instantânea** para download de livros do [welib](https://welib.st) — o
"yt-dlp dos livros": cola o link (ou lista), e o livro chega em segundos.

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

## Termux / Cloudflare travando tudo

Quando o welib desafia o Cloudflare em todas as tentativas (comum em
redes IPs compartilhadas/celular), o libert acusa exatamente isso e
aceita os **cookies do seu próprio navegador**:

1. No navegador (desktop ou Android), abra `https://welib.org` uma vez
   (passa o "Just a moment..." normalmente);
2. Exporte os cookies para `cookies.txt` — formato Netscape (extensões
   tipo *Cookie-Editor* / *Get cookies.txt*) **ou** só as linhas
   `nome=valor` (ex: `cf_clearance=...`);
3. Rode:

```bash
libert --cookies cookies.txt <URL_DO_WELIB>
libert --cookies cookies.txt -l links.txt
```

No Termux o libert já usa perfil de navegador Android automaticamente.

## Fallbacks

- **IPFS em corrida (race)** — 10+ gateways disparados em paralelo; o
  primeiro que devolver bytes reais (não HTML) ganha o download;
- **libgen** — link direto do `file.php`, último recurso.

## Como fica instantâneo (v0.3)

O libert resolve o arquivo **sem esperar**, por rotas paralelas — a primeira
que responder vale o download:

1. **CDN direta (partner)** — encontra o `js-partner-download-link`
   (`📚 Download`) dentro das páginas `slow_download/fast_download` e baixa
   o arquivo original imediatamente (a espera de minutos não existe: o link
   já vem pronto no HTML); com validação anti-livro-diferente (o md5 do
   livro precisa estar no URL do CDN);
2. **IPFS em corrida (race)** — 10+ gateways disparados em paralelo; o
   primeiro que devolver bytes reais (não HTML) ganha o download;
3. **libgen** — link direto do `file.php`, último recurso.

Extras de robustez:

- Sessão **cloudscraper** aquecida passa o desafio Cloudflare sem browser;
  challenge → retry espelhado e escalonado automaticamente.
- **Rate-limit** não aborta: o libert respira (8s → 20s) e resolve de novo,
  sozinho — você não precisa re-executar o comando.
- **Lote em pipeline**: enquanto baixa o livro atual, o próximo já está
  sendo resolvido em background. Zero segundo morto entre livros; sem
  pausa fixa de 3s e sem "educar" o servidor quando ele não reclama.
- Link partner que vira HTML (token expirado) é re-resolvido na hora.
- Validação de conteúdo: PDF/EPUB/MOBI checados por magic bytes; se o
  servidor devolver HTML, nada é salvo.
- URLs de download coladas direto (`.../slow_download/<md5>/...`) viram
  página do livro automaticamente.
- Espelhos automáticos (`welib.org`, `welib.st`, `pt/en/es.welib.st`).
- Arquivos salvos em `~/Downloads` (Linux) ou `~/storage/downloads` (Termux);
  nunca sobrescreve (adiciona ` (1)`, ` (2)`…).
- Barra de progresso com tamanho, % e velocidade.

## Menu interativo (`libert` sem argumentos)

```
=== libert - Gerenciador de Downloads do welib ===

  [1] Baixar um único livro (inserir URL)
  [2] Baixar múltiplos livros (colar lista de URLs)
  [3] Baixar a partir de arquivo de texto (ex: links.txt)
  [0] Sair

Escolha uma opção (0-3):
```

## Lote (batch)

```
[1/3] https://pt.welib.st/md5/aaa...
[+] Livro: "Nome do Livro"
[+] Link direto do arquivo resolvido.
                     Baixando Nome do Livro.pdf ━━━━━ 100% ...
[+] Concluído em 6.4s → ~/Downloads/Nome do Livro.pdf
[2/3] https://pt.welib.st/md5/bbb...
...
[+] Resumo: 3 baixado(s) com sucesso, 0 falha(s) de 3.
```

Arquivo `.txt` de exemplo (`links.txt`):

```txt
# meus livros
https://pt.welib.st/md5/55d053185d2b4871c7a0549b997135f9
https://welib.org/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Linhas vazias e `#` são ignoradas; texto extra antes da URL é aceito.

## Opções

```
libert [-f FORMATO] [-o PASTA] [-l ARQUIVO.txt] [URL]
libert [-l links.txt] --jobs 3
libert [-l links.txt] --cookies cookies.txt --max-wait 600
libert -V
```

| Flag | Para que serve |
|---|---|
| `-f, --format` | Formato desejado (pdf, epub, mobi...) — pula a pergunta interativa |
| `-l, --list` | Arquivo `.txt` com URLs (uma por linha) |
| `-o, --output-dir` | Pasta de destino (padrão: `~/Downloads`) |
| `--cookies` | Cookie jar do navegador — vence Cloudflare no Termux |
| `--jobs N` | lote em paralelo: N livros de uma vez (2–3 é o ponto seguro) |
| `-w, --max-wait SEG` | paciência ante rate-limit/Cloudflare (padrão 300s; `0` = desiste já) |
| `-V` | versão |

## Paciência automática (v0.4)

Travou no welib/Cloudflare? O libert não desiste: entra em ciclos
automáticos com barra **⏳ Aguardando liberação...** (5s → 45s) e vai
re-resolvendo tudo até liberar — ou até esgotar `--max-wait` (300s por
padrão). Nada de rerodar na mão; deixe o terminal aberto.

No fim de um lote, os links que falharam aparecem no resumo — cole-os de
novo num `.txt` e rerode só eles. Todos os modos usam um `RatePacer`
(espaçamento entre requisições) para não ativar o limite de geração de
links por IP do welib. Ainda assim, welib às vezes guarda o link e só
solta depois de alguns minutos — a paciência costuma resolver.

## Downloads em paralelo (v0.4)

```
libert -l links.txt --jobs 3
```

Uma única barra com um arquivo por linha; enquanto um baixa, os outros
resolvem. Um `RatePacer` compartilhado mantém as requisições ao welib
bem-comportadas (nada de ban). Sem `--jobs`, o lote serial continua com
pipeline: o próximo livro já está sendo resolvido enquanto o atual baixa.

## Notas

- O welib limita ritmo por IP; o libert já respeita isso de forma adaptativa
  (sem pausas fixas) e recupera sozinho quando esbarra no limite.
- Formatos conversão (`convert/epub`, etc.) usados quando você pede `-f`.

## Desenvolvimento

```bash
pip install -e .
python -m build
```

## Licença

MIT

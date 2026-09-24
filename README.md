# tradutor

Tradutores de livros com interface gráfica (Tkinter), usando o Google Tradutor gratuito (sem chave de API).

| Programa | Arquivo | Descrição |
|---|---|---|
| Tradutor de EPUB | `tradutor_epub.py` | Traduz livros `.epub` preservando itálico, negrito, links, marcadores de página e letra capitular. |
| Tradutor de TXT | `tradutor_txt.py` | Traduz arquivos `.txt` (parágrafos separados por linha em branco). |

## Recursos

- **Limite por parágrafos** definido na interface: parágrafo inicial + quantidade (ou "traduzir até o fim").
- **Tradução em partes:** ao terminar uma rodada, o parágrafo inicial avança sozinho para o próximo pendente.
- **Retomada após travamento:** o progresso é gravado em disco a cada bloco traduzido
  (`<saida>.progresso.json` / `.progresso.jsonl`). Ao reabrir, o programa oferece continuar de onde parou.
- No EPUB, os parágrafos ainda não traduzidos permanecem no idioma original; o botão
  **Gerar EPUB agora** monta o livro com o que já foi traduzido.

## Executar

```
pip install -r requirements.txt
python tradutor_epub.py
python tradutor_txt.py
```

## Gerar os executáveis (Windows)

```
python -m PyInstaller --onefile --windowed --name TradutorEPUB tradutor_epub.py
python -m PyInstaller --onefile --windowed --name TradutorTXT tradutor_txt.py
```

## Estrutura

- `nucleo.py` — comunicação com o serviço de tradução (lotes, novas tentativas, parágrafos longos) e utilidades comuns.
- `tradutor_epub.py` — leitura do EPUB, extração dos parágrafos com marcadores de formatação e montagem do livro traduzido.
- `tradutor_txt.py` — tradutor de arquivos de texto.

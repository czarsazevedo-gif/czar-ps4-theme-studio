# Registro da recuperação

| Referência inspecionada | Evidência |
| --- | --- |
| `aa595aa` | Árvore comum às branches main do upstream e do fork. Base anterior às divergências observadas. |
| `41050b1` e `c505a99` | O Studio introduziu outro caminho de inspeção e prévia. A inspeção de entrada limita cada dimensão a 4096 e a área a 4096 × 2304. O preview usa outro renderizador. |
| `40c7140` | O script de inicialização seleciona o Studio e desativa os endpoints do editor original. |
| `14b9f1b` | A branch fix/resumable-media-upload adiciona sessões e envio de partes ao cliente. O histórico não identifica qual commit falhou pela primeira vez com os arquivos do proprietário. |

## Decisões

O checkout de recuperação parte de aa595aa. O cliente usa os endpoints multipart originais. A prévia usa as mesmas regras CSS e o mesmo código de disposição e seleção do upstream. DDS, DXT1a, MDX, RAF, FFmpeg, VAG, ATRAC9, estrutura do tema e perfil de pacote vêm dessa base. O upload conserva o limite original de 512 MiB por arquivo. A cena mantém os dois limites independentes de 48 quadros e 6 MiB de DDS.

O upstream limpava pastas antigas a cada novo trabalho. A recuperação desativa essa remoção, grava em um diretório próprio e guarda a sessão em uma chave própria do navegador. Não há migração de projetos. O suporte ao Codespaces acrescenta o devcontainer e o lançador Uvicorn. A interface usa o dicionário pt-BR por padrão; diagnósticos do upstream podem aparecer em inglês.

## Evidência de bancada Linux

Foram reutilizados os arquivos sample.mp4, sample.gif, sample.png e sample_music.mp3 da bancada anterior. A suíte HTTP conferiu o ZIP RAF de 12 quadros, os cabeçalhos DDS, plane.mdx, nomes literais e wait em segundos. O teste DXT1 verificou tamanho, cabeçalhos e transparência. A suíte de conversão verificou MP3 para WAV e VAG com FFprobe. Também foi importada a imagem 1400 × 900 docs/03-preview-home.png, com destino home a 1920 × 1080. Na suíte de preview, as asserções de posição e tamanho usam as tolerâncias da referência. O teste de disponibilidade de PKG agora consulta /api/system.

Para repetir em um Codespace de teste, inicie o editor, gere as fixtures com `python tests/make_fixtures.py` e execute `PYTHONPATH=. python tests/test_dxt1.py`, `python tests/test_http.py` e `python tests/test_convert.py`. A suíte `python tests/test_ui_preview.py` requer Playwright com Chromium. Os testes HTTP usam 8000 por padrão e aceitam CZAR_TEST_URL para uma bancada isolada.

## Aceitação pendente

No Chrome do Galaxy Tab S10 Ultra, abra a URL privada da porta 8000. Selecione os mesmos arquivos que falharam, registre nome, dimensões, duração e resultado do upload. Confira as três telas, exporte o RAF e registre o resultado no console quando disponível. A aprovação depende desse ensaio no ambiente de destino. PKG real e conversão ATRAC9 externa também dependem de ferramentas e arquivos adequados.

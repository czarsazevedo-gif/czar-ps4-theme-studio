# Biblioteca de mídia · fase 3.2

A biblioteca fica em um painel recolhível do editor. Importe um ou mais arquivos pelo seletor. Cada arquivo tem seu próprio resultado. A barra mede a transferência pelo navegador e passa para **Verificando e salvando no servidor…** quando o envio acaba. Só aparece **Disponível na biblioteca** após a confirmação do servidor.

Abra uma mídia para consultar formato, dimensões, tamanho, duração, FPS e codec, quando disponíveis. Use o seletor de destino para aplicá-la ao projeto aberto. GIF pode servir como imagem de slot ou fundo animado. A música do menu obedece à exigência original de ATRAC9 quando o codificador opcional está ausente.

## Armazenamento e compatibilidade

- O catálogo usa o cookie de sessão da fase 3.1. Cada sessão tem seu próprio diretório `library-v1` dentro de `workspace/studio-projects-v1/`. Um SHA-256 identifica o conteúdo e permite deduplicação dentro da sessão.
- O upload usa multipart em uma requisição por arquivo, no limite canônico de 512 MiB. O servidor copia o arquivo temporário do formulário em blocos internos, mede o tamanho e calcula o hash. FFprobe verifica contêiner e fluxos. FFmpeg verifica a decodificação inicial e produz uma miniatura WebP de 320 × 180 para imagens, vídeos e GIFs. O catálogo publica metadados e original juntos por renomeação do diretório de preparação.
- O original fica em modo somente leitura. Cada aplicação cria um job canônico com hard link para o original, no mesmo volume, e gera seus próprios derivados. Os manifests de projeto continuam na versão 1 e apontam para jobs como antes. A abertura de projetos anteriores não depende do catálogo. Os uploads feitos diretamente nos slots continuam no fluxo avulso de cada projeto.
- Arquivos originais e miniaturas só são servidos no escopo da sessão. O cliente recebe links da API e metadados, sem o caminho de disco. A busca e os filtros retornam páginas de até 48 itens. As imagens da grade são carregadas sob demanda.

## Limites conhecidos

O navegador precisa preservar o cookie e o Codespace precisa preservar seu disco. A biblioteca não recolhe mídias automaticamente e usa espaço adicional para miniaturas e derivados. O limite de 512 MiB é o mesmo do editor. As duas verificações de biblioteca têm prazo de até 90 segundos cada. A validação examina o contêiner e a decodificação inicial. A geração de um RAF ainda depende dos quadros que o arquivo consegue decodificar ao longo do vídeo. O navegador pode reconhecer menos codecs na reprodução direta do que o FFmpeg no processamento. A interrupção da fila para o envio no navegador. Se o servidor já tiver concluído a gravação, a mídia aparecerá na próxima listagem.

## Roteiro de aceitação no Galaxy Tab

1. Abra a branch `feature/studio-media-library-v1` em um Codespace de teste. Execute `./start.sh` e abra a porta 8000 como **Privada** no Chrome.
2. Crie um projeto e abra **Biblioteca de mídia**. Selecione um PNG, um GIF, um MP4 que você costuma usar e um WAV. Confira o progresso e os resultados. Em paisagem e retrato, teste rolagem, grade, lista, busca por nome, categorias, ordenação e abertura de uma mídia.
3. Aplique a imagem como fundo e como ícone. Aplique o vídeo como fundo animado, o WAV como efeito e exporte o RAF. Ajuste quadros na cópia do projeto e confira que a animação do projeto original conserva seus ajustes.
4. Crie outro projeto e aplique a mesma imagem ou vídeo a partir do catálogo. Reinicie o servidor, reabra o mesmo Codespace e volte pelo mesmo endereço e perfil do Chrome. Confira o catálogo, os trabalhos e a restauração de um projeto da fase 3.1, se disponível nesse disco.

## Testes na bancada

Com as fixtures de `python tests/make_fixtures.py`, execute `PYTHONPATH=. python tests/test_library.py -v` e `python tests/test_library_browser.py` com Playwright/Chromium disponíveis. O segundo teste também aceita `CZAR_ACCEPTANCE_VIDEO` apontando para um MP4 real. A bancada usou a captura `docs/03-preview-home.png` e o vídeo de referência [flower.mp4, disponibilizado pela MDN](https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4), além das fixtures. Os arquivos reais do proprietário e o teste físico no Galaxy Tab ficam para a aceitação.

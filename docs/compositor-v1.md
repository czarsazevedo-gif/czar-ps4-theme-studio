# Compositor visual · fase 3.3

## Auditoria e arquitetura

A branch partiu de `3df0197`, HEAD verificado de `feature/studio-media-library-v1`. As referências da recuperação e dos projetos eram `cbeffaa` e `07ff25d`. Os manifests existentes continuam na versão 1. O campo opcional `composition` tem versão própria e aponta para hashes já existentes no catálogo da mesma sessão. O autosave utiliza a mesma revisão, gravação atômica e resposta HTTP 409. Duplicar copia as configurações das camadas e conserva referências aos originais.

O upload multipart, a importação da biblioteca, os 512 MiB e os originais continuam com os contratos anteriores. O editor interativo usa Pointer Events e trabalha em coordenadas lógicas. As imagens raster passam por FFmpeg e o Pillow compõe os derivados em RGBA no servidor. Fundos usam 1920 × 1080. Sobreposições usam as mesmas coordenadas lógicas, mapeadas à resolução RAF ativa. Os ícones são editados internamente em 128 × 128 ou 512 × 512. A interface PS4 existente mantém seu CSS e seus slots. A prévia usa as imagens do servidor atrás dos elementos originais.

A composição produz PNGs de entrada para os processadores de slots existentes. A saída preserva o tratamento canônico dos slots e do glow de 152 × 152. Para RAF, a prévia lossless e a exportação partem do mesmo amostrador RGBA canônico e da mesma sobreposição. O codificador DDS, o plano MDX e o XML são os originais. Uma configuração de camadas ausente, inválida ou acima do orçamento é recusada no caminho correspondente. O ZIP RAF composto substitui o ZIP anterior após terminar integralmente.

## Uso no Galaxy Tab

1. No Chrome, abra `feature/studio-visual-compositor-v1` e crie um Codespace nela. Execute `./start.sh` e abra a porta **8000** marcada como **Privada**. Se estiver reaproveitando o contêiner, instale antes as dependências atualizadas com `python -m pip install -r requirements.txt`.
2. Abra ou crie um projeto. Em **Compor**, escolha um fundo, importe um PNG/JPEG ou abra uma mídia raster da biblioteca e toque em **Adicionar como camada**. Arraste com um dedo. As alças redimensionam e rotacionam. Dois dedos movem e ampliam a vista. O botão de porcentagem restaura o zoom. A rolagem dos painéis fica fora do canvas. Ajuste ordem, opacidade e enquadramento e use Desfazer e Refazer.
3. Em **Prévia PS4**, confira a tela inicial, o menu superior e a tela de aplicação. Em **Pacote**, exporte **recursos do tema (ZIP)** e compare os PNGs. Escolha um ícone de função ou conteúdo em **Compor** e confira o enquadramento e o PNG exportado.
4. Importe um vídeo em **Fundo**. Em **Compor**, selecione **Sobreposições do fundo animado**. Adicione uma imagem e confira a prévia PS4. Volte a **Fundo** para exportar o RAF. Confira `scene/background.raf/`, as dimensões DDS, a temporização no XML, o MDX e o orçamento.
5. Em paisagem e retrato, duplique o projeto, altere uma camada na cópia e confirme que o original conserva a composição. Espere **Salvo**, recarregue e, por fim, reabra o mesmo Codespace. Confira o estado com o mesmo perfil e endereço do Chrome.

## Limites

Esta fase usa camadas raster estáticas e uma fonte animada canônica. GIF usado como camada raster contribui com o primeiro quadro. Em modo animado, o PS4 usa o mesmo RAF nas duas telas, conforme o comportamento original. O quadro de edição interativo aproxima filtros de escala. Para conferir o raster processado, use **Prévia PS4**. O DDS DXT1a aplica sua própria quantização de cor e transparência. A tela de aplicação mantém a miniatura e a geometria já existentes.

O editor permite 16 camadas por superfície e 48 por projeto. O derivado raster de edição tem lado maior de até 1920 px. Essa normalização limita detalhes em ampliações extremas. O original preserva todas as dimensões. A prévia animada pode levar alguns segundos e guarda quadros derivados por configuração renderizada no projeto. Cancelar a navegação descarta a resposta pendente no navegador, enquanto o servidor pode concluir o render. Uma camada ausente pode ser removida explicitamente e o manifesto salvo continua recuperável.

O ZIP de recursos contém a árvore gerada pelo coletor canônico, incluindo fundos, ícones, glow, áudio, XML e RAF quando aplicáveis. Ele é um recurso intermediário e não contém PKG ou SFO. O empacotador opcional continua sendo a via para um PKG real.

## Verificação

Com as fixtures de `python tests/make_fixtures.py`, execute `PYTHONPATH=. python tests/test_compositor.py -v` e `python tests/test_compositor_browser.py` com Playwright e Chromium disponíveis. `CZAR_ACCEPTANCE_VIDEO` pode apontar para um vídeo real. A comparação de RAF sem composição usa os bytes descompactados de cada entrada, pois metadados ZIP podem conter horários de execução. Os testes em Chromium emulam toque e reinício do Uvicorn. A aceitação física da fase 3.3 no tablet e o empacotamento com ferramentas autorizadas exigem verificações separadas.

- Na comparação com o commit `3df0197`, o vídeo real CC0 `flower.mp4` produziu DDS, MDX e XML idênticos sem composição em 640 × 360, 960 × 540 e 1280 × 720.
- Nas três resoluções, o DDS composto correspondeu à codificação DXT1a nativa do quadro lossless exibido na prévia. MDX e XML ficaram iguais aos da mesma cena sem camadas.
- O navegador emulado importou a captura PNG e o vídeo de referência, moveu uma camada por toque, usou Desfazer e Refazer, comparou o PNG exibido com o ZIP pixel a pixel e reabriu o projeto após reiniciar o servidor.
- As regressões exercitaram projetos e biblioteca, incluindo isolamento, restauração, áudio WAV/VAG e a geometria das três telas originais.

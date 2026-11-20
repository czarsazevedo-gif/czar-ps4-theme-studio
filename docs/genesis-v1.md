# Gênesis V1 · translação periódica por faixas

O exemplo tem um segundo de vídeo, uma lua fixa e duas formações atmosféricas sintéticas. O motor mediu velocidades diferentes nas duas faixas e produziu um ciclo de 1,2 s. Os arquivos abaixo vieram da execução do encoder nativo. [Fonte MP4](examples/genesis/lua-sintetica.mp4) · [RAF 720p](examples/genesis/lua-1280.raf.zip) · [RAF 540p](examples/genesis/lua-960.raf.zip) · [RAF 360p](examples/genesis/lua-640.raf.zip) · [Hashes dos arquivos distribuídos](examples/genesis/SHA256SUMS)

## Algoritmo

1. O usuário escolhe a fonte importada, o eixo, a resolução e o multiplicador de velocidade. O serviço reutiliza o isolamento por projeto, a fila cancelável e o cache privado da Fundação. O vídeo original permanece no workspace. O aplicativo continua a iniciar com `./start.sh`.
2. FFmpeg fornece até oito amostras iniciais em 160×90, durante até 0,8 s. O detector agrupa linhas com atividade temporal. Para o eixo vertical, o cálculo opera com a imagem transposta. O modelo aceita até quatro faixas paralelas de uma mesma gravação.
3. Cada faixa precisa conter ao menos duas ocorrências de uma mesma textura. A diferença RGB entre janelas mede o período espacial inteiro. O deslocamento é estimado por correspondência direta de cada par consecutivo, comparando também a hipótese de posição fixa. O eixo vem do usuário e o sinal vem da medição. Mudança de sinal, velocidade variável ou grande erro de reconstrução da fonte impedem a síntese.
4. Para um período espacial P e deslocamento v por intervalo, o período temporal mínimo em intervalos é P/mdc(P,|v|). O mínimo múltiplo comum entre as faixas determina a volta completa. O motor escolhe fases i/N com i entre zero e N−1 e rejeita esperas fora de 0,02–0,2 s. As velocidades relativas permanecem proporcionais. O multiplicador explícito atua sobre a duração de reprodução.
5. O primeiro quadro na resolução de saída preserva a textura da fonte. A faixa utiliza um período interno desse quadro após verificar a repetição na resolução final. As coordenadas espaciais usam módulo P e interpolação linear entre texels vizinhos. Fora das faixas, os pixels vêm do quadro fixo. Os limites das faixas são arredondados para dentro em blocos de quatro pixels para proteger os blocos DDS adjacentes.
6. Cada quadro é codificado por `dds.encode_frame`. O motor decodifica os DDS e mede o sentido e a velocidade por alinhamento de cada faixa. Compara a hipótese prevista com deslocamentos próximos, incluindo parada e direção oposta. Verifica igualdade exata da região protegida e compara mudança de aparência e brilho na junção com as transições internas. O relatório privado guarda vetores e medidas em ordem, com a junção na última entrada. O ZIP usa os geradores existentes de plano MDX, cena XML e árvore RAF. A prévia exibe WebP sem perdas dos DDS decodificados.

## Medições

A tabela mostra a execução com a lua horizontal sintética, em CPU Linux. A diferença visual é o erro absoluto médio RGB em escala 0–255. O deslocamento é medido entre o último e o primeiro DDS. Os tempos incluem as verificações do teste no processo Python. O pico acumulado de memória residente desse processo foi de 173,4 MiB. FFmpeg roda em subprocesso e sua utilização fica fora dessa medição.

| Saída | Quadros | Espera | Soma dos DDS | Avanço das faixas na junção | Diferença da junção / mediana interna | CPU |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| 1280×720 | 12 | 0,100 s | 5.531.136 B | +16 e +32 px | 3,886 / 3,877 | 4,31 s |
| 960×540 | 24 | 0,050 s | 6.223.872 B | +6 e +12 px | 2,108 / 2,111 | 4,42 s |
| 640×360 | 48 | 0,025 s | 5.535.744 B | +2 e +4 px | 1,128 / 1,130 | 3,89 s |

Todos os pares avançaram no sentido esperado, inclusive a volta. Os DDS de cada sequência são distintos. A região da lua foi comparada com a codificação independente do primeiro quadro da fonte. O teste vertical a 640×360 também fechou com +2 e +4 px. As mutações de reversão, congelamento e salto espacial foram reprovadas. O vídeo real disponível, flower.mp4, foi recusado por ausência de repetição espacial coerente.

## Campo de aplicação

O motor requer fonte 16:9 e repetição espacial mensurável. A amostragem inicial observa velocidades inteiras entre um e seis pixels da grade por intervalo, conforme o período. Movimentos subpixel, diagonais, câmera móvel, nuvens que mudam de forma, oclusões, camadas sobrepostas e mudanças posteriores ao trecho analisado limitam o resultado. A proteção se aplica às regiões fixas fora das faixas. Um objeto estático dentro de uma faixa móvel requer outra representação. A interpolação espacial pode suavizar textura. A qualidade percebida e a aplicação no console precisam de inspeção física.

## Reprodução e aceitação

Em um checkout da branch `feature/ouroboros-genesis-v1`, execute:

```bash
PYTHONPATH=. GENESIS_DEMO_DIR=/tmp/czar-genesis-demo python tests/test_genesis.py -v
PYTHONPATH=. python tests/test_genesis_browser.py
```

O segundo teste utiliza Playwright e Chromium. `CZAR_CHROMIUM` pode indicar o executável instalado. A pasta de demonstração recebe MP4 e ZIPs gerados, inclusive o exemplo vertical. Escolha uma pasta dedicada.

1. No Chrome do Galaxy Tab, abra um Codespace nesta branch. Execute `./start.sh` e abra a porta privada 8000.
2. Importe **lua-sintetica.mp4** em um projeto de ensaio. Em **Diagnóstico de loops**, escolha a fonte, eixo horizontal, 640×360 e velocidade 1.
3. Toque em **Sintetizar faixas periódicas**. Observe dez voltas. Confira a lua e as velocidades relativas, compare com o vídeo original e use a inspeção último → primeiro em retrato e paisagem.
4. Baixe o RAF, confira a contagem, a espera e a soma de bytes e repita nas outras resoluções. Registre SHA, Chrome, material, resolução e qualquer costura percebida. A aceitação física continua pendente.

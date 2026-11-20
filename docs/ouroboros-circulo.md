# Círculo · descoberta de cortes naturais

## Da fonte ao candidato

A página **Diagnóstico de loops** recebe um ID da biblioteca da sessão ou do fundo animado do projeto aberto. O navegador acompanha o trabalho na API e carrega o candidato escolhido. No servidor, `ouroboros_media.py` lê a fonte, `sentinela.py` caracteriza o movimento e `circulo.py` procura e mede os cortes. A comparação entre vizinhanças temporais segue a ideia de *Video Textures*, de Schödl e colaboradores. A fundação procura um único ciclo cronológico. [Publicação original, SIGGRAPH 2000](https://doi.org/10.1145/344779.345021)

1. A Sentinela percorre toda a duração em até 240 amostras de 96×54. Quando o passo excede 160 ms, o Círculo também inspeciona até nove janelas de até seis segundos, distribuídas pelo vídeo e pela região de maior mudança. O relatório registra a cobertura e as janelas.
2. Cada resolução obtém o teto de quadros diretamente de `app.config.max_frames_for`. Para cada teto, a busca avalia durações compatíveis com o material e cadências próximas de 8, 10, 12 e 16 FPS, limitadas pela fonte. Compara pequenas vizinhanças para uma lista limitada de defasagens, mede a atividade dentro do intervalo e distribui a triagem por seis partes da gravação. A lista limitada evita comparar todos os pares de timestamps.
3. Até 18 cortes distintos por resolução são examinados na cadência efetiva de cada candidato. Os três primeiros são apresentados, quando disponíveis. Buscas que chegam ao mesmo intervalo e contagem podem reutilizar sua medida da fonte. A estimativa inicial é obtida nas imagens reduzidas. A medição em resolução de saída acontece ao tocar em **Conferir DDS**.
4. O primeiro quadro está em `início`. O último está em `início + (n − 1) × espera`, com saída exclusiva. A fonte sempre avança. O código usa a precisão de seis algarismos significativos da espera canônica. A duração exibida é `n × espera`.

## Como interpretar a junção

O motor compara penúltimo → último, último → primeiro e primeiro → segundo. Usa diferenças de luminância no quadro e por região, vetores observáveis nos mesmos blocos, oposição e ângulo, mudança de velocidade, descontinuidade fotométrica e alteração cromática. A diferença da junção e a mediana interna estão em níveis de luminância de 8 bits. Os custos visual e temporal são indicadores relativos. Quanto menor o custo, mais a transição se aproxima do padrão interno. A confiança indica se existiam textura e movimento mensuráveis para sustentar a leitura.

Um retorno estrutural no trecho, corte, repetição em trajetória ou salto pronunciado impede aprovação. Uma cena imóvel pode ficar em **material inadequado**. Evidência ambígua, cadência baixa ou um possível objeto transitório levam a **condicional**. **Reconstrução necessária** indica um defeito encontrado naquele corte. Uma variação de luz pode fechar um ciclo quando a fase e a derivada continuam coerentes. Verifique também outros candidatos da mesma fonte.

O ranqueamento usa primeiro a classificação, depois os custos e a quantidade de quadros. Um vídeo longo pode conter eventos curtos entre as amostras. Deslocamentos acima de quatro pixels na grade de diagnóstico, movimento subpixel, oclusões e texturas sem contraste dificultam a medida da direção. Cortes úteis fora da lista de cadências ou da triagem podem passar despercebidos.

## Prévia, armazenamento e falhas

O servidor recorta e dimensiona a fonte com o filtro da 3.3, chama o encoder DDS nativo e volta a medir o ciclo sobre os DDS decodificados. Cada imagem entregue ao navegador é WebP sem perdas dessa decodificação. A tela identifica a origem, a resolução, o trecho, a espera, as classificações antes e depois e a soma real de bytes dos DDS. O player repete os quadros pré-carregados pelo relógio monotônico do navegador. O modo de inspeção percorre os dois últimos e os dois primeiros quadros. O teste perceptivo pede a reprodução normal.

A página é um diagnóstico da fonte. A composição de camadas e a exportação RAF continuam no editor 3.3. A API aceita somente identificadores privados. Um projeto pode processar um trabalho de cada vez, até dois no servidor Uvicorn de processo único do `start.sh`. O progresso avança por etapa ou candidato. Cancelar interrompe FFmpeg e descarta a área temporária do trabalho. Uma retomada após reinício cria um novo trabalho e pode reaproveitar derivados concluídos.

Os resultados ficam em `ouroboros-v1/<chave>/` dentro do projeto. A chave inclui versão e identidade do conteúdo ou receita da prévia. O cache verifica os hashes das imagens antes de reutilizar uma prévia. Os resultados são preparados fora do diretório final e publicados após a conclusão. Os originais e o manifesto do editor são preservados. O armazenamento de prévias cresce conforme se inspecionam novos candidatos.

## Ensaios de bancada

A fixture de 20 s a 10 FPS contém 12 s imóveis e uma textura com período de 16 quadros até o fim da gravação. Os intervalos abaixo foram encontrados pelo motor. A medição do ciclo progressivo de referência deu 29,931 níveis tanto na fronteira quanto na mediana interna, com fração de vetores opostos igual a zero.

| Resolução | Três melhores intervalos em segundos | Quadros por corte | FPS | Classificação da fonte |
| --- | --- | ---: | ---: | --- |
| 1280×720 | 12,0–13,6 · 12,4–14,0 · 12,8–14,4 | 13 | 8,125 | Condicional |
| 960×540 | 12,0–13,6 · 12,4–14,0 · 12,8–14,4 | 16 | 10 | Aprovado |
| 640×360 | 12,0–16,8 · 14,0–18,8 · 15,2–20,0 | 48 | 10 | Aprovado |

A sequência de ping-pong fechou com diferença visual zero e mediana interna de 29,931. O diagnóstico indicou **reconstrução necessária** por reversão e repetição. O teste da Lua sintética também impediu aprovação de um corte através do retorno estrutural. A pulsação luminosa de período conhecido recebeu aprovação instrumental com confiança limitada. A cena imóvel recebeu **material inadequado**.

O arquivo real disponível foi `flower.mp4`, H.264, 960×540, a cerca de 29,97 FPS. As três buscas avaliaram 18 cortes cada. Todos os finalistas foram condicionais. O primeiro intervalo em cada resolução foi 3,586917–4,087417 s, seis quadros, espera de 0,0834167 s. A grade de movimento teve evidência insuficiente para resolver a direção da flor. A inspeção dos quadros adjacentes à junção mostra que a posição relativa das pétalas e folhas varia.

| Resolução | Inícios dos três primeiros em segundos | Bytes reais do primeiro | Diferença da junção / mediana interna nos DDS | Tempo da prévia na bancada |
| --- | --- | ---: | ---: | ---: |
| 1280×720 | 3,587 · 3,921 · 2,419 | 2.765.568 | 5,098 / 2,016 | 3,82 s |
| 960×540 | 3,587 · 4,171 · 3,921 | 1.555.968 | 5,078 / 2,012 | 1,89 s |
| 640×360 | 3,587 · 4,171 · 3,921 | 691.968 | 5,114 / 2,059 | 0,86 s |

A busca sem cache nesse ensaio levou 5,6 s em CPU. A prévia mantém em disco uma sequência RGB mapeada com até aproximadamente 36 MiB para os limites canônicos. A análise de blocos opera em lotes. As medidas de tempo dependem da CPU e da carga do Codespace.

## Reproduzir os testes

A bancada usou Python 3.12.14, FFmpeg 7.1.5 e Chromium 156 em Linux. Os testes de integração usam as fixtures da base. Em um checkout novo, gere-as com `python tests/make_fixtures.py`. O cliente de testes requer HTTPX e a suíte do navegador requer Playwright e Chromium. Essas ferramentas são de desenvolvimento.

```bash
PYTHONPATH=.:tests python -m unittest test_sentinela test_circulo test_ouroboros_api test_ouroboros_compat test_projects test_library test_compositor -v
PYTHONPATH=. python tests/test_ouroboros_browser.py
```

`CZAR_CHROMIUM` pode apontar para o Chromium já instalado. `CZAR_ACCEPTANCE_VIDEO`, `CZAR_LUA_VIDEO` e `CZAR_COROA_VIDEO` permitem repetir a análise com arquivos locais privados. O teste de equivalência usa o objeto Git da base `8a9ef7f`; o checkout de teste precisa conter esse objeto. Os relatórios de ensaio ficam no terminal. [Evidências da Sentinela](ouroboros-sentinela.md) · [Estado das regressões](ouroboros-status.md)

## Aceitação no Galaxy Tab S10 Ultra

1. No Chrome, abra a branch de desenvolvimento no repositório do proprietário e crie um Codespace nela. Confira `git branch --show-current` e `git rev-parse --short HEAD`. Execute `./start.sh` e abra a URL encaminhada privada da porta 8000.
2. Abra um projeto de ensaio. Importe uma fonte conhecida e as fontes Lua e Coroa pela biblioteca quando disponíveis. Registre o nome local, hash, codec, duração e histórico da regressão. Mantenha as mídias no workspace privado.
3. Em paisagem, abra **Diagnóstico de loops**. Escolha o vídeo e toque em **Analisar movimento e ciclos**. Confira os motivos da Sentinela e os três resultados por resolução. Registre os timestamps, a cadência, a direção e o veredito.
4. Toque em **Conferir DDS**. Observe dez voltas em reprodução normal. Use **Inspecionar último → primeiro** e depois **Próximo quadro** para localizar uma falha percebida. Registre separadamente retorno de um objeto, pulsação de luz, congelamento, salto ou problema de reprodução do navegador.
5. Repita em retrato. Volte ao editor e confira o projeto e suas camadas. Recarregue, reinicie o Uvicorn e abra o diagnóstico novamente. Compare os resultados para a mesma fonte e as medições de bytes.
6. Registre commit, dispositivo, versão do Chrome, origem da mídia, resolução e resultado perceptivo de cada corte. Um retorno estrutural recomendado como progressivo ou uma regressão da 3.3 bloqueia a aceitação. A integração depende da aprovação do proprietário.

# Estado recuperável

## ARCA

A bancada tinha o repositório Git comum em `/workspace/PS4-Ultimate-Theme-Creator/.git` e o worktree interrompido em `/workspace/czar-motion`. A branch 3.4 local apontava para `8a9ef7f`, sem commit adicional e sem referência remota. As bases anteriores eram `cbeffaa`, `07ff25d`, `3df0197`, `271d137` e `8a9ef7f`.

Foram encontrados dez arquivos rastreados modificados e doze novos, todos de texto. A inspeção dos caminhos, do conteúdo e de padrões de credenciais delimitou o checkpoint. Mídias, workspace privado, ambiente Python, logs e temporários ficaram no local de origem. O snapshot integral está em `8fc7785` e na branch local `rescue/ouroboros-pre-foundation`. Esse commit também integra o histórico recuperável da branch de desenvolvimento. O checkout dedicado à fundação é `/workspace/czar-ouroboros`.

O protótipo implementava leitura por FFmpeg, amostragem parcial, classificação da junção, alinhamento experimental, recorrência, cache privado, tela de edição e integração opcional ao exportador. Os logs anteriores registram quatro testes de API, dois experimentais, um de compatibilidade com 30 entradas ZIP e Chromium com dez voltas. A compilação do OpenCV terminou, mas esses logs não certificam a suíte com essa compilação nem a aceitação física. Faltavam evidências de detecção de simetria versus pulsação e três buscas independentes. A árvore ativa volta à 3.3 neste checkpoint. A implementação reaproveitará o código de medição e leitura compatível com o escopo aprovado.

## Sentinela

O checkpoint ARCA da fundação é `d4e4283`. A leitura limitada do protótipo foi reaproveitada e o detector passou a usar NumPy e Pillow já presentes. Os dois testes da Sentinela passaram, incluindo as 12 fixtures e o arquivo flower.mp4. O checkpoint da Sentinela é `d5e9701`. [Método e ensaios](ouroboros-sentinela.md)

## Círculo e regressões

O checkpoint **Círculo** da branch `feature/studio-infinite-motion-v1` contém a busca por orçamento, o diagnóstico privado e os testes da fundação. A base de comparação é `8a9ef7f2ca990cea2ed01439285a90f0770876ea`. O repositório de publicação é `czarsazevedo-gif/czar-ps4-theme-studio`. Para localizar o commit pelo nome e confirmar o HEAD do checkout, use `git log -4 --oneline`. [Implementação, métricas e roteiro no Galaxy](ouroboros-circulo.md)

| Verificação em Linux | Resultado observado |
| --- | --- |
| Execução conjunta de Sentinela, Círculo, API, equivalência, projetos, biblioteca e compositor | 22 testes passaram em 63,7 s, com flower.mp4 habilitado |
| Equivalência com o código da base Git | 6 ZIPs, 3 resoluções com e sem overlay, hashes iguais em todas as 30 entradas descompactadas |
| Círculo | 3 candidatos por resolução na fixture de 20 s e em flower.mp4. O último candidato de 360p da fixture termina em 20,0 s |
| Recuperação de erro | O cache detectou um WebP truncado, preservou o relatório existente durante falha simulada de disco e recuperou os mesmos bytes na nova tentativa |
| Isolamento | Recusa de acesso de outra sessão ou projeto, recusa de origem externa, ID inválido e fonte ausente. Cancelamento liberou a área temporária |
| Pipeline aberto | Smoke HTTP com ZIP RAF, DDS, MDX e XML, conversor WAV/VAG e script DXT1 executados |
| Chromium com toque | Diagnóstico em 1480×924 e 924×1480, dez voltas, inspeção da junção, passo por quadro, retorno ao editor e cache após reiniciar o Uvicorn |
| Outras seis suítes de navegador | Projetos, biblioteca, compositor, três telas de preview, fluxo geral de RAF e slots passaram |

Os testes HTTP da bancada usaram a porta isolada 18088 e os testes de reinício usaram portas efêmeras. A configuração do Codespace continua na porta privada 8000. As partes legadas que exigem PKG proprietário e a conversão ATRAC9 ficaram fora da execução. A fundação usa FFmpeg, FFprobe, NumPy e Pillow sem dependências de execução novas.

A revisão encontrou três situações que poderiam distorcer o diagnóstico. A classificação descartou evidência de movimento causada pelo par de um corte abrupto. A busca passou a penalizar janelas imóveis, incluir o limite final da gravação e informar falhas de leitura separadamente da inadequação do material. Uma prévia truncada passou a ser verificada e refeita sob a mesma chave.

A próxima evidência necessária é a observação da junção pelo proprietário no Chrome físico. Lua e Coroa foram representadas por sinais sintéticos. O teste físico 3.3, a integração em branches anteriores e a aceitação da fase 3.4 seguem pendentes.

## Gênesis V1

Branch independente `feature/ouroboros-genesis-v1`, baseada em `e578614`. O checkpoint inicial do sintetizador é `ae7be83`. A demonstração produziu RAF nas três resoluções e preservou a lua sintética durante todo o ciclo. Os ensaios de regressão e o navegador com toque emulado acompanham o código. A aprovação perceptiva no tablet e a aplicação no PS4 seguem pendentes. [Resultados e arquivos de demonstração](genesis-v1.md)

Na bancada Linux, passaram 22 testes de regressão da Fundação e das fases anteriores. O Chromium com toque emulado reproduziu dez voltas, abriu a inspeção da junção em retrato, recuperou o vídeo original e baixou o RAF. A regressão registrou um ResourceWarning de fechamento de upload na rota legada e um aviso de depreciação do cliente HTTP de testes.

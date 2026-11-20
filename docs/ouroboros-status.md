# Estado recuperável

## ARCA

A bancada tinha o repositório Git comum em `/workspace/PS4-Ultimate-Theme-Creator/.git` e o worktree interrompido em `/workspace/czar-motion`. A branch 3.4 local apontava para `8a9ef7f`, sem commit adicional e sem referência remota. As bases anteriores eram `cbeffaa`, `07ff25d`, `3df0197`, `271d137` e `8a9ef7f`.

Foram encontrados dez arquivos rastreados modificados e doze novos, todos de texto. A inspeção dos caminhos, do conteúdo e de padrões de credenciais delimitou o checkpoint. Mídias, workspace privado, ambiente Python, logs e temporários ficaram no local de origem. O snapshot integral está em `8fc7785` e na branch local `rescue/ouroboros-pre-foundation`. Esse commit também integra o histórico recuperável da branch de desenvolvimento. O checkout dedicado à fundação é `/workspace/czar-ouroboros`.

O protótipo implementava leitura por FFmpeg, amostragem parcial, classificação da junção, alinhamento experimental, recorrência, cache privado, tela de edição e integração opcional ao exportador. Os logs anteriores registram quatro testes de API, dois experimentais, um de compatibilidade com 30 entradas ZIP e Chromium com dez voltas. A compilação do OpenCV terminou, mas esses logs não certificam a suíte com essa compilação nem a aceitação física. Faltavam evidências de detecção de simetria versus pulsação e três buscas independentes. A árvore ativa volta à 3.3 neste checkpoint. A implementação reaproveitará o código de medição e leitura compatível com o escopo aprovado.

## Sentinela

O checkpoint ARCA da fundação é `d4e4283`. A leitura limitada do protótipo foi reaproveitada e o detector passou a usar NumPy e Pillow já presentes. Os dois testes da Sentinela passaram, incluindo as 12 fixtures e o arquivo flower.mp4. O próximo checkpoint é a busca independente por resolução e sua interface compacta. [Método e ensaios](ouroboros-sentinela.md)

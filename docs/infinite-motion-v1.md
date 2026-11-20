# Czar Infinite Motion · laboratório de loops

## Decisão de arquitetura antes da implementação

A base auditada é `8a9ef7f`, da branch `feature/studio-visual-compositor-v1`. A importação já cria um trabalho animado privado com o original, inclusive por referência da Biblioteca. A fase 3.3 compõe camadas raster depois da extração. O exportador canônico chama `dds.encode_sequence`, `mdx.quad_model`, `raf.build_scene` e `theme.zip_raf_folder`. O manifesto aceita estado opcional e confere a revisão sob bloqueio. Esses são os pontos de integração do laboratório.

Video Textures usa vizinhanças temporais para escolher transições. Uma imagem parecida na volta pode pertencer a uma trajetória com velocidade oposta. Para o RAF adotamos cortes entre amostras em ordem crescente e comparação com transições internas do próprio trecho. [Schödl et al., SIGGRAPH 2000](https://www.microsoft.com/en-us/research/publication/video-textures/)

O fluxo denso de Farnebäck permite estimar deslocamento local em CPU. Avaliamos direção, velocidade e erro fotométrico do campo na junção. Oclusões e pouca textura tornam o campo ambíguo. A alternativa de alinhamento exige consistência nos dois sentidos e nova medição após a compressão. [Farnebäck, SCIA 2003](https://www.ida.liu.se/ext/WITAS-ev/Computer_Vision_Technologies/PaperInfo/farneback03.html)

O trabalho com campos eulerianos trata movimento aproximadamente estacionário de fluidos a partir de uma foto com um modelo aprendido. As pesquisas sobre loops de vídeo também mostram os custos da costura espacial e da mistura temporal. Nesta versão, água e fumaça são candidatas à seleção temporal. Um deslocamento periódico imposto a uma cena com margens abertas pode reiniciar objetos na borda. A síntese espacial permanece experimental, fora dos controles de edição. [Holynski et al., CVPR 2021](https://openaccess.thecvf.com/content/CVPR2021/html/Holynski_Animating_Pictures_With_Eulerian_Motion_Fields_CVPR_2021_paper.html) · [Liao et al., Fast Computation of Seamless Video Loops](https://hhoppe.com/proj/fastloops/)

O plano separa a busca em janelas pequenas, a verificação dos candidatos na cadência escolhida e a geração progressiva no tamanho final. A prévia final será lida dos próprios DDS do RAF. Uma alteração de camada invalidará o uso da versão composta anterior. O fluxo da fase 3.3 continua disponível ao desativar o laboratório.

## Usar o laboratório

1. Abra um projeto, selecione **Fundo → Animado** e importe o vídeo. Também é possível aplicar um vídeo da Biblioteca ao fundo animado.
2. Abra **Loops**, escolha o perfil e a estratégia e toque em **Procurar junções**. A faixa de miniaturas informa o instante e a variação medida. A régua sinaliza o intervalo selecionado, o cursor e os candidatos. No vídeo longo, as miniaturas cobrem as janelas amostradas e cada uma tem seu timestamp.
3. Toque em um candidato ou ajuste **Entrada** e **Saída exclusiva**. **Marcar entrada** e **Marcar saída** usam o instante do cursor. Alterar o FPS desejado arredonda a quantidade de quadros. O orçamento mostra o FPS efetivo, o intervalo no XML e o consumo estimado. Um valor fora da faixa mantém a última edição válida salva.
4. Toque em **Renderizar e verificar**. Alterne entre **Fonte original**, **Corte sem refinamento**, **Loop otimizado** e **RAF final**. Nas três vistas de quadros, as camadas do animado já estão compostas. Na primeira, o navegador tenta reproduzir o arquivo original com os codecs que suporta.
5. Deixe **Repetir** ligado e observe dez voltas. O RAF é visual, e a trilha eventualmente presente no arquivo de origem pertence à vista do original. **Ver a junção** inspeciona os dois quadros de cada lado da volta. Reduzir a velocidade nessa vista afeta apenas o diagnóstico.
6. **Aplicar ao tema** seleciona a cena para a prévia PS4, a exportação RAF e o ZIP de recursos. **Desativar loop aplicado** recupera o fluxo de animação da fase 3.3. Um ensaio novo mantém o último loop aplicado até você escolher outro. Uma alteração de sobreposição pede nova renderização antes de usar o resultado composto.

## Estratégias e indicadores

| Estratégia | Operação efetivamente executada | Condição de uso |
|---|---|---|
| Corte otimizado | Procura vizinhanças temporais semelhantes, confere a passagem do fim ao início na cadência escolhida e reordena candidatos com uma prova de DXT em miniatura. | Escolha inicial. Candidatos alternativos e a avaliação de saída permanecem disponíveis. |
| Alinhamento local | Compara o fluxo fim→início ao movimento previsto pelos vizinhos. Reamostra uma única imagem por instante, com deslocamentos pequenos e graduais perto do fim. | Exige fluxo consistente em ambas as direções, área móvel, deformação sem dobramento e melhora antes e depois do DDS. Se falhar, conserva o corte. |
| Recorrência de textura | Procura um período observado em várias fases de **dois ciclos consecutivos**. Compara o erro da repetição ao movimento natural em blocos de uma grade 8×6. | Pelo menos três blocos móveis e concordância em 80% deles. A fase final volta a conferir o ciclo vizinho. Ausência de evidência impede aplicar o resultado desta estratégia. |
| Ajuste manual e proteção | A entrada e a saída podem ser editadas em todos os modos. O alinhamento permite proteger um retângulo em percentuais da tela. | O retângulo, o fundo imóvel e a margem da imagem ficam protegidos da deformação. As camadas são aplicadas depois do ajuste temporal. |

A hipótese testada é que uma boa volta precisa preservar o deslocamento habitual da cena. O motor mede cor, luminância, bordas, diferença máxima por região, resíduo após compensar movimento, velocidade e direção. Compara penúltimo→último e primeiro→segundo com último→primeiro e com as transições internas. A fração de vetores em sentidos opostos e o mapa espacial 4×3 ajudam a interpretar objetos pequenos e regiões imóveis. A análise usa estimativas ópticas. Oclusões, reflexos, texturas sem contraste e movimentos opostos em regiões distintas podem produzir avaliações conservadoras.

**Candidato** significa que o trecho passou pelos critérios medidos. **Revisão visual** indica evidência fraca ou custo elevado. **Inadequado** bloqueia aplicação e download do RAF daquele ensaio. O monitor continua disponível para localizar o defeito. Uma fonte completamente parada também fica fora da aplicação como loop contínuo. O relatório registra quatro medições, além do DDS de referência anterior ao refinamento. O custo é uma penalidade relativa à cena. Seu valor não expressa probabilidade de invisibilidade.

O relatório de compressão inclui erro RGB, nitidez relativa, variação do erro entre quadros, gradientes que viraram patamares e indício de uma segunda borda perto de uma borda preservada. Os dois últimos são sinais para inspeção de banding e ghosting. Diferenciar um defeito do vídeo de um artefato introduzido requer comparar a mesma região nas quatro vistas. Objetos com trajetórias não periódicas são sinalizados por deslocamento local e quebra de fluxo, sem reconhecimento semântico de objetos. A síntese de água a partir de uma imagem, preenchimento de oclusões e a mistura de várias fontes animadas permanecem fora desta versão.

## Execução e armazenamento

- FFprobe lê metadados e até 240 pacotes para obter timestamps de apresentação e regularidade da **amostra**. FFmpeg começa antes do ponto de entrada quando possível, seleciona os instantes crescentes na grade da fonte e usa o enquadramento canônico. A prévia do RAF usa a cadência do XML. O teste inclui entradas entre dois quadros do vídeo.
- A busca preliminar cobre até quatro janelas distribuídas pela duração, com até seis segundos, 72 amostras e 128×72 pixels por janela. Os seis candidatos mais promissores e suficientemente distintos passam por avaliação mais detalhada. Em vídeos longos, intervenções manuais permitem trabalhar fora das janelas. O render final lê apenas o trecho selecionado, com um curto trecho anterior para acertar a amostragem.
- O perfil **Qualidade cinematográfica** prioriza 1280×720. **Movimento suave** usa o orçamento de 640×360 para maior cadência. **Loop prolongado** avalia a duração e reduz a resolução quando o número de quadros pede isso. **Personalizado** permite controlar o intervalo, os quadros e a resolução. Os perfis conservam a velocidade temporal da fonte. A busca descarta recomendações com cadência muito baixa para o material.
- `POST /api/motion/analyze` e `POST /api/motion/render` devolvem um identificador com HTTP 202. A UI consulta o progresso em `/api/motion/jobs/{id}` e pode solicitar cancelamento. Há até dois trabalhos por processo e um por projeto. FFmpeg tem prazo por operação e é encerrado em cancelamentos. A codificação verifica cancelamento entre quadros. `./start.sh` usa um processo Uvicorn.
- Os pixels RGB em resolução final ficam em arquivo temporário mapeado, limitado pela combinação de tamanho e quadros. O maior lote bruto das três resoluções ocupa menos de 36 MiB de RGB. A composição e a comparação abrem imagens individuais e pequenos proxies. O navegador carrega uma sequência do laboratório por vez e usa o relógio monotônico para escolher o quadro. Uma aba em segundo plano suspende a reprodução. A carga do dispositivo pode fazer o navegador perder atualizações de tela.
- `state.motion.version = 1` guarda o trabalho de origem, estratégia, região de proteção, intervalo, resolução, contagem e referências à análise, à última renderização e ao resultado aplicado. A espera deriva do intervalo e consta no relatório imutável e no XML. O campo é opcional no manifesto v1. A duplicação usa vínculos de arquivos imutáveis e conserva revisões independentes. A autenticação por sessão, o autosave e o conflito HTTP 409 seguem o sistema de projetos.
- O cache reside dentro do projeto e é identificado por versão do algoritmo, SHA-256 da fonte, parâmetros e camadas. Cada operação publica o resultado depois da verificação do ZIP. Resultados salvos ficam protegidos. Resultados transitórios antigos são descartados conforme idade e quantidade. Uma referência muito antiga de desfazer em outra aba pode precisar de nova renderização. Um reinício interrompe operações pendentes e mantém os resultados concluídos. A mesma solicitação recupera o cache ou reconstrói a saída danificada. Nenhum original é alterado por essa limpeza.

## Contrato RAF e compatibilidade

| Resolução | Limite efetivo | Bytes por DDS |
|---|---:|---:|
| 1280×720 | 13 | 460.928 |
| 960×540 | 24 | 259.328 |
| 640×360 | 48 | 115.328 |

O orçamento corresponde à soma dos DDS, com teto de 6.291.456 bytes. O render usa o DXT1a nativo, o plano MDX e o construtor de cena da recuperação. Cada `<case>` tem a mesma espera decimal em segundos, com a precisão que o gerador original escreve. O servidor reabre o ZIP, verifica arquivos, conteúdo, tamanho real e esperas, e registra hashes do ZIP e dos membros. A vista final decodifica os DDS e os transporta como WebP sem perdas. A árvore do tema reutiliza exatamente essa cena.

## Reproduzir no Linux

A preparação do Codespace instala FFmpeg e compila OpenCV 4.11.0 a partir do pacote-fonte `opencv-python-headless` 4.11.0.86, fixado por SHA-256. A compilação seleciona core, imgproc, video e os bindings de Python. IPP, GPU/OpenCL, rede neural e codecs de vídeo do OpenCV ficam desativados. FFmpeg continua responsável pela leitura. O código do OpenCV usa Apache-2.0 e o empacotador Python usa MIT. A compilação exige C++ e leva alguns minutos na primeira abertura. Nenhum componente Sony participa do laboratório. [Código do OpenCV](https://github.com/opencv/opencv/tree/4.11.0) · [Publicação do pacote-fonte](https://pypi.org/project/opencv-python-headless/4.11.0.86/)

No terminal do Codespace, os testes usam as dependências de desenvolvimento da bancada, incluindo HTTPX e Playwright com Chromium instalado. A suíte de compatibilidade lê o commit `8a9ef7f` dos objetos Git. Para usar um vídeo próprio, defina `CZAR_ACCEPTANCE_VIDEO` com o caminho de uma cópia privada no Codespace. O uso normal do estúdio permite selecionar o arquivo diretamente no tablet.

```sh
source .venv-motion/bin/activate
PYTHONPATH=. python tests/test_motion_experiments.py -v
PYTHONPATH=. CZAR_ACCEPTANCE_VIDEO=/caminho/privado/flower.mp4 python tests/test_motion_api.py -v
PYTHONPATH=. CZAR_ACCEPTANCE_VIDEO=/caminho/privado/flower.mp4 python tests/test_motion_compat.py -v
PYTHONPATH=. CZAR_CHROMIUM=/usr/bin/chromium python tests/test_motion_browser.py
```

## Aceitação física no Galaxy Tab S10 Ultra

1. No Chrome em paisagem, abra a branch `feature/studio-infinite-motion-v1` no repositório `czarsazevedo-gif/czar-ps4-theme-studio`. Em **Code → Codespaces**, crie um ambiente nessa branch. Um ambiente novo tem armazenamento próprio. Projetos de outro Codespace permanecem no disco em que foram criados.
2. Aguarde a preparação do contêiner. Confirme a branch com `git branch --show-current` e registre `git rev-parse HEAD`. Inicie com `./start.sh`, abra **Portas → 8000 → Abrir no navegador** e confira a visibilidade **Privada**. Registre versão do Chrome, dispositivo e orientação.
3. Em um projeto de teste, importe uma gravação de rio ou nuvens com deslocamento conhecido. Registre nome, resolução, FPS e duração. Selecione um ponto reconhecível na água ou nas nuvens para acompanhar a direção. Procure junções nos perfis de qualidade e de movimento e ouça o vídeo apenas na vista original, se houver áudio.
4. Renderize o candidato escolhido. Reproduza dez voltas no **RAF final** com o contador ligado para conferir a quantidade. Oculte o contador e tente identificar a junção sem olhar a timeline. Registre saltos, pausa, inversão, luminosidade, margem exposta, perda de nitidez e se os defeitos já aparecem no corte. Aplique apenas o resultado que você aceitaria usar no tema.
5. Repita com uma cena de objeto atravessando o quadro e outra com um corte. Confira a indicação da causa ou anote um falso positivo ou falso negativo. Teste a estratégia de textura com duas repetições naturais disponíveis e o alinhamento com uma região protegida. Compare a mesma borda antes e depois.
6. Em retrato, deslize a faixa de miniaturas, use a S Pen nos campos de entrada e saída, ajuste o zoom, desfaça e refaça. Confira que ainda alcança os controles de renderização e cancelamento. Acrescente uma camada raster no compositor. Verifique a posição no primeiro e no último quadro. Exporte o RAF e a árvore do tema com a composição aplicada.
7. Recarregue o navegador, reinicie o Uvicorn, reabra e duplique o projeto. Edite a cópia e confira o original. Registre o resultado observado e os timestamps do trecho, resolução, quadros, espera e bytes dos DDS. A aceitação de instalação e aplicação no PS4 requer um ensaio físico separado com um pacote legítimo.

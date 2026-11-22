# Prompt — Auditoria adversarial e reforço do motor PS4 PKG (Gold Master)

> **Papel:** atue como engenheiro sênior de engenharia reversa de formatos, integração multimídia, qualidade de software e testes de compatibilidade PS4. Seja obstinado, mas rigoroso: não confunda um arquivo estruturalmente válido com um tema comprovadamente funcional no console.

## Objetivo e limites obrigatórios

Faça uma auditoria integral do motor **CZAR PS4 Theme Studio / Ouroboros** no repositório `czarsazevedo-gif/czar-ps4-theme-studio`, exclusivamente na branch **`feature/czar-studio-gold-master-v1`**. Não altere a `main`, não faça *merge*, não force *push* e não destrua modificações preexistentes. Crie commits pequenos, reversíveis e acompanhados dos respectivos testes. Antes de executar, identifique o HEAD e inventarie o estado do repositório.

O fim é produzir temas dinâmicos `.pkg` confiáveis e auditar, com evidências independentes, o pacote anexado `UP9000-CUSA00000_00-ZENITSUSOUL00000.pkg`, os pacotes gerados pelo motor e, quando legalmente disponíveis, pacotes obtidos de **outros empacotadores**. **Não parta da premissa de que foram anexados dois PKGs:** confirme o inventário real. Se houver apenas um, declare isso e fabrique comparadores por rotas verificáveis.

**Regra de honestidade:** nenhuma quantidade de testes em PC prova, por si só, que um PS4 reproduzirá vídeo, ícones e música perfeitamente. Distinga sempre três níveis: `PACOTE GERADO`, `PACOTE VALIDADO ESTRUTURALMENTE` e `PACOTE TESTADO EM PS4 REAL`. O terceiro exige instalação e uso efetivos em um PS4 compatível, com registro da versão do firmware. Nunca o infira de checksums, emuladores ou prints de terminal.

## 1. Preparação reproduzível

1. Identifique a branch, HEAD, dependências, SO, arquitetura, espaço livre, versões de Python, FFmpeg, ImageMagick e .NET e o estado de cada ferramenta.
2. Faça inventário das ferramentas externas de código aberto, suas versões, licenças e commits exatos. Instale/compile apenas de repositórios reconhecidos e commits fixados, sem misturar material proprietário sem licença.
3. Use como referências independentes, quando disponíveis: **OpenOrbis/LibOrbisPkg (`pkg_validate`, `pkg_extract`)**, o leitor binário independente do projeto (`pkg_inspector.py`), **OrbisPkgTool** ou outro leitor de terceiro autor, e, **somente se houver licença**, as ferramentas oficiais Sony/Orbis. Declare claramente quando duas verificações partilham a mesma biblioteca interna e, portanto, não são independentes entre si.
4. Preserve todo artefato original; opere em cópias temporárias e registre SHA-256 antes e depois. Não altere o PKG fornecido.
5. Registre cada comando, parâmetros, códigos de saída e logs em formato legível por máquina e por humano. Qualquer ferramenta indisponível vira bloqueio documentado para aquele teste, não um resultado `PASS`.

## 2. Corpo de prova com vídeo REAL

Na pasta pública de mídia `https://drive.google.com/drive/folders/1V5pGlaNAU_QrD0SBVV6c1hy4VtmZBhto`, escolha o vídeo da **Zero Two** e/ou **Coroa de Espinhos**. Identifique o arquivo por nome e hash, além de codec, resolução, FPS, duração e faixa de áudio. Não substitua esses arquivos por vídeos sintéticos quando o teste tiver por objetivo comprovar a ingestão da mídia original.

Monte ao menos estes cenários, observando limites reais do RAF e a duração de loop:

| Cenário | Conteúdo | Finalidade |
|---|---|---|
| T0 | Tema estático simples | Controle do pipeline e regressão |
| T1 | Coroa de Espinhos, animação, música em loop, efeitos e **todos os ícones personalizados** | Caso completo principal |
| T2 | Zero Two, animação com cortes, contraste e movimento diferentes | Variabilidade de conteúdo |
| T3 | Animação no limite de orçamento RAF e de quantidade de quadros | Teste de fronteira |
| T4 | Música sem loop, música em loop, áudio curto, áudio longo, áudio ausente | Coerência de XML e metadados |
| T5 | Ícones RGBA com transparência, curvas finas e bordas de alto contraste | Regressão visual |

Empregue dimensões/quantidade de quadros admitidas pelo motor (por exemplo, 640×360, 960×540 e 1280×720), e nunca exceda o limite efetivo de bytes DDS/RAF. Se o vídeo original for longo demais para a taxa de quadros e o orçamento RAF, crie e identifique uma **amostra temporal derivada** para um teste adicional, sem fingir que ela representa a reprodução integral do original. Verifique quantitativamente `duração_da_animação ≈ quantidade_de_quadros × wait`, e examine a continuidade no ponto de retorno.

## 3. Testes estáticos, estruturais e criptográficos de cada PKG

Para cada candidato, automatize e arquive ao menos:

- `magic` CNT, offsets e limites de arquivo, contagem/tabela de entradas, alinhamentos, tamanhos de body/PFS, nomes, duplicatas, diretórios e ausência de caminhos perigosos.
- Identidade de **tema de sistema**: `content_type=0x1B`, `CATEGORY=ac`, `IRO_TAG=2` no cabeçalho (offset `0x98`) e no `param.sfo`, `CONTENT_ID/TITLE_ID` coerentes, `volume_type=pkg_ps4_theme`; rejeitar metadados exclusivos de aplicativos.
- Integridade SHA-256: HeaderDigest, body, tabela e entradas, imagem PFS e região assinada, Sc0; distinguir presença de bytes de assinatura, validade criptográfica da assinatura e aceitação pelo firmware.
- Extração **real do PFS/Image0** e de Sc0 por uma ferramenta independente. Se não houver extração, o estado máximo é `header_and_sc0_only`; nunca chame de `estrutura validada`.
- `theme.xml`: XML bem formado, `format-version`, paleta e cores, `homebgm-enable`, referências a imagens e sons, consistência estático versus dinâmico, sem caminhos inexistentes.
- RAF: `scene/background.raf/index.xml`, `plane.mdx`, cada DDS DXT1a, resolução, FourCC, comprimento exato, quadros referenciados na ordem correta, `wait`, número de quadros e orçamento total. Reabra e decodifique os DDS independentemente; compare com os quadros-fonte.
- Ícones de função: todos os slots personalizados previstos, **128×128 RGBA**, mais *glow* **152×152**. Ícones de conteúdo: **512×512 RGBA**. Inspecione alfa, halos, recortes, canais invertidos, bordas serrilhadas, arquivos ausentes, nomes exatos e arquivos duplicados. Valide também `sce_sys/icon0.png` (512×512), `pic1.png` (1920×1080) e preview (740×416).
- Entrada SVG para ícones: testar branco puro sobre transparência em 128/152/512 sem halos escuros, verificar renderização **mesmo quando ImageMagick não dispõe de `rsvg-convert`**, usar CairoSVG ou outro fallback seguro, e rejeitar `script`, entidades XML, `foreignObject`, `url(https://...)`, imports CSS e arquivos externos antes de rasterizar.
- Música: `sound/bgm_home.at9` com RIFF/fact/smpl quando exigidos, ATRAC9, 48 kHz, estéreo, bitrate/configuração compatíveis, `playCount=0`, marcação coerente do loop, duração e `bgmhome_loud` no SFO. Decodifique o áudio para PCM e meça descontinuidade no ponto de junção, silenciosos indevidos, clipping e níveis. Não afirme *loop perfeito* se não for testado em console.
- **Prova de audibilidade:** antes e depois do empacotamento, meça `peak dBFS` e nível médio; um AT9 formalmente válido com música praticamente muda NÃO conta como aprovação do caso de teste de BGM audível. Se o vídeo selecionado tiver uma faixa AAC silenciosa, obtenha música genuinamente audível de uma fonte autorizada e registre a proveniência.
- Efeitos sonoros: `.vag` com cabeçalho VAGp, 48 kHz, tamanho do payload, decodificação e vinculação aos eventos corretos.
- Descompactação e comparação byte a byte dos arquivos extraídos com os do bundle original, via SHA-256; permita diferenças apenas onde o formato exige transformação/normalização documentada.

**Bloqueio:** um único recurso essencial ausente, corrompido ou incoerente reprova aquele PKG. Um `pkg_validate` verde não pode encobrir `theme.xml`, RAF, áudio ou PNG inválidos.

## 4. Testes diferenciais entre ferramentas

Produza uma matriz por pacote e ferramenta: criação, leitura de cabeçalho, validação de digests, extração do PFS, leitura do `param.sfo`, validação de mídia, comparação do bundle e resultado físico. Procure diferenças reais de layout e de semântica, não igualdade binária entre temas distintos: `CONTENT_ID`, obra visual, número de quadros e áudio **devem** variar. Igualdade de SHA-256 entre arquivos inteiros distintos **não** é critério pertinente de compatibilidade.

Quando o mesmo tema puder ser produzido por mais de um empacotador independente, gere os dois pacotes e execute **validação cruzada** (construtor A → leitores A/B/C; construtor B → leitores A/B/C). Onde só houver um construtor viável, diga explicitamente que falta a prova de geração por um segundo motor e não invente um comparador.

## 5. Testes adversariais e de robustez

Trate todo PKG recebido como entrada não confiável. Desenvolva testes automáticos de:

1. Cabeçalho alterado, `IRO_TAG` incorreto, categoria de aplicativo, IDs divergentes e tamanhos/offsets inconsistentes.
2. Truncamento em vários offsets, um byte alterado no PFS, Sc0 e metadata, tabelas de entrada excessivas, entradas duplicadas e colisões de nomes.
3. `theme.xml` truncado, malformado, válido mas contraditório, referência inexistente e ciclo inesperado.
4. DDS incorreto, `index.xml` sem quadro, índices desordenados, `plane.mdx` inválido, orçamento excedido, dimensões não aceitas e tempo de espera estranho.
5. PNG com CRC ruim, PNG válido em dimensão errada, falso PNG, canal alfa ausente, transparência problemática e efeito *glow* inexistente.
6. ATRAC9 inválido ou interrompido, bitrate/config incompatível, tags RIFF/fact/smpl faltantes, duração de loop incorreta e áudio de volume extremo; VAG inválido.
7. Falta de ferramenta, execução com retorno não-zero, travamento, *timeout*, cancelamento, disco cheio, queda de energia simulada, repetição idempotente e concorrência de builds.
8. Variantes de nome/caminho (espaços, Unicode, caracteres de escape), pastas vazias, arquivo maior que o limite e entradas deliberadamente hostis.

Inclua pelo menos **1.000 mutações determinísticas com sementes registradas** (respeitando limites de CPU/memória), além dos casos de regressão nomeados; gere um resumo `pass/fail/skip` e guarde o exemplo mínimo que reproduz cada falha. Repita builds críticos ao menos 20 vezes; confira que não há arquivos `.tmp` órfãos, vazamentos, estados concorrentes ou PKGs parcialmente publicados. Uma falha intermitente é falha real até investigação.

## 6. Comprovação de execução no PS4

Planeje a instalação em **PS4 Slim, firmware 12.52**, em ambiente autorizado e compatível com o tipo de PKG. Antes de instalar, valide a legitimidade do método de instalação; não pressuponha que um console de varejo sem suporte a FPKG aceitará pacotes de desenvolvimento.

Checklist físico obrigatório com vídeo/foto e registro de data/firmware:

1. O console reconhece e instala o arquivo sem erro de pacote ou licença.
2. O tema aparece na lista, é selecionável e persiste após fechar o menu e reiniciar.
3. O fundo animado é reproduzido corretamente, com ordem, cores, escala, FPS e loop visual coerentes; não fica preto nem congela.
4. **Cada** ícone de função e conteúdo personalizado aparece na seção correta, sem aliasing grosseiro, rebarbas, trocas de nomes, recortes, deformações ou substituição indevida por padrão.
5. Música inicia, mantém volume correto e completa **três ciclos de loop**, sem silêncio, estouro, mudança abrupta ou travamento; eventos `.vag` são disparados quando apropriado.
6. Teste troca de aplicativos, navegação intensiva, suspensão/retomada, alternância entre temas, desligamento e reinicialização.
7. Compare capturas do PS4 com os recursos extraídos e anote diferenças visuais/sonoras com hipótese causal.

Se o console não estiver disponível, reporte **`COMPATIBILIDADE FÍSICA PENDENTE`**. Não transforme simulação em prova de instalação.

## 7. Correção imediata com prevenção de reincidência

Para **cada defeito reproduzido**:

1. Documente severidade, causa-raiz, reprodução mínima, camada atingida e evidência antes da mudança.
2. Corrija o ponto de origem com o menor patch seguro e preserve a compatibilidade dos formatos válidos. Não conserte dados alterando hashes ou silenciando validadores.
3. Crie um teste de regressão que **falhava antes e passa depois**; adicione *fault injection*, testes de propriedades e limites na camada apropriada.
4. Reexecute teste unitário, integração, testes diferenciais, benchmark real de mídia e verificação pós-extração. Evite falso `PASS` proveniente de ferramentas ausentes.
5. Adicione *fail-closed*: produção e download público de PKG bloqueados se algum portão crítico falhar; publicação atômica; locks, *timeouts*, limpeza e diagnóstico claro.
6. Fixe versões/commits e gere hashes dos executáveis auxiliares, um arquivo de manifesto de ferramentas, *logs* de CI e *golden tests* de temas estático e dinâmico.
7. Faça commit apenas na Gold Master, com mensagem explicativa; não faça *force push* e não oculte testes que falham por restrições do ambiente.

## 8. Critérios de aceitação e entregáveis

**Nenhuma liberação como “PKG pronto para PS4”** sem: (a) validação de cabeçalho e integridade; (b) extração PFS; (c) validação integral de XML/RAF/ícones/AT9/VAG; (d) comparação de hashes com a árvore de origem; (e) testes de regressão e corrupção aprovados; (f) teste físico completo, se esse status for reivindicado.

Entregue na branch Gold Master:

- PKGs de benchmark (Coroa e Zero Two, quando os dois forem acessíveis), bundles de origem, hashes SHA-256 e manifestos.
- Relatório Markdown e JSON com a matriz comparativa, versões de ferramentas, resultados individuais, logs, arquivos de falha reproduzíveis e classificação das evidências.
- Uma tabela de divergências **críticas/altas/médias/baixas**, com vínculo para o commit corretivo e seu teste.
- Scripts reproduzíveis para gerar, inspecionar e comparar PKGs e uma suíte contínua de regressão integrada ao CI.
- Checklist físico para o usuário preencher no PS4 (com observações e imagens) e lista de **pendências reais**, se houver.
- Resumo final honesto: o que foi comprovado, o que falhou e foi corrigido, o que foi pulado e **o que continua impossível de garantir sem hardware**.

Comece já pelo inventário, pela inspeção do PKG anexado e pela geração com mídia real. Não me peça aprovação a cada teste reversível; pause apenas quando faltar acesso, licença, hardware ou quando surgir alguma ação destrutiva/não autorizada.

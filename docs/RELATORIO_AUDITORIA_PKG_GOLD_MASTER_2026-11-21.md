# CZAR Gold Master — Relatório independente de auditoria PKG

**Data (horário do usuário):** 21/11/2026

**Branch auditada:** `feature/czar-studio-gold-master-v1`

**Escopo:** tema anexado ZenitsuSoul e benchmark dinâmico Coroa de Espinhos criado pelo CZAR Ouroboros.
**Status físico:** **PENDENTE — nenhum PS4 foi acessado nesta sessão.**

## 1. Resultado executivo

Os dois PKGs passaram em **três implementações de leitura/verificação**: o inspetor Python independente do CZAR, `OpenOrbis/LibOrbisPkg` (26 checagens explícitas por pacote) e `pearlxcore/OrbisPkgTool` (8 fases de análise por pacote). Seus cabeçalhos, checksums, imagens PFS e estruturas necessárias são íntegros segundo esses validadores.

O conteúdo do `ZenitsuSoul` foi completamente extraído e aprovado nos dez grupos de controles de mídia do projeto. O benchmark `Coroa de Espinhos`, que usou vídeo verdadeiro do Google Drive, música audível e todos os ícones de função/conteúdo personalizáveis, passou pela validação integral do bundle, extração PFS e **comparação SHA-256 de todos os arquivos extraídos** com os fontes produzidos pelo motor.

**Importante:** essas conclusões são de compatibilidade estrutural, não de instalação nem de reprodução efetiva no PS4. A equivalência perfeita de experiência entre temas com vídeos e músicas distintos não é demonstrável por identidade binária; a comparação útil é a conformidade de cada campo/recurso ao formato esperado.

## 2. Inventário, proveniência e hashes

| Evidência | PKG anexado — ZenitsuSoul | PKG gerado — Coroa de Espinhos |
|---|---|---|
| Origem | Anexo único fornecido pelo usuário; ferramenta de construção original não comprovada | API real do CZAR Gold Master, com `LibOrbisPkg` pinado |
| `CONTENT_ID` | `UP9000-CUSA00000_00-ZENITSUSOUL00000` | `UP9000-CUSA08652_00-COROADEESPINHOSQ` |
| SHA-256 | `9647b10bcdc99001500b0b0a49e3941c80d48322ceb31ec94ca7fbc0d79ef363` | `9355d48a32360a7ad717da969fdae82f0f1237b4b79f33f775e47e22da8ec2b5` |
| Tamanho | 13.762.560 bytes | 14.811.136 bytes |
| Total de arquivos Sc0 + Image0 | 60 | 61 |
| `content_type`, `CATEGORY`, `IRO_TAG` | `0x1B`, `ac`, `2` | `0x1B`, `ac`, `2` |
| `theme_type_valid` | Sim | Sim |
| Digests e integridade | Sem divergências | Sem divergências |
| Extração PFS | Sim | Sim |
| Áudio de fundo | ATRAC9, 48 kHz, estéreo, loop | ATRAC9, 48 kHz, estéreo, loop |
| Tema animado | 24 quadros DXT1a, 960×540 | 24 quadros DXT1a, 960×540 |
| Ícones de função e `glow` | 9 + 9 | 9 + 9 |
| Ícones de conteúdo | 10 | 10 |
| Efeitos `.vag` | Ausentes no exemplo | `se_decide.vag` presente e validado |
| Experimento em PS4 real | Não realizado | Não realizado |

**Fonte do vídeo da Coroa:** `the-crown-of-thorns-moewalls-com.mp4` do [Google Drive autorizado pelo usuário](https://drive.google.com/drive/folders/1V5pGlaNAU_QrD0SBVV6c1hy4VtmZBhto); original H.264 1920×1080, 60 FPS, 20,70 s. O benchmark dinâmico foi gerado de **trecho temporal derivado de 2,4 s** com 24 quadros: não se deve apresentar esse ensaio como teste de reprodução dos 20,7 s integrais.

**Fonte da música:** áudio ATRAC9 do PKG ZenitsuSoul anexado, decodificado/reconvertido para testar o codificador do motor. A faixa AAC originalmente embutida no vídeo da Coroa foi detectada como praticamente muda (máximo por volta de −91 dBFS); usá-la teria produzido um falso teste positivo de “música em loop”. A entrada audível teve pico de −9,9 dBFS; o ATRAC9 extraído do novo PKG teve pico de aproximadamente **−9,3 dBFS**, sem amostras decodificadas clipadas.

## 3. Ferramentas e independência

| Ferramenta | Origem | Método e resultado |
|---|---|---|
| `app/forge/pkg_inspector.py` | Leitor Python do CZAR, independente do construtor PFS | Cabeçalho, digests, `IRO_TAG`, Sc0 e relatório de extração: **ambos aprovados** |
| `pkgtool pkg_validate` + `pkg_extract` | `OpenOrbis/LibOrbisPkg`, commit `92a4e7f8e5914cb06b52ba3ae6b0675da438a051` com patch auditável de tema do repositório | **26/26 verificações OK** para cada PKG; extração funcional |
| `OrbisPkgTool validate` | `pearlxcore/OrbisPkgTool`, commit `3de6da849b64a8ad0d5ba00dd4e8919934b868b0`; compilado para .NET 10 em Linux no experimento | **8/8 estágios PASS** para cada PKG, inclusive PFS/PFSC e assinaturas |
| `FFprobe`/`FFmpeg` | Projeto FFmpeg | Decodificação separada de PNG/DDS, ATRAC9 e VAG; sem inconsistência no conteúdo auditado |
| `psgfx-at9` | `elripalda/psgfx`, commit `5ee7aa9e7d91c9f518322847d13a6bda5ba97837` | Codificador de música independente em Linux, usado pelo pipeline de teste |
| `vgmstream-cli` | `vgmstream/vgmstream`, commit `24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce` | Decodificou o ATRAC9 do PKG gerado, confirmou metadados de loop e **três ciclos contínuos** com `vgmstream_multi_cycle_verified=true` |

O `OrbisPkgTool` reporta `Type: Dlc` na tela de metadados porque seu classificador usa heurísticas de prefixo de `CONTENT_ID` e texto do título, não o `IRO_TAG=2` para esse caso. Isso é uma divergência da **etiqueta da ferramenta**, e não evidência de que o pacote seja DLC no console. O próprio validador profundo da ferramenta aprovou os dois arquivos. Seu **construtor** atualmente não preserva automaticamente a semântica de tema (`CATEGORY=ac`/`IRO_TAG=2`) ao fabricar novos PKGs; portanto, **não foi contado como um segundo construtor de tema validado**.

## 4. Validação de recursos extraídos

**ZenitsuSoul — dez grupos aprovados (zero erro, zero aviso):**

1. `theme.xml` versão 4.0, BGM habilitado e cores válidas.
2. Exclusividade da animação sem fundos estáticos mortos.
3. Miniatura PNG 740×416 válida.
4. `icon0.png` 512×512 válida.
5. `pic1.png` 1920×1080 válida.
6. Nove ícones de função 128×128 e nove `glow` 152×152 válidos.
7. Dez ícones de conteúdo 512×512 válidos.
8. RAF com 24 DDS DXT1a 960×540, `plane.mdx` e `index.xml` válidos; total DDS de 6.223.872 bytes, sob o teto de 6 MiB.
9. ATRAC9 48 kHz estéreo, 144 kb/s, configData `fe740bf0`, chunks `fmt`/`fact`/`smpl`/`data`, loop infinito (`playCount=0`).
10. `param.sfo` coerente, `CATEGORY=ac`, `IRO_TAG=2`, `CONTENT_ID` consistente e volume BGM descrito.

**Coroa de Espinhos — todos os controles aprovados:** além da equivalência de inventário dos 28 PNGs de ícones personalizados, os arquivos `scene/background.raf`, SFO, BGM, `se_decide.vag` e o conjunto Sc0 foram extraídos e comparados aos fontes por hashes. O relatório do motor indica `full_bundle_verified`, zero erros e zero avisos.

Uma análise auxiliar dos quadros DDS em miniatura encontrou descontinuidade visual do último para o primeiro de cerca de **1,58×** a mediana das transições adjacentes (indicador exploratório, sem limiar oficial de compatibilidade). A análise de áudio decodificado não encontrou pico de transição brusco entre o último e o primeiro sample; contudo, **um loop do hardware só pode ser julgado pelo hardware**.

Com `vgmstream-cli` compilado, a música gerada passou também em `validate_at9(... require_ps4_profile=True, require_loop=True)` usando o backend **`ffmpeg+vgmstream+riff`**, e `evaluate_post_at9_loop` confirmou **três ciclos** sem falha de decodificação (`vgmstream_verified=true`, `vgmstream_multi_cycle_verified=true`, `click_risk=0.00093`).

## 5. Defeito reproduzível encontrado e corrigido

**GM-PKG-POST-001 — aprovação indevida de tema interno defeituoso, severidade alta.** Antes da intervenção, se o extrator retornasse sucesso e houvesse um arquivo `theme.xml`, o inspetor pós-PKG podia produzir `structure_validated=True` mesmo com XML malformado, PNG essencial corrompido ou artefatos multimídia ausentes. O construtor normalmente validava o bundle na origem, mas um inspetor que recebe PKGs externos não pode pressupor essa etapa.

**Correção:** `app/forge/pkg_inspector.py` agora reconstrói temporariamente os arquivos Sc0, executa `validate_theme_bundle` **sobre o conteúdo extraído do PKG**, incorpora falhas reais ao status e rejeita links simbólicos que poderiam redirecionar a reconstrução para caminhos externos. O retorno fica em `verification_level=failed` quando os recursos internos são inválidos, mesmo que os digests do contêiner pareçam bons.

**Regressões adicionadas:** `tests/test_pkg_inspector_hardening.py` cobre XML quebrado, PNG Sc0 inválido com integridade de contêiner simulada e symlink malicioso sem sobrescrever arquivo externo. `tests/test_gold_master_checkpoint_e.py` ganhou três injeções de falha reais por byte em `IRO_TAG`, PFS e Sc0 icon; todas devem resultar em falha fechada.

**Prevenção complementar:** `scripts/audit_real_media_pkg.py` gera benchmark real com áudio, animação e todos os ícones; exige `full_bundle_verified`, faz download, confere SHA-256, extrai o resultado novamente e rejeita música praticamente inaudível tanto na entrada quanto no ATRAC9 final. Seu relatório JSON contém hashes das fontes e a situação física explicitamente pendente.

**GM-ICON-SVG-002 — conversão de SVG dependente de delegado ausente, severidade alta para ícones personalizados.** Um teste com SVG branco de alto contraste falhou porque o ImageMagick estava presente, mas seu delegado `rsvg-convert` não estava instalado. A presença do executável `magick` isoladamente não assegurava a importação de SVG. Corrigimos `app/media.py` para usar **CairoSVG** como rasterizador principal e mantivemos ImageMagick como alternativa, declarando a dependência em `requirements.txt`. O teste de transparência/halo em 128, 152 e 512 pixels passou. O sanitizador SVG foi reforçado para rejeitar `url(https://...)`, referências externas com atributos variantes, scripts e importações CSS; nova regressão exercita a rejeição.

## 6. Limites e riscos residuais

- **Não houve instalação em PS4 Slim 12.52**, nem inspeção no tema aplicado à interface real. Assim, reprodução de animação, exibição de ícones, licença/instalação, continuidade de áudio por três ciclos e comportamento pós-reinício ainda não foram comprovados.
- O usuário anexou apenas **um** PKG; sua origem de construção não foi comprovada. A comparação com o pacote novo confirma conformidade de formato e cobertura de recursos, não identidade de construtores nem igualdade funcional de conteúdos distintos.
- Não havia o construtor proprietário Sony/orbis-pub-cmd licenciado. O novo validador independente foi compilado e executado, mas não fornece, sem mudanças próprias, uma segunda produção de tema com metadados equivalentes.
- O teste de interface Chromium da suíte anterior esbarrou no isolamento deste ambiente (`socket() failed: Operation not permitted`). Testes que dependem de navegador Playwright instalado ou de servidor HTTP externo também podem ficar bloqueados por infraestrutura. Esses resultados não podem ser convertidos em aprovações silenciosas.
- Uma bateria inicial de 17 arquivos de testes relacionados reportou **60 aprovados, 7 falhos e 3 ignorados**. Os sete falhos decorriam de: ausência do decodificador `vgmstream-cli` (2), falta de `gifsicle` (2), falha SVG reproduzível (1), Chromium impedido pelo sandbox (1) e ausência de commit histórico no clone raso (1). **Seis dessas sete condições foram resolvidas e seus testes repetidos com sucesso:** `vgmstream-cli` foi compilado do commit pinado, Gifsicle 1.96 foi compilado da distribuição oficial, a falha SVG foi corrigida e o objeto Git histórico recuperado. A única categoria ainda bloqueada é a execução de Chromium por proibição de `socket()` no sandbox.
- **Reexecução concluída:** `67 passed, 3 skipped, 2 deselected, 0 failed`, em **347,88 segundos**. Os dois testes excluídos são especificamente os que lançam Chromium nesta infraestrutura impedida; não foram considerados aprovados. Os três *skips* foram declarados pelo próprio pytest. O resultado cobre 17 arquivos de testes voltados a Forge, Gold Master, áudio/ícones, regressões, Ouroboros e compatibilidade histórica, mas não substitui a instalação real no PS4 nem a totalidade dos testes que dependem de servidor externo ou browser.
- **Diagnóstico final das ferramentas:** `scripts/doctor_czar_full.py --json` mostrou **20 de 21 itens obrigatórios prontos**. O único indisponível é `PLAYWRIGHT`, não por ausência da biblioteca, mas porque o Chromium instalado aborta quando tenta abrir um socket proibido pelo ambiente. O Hyperfine 2.0.0 foi instalado com SHA-256 verificado contra o lock do projeto; os demais codificadores, validadores e ferramentas de benchmark essenciais para o experimento ficaram operacionais.
- Hashes e checksums confirmam integridade contra alterações, não substituem validação semântica nem comprovam aceitação pelo firmware.

## 7. Protocolo físico ainda exigido

1. Instalar o PKG em PS4 autorizado e apto a aceitar seu tipo de pacote; registrar firmware e método de instalação.
2. Selecionar o tema e verificar fundo animado, ordem dos 24 quadros, cores, escala e continuidade ao voltar ao quadro inicial.
3. Percorrer as nove funções e dez conteúdos com ícones customizados; fotografar quaisquer imagens ausentes, serrilhadas, deformadas ou com nomes trocados.
4. Escutar a música até completar pelo menos três loops, captando qualquer silêncio, clique, deslocamento de volume ou travamento. Testar o efeito `se_decide.vag`.
5. Navegar, abrir/fechar jogos, trocar tema, suspender/retomar e reiniciar o console; verificar persistência e ausência de erro.
6. Registrar vídeo/fotos, código de erro (se houver) e resultado por item. **Somente então** marcar `PACOTE TESTADO NO PS4`.

## 8. Reproduzir a auditoria automatizada

No repositório Gold Master, com o toolchain instalado:

```bash
bash scripts/install_forge_tools.sh at9
bash scripts/install_forge_tools.sh pkg

# Tests independentes de regressão (não requerem PS4):
PYTHONPATH=. pytest -q tests/test_pkg_inspector_hardening.py
PYTHONPATH=. pytest -q tests/test_gold_master_checkpoint_e.py

# Benchmark com vídeo/música/ícones fornecidos e uma workspace QA isolada:
CZAR_WORKSPACE=/caminho/seguro/qa PYTHONPATH=. python scripts/audit_real_media_pkg.py \
  --video /caminho/trecho-da-coroa.mp4 \
  --music /caminho/musica-audivel.mp3 \
  --icon /caminho/icone-coroa.png \
  --icons-source /caminho/referencia-extraida/uroot \
  --frames 24 --size 960x540 --output /caminho/resultado/Coroa_QA.pkg
```

**Conclusão:** ambos os pacotes demonstraram robusta conformidade estrutural em leitores independentes, com um defeito real de pós-validação identificado e corrigido. A reivindicação de compatibilidade **perfeita** com o PS4 permanece **não demonstrada** até o ensaio físico.

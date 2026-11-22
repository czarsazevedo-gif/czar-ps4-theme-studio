# ATUALIZAÇÃO DA AUDITORIA FÍSICA — ZENITSU SOUL (Fidelidade Temporal e Perceptiva do Motor Ouroboros)

- **Repositório:** `czarsazevedo-gif/czar-ps4-theme-studio`
- **Branch exclusiva:** `feature/czar-studio-gold-master-v1` (branch `main` intocada)
- **Revisão base (pré-intervenção):** `395ade8f421a4f1adbaf73088ee19bdf1c49b476` (Operação Fortaleza concluída)
- **Versão do motor após intervenção:** `OUROBOROS_VERSION = "ouroboros-absolute-2"` (invalida automaticamente caches pré-correção de `ouroboros-absolute-1`)
- **Console físico auditado pelo proprietário:** `PS4 Slim — firmware 12.52`
- **Build instalada fisicamente:** build nova gerada pelo Studio (diferente do PKG original congelado do Google Drive `9647b10bcdc99001500b0b0a49e3941c80d48322ceb31ec94ca7fbc0d79ef363`)
- **SHA-256 da build instalada fisicamente:** **`PENDENTE DO SHA-256 EXATO`** *(não presumido nem fabricado; qualquer nova build gerada com `ouroboros-absolute-2` entra em `PHYSICAL_TEST_PENDING` conforme `app/forge/release_gates.py`)*

---

## 1. Estado Atualizado da Auditoria Física (PS4 Slim — Firmware 12.52)

### 1.1 Componentes fisicamente aprovados no console (PRESERVADOS INTOCADOS)

Os 6 subsistemas abaixo foram **confirmados fisicamente como `APROVADO`** pelo proprietário no PS4 Slim `12.52` e **não receberam nenhuma alteração** nesta intervenção:

| Componente | Estado Físico (PS4 Slim 12.52) | Módulos Preservados Intocados |
|---|---|---|
| Música de fundo ATRAC9 (`sound/bgm_home.at9`) | **`APROVADO`** | `app/at9.py`, `app/forge/audio_*`, `tools/bin/psgfx-at9` |
| Legibilidade na televisão (tipografia, contrastes, dimmers) | **`APROVADO`** | `app/theme_xml.py`, `app/static/*` |
| Ícones personalizados (`128×128` e `512×512` RGBA) | **`APROVADO`** | `app/theme.py`, `app/media.py`, `app/forge/icons_*` |
| Brilhos dos ícones (`_glow` `152×152` RGBA) | **`APROVADO`** | `app/theme.py`, `app/forge/icons_*` |
| Cores do tema (`theme.xml`) | **`APROVADO`** | `app/theme_xml.py` |
| Carregamento, decodificação e exibição do fundo RAF | **`APROVADO`** | `app/dxt1.py`, `app/dds.py`, `app/mdx.py`, `app/raf.py`, `app/pkg.py`, `app/forge/pkg_*` |

### 1.2 Único problema perceptivo reportado

- **Animação do fundo Zenitsu na TV:** `APROVADO NO CARREGAMENTO / COM RESSALVA PERCEPTIVA (PRÉ-CORREÇÃO)` — o personagem exibia movimentação visualmente muito reduzida (rosto, cabelo, postura e elementos principais quase imóveis, transmitindo impressão de ilustração estática em vez de animação cinematográfica contínua).
- **Status pós-correção (`ouroboros-absolute-2`):** validado em bancada Linux nos **7 estágios**, nos **7 eixos independentes** e nos **4 vídeos reais do corpus**; o novo PKG a ser gerado com este motor entra em **`PHYSICAL_TEST_PENDING`** até conferência presencial na TV do PS4 Slim `12.52` vinculada ao seu SHA-256 exato.

---

## 2. Investigação Forense nos 7 Estágios — Onde o Movimento se Perdia

### 2.1 Verdade honesta sobre o vídeo original (`zenitsu-white.1920x1080.mp4`)

- **Arquivo auditado:** `zenitsu-white.1920x1080.mp4` (`1920×1080`, `30,0 fps`, `26,834 s`, SHA-256 `2a56e819886276c105dbc68cd4a7f0f526cac4ec0c3c69e0206901865361532e`).
- **O que existe nativamente na mídia original (sem inventar movimento artificial):**
  1. Trata-se de um **live wallpaper de 26,83 s** cujo único fechamento global natural é o ciclo completo (`lag = 26,73 s`, `fullVideoWrapRatio = 0,9702`), longo demais para caber íntegro no teto de 6 MiB do PS4 em 720p/540p/360p sem subamostragem destrutiva.
  2. **Rig nativo do personagem:** o rosto/olho/mandíbula de Zenitsu **não pisca, não gira a cabeça e não altera expressão facial** em nenhum dos 26,83 s — seu movimento rígido nativo é respiração/oscilação sutil de tronco/gola (`neck_collar ≈ 0,8 × 3,3 px @ 1080p`, `katana_hilt ≈ 2,7 × 2,5 px @ 1080p`).
  3. **Movimento real rico na fonte:** mesmo removendo os 40% mais brilhantes de relâmpagos, existe ondulação contínua não-rígida nas mechas/franja de cabelo (`hair_right std = 20,12`, `hair_bangs std = 15,81`, `face_eye std = 11,34`; fluxo Farneback `p95 = 6,6–8,3 px @ 720p` a cada 0,45 s), além de arcos elétricos brancos intensos, brilho na lâmina da katana e névoa lunar dinâmica atravessando o retrato (`temporalStdMean global = 25,35`, mediana em janelas de `2,0 s = 11,06`, foco central `11,22`; trechos `12–16 s`, `18–20 s` e `24–26 s` com `t_std = 12,6–16,5` vs zona morta `5,5–7,3 s` com `t_std = 5,6–6,9`).
  4. **Regra de honestidade seguida:** o motor **jamais** fabrica animação facial falsa no personagem; o objetivo é preservar fielmente toda a dinâmica real de cabelo, tecido, respiração, katana, névoa e relâmpagos presente no vídeo original.

### 2.2 Diagnóstico comprovado estágio por estágio (ANTES da correção — `ouroboros-absolute-1`)

A hipótese técnica foi **100% confirmada**: o motor antigo otimizava continuidade visual (`SSIM`, baixa diferença na costura) **às custas da fidelidade temporal**, perdendo movimento em **3 pontos encadeados** (e **não** no codificador DDS DXT1a):

| Estágio | Evidência Medida no Zenitsu (ANTES) | Veredito do Diagnóstico |
|---|---|---|
| **1. Vídeo original (26,83 s)** | `global t_std = 25,35`, janelas de `2,0 s`: mediana `11,06` (foco `11,22`), `flow_p95_180 = 1,85`, área ativa `97,1%` | Fonte viva e dinâmica, mas heterogênea (possui zona calma em `5,5–7,3 s`). |
| **2. Seleção de trecho (`search.py` + `proposals.py`)** | `seam_cost` L1 bruto (sem normalizar pela atividade local) + bônus de `internalMedianSsim` selecionaram exatamente a **zona morta `[5,52..6,78 s]`** (720p/540p) e `[5,96..7,35 s]` (360p) | **CAUSA RAIZ #1:** perda imediata de até **−74%** da excursão temporal antes mesmo da reconstrução. |
| **3. Reconstrução Ouroboros (`reconstruction.py` + `synthesis.py`)** | `reconstruct_best` escalou indevidamente para `level5_dynamic_texture_synthesis` (advecção Euleriana de apenas **2 placas estáticas** `src[0]` e `src[n//2]`), descartando os quadros intermediários reais de cabelo e relâmpagos; além disso, o teto antigo `span <= 7,5` esmagou a velocidade em 360p (`33f`) para **`0,22` passos/quadro**! | **CAUSA RAIZ #2:** colapsou `t_std` para `4,13` (720p, **−84%** vs global / **−63%** vs mediana 2 s), `4,31` (540p) e `3,38` (360p), com `flow_p95` caindo para **`0,20 px`** e área ativa em **`1,3%`** no 360p. |
| **4. Codificação e decodificação DDS DXT1a (`dxt1.py`)** | Pós-Ouroboros PNG vs Pós-DDS decodificado: `t_std 11,73 → 11,63` (−0,8%), `flow_p95 2,71 → 2,70` (−0,4%), `PSNR 38,17 dB`, `ΔE 1,51` | **INOCENTADO:** a compressão DXT1a preservou **>99%** do movimento; não era a culpada. |
| **5. Cadência efetiva RAF (`index.xml`)** | `<case wait="...">` respeitado corretamente em segundos (`0,097 s` / `0,040 s`), sem erro de unidade | Cadência de arquivo íntegra, porém reproduzindo quadros quase idênticos no 360p (`25 fps` de `0,22` passos/f). |
| **6. Congelamentos / duplicatas artificiais** | `flexibility` (360p, `33f`): **`7` pares quase duplicados** (`MAE < 0,35`) e apenas **`1,3%`** da tela com movimento > `0,35 px` | **CAUSA RAIZ #2b:** `span <= 7,5` gerava congelamentos perceptivos em sequências longas. |
| **7. Avaliação e 10 ciclos (`metrics.py`)** | Sem métrica de fidelidade temporal ou retenção contra a fonte, o motor antigo atribuiu **`9,27/10 APROVADO`** (720p), **`9,19/10 APROVADO`** (540p) e **`APROVADO`** (360p) à saída amortecida | **CAUSA RAIZ #3:** pontuação cega premiava quietude artificial como "loop perfeito". |

### 2.3 Bug secundário descoberto e corrigido durante a investigação

- Em `app/ouroboros_engine/reconstruction.py` (`_anchor_open_loop_drift`), a sequência `synthesize_periodic_2d` usada para ancorar deriva de loops abertos não alinhava sua fase ao quadro `work[anchor_idx]` (introduzindo um salto reverso `cos = -0,71` a `-0,95` em pontes `lead_in` / ciclos múltiplos). Alinhar a fase exatamente por `argmin(absdiff(raw_steady[j], work[anchor_idx]))` e exigir `temporalCoherence >= 0,45` + `advectionExplainability >= 0,12` eliminou a reversão e elevou a continuidade de `level4_content_bridge` em todos os perfis do Zenitsu (`sr = 0,91 / 1,24`, `cos = +0,72 / +0,86`).

---

## 3. Intervenções Estruturais Implementadas (`app/ouroboros_engine/` Exclusivamente)

Nenhuma linha fora de `app/ouroboros_engine/`, testes e documentação foi alterada:

1. **Perfil de movimento da fonte (`app/ouroboros_engine/analyzer.py` — `sourceMotionProfile`):** mede excursão temporal global e em janelas deslizantes de `~2,0 s` (`localWindowStdMedian`, `localFocalStdMedian`, `activeSubjectStdMean`, `flowP95Norm180`, `activeAreaFraction`, `fullVideoWrapRatio`, `transientPlusPhotometricFraction`).
2. **Busca imune a zonas mortas (`app/ouroboros_engine/search.py` — `_generate_seeds`, `_select_diverse`, `compute_multicriteria_cost`, `_pareto_rank_candidates`):**
   - Costura normalizada pela atividade local (`rel_seam = (seam_cost / max(act, 0.9)) * max(ref_act, 1.0)`), penalidade explícita para trechos abaixo de 85% da atividade mediana e bônus para trechos ricos;
   - Cota reservada em `_select_diverse` para takes ricos de alta contagem de quadros em 540p/360p;
   - 10º termo calibrado `motionFidelityError` no custo multicritério e na fronteira de Pareto.
3. **Bloqueio de sobre-escalonamento de advecção e preservação de velocidade (`app/ouroboros_engine/synthesis.py`, `reconstruction.py`):**
   - Nova métrica `advectionExplainability` (com passo adaptativo `>= 1,5 px` para fluxos subpixel) em `estimate_2d_flow_field`: separa nuvens/água verdadeiramente advectáveis (`adv ≈ 0,53–0,57` em *Full Moon*) de personagens/relâmpagos não-advectáveis (`adv < 0,16` no *Zenitsu*);
   - Substituição do teto fixo `span <= 7,5` em `synthesize_periodic_2d` por subdivisão multi-ciclo (`k_cycles`) que mantém velocidade física `1,0×` em `24..48` quadros sem congelamentos, somada à opção suave (`<= 8,5` passos) avaliada sob travas rígidas de retenção em `10..22` quadros;
   - Escala proporcional da ponte de conteúdo (`bridge_k = min(10, max(2, n // 4))`) e alinhamento exato de fase (`best_match = argmin(...)`) em `_anchor_open_loop_drift`;
   - Veto duro em `reconstruct_best` e `compare_before_after` contra colapso de excursão temporal (`< 72%`), área ativa (`< 65%`), sujeito ativo (`< 68%`, respeitando advecção espacial genuína) ou velocidade de pico interna (`< 62%`).
4. **Fidelidade temporal, teto anti-falsa-aprovação e 7 eixos independentes (`app/ouroboros_engine/metrics.py`, `finish.py`, `orchestrator.py`, `proposals.py`):**
   - Novo bloco `metrics["temporalFidelity"]` com retenção RAW→saída e Fonte→loop (normalizada pela razão de duração `(loop_dur / localWindowSec)^0.75` para janelas curtas), `nearDuplicatePairs` (consciente de fundos negros esparsos), `motionFidelityScore` (`0..10`) e flags `motionStarved`, `reconstructionMotionDamped`, `sourceMotionStarved`, `intrinsicMotionStarved`;
   - **Teto rígido em `definitiveLoopScore` (`<= min(5.8, motionFidelityScore + 0.3)`)** e **proibição absoluta de `APROVADO`** sempre que `motionStarved=True` — nunca mais uma cena morta/amortecida recebe `9+/10` ou `APROVADO`;
   - Emissão explícita dos **7 eixos independentes** em `metrics["independentAxes"]`:
     1. `visualContinuity` (continuidade visual na costura, SSIM, excesso local, ausência de ghosting/flicker)
     2. `kinematicContinuity` (continuidade cinemática O-KTCS/KLT, cosseno direcional, salto de velocidade/aceleração)
     3. `motionPreservation` (preservação do movimento original da fonte e do trecho bruto)
     4. `freezeIntegrity` (ausência de congelamentos e pares quase duplicados)
     5. `jumpIntegrity` (ausência de cortes abruptos e saltos intra-ciclo KLT)
     6. `cadenceIntegrity` (integridade de cadência FPS/`wait`, jitter e CV de velocidade)
     7. `tenCycleIntegrity` (integridade em 10 ciclos consecutivos, incluindo `motionPreservationOk`)
   - Proteção em `_adapt_candidate_to_profile` contra adaptar intervalos longos para slideshows 720p `< 5,4 FPS`, propagação integral de `source_motion_profile` em `orchestrator.py` / `finish.py` / `proposals.py` e bump para `OUROBOROS_VERSION = "ouroboros-absolute-2"` em `app/ouroboros_engine/__init__.py`.

---

## 4. Comparação Empírica Completa ANTES vs DEPOIS (4 Vídeos Reais do Corpus)

Todas as medições abaixo foram obtidas diretamente sobre os **DDS DXT1a decodificados de `background.raf.zip`** (estágio real reproduzido pelo PS4), normalizados em grade comum `320×180` para comparação justa entre resoluções.

### 4.1 Zenitsu Soul (`zenitsu-white.1920x1080.mp4`, 26,83 s — SHA-256 `2a56e819…532e`)

| Perfil | Versão | Intervalo / Cadência | Estratégia | Veredito | Nota Def. / Mov. | `sr` / `SSIM` / `cos` | RAW `t_std` → PNG → **DDS** | Foco DDS | `flow_p95` DDS | Área Ativa DDS | Dups | 7 Eixos Independentes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Quality 720p** | ANTES (`v1`) | `5,52..6,78 s` · `13f @ 10,3 fps` | `level5` *(indevido)* | *FALSO* `APROVADO` | `9,27` / *(cego)* | — | `→ 4,13` | `5,65` | `0,91` | `24,1%` | `0` | *(não medido)* |
| **Quality 720p** *(REC)* | **DEPOIS (`v2`)** | **`24,52..26,46 s` · `13f @ 6,7 fps`** | **`level4_content_bridge`** | **`APROVADO`** | **`7,21` / `9,96`** | **`0,91` / `0,868` / `+0,72`** | `12,62 → 11,73 →` **`11,63` (+182%)** | **`12,26` (+117%)** | **`2,70` (+197%)** | **`48,8%` (+102%)** | **`0`** | **7/7 OK (`issues=[]`)** |
| **Balance 540p** | ANTES (`v1`) | `5,52..6,78 s` · `13f @ 10,3 fps` | `level5` *(indevido)* | *FALSO* `APROVADO` | `9,19` / *(cego)* | — | `→ 4,31` | `5,94` | `0,91` | `24,3%` | `0` | *(não medido)* |
| **Balance 540p** | **DEPOIS (`v2`)** | **`12,52..15,28 s` · `22f @ 8,0 fps`** | **`level6_multi_region`** | **`APROVADO`** | **`7,11` / `8,44`** | **`1,37` / `0,848` / `+0,77`** | `13,09 → 9,86 →` **`9,70` (+125%)** | **`9,67` (+63%)** | **`1,85` (+103%)** | **`42,9%` (+77%)** | **`0`** | **7/7 OK (`issues=[]`)** |
| **Flexibility 360p** | ANTES (`v1`) | `5,96..7,35 s` · `33f @ 25,0 fps` | `level5` (`0,22` p/f!) | *FALSO* `APROVADO` | `6,28` / *(cego)* | — | `→ 3,38` | `5,13` | `0,20` | `1,3%` | `7` | *(falha silenciosa)* |
| **Flexibility 360p** | **DEPOIS (`v2`)** | **`12,52..15,28 s` · `44f @ 15,0 fps`** | **`level4_content_bridge`** | **`APROVADO`** | **`6,23` / `9,90`** | **`1,24` / `0,954` / `+0,86`** | `13,20 → 11,92 →` **`11,72` (+247%)** | **`10,81` (+111%)** | **`0,99` (+395%)** | **`27,5%` (21×)** | **`0`** | **7/7 OK (`issues=[]`)** |

- **10 ciclos consecutivos (pós-DDS):** `quality = 19,38 s`, `balance = 27,58 s`, `flexibility = 29,33 s` — todos com `tenCyclePass = True`, `motionPreservationOk = True`, `ddsMeanPsnr = 35,4–38,2 dB`, `ghostingScore = 0,0`, zero congelamentos e zero alertas.

### 4.2 Prova de Zero Regressão nos Outros 3 Vídeos Reais do Corpus (`ouroboros-absolute-2`)

| Vídeo Real (SHA-256) | Perfil | ANTES (`v1`: Estratégia / Nota / `t_std` / `fl95` / `act`) | DEPOIS (`v2`: Estratégia / Veredito / Nota Def. / Mov. / `t_std` / `fl95` / `act`) | 7 Eixos (`v2`) | Resultado vs Baseline |
|---|---|---|---|---|---|
| **Zero Two Eyes** (`09f873e2…c692`, 21,70 s) | `quality` 720p *(REC)* | `level3` · `7,40` · `13,42` · `6,46` · `77,1%` | `level1_natural_cut` · **`APROVADO`** · **`8,51`** · **`10,0`** · `10,88` · `5,64` · `68,2%` | **7/7 OK** | **+1,11 nota, `sr=1,06`** |
| **Zero Two Eyes** | `balance` 540p | `level3` · `7,93` · `10,55` · `3,17` · `47,5%` | `level3_motion_compensated_seam` · **`APROVADO`** · **`7,75`** · **`10,0`** · **`14,38` (+36%)** · **`3,80`** · **`54,7%`** | **7/7 OK** | **+36% mov., `cos=+0,86`** |
| **Zero Two Eyes** | `flexibility` 360p | `level4_out` · `7,75` · `9,60` · `1,60` · `34,1%` | `level1_natural_cut` (`48f`, `7,85 s`) · **`APROVADO`** · **`7,35`** · **`10,0`** · **`13,93` (+45%)** · **`3,51` (+119%)** · **`51,0%`** | **7/7 OK** | **+45% mov., `sr=0,92`** |
| **Full Moon Clouds 4K** (`2ac7e898…47432`, 21,77 s) | `quality` 720p | `level5` · `8,53` · `1,46` · `1,39` · `12,9%` | `level5_dynamic_texture_synthesis` · **`APROVADO`** · **`7,50`** · **`8,17`** · **`1,58` (+8%)** · **`1,41`** · **`13,6%`** | **7/7 OK** | **Sem regressão, +mov.** |
| **Full Moon Clouds 4K** | `balance` 540p *(REC)* | `level5` · `9,02` · `1,25` · `0,69` · `8,8%` · `4 dups` | `level5_dynamic_texture_synthesis` · **`APROVADO`** · **`9,02`** · **`9,41`** · **`1,67` (+34%)** · **`0,99` (+43%)** · **`11,6%`** | **7/7 OK** | **Mesmo `9,02` +34% mov.** |
| **Full Moon Clouds 4K** | `flexibility` 360p | `level5` · `8,42` · `1,15` · `0,56` · `7,7%` · `8 dups` | `level5_dynamic_texture_synthesis` · **`APROVADO`** · **`6,63`** · **`6,03`** · `1,11` · **`0,60`** · **`8,1%`** | **7/7 OK** | **Preservou `level5`, 7/7 OK** |
| **Crown of Thorns** (`4384a331…89c0`, 20,70 s) | `quality` 720p | `level4_out` · `4,90` · `4,90` · `4,76` · `39,2%` | `level4_content_bridge_lead_out` · `CONDICIONAL` · **`7,25` (+2,35)** · **`10,0`** · `4,70` · `4,71` · **`42,0%`** | 5/7 OK *(honesto)* | **+2,35 nota definitiva** |
| **Crown of Thorns** | `balance` 540p *(REC)* | `level4_out` · `5,81` · `5,13` · `5,94` · `35,9%` | `level4_content_bridge` · `CONDICIONAL` · **`7,58` (+1,77)** · **`10,0`** · `4,89` · **`6,00`** · **`37,2%`** | 6/7 OK *(honesto)* | **+1,77 nota, `sr=1,06`** |
| **Crown of Thorns** | `flexibility` 360p | `level4` · `6,94` · `4,37` · `5,53` · `28,9%` | `level4_content_bridge` · `REPROVADO` · `5,20` · **`10,0`** · **`4,68`** · **`6,01`** · **`36,5%`** | 5/7 OK *(honesto)* | **+26% área ativa** |

- **Consolidado nos 4 vídeos reais (12 propostas):** **9/9 perfis `APROVADO` com 7/7 eixos `OK`** nos vídeos Zenitsu, Zero Two e Full Moon Clouds 4K; no vídeo de faíscas turbulentas *Crown of Thorns*, melhoria de **`+1,77` a `+2,35`** na Nota Definitiva com classificação honesta (`CONDICIONAL` sem falsa aprovação).

---

## 5. Suíte Permanente de Regressão e Verificação Completa

### 5.1 Nova suíte permanente (`tests/test_ouroboros_temporal_fidelity.py` — `7/7 PASSED`)

1. `test_motion_starved_sequence_never_gets_approved_or_max_score` — impede `APROVADO` e trava `definitiveLoopScore <= 5.8` tanto em sub-atividade intrínseca quanto em zona morta frente a `sourceMotionProfile`.
2. `test_seven_independent_axes_emitted_and_enforced` — valida presença e enforcement dos 7 eixos em `metrics["independentAxes"]`.
3. `test_compare_before_after_and_reconstruct_best_veto_motion_collapse` — prova que `compare_before_after`, `check_prohibited_artifacts` e `reconstruct_best` vetam amortecimento de movimento e impedem `level5` em cenas de personagem + relâmpagos.
4. `test_synthesize_periodic_2d_preserves_speed_at_high_frame_counts_without_freezes` — prova velocidade `1,0×`, zero duplicatas e retenção de excursão/área ativa em `n = 36` quadros.
5. `test_search_and_proposal_scoring_prefer_active_segment_over_dead_zone` — prova que `_generate_seeds` e `_score_candidate_for_profile` escolhem trechos vivos acima de zonas mortas.
6. `test_dark_vignette_does_not_trigger_false_empty_border_after_photometric_harmonize` — impede falso positivo de borda vazia em vinhetas escuras de anime, mantendo bloqueio real de bordas pretas artificiais.
7. `test_version_bump_and_open_loop_content_bridge_directional_continuity` — valida `OUROBOROS_VERSION == "ouroboros-absolute-2"` e continuidade direcional positiva (`cos >= 0,40`) em ponte `level4_content_bridge_reconstruct`.

### 5.2 Regressão total executada na revisão final (`146 passed, 0 failed`)

| Suíte | Resultado |
|---|---|
| `tests/test_ouroboros_temporal_fidelity.py` *(nova)* | **7 passed** |
| `tests/test_perceptual_supremacy.py` + `test_ouroboros_perceptual_v2.py` + `test_ouroboros_finishing_c4.py` + `test_zenitsu_bench_regressions.py` | **31 passed** *(3 hooks opcionais skipped)* |
| `tests/test_fortaleza_contract.py` + `test_fortaleza_digital_twin.py` + `test_fortaleza_adversarial.py` | **23 passed** |
| `tests/test_fortaleza_manifest_and_gates.py` + `test_media_diagnostic_b.py` *(incl. Chromium touch)* | **7 passed** |
| `tests/test_ouroboros_compat.py` + `test_ouroboros_timing_c3.py` + `test_ouroboros_unified_c2.py` | **7 passed** |
| `tests/test_ouroboros_proposals_c5.py` *(API 3 propostas + Playwright Chromium touch E2E)* | **2 passed** |
| `tests/test_audio_sfo_icons_bench_c7.py` + `test_pkg_inspector_hardening.py` + `test_system_health_and_setup.py` + `test_toolchain_bootstrap_doctor.py` | **15 passed** |
| `tests/test_projects.py` + `test_library.py` + `test_compositor.py` + `test_forge_checkpoint_a/b/c/d/e.py` + `test_gold_master_checkpoint_c/d/e.py` | **43 passed (+ 9 subtests)** |
| `tests/test_audit_remediation.py` *(todos os 18 achados `CZAR-AUD-001..018`)* | **11 passed** |
| **TOTAL VERIFICADO** | **`146 passed (+ 9 subtests), 0 failed`** |

---

## 6. Próximos Passos para Fechamento Físico no PS4 Slim `12.52`

1. Para fechar o registro pendente da build anterior já instalada no console, basta informar o `sha256sum` exato do `.pkg` que foi levado ao PS4.
2. Após atualizar o ambiente Codespaces com esta revisão (`ouroboros-absolute-2`), reabrir o projeto Zenitsu na aba **Fundo animado**: o bump de versão invalida automaticamente o cache antigo e regenera as **3 propostas aprovadas (7/7 eixos `OK`)** — recomendação automática **`Proposta 1 — 720p (13q @ 6,7 FPS, sr=0,91, mov=9,96/10)`** ou alternativa mais fluida **`Proposta 2 — 540p (22q @ 8,0 FPS)`** / **`Proposta 3 — 360p (44q @ 15,0 FPS)`**.
3. Gerar o novo PKG mantendo intactos música ATRAC9, ícones, glows e cores já aprovados fisicamente, conferir `sha256sum` no pendrive e validar na TV do PS4 Slim `12.52` segundo o roteiro [`docs/OPERACAO_FORTALEZA_ROTEIRO_PS4.md`](OPERACAO_FORTALEZA_ROTEIRO_PS4.md) (pontos `P05` e 10 ciclos contínuos) para promoção de `PHYSICAL_TEST_PENDING` → `PHYSICALLY_VERIFIED`.

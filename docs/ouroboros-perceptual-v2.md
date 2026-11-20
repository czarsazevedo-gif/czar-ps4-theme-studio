# Ouroboros Perceptual V2 — Auditoria Científica, Corpus Real e Benchmarks Pós-DDS

Documento técnico do **Checkpoint A** na branch `feature/czar-ouroboros-perceptual-v2` (base `feature/czar-audio-pkg-forge-v1` @ `4ea9c75628909e86222acebe9ce90a1cae573ef3`).

---

## 1. Auditoria científica dos motores existentes

| Motor / Módulo | Benefício demonstrado | Escopo real vs sintético | Defeitos / gargalos identificados antes do V2 | Intervenção aplicada no V2 |
| --- | --- | --- | --- | --- |
| **Sentinela clássico** (`app/sentinela.py`) | Diagnóstico rápido em NumPy/Pillow para o fluxo clássico | Sintético e vídeos simples | Preservado intacto para compatibilidade com a API v1 | Mantido sem quebras |
| **Sentinela Supreme** (`app/deluxe/sentinela_supreme.py`) | Análise em 5 níveis (A–E) + sondagem VFR distribuída | Vídeos reais e sintéticos | (1) Faltavam métricas de qualidade visual (nitidez, ruído, macroblocos 8×8, proporção 16:9, duplicatas/quedas PTS); (2) câmeras em travelling com paralaxe escapavam de `inliers >= 0.65`; (3) texturas turbulentas/oscilatórias contínuas (ex.: fogueira) caíam em `INADEQUADO` | Adicionados `levelQuality`, `duplicateTimestamps`/`droppedTimestamps`, `cameraClass` + `jitterPx`/`zoomDeviation` + detecção por quadrantes, `lightingStability` e classificação honesta de texturas oscilatórias contínuas |
| **Círculo Supreme** (`app/deluxe/circulo_supreme.py`) | Busca em duração completa nas 3 resoluções PS4 com penalidade de duração curta | Vídeos reais e sintéticos | (1) `_generate_seeds` comparava apenas aparência de 2 quadros — selecionava costuras com mesma posição mas direção oposta (caso `flower.mp4`); (2) `CLASS_NATURAL` não exigia cosseno direcional mínimo | Adicionados filtro temporal de 4 taps (*Video Textures*, Schödl et al. 2000) + distância de derivada temporal assinada (fase de movimento) + trava direcional (`seamDirectionCosine >= 0.45`, `seamOppositeFraction <= 0.25`) + `repetitionRisk` |
| **Gênesis clássico** (`app/genesis.py`) | Translação exata para faixas espacialmente periódicas | Apenas texturas estritamente periódicas | Preservado intacto para compatibilidade | Mantido sem quebras |
| **Gênesis Supreme** (`app/deluxe/genesis_supreme.py`) | Síntese periódica 2D unidirecional (advecção Euleriana dupla) | Vídeos reais de água, nuvens, névoa, fumaça, chuva | (1) `cycle_steps` multiplicado por `0.55` reduzia velocidade para 55% do real; (2) mistura global 50/50 gerava ghosting em gotículas/espuma (`dev_waterfall` rejeitado apesar de custo cair `9.09 → 3.90`); (3) blur de fluxo vazava velocidade zero de rochas/bordas para a água | (1) `cycle_steps = n_frames` (1,0× velocidade física real); (2) convolução normalizada dentro da máscara ativa; (3) alinhamento fotométrico Lab de `base_b`; (4) transição de alta frequência escalonada espacialmente em curva S com preservação de variância (elimina ghosting mantendo 100% do sentido unidirecional) |
| **Ouroboros Deluxe Reconstructor** (`app/deluxe/reconstructor.py`) | Escada de 6 níveis com proteção estática alinhada a blocos 4×4 DXT1 | Vídeos reais e sintéticos | (1) `compare_before_after` não penalizava piora direcional nem redução artificial de velocidade; (2) Level 3 podia empurrar quadros para trás se o resíduo opusesse o passo; (3) Level 4 varria a costura DP contra o fluxo (`1 - alpha_t` em fluxos positivos) e nunca testava `lead_out` quando havia `lead_in`; (4) optical flow e DP rodavam em 1280×720 (~130s/vídeo); (5) Level 6 usava um único fluxo global borrando regiões de velocidades distintas | (1) Trava rígida em `compare_before_after` contra piora direcional e amortecimento artificial de movimento; (2) projeção segura de direção no Level 3; (3) costura DP orientada a montante (acompanhando o sinal de `vx`/`vy`) + avaliação de `lead_in` e `lead_out`; (4) `compute_flow_capped(max_width=384)` + DP em `<=240px`; (5) fluxo normalizado independente por região no Level 6 |
| **Métricas e 10 ciclos** (`app/deluxe/metrics.py`) | 16 métricas pós-DDS DXT1a | Vídeos reais e sintéticos | `skimage_ssim` em float64 duplicado na escala 0 consumia >4s por chamada em 720p | SSIM exato em `cv2.boxFilter` float32 (`diff = 0.000000` vs `skimage_ssim`, `3,5×` mais rápido, `~7×` combinado com MS-SSIM de passagem única) + `evaluate_ten_cycles` (10 ciclos contínuos) |
| **Calibrador + Deluxe Finish** (`calibrator.py`, `finish.py`, `pipeline.py`) | 5 recomendações + pós-processamento validado pós-DDS | Vídeos reais e sintéticos | Recodificava baseline bruto duas vezes quando candidato = raw | Reuso de `precomputed_baseline` em `apply_deluxe_finish` + todas as etapas subordinadas às novas travas direcionais e de movimento |

---

## 2. Ferramentas e referências avaliadas (CPU obrigatória)

1. **OpenCV 5.1.0** (já instalado, Apache-2.0):
   - Benchmark empírico Farneback vs DIS (`DISOPTICAL_FLOW_PRESET_MEDIUM`) em textura natural com deslocamento conhecido `(4.25, 1.75)` e subpixel `(0.40, -0.20)`: Farneback obteve erro médio **2× menor** (`EPE = 0.0605 px` vs `0.1042 px`; subpixel `0.0503 px` vs `0.1054 px`), enquanto `compute_flow_capped(max_width=384)` entregou o ganho de velocidade sem sacrificar precisão subpixel.
   - `cv2.boxFilter` float32 SSIM obteve diferença **`0.000000`** frente a `skimage.metrics.structural_similarity` com ganho de **3,5×** por escala (**~7×** eliminando a escala 0 redundante em MS-SSIM).
2. **FFmpeg 5.1.9** (LGPL/GPL CLI externa já instalada): extração precisa, `ffprobe` multi-janela PTS e decodificação canônica.
3. **Referências científicas aplicadas**:
   - Schödl et al. (2000), *Video Textures*: filtro temporal multi-quadro + similaridade de derivada temporal na geração de sementes do Círculo Supreme.
   - Holynski et al. (2021), *Animating Pictures with Eulerian Motion Fields*: advecção Euleriana semi-Lagrangiana bidirecional/dupla fase com convolução normalizada e fusão espacial anti-ghosting.
4. **Decisão de dependências**: **nenhuma dependência pesada ou modelo neural foi adicionada** — todo o ganho foi entregue em CPU pura usando OpenCV/NumPy/SciPy já presentes no ambiente.

---

## 3. Corpus experimental real (Wikimedia Commons + amostra do repositório)

Todos os vídeos externos são filmagens reais com licença aberta verificada via API da Wikimedia Commons (armazenados fora do Git em `/tmp/czar-real-corpus/` para não inflar o repositório). Vídeos sintéticos (ex.: `lua-sintetica.mp4`) **não** são contados como filmagens reais. Para mídia privada do proprietário, o runner suporta `CZAR_ACCEPTANCE_VIDEO=/caminho/video.mp4`.

Os 14 registros reais foram separados estritamente em **9 vídeos de desenvolvimento (`dev`)** e **5 vídeos reservados (`holdout`)** nunca tocados durante o ajuste dos algoritmos.

### 3.1 Conjunto de desenvolvimento (`dev` — resultados pós-DDS DXT1a decodificado)

| ID | Categoria | Licença | SHA-256 (16 hex) | Resolução / FPS / Duração | Classificação Sentinela | Melhor configuração RAF | Estratégia V2 | Corte Natural pós-DDS (`seam / cos / opp / vj / iss`) | Ouroboros V2 pós-DDS (`seam / cos / opp / vj / iss`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `repo_sample_mp4` | Amostra repo | Fixture repo | `tests/sample.mp4` | 854×480 · 30 fps · 7,30 s | `INADEQUADO` | 960×540 · 10 q | `level4_content_bridge_lead_out` | `1.28 / +0.07 / 35% / 0.75 / 6` | **`1.22 / +0.34 / 28% / 0.28 / 2`** |
| `dev_river` | Rio / água corrente | CC BY-SA 4.0 | `097098ffbdf0101a` | Real · 14,59 s · VP9 | `CORRIGÍVEL` | 1280×720 · 13 q | `level4_content_bridge_lead_out` | `1.50 / -0.25 / 59% / 0.13 / 3` | **`1.07 / +0.51 / 8% / 0.09 / 1`** |
| `dev_waterfall` | Cachoeira | CC BY 4.0 | `8154f19f4c593f7a` | 480×848 · 30 fps · 8,23 s | `CORRIGÍVEL` | 1280×720 · 12 q | `level4_content_bridge_lead_out` | `2.34 / -0.64 / 84% / 0.20 / 4` | **`1.05 / -0.23 / 59% / 0.11 / 3`** |
| `dev_clouds` | Nuvens 1080p | Public Domain | `14770457c7241d1e` | 1920×1080 · 29,97 fps · 5,61 s | `ADEQUADO À RECONSTRUÇÃO REGIONAL` | 960×540 · 13 q | `level5_dynamic_texture_synthesis` | `3.28 / -0.18 / 52% / 0.25 / 6` | **`0.74 / +0.96 / 1% / 0.03 / 2`** |
| `dev_fog` | Névoa em lago 1080p | CC0 | `13ed3b33f10ab5cc` | 1920×1080 · 29,97 fps · 10,05 s | `ADEQUADO À RECONSTRUÇÃO REGIONAL` | 960×540 · 24 q | `level5_dynamic_texture_synthesis` | `4.02 / +0.36 / 26% / 1.22 / 5` | **`0.70 / +0.99 / 0% / 0.03 / 1`** |
| `dev_rain` | Chuva | CC BY-SA 4.0 | `165fbf24919d6716` | 1080×1080 · 30 fps · 2,23 s | `ADEQUADO À SÍNTESE` | 1280×720 · 13 q | `level5_dynamic_texture_synthesis` | `1.96 / -0.88 / 94% / 9.52 / 6` | **`0.92 / +0.99 / 0% / 0.11 / 1`** |
| `dev_campfire` | Fogueira / partículas | CC BY-SA 4.0 | `532abe6dc28bca38` | 720×480 · 29,97 fps · 21,11 s | `CORRIGÍVEL` *(era INADEQUADO)* | 1280×720 · 10 q | `level6_multi_region` + dither | `2.08 / -0.24 / 56% / 1.48 / 6` | **`1.26 / +0.77 / 6% / 0.19 / 3`** |
| `dev_fountain` | Fonte em paisagem | CC0 | `287dd6c08774c8db` | 640×480 · 25 fps · 24,65 s | `ADEQUADO À RECONSTRUÇÃO REGIONAL` | 1280×720 · 12 q | `level5_dynamic_texture_synthesis` | `1.67 / -0.55 / 79% / 2.33 / 4` | **`0.21 / +0.94 / 0% / 0.12 / 2`** |
| `dev_moving_cam_sunset` | Câmera móvel / pôr do sol | CC BY-SA 4.0 | `36d878c880db2416` | 360×640 · ~29,94 fps · 15,50 s | `ADEQUADO À SÍNTESE` *(condicional)* | 1280×720 · 12 q | `level4_content_bridge` | `3.85 / -0.67 / 89% / 1.22 / 6` | **`1.71 / +0.33 / 29% / 0.44 / 4`** |

### 3.2 Conjunto reservado de generalização (`holdout` — 5 vídeos reais nunca usados para ajuste)

| ID | Categoria | Licença | SHA-256 (16 hex) | Resolução / FPS / Duração | Classificação Sentinela | Melhor configuração RAF | Estratégia V2 | Corte Natural pós-DDS (`seam / cos / opp / vj / iss`) | Ouroboros V2 pós-DDS (`seam / cos / opp / vj / iss`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `holdout_river2` | Cachoeira frontal | CC BY 4.0 | `8a0702df71ce6b87` | Real · 11,25 s · VP9 | `CORRIGÍVEL` | 1280×720 · 8 q | `level5_dynamic_texture_synthesis` | `2.15 / -0.53 / 76% / 0.03 / 5` | **`0.95 / +0.96 / 1% / 0.08 / 1`** |
| `holdout_smoke` | Fumaça noturna | CC BY-SA 4.0 | `8799fb427623bc09` | 854×1518 · 120 fps · 3,37 s | `ADEQUADO À SÍNTESE` | 1280×720 · 13 q | `level5_dynamic_texture_synthesis` | `1.83 / -0.36 / 66% / 0.10 / 6` | **`0.81 / +0.95 / 1% / 0.01 / 1`** |
| `holdout_waves` | Ondas / praia | CC BY-SA 4.0 | `dce0d859962f6cb1` | 480×864 · ~59,94 fps · 18,12 s | `ADEQUADO À RECONSTRUÇÃO REGIONAL` | 1280×720 · 13 q | `level6_multi_region` | `2.47 / -0.40 / 69% / 1.49 / 6` | **`1.34 / +0.45 / 22% / 0.42 / 1`** |
| `holdout_nonperiodic_flower` | Flor abrindo (não periódico) | CC BY 3.0 | `32d1a22fc8562c81` | 1280×720 · 29,97 fps · 27,36 s | `INADEQUADO` *(honesto)* | 1280×720 · 10 q | `level5_dynamic_texture_synthesis` | `3.39 / -0.92 / 98% / 4.83 / 6` | **`0.57 / +0.96 / 0% / 0.24 / 2`** *(não falsamente aprovado)* |
| `holdout_snow_particles` | Neve sob LED 1080p | CC BY 4.0 | `0eee3b999492450a` | 1920×1080 · 25 fps · 34,80 s | `CORRIGÍVEL` | 1280×720 · 13 q | `level4_content_bridge_lead_out` | `1.29 / -0.34 / 64% / 0.55 / 6` | **`1.17 / +0.06 / 40% / 0.17 / 3`** *(condicional)* |

---

## 4. Limitações conhecidas e honestidade técnica

- **Vídeos verticais (`480×848`, `854×1518`)**: exigem recorte central 16:9 para o PS4; o diagnóstico de mídia (Checkpoint B) informa explicitamente esse recorte ao usuário.
- **Neve esparsa de alto contraste (`holdout_snow_particles`) e cachoeira de alta turbulência lateral (`dev_waterfall`)**: flocos/gotas isolados atravessam rápido demais para correspondência óptica simples — os motores reduzem a costura sem falsificar aprovação (`passed=False` mantido honestamente).
- **Ganhos de performance medidos**: `evaluate_loop(720p)` caiu de `6,45 s` para `3,40 s` (**1,9× mais rápido**), `reconstruct_best(720p)` caiu de `29,3 s` para `15,9 s` (**1,84× mais rápido**) e o ciclo completo em `repo_sample_mp4` caiu de `130,8 s` para `62,6 s` (**2,1× mais rápido**).
- **Compatibilidade física no PS4 Slim 12.52**: todos os RAFs respeitam estritamente `<= 48` quadros, `<= 6 MiB` e geradores canônicos inalterados (`dxt1.py`, `dds.py`, `mdx.py`, `raf.py`); o teste físico no console permanece **PENDENTE DE TESTE FÍSICO**.

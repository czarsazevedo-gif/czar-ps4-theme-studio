# Relatório Técnico Gold Master — Czar PS4 Theme Studio

> **Branches da Missão (sem merges, sem modificação de branches anteriores)**
> - **Base de referência**: `feature/czar-audio-pkg-forge-v1` (`4ea9c75628909e86222acebe9ce90a1cae573ef3`)
> - **Etapa 1 (Pilar I + Pilar II)**: `feature/czar-ouroboros-perceptual-v2` (`315f0cb`)
> - **Etapa 2 (Pilar III — Gold Master)**: `feature/czar-studio-gold-master-v1`
> - **Integridade canônica**: `0` alterações em `app/dxt1.py`, `app/dds.py`, `app/mdx.py` e `app/raf.py` em relação a `02ef1552b44074678f8874304c5df9ee077a2162`.

---

## 1. Resumo dos Três Pilares

### Pilar I — Ouroboros Perceptual (`feature/czar-ouroboros-perceptual-v2`, Checkpoint A)
Detalhes completos e tabelas de benchmark no corpus real de 14 vídeos (9 `dev` + 5 `holdout` intocados) estão documentados em [`docs/ouroboros-perceptual-v2.md`](ouroboros-perceptual-v2.md) e reproduzíveis via `scripts/benchmark_ouroboros_corpus.py`.
- `compare_before_after()` em `app/deluxe/metrics.py` agora impõe rejeição rigorosa para degradação direcional (`seamDirectionCosine`, `seamOppositeFraction`) e amortecimento artificial de movimento (`motionRetention`), com SSIM/MS-SSIM em float32 `cv2.boxFilter` ~7× mais rápido.
- `circulo_supreme.py` incorpora sementes estilo *Video Textures* (4-tap temporal + fase de derivada/movimento).
- `reconstructor.py` corrige o Level 3 (projeção direcional segura) e o Level 4 (varredura DP alinhada à direção do fluxo em `+x/-x/+y/-y`, avaliando `lead_in` e `lead_out`).
- `genesis_supreme.py` preserva a velocidade física real (`1.0×`), usa convolução normalizada sem vazamento de borda e mescla dual-phase com curva S anti-ghosting.
- `sentinela_supreme.py` inclui qualidade visual (`levelQuality`), estabilidade de câmera com guarda de âncora estática texturizada (`textured_static_frac`) e auditoria contínua de 10 ciclos (`evaluate_ten_cycles()`).

### Pilar II — Inteligência de Mídia e Guia do Loop Perfeito (`feature/czar-ouroboros-perceptual-v2`, Checkpoint B)
- Guia acessível diretamente na aba **Fundo animado** (`COMO ESCOLHER UM VÍDEO PARA UM LOOP PERFEITO`) explicando duração, resolução/16:9, FPS/timestamps, codec/compressão pós-DXT1a, câmera, iluminação, movimento, composição e exemplos favoráveis vs desafiadores sem rejeição automática.
- Diagnóstico automático pós-importação não-bloqueante e cancelável (`POST /api/ouroboros/deluxe/diagnose`) compartilhando a mesma chave de cache por conteúdo (`analyze_v2`) com `/api/ouroboros/deluxe/analyze`, retornando todos os **21 campos obrigatórios** e as **5 categorias oficiais de aptidão**:
  1. `FAVORÁVEL A LOOP NATURAL`
  2. `CORRIGÍVEL`
  3. `ADEQUADO À SÍNTESE`
  4. `DIFÍCIL / RESULTADO CONDICIONAL`
  5. `INADEQUADO AO RESULTADO DESEJADO` (cenas estáticas/sem movimento são estritamente classificadas nesta categoria).

### Pilar III — Gold Master (`feature/czar-studio-gold-master-v1`, Checkpoints C–G)
Auditoria profunda por subsistema com reprodução de falhas, correção na causa raiz e testes de regressão automatizados.

---

## 2. Registro Formal de Defeitos Corrigidos (Pilar III + Pilar II)

| ID | Problema | Reprodução | Causa | Solução | Arquivos | Teste Antes | Teste Depois | Regressão | Risco | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| **GM-B-001** | Cena periódica com câmera fixa e margem estática texturizada (rio cobrindo 85% do quadro) recebia falso positivo `cameraMoving=True`, rebaixando aptidão para `CONDICIONAL`. | Upload de vídeo periódico de fluxo com margem superior estática de 15% e diagnóstico via `/api/ouroboros/deluxe/diagnose`. | `sentinela_supreme.py` usava apenas `static_frac < 0.18`, sem verificar presença de âncoras estáticas texturizadas (`std >= 5.0`). | Adicionada guarda `textured_static_frac < 0.08` em `analyze_video_supreme()`. | `app/deluxe/sentinela_supreme.py` | Falhou (`CONDICIONAL` em vez de `NATURAL`). | Passou (`FAVORÁVEL A LOOP NATURAL`, `camera_pan` segue detectado). | `test_media_diagnostic_b.py` + `test_deluxe_checkpoint1.py` | Baixo | **RESOLVIDO** |
| **GM-C-001** | Prévia animada do Compositor (`POST /api/composition/animation`) ignorava loops Ouroboros Deluxe aplicados e exibia quadros brutos do vídeo original, divergindo do RAF/PKG exportado. | Aplicar loop Deluxe de sub-intervalo, adicionar camada no Compositor `animated`, comparar prévia com DDS exportado. | `compositor.py:animation_preview()` sempre lia `media.iter_raw_frames(job.source, ...)` e omitia `deluxe.key` na chave de cache. | Adicionado `_iter_animation_base_frames()` honrando `deluxe_frames`/`deluxe_dds` e incluído `deluxe_sig` na chave de cache. | `app/compositor.py` | Prévia divergia do DDS exportado. | Passou (`MAE < 4.5` quadro a quadro + `0.0` drift temporal). | `test_gold_master_checkpoint_c.py` + `test_compositor.py` | Baixo | **RESOLVIDO** |
| **GM-C-002** | Ícones PNG transparentes redimensionados (`128×128`, `152×152` glow, `512×512`) exibiam halo escuro (`RGB 190` em branco puro sobre branco, 279 pixels afetados). | Redimensionar círculo branco RGBA 256×256 para 128×128 via `media.render_image()` e compor sobre branco. | FFmpeg `swscale` interpola RGBA reto (não pré-multiplicado), misturando RGB `(0,0,0)` de pixels transparentes. | Adicionado `_render_transparent_rgba()` com Pillow Lanczos (alfa pré-multiplicado) e WebP sem perdas para prévias transparentes; preservado FFmpeg para fontes opacas/DDS. | `app/media.py` | `min RGB = 190` (279 pixels `< 245`). | `min RGB = 255` (`0` pixels `< 245`), paridade prévia ↔ exportação exata. | `test_gold_master_checkpoint_c.py` + `test_compositor.py` | Baixo | **RESOLVIDO** |
| **GM-C-003** | Uploads corrompidos ou truncados em `/api/slot/{id}/upload`, `/api/animated/upload` e `/api/convert/{target}` vazavam diretórios órfãos no projeto/workspace. | Enviar PNG/MP4/WAV truncado para os 3 endpoints e listar `media/` antes/depois. | `jobs.create()` era chamado sem limpeza de `job.root` em caso de exceção de decodificação. | Envolvido preparo em `try ... except BaseException: shutil.rmtree(job.root, ignore_errors=True); raise`. | `app/main.py` | Diretórios órfãos permaneciam em `media/`. | `0` diretórios vazados após rejeições `422`. | `test_gold_master_checkpoint_c.py` | Baixo | **RESOLVIDO** |
| **GM-C-004** | Duplicar um projeto (`POST /api/projects/{id}/duplicate`) descartava receitas regionais salvas do Ouroboros Deluxe. | Salvar receita em `ouroboros-deluxe/recipes/principal.json`, duplicar projeto e consultar receita no duplicado. | `projects.py:new_project()` copiava apenas `manifest.json` e `media/`. | Copiada pasta `ouroboros-deluxe/recipes/*.json` dentro da etapa atômica de duplicação. | `app/projects.py` | `404` no projeto duplicado. | `200` com regiões preservadas integralmente. | `test_gold_master_checkpoint_c.py` + `test_projects.py` | Baixo | **RESOLVIDO** |
| **GM-C-005** | Ícones de função compostos no Compositor (`fn:*`) geravam `*_glow.png` (`152×152`) por ampliação do PNG `128×128` em vez de renderização nativa em `152×152`. | Compor `fn:setting` com camada 256×256 e exportar bundle. | `composed_slots()` só gerava 128×128 e `_collect_slot_assets()` reescalava para 152×152. | Criado `composed_glow_slots()` renderizando nativamente em `152×152` a partir das camadas originais. | `app/compositor.py`, `app/main.py` | Glow ampliado de 128×128. | Glow nativo `152×152` RGBA sem halo. | `test_gold_master_checkpoint_c.py` | Baixo | **RESOLVIDO** |
| **GM-D-001** | Alterar ganho via `/api/audio/{job_id}/gain` destruía recorte/crossfade/fades do CZAR ATRAC9 Studio e deixava `post_at9_loop` obsoleto. | Converter FLAC 4s com trecho `0.5→2.5s` + crossfade 40 ms e aplicar `-6 dB` em `/api/audio/{id}/gain`. | `audio_gain()` chamava `_procesar_audio()` direto sobre `job.source`, ignorando `conversion_params`. | Adicionado `_reapply_forge_bgm_with_gain()` preservando `conversion_params` e atualizando `post_at9_loop` em ambos os fluxos. | `app/main.py` | Duração voltava para `4.0s` e perdia loop. | Duração mantida em `~1.96s`, loudness reduzido ~6 dB e `post_at9_loop` atualizado. | `test_gold_master_checkpoint_d.py` + `test_forge_checkpoint_b.py` | Baixo | **RESOLVIDO** |
| **GM-D-002** | Intervalos de loop invertidos, negativos, `NaN` ou fora da faixa em `finish_audio_loop()` geravam clique de 5 ms silenciosamente ou erro não padronizado. | Enviar `start_sec=1.5, end_sec=0.5`, `crossfade_ms=-10`, `start_sec=nan`, `start_sec=50.0` em `/api/forge/audio/preview-process`. | Faltava validação explícita de finitude e ordenação antes do clamp. | Validados finitude, não-negatividade e `end > start` em `finish_audio_loop()` retornando `ValueError` → HTTP `422`. | `app/forge/audio_loop.py` | Produzia trecho inválido silencioso ou erro opaco. | Todos os 4 casos retornam HTTP `422` claro em pt-BR. | `test_gold_master_checkpoint_d.py` | Baixo | **RESOLVIDO** |
| **GM-E-001** | Estágios de maturidade do PKG não expunham explicitamente o campo contratual de 3 níveis (`PACOTE GERADO` / `PACOTE VALIDADO ESTRUTURALMENTE` / `PACOTE TESTADO NO PS4`). | Inspecionar resposta JSON de `/api/forge/pkg/build`, `/history`, `/inspect` e cartão UI `#pkg-result`. | `PkgValidationReport` tinha apenas flags booleanas e `verification_level`. | Adicionado `package_status` em `contracts.py`, `pkg_inspector.py`, `pkg_router.py` e exibidos explicitamente os 3 níveis em `app.js` (nunca afirmando teste físico no PS4). | `app/forge/contracts.py`, `app/forge/pkg_inspector.py`, `app/forge/pkg_router.py`, `app/static/app.js` | Sem campo `package_status` unificado. | API e UI distinguem os 3 estágios com clareza. | `test_gold_master_checkpoint_e.py` + `test_forge_checkpoint_d/e.py` | Baixo | **RESOLVIDO** |
| **GM-E-002** | Registro histórico sem `verification_level` em `pkg_router.py:422` assumia padrão `"full_bundle_verified"`. | Inserir `report.json` legado sem `verification_level` e consultar `/api/forge/pkg/history`. | Valor default incorreto em `pr.get("verification_level", "full_bundle_verified")`. | Corrigido default seguro para `"failed"`. | `app/forge/pkg_router.py` | Reportava falsamente `full_bundle_verified`. | Reporta honestamente `"failed"` / `"PACOTE GERADO"`. | `test_gold_master_checkpoint_e.py` | Baixo | **RESOLVIDO** |
| **GM-E-003** | Falha de armazenamento (`OSError`) durante a publicação atômica do PKG deixava arquivo `.pkg.tmp` parcial em disco. | Injetar `OSError("No space left on device")` em `shutil.copyfile` durante build PKG. | `pkg_forge.py:217-219` não tinha `try ... finally` removendo `tmp_final`. | Envolvido `copyfile` + `replace` em `try ... finally: tmp_final.unlink(missing_ok=True)`. | `app/forge/pkg_forge.py` | `.tmp` permanecia no diretório. | `0` arquivos `.tmp` / `.staging-*` residuais; PKG anterior preservado. | `test_gold_master_checkpoint_e.py` | Baixo | **RESOLVIDO** |
| **GM-F-001** | Textos residuais em espanhol apareciam na interface pt-BR e respostas da API (`"Convirtiendo a .dds…"`, `"Exportado correctamente."`, `"(no instalado)"`, `"No se pudo contactar al backend."`, `"Hueco desconocido"`, `"Fondo animado"`). | Busca em `app/static/app.js` e `app/main.py` e inspeção da exportação RAF. | Strings antigas da base upstream não migradas para pt-BR / `i18n.js`. | Traduzidas todas as ocorrências para pt-BR e conectado `t("slot.themePurpose")`. | `app/static/app.js`, `app/main.py` | 6 ocorrências em `app.js` e 4 em `main.py`. | `0` strings residuais em espanhol. | `test_gold_master_checkpoint_f.py` | Baixo | **RESOLVIDO** |
| **GM-F-002** | Controle `<summary>` do **CZAR ATRAC9 Studio** media `34.4 px` de altura na aba Sons em paisagem (`1480×924`), abaixo do mínimo ergonômico de `44 px` para toque/S Pen. | Auditoria Chromium CDP com toque emulado em todas as 6 abas (`1480×924` e `924×1480`). | `.forge-audio-studio > summary` possuía apenas `padding: 6px 0` sem `min-height: 44px`. | Ajustado CSS para `min-height: 44px; padding: 10px 4px; display: flex; align-items: center;`. | `app/static/style.css` | 1 alvo `< 44 px` (`34.4 px`). | `0` alvos `< 44 px` em todas as 6 abas e `ouroboros.html` em ambas as orientações. | `test_gold_master_checkpoint_f.py` | Baixo | **RESOLVIDO** |

---

## 3. Benchmarks Reais Medidos (Linux 2 vCPU / 8 GiB, `test_gold_master_checkpoint_f.py`)

| Operação | Tempo medido (s) | Observação |
|---|---:|---|
| Arranque / `/healthz` + sessão inicial | **0.053 s** | Resposta instantânea offline |
| Importação de vídeo na Biblioteca (`sample.mp4`) | **0.206 s** | Upload multipart único + hash SHA-256 + miniatura WebP |
| Diagnóstico automático (21 campos) + Análise Deluxe | **16.418 s** | Cache por conteúdo compartilhado (`< 0.5 s` nas reconsultas) |
| Renderização Ouroboros Deluxe (`reconstruct_auto` + Finish) | **6.371 s** | Avaliação pós-DDS real + auditoria de 10 ciclos |
| Exportação `background.raf.zip` (reuso byte a byte Deluxe) | **0.025 s** | `0` recompressão redundante |
| Conversão ATRAC9 144 kbps (`auto_refined` + `ambient` + validação) | **1.820 s** | `psgfx-at9` + validação RIFF/FFmpeg round-trip + EBU R128 |
| Build PKG nativo Linux + extração `Image0`/`Sc0` + SHA-256 | **1.490 s** | `LibOrbisPkg` + `pkg_inspector` completo |
| **Pico de memória RSS do processo Python** | **312.1 MiB** | Folga de > 7,5 GiB no ambiente de 8 GiB |

### Ergonomia em Chromium Headless com Toque Emulado (`Emulation.setTouchEmulationEnabled`)
- **Paisagem (`1480×924`)** — todas as 6 abas do Studio (`backgrounds`, `icons`, `compositor`, `sounds`, `preview`, `package`) + `/static/ouroboros.html`: **0 overflow horizontal**, **0 alvos `< 44 px`**, **0 exceções JS**.
- **Retrato (`924×1480`)** — todas as 6 abas do Studio + `/static/ouroboros.html`: **0 overflow horizontal**, **0 alvos `< 44 px`**, **0 exceções JS**.

### Suíte Consolidada de Regressão e Aceitação (26 módulos pytest + `test_dxt1.py` + `diagnose_forge.py`)
- **Resultado consolidado**: **118 passed** (+ **26 subtests passed**), **3 skipped** (ganchos opcionais de vídeos privados do proprietário, preservando `CZAR_ACCEPTANCE_VIDEO`), **0 failed**.
- **Compatibilidade canônica RAF (`test_ouroboros_compat.py`)**: **6 variações / 30 entradas canônicas idênticas byte a byte** frente à base publicada `8a9ef7f2ca990cea2ed01439285a90f0770876ea` (com guarda de compatibilidade `tarfile.data_filter` para Python 3.11.2).
- **Aceitação E2E real de 20 etapas (`tests/test_gold_master_e2e_20steps.py`)**: executada contra subprocessos Uvicorn reais com **2 reinícios reais de servidor**, vídeo real (`dev_river.webm`), diagnóstico de 21 campos, renderização Deluxe + vínculo criptográfico (`HTTP 409`), ícones sem halo, Compositor animado + glow nativo `152×152`, ATRAC9 + VAG + preservação de loop no ajuste de ganho, RAF, validação de bundle, PKG nativo Linux (`LibOrbisPkg`), extração independente `Sc0`/`Image0` + conferência SHA-256 arquivo a arquivo e download pós-reinício — **APROVADO**.

---

## 4. Limites de Honestidade Técnica e Testes Físicos Pendentes

- **Testes automatizados em Linux / Chromium headless NÃO substituem testes físicos.**
- Os ensaios físicos abaixo permanecem explicitamente **`PENDENTES`**:
  1. **Galaxy Tab S10 Ultra (Chrome Android real + dedo + S Pen)** — roteiro detalhado em [`docs/acceptance-ps4.md`](acceptance-ps4.md).
  2. **PS4 Slim (Firmware 12.52)** — instalação via USB, aparição exclusiva em *Configurações → Temas*, reprodução prolongada de animação RAF e música ATRAC9, efeitos VAG e reinício do console (roteiro em [`docs/acceptance-ps4.md`](acceptance-ps4.md)).
  3. **Vídeos privados do proprietário** — o gancho `CZAR_ACCEPTANCE_VIDEO` em `tests/test_ouroboros_perceptual_v2.py` permanece disponível para validação local reproduzível pelo proprietário.

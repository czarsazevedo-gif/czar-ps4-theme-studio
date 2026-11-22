# MISSÃO I — OPERAÇÃO FORTALEZA: Relatório Técnico Completo de Auditoria Diferencial, Blindagem e Compatibilidade PKG PS4

- **Repositório:** `czarsazevedo-gif/czar-ps4-theme-studio`
- **Branch exclusiva:** `feature/czar-studio-gold-master-v1` (branch `main` intocada)
- **Baseline inicial (Regra Zero):** `d2114c97346c4ad1aa83b1615c2866a4d9e15c30` (`81/81` testes de baseline verdes antes de qualquer alteração; `21/21` dependências obrigatórias prontas no `doctor_czar_full.py`)
- **Referência original comparada:** `Imxnxl/PS4-Ultimate-Theme-Creator` (commit `aa595aa6cda8e906c7f21086b829a6b23223784e`)
- **Plataforma da bancada:** Linux Debian 12 / Python 3.11.2 / `.NET 8.0.428` / FFmpeg 5.1.9 / `vgmstream-cli r2168` / `psgfx-at9`
- **Estado de compatibilidade física (PS4 Slim, firmware `12.52`):** **`PHYSICAL_TEST_PENDING`** — **nenhuma compatibilidade física foi declarada ou presumida nesta rodada**; todas as aprovações em Linux restringem-se a integridade estrutural, contrato semântico, paridade multi-ferramenta e simulação no Digital Twin (`SEMANTIC_SIMULATION_ONLY`).

---

## Entregável 1 — Inventário e Baseline (Regra Zero & Fase 1)

Antes de qualquer alteração no código, foram auditados todos os módulos canônicos (`app/dxt1.py`, `app/dds.py`, `app/mdx.py`, `app/raf.py`, `app/pkg.py`, `app/theme.py`, `app/theme_xml.py`, `app/at9.py`, `app/vag.py`, `app/forge/*`, `app/ouroboros_engine/*`, `tools/src/LibOrbisPkg`):

| Área / Módulo | Estado no Baseline (pré-Fortaleza) | Lacunas de Falso-Positivo Encontradas na Fase 1 | Ação Aplicada na Operação Fortaleza |
|---|---|---|---|
| `app/mdx.py`, `app/theme.py`, `app/vag.py`, `app/dds.py`, `app/theme_xml.py`, `app/raf.py` | **100% canônico** (idênticos ao Imxnxl, com adições compatíveis `wait_list` e rótulos PT-BR) | Nenhuma divergência estrutural | Preservados integralmente (Regra Zero) |
| `app/dxt1.py` | Otimizado em `d2114c9`, 18/18 matrizes Zenitsu idênticas | **Descoberta na Fase 3 (A/B em `A3_dynamic_raf`):** 4/32.400 blocos com gradiente anti-correlacionado (`cov=[[8,0,-6],[0,0,0],[-6,0,8]]`) caíam no autovetor menor instável `[1,0,1]` e divergiam em 5–36 bytes de 259.328 B por diferença de arredondamento `matmul` vs `einsum` | **Corrigido na Fase 10:** `_principal_axis` restaurado para `einsum`/`linalg.norm` mantendo eliminação do buffer 4D → **0 bytes de diferença** em todos os 6 temas e **−30,6%** mais rápido em `13×720p` |
| `app/forge/bundle_validator.py` | Validava existência básica e contagens | Permitia `theme.xml` sem `label`/cores/dimmers; não bloqueava `<!DOCTYPE>`/`<!ENTITY>`; validava apenas `len(mats)==len(dds)` no `index.xml` (deixava passar ordem trocada, DDS órfão/ausente, `wait` em ms); validava apenas 16 B mágicos do `plane.mdx`; não aplicava lista branca em `Image0/` nem path-traversal/duplicatas em `.gp4` | **Blindado (Fase 5/7/10):** guarda XXE/DOCTYPE, tags obrigatórias em `theme.xml`, `mdx.parse` completo, decodificação `dxt1.decode_dds`, validação semântica 1:1 de `<material>`/`<actor>`/`<sequence>`/`wait` em `[0.01, 10.0]`s, lista branca canônica em `Image0/`, GP4 anti-traversal/duplicata |
| `app/forge/audio_validation.py` | Verificava perfil 144 kbps + `fact`/`smpl` + `ffmpeg` | Verificava apenas `loop_end > loop_start` sem checar limites contra amostras decodificadas/delay (`loop_end` fora de faixa passava!) | **Blindado (Fase 7/10):** validação estrita de `loop_start >= encoder_delay`, `loop_end - loop_start >= 1024`, `loop_end <= fact_samples/decoded_samples + delay + 2048` + aviso de áudio mudo |
| `app/forge/pkg_inspector.py` | Verificava cabeçalho CNT, 7 digests, Sc0, `pkg_extract` e `validate_theme_bundle` | Sobrescrevia silenciosamente IDs CNT duplicados (`by_id = {...}`); sobrescrevia `Image0/sce_sys/*` do PFS interno com `Sc0/*` sem checar divergência; não executava Digital Twin nem Portões de Confiança | **Blindado (Fase 6/7/10/11):** bloqueio de IDs CNT duplicados, paridade PFS interno ↔ Sc0 antes da remontagem, execução automática do PS4 Theme Digital Twin sobre extraído e cálculo fail-closed dos 7 Release Gates |
| `app/ouroboros_engine/applier.py`, `orchestrator.py`, `proposals.py`, `app/ouroboros.py` | Verificava hashes quando presentes | Ignorava `ddsHashes`/`files` ausentes ou parciais; não registrava/chegava `deluxe_frames/*.png`; não validava conteúdo interno de `background.raf.zip`; não recalculava `renderKey` nem coerência com `recipe` | **Blindado (Fase 8):** `framePngHashes` persistido, fail-closed em manifestos incompletos, auditoria interna do ZIP (`scene/background.raf/{00..NN.dds,plane.mdx,index.xml}`), recálculo criptográfico do `renderKey` e conferência de `recipe` |
| `app/forge/pkg_router.py` & `app/static/app.js` | Persistia `report.json` e bloqueava histórico legado | Alterar entradas/cores/ícones do projeto não invalidava selos antigos no histórico/relatório | **Blindado (Fase 8/11):** persistência de `inputs_fingerprint` + `integrity_manifest.json` + invalidação automática (`stale_vs_project=True`, `release_gate="REGRESSION_DETECTED"`, UI exibe `"Obsoleto (projeto alterado após geração)"`) |
| Compatibilidade física PS4 Slim `12.52` | Dependente de hardware físico | Nunca comprovável em Linux | Permanece estritamente `PHYSICAL_TEST_PENDING` até execução do roteiro presencial ([`OPERACAO_FORTALEZA_ROTEIRO_PS4.md`](OPERACAO_FORTALEZA_ROTEIRO_PS4.md)) |

---

## Entregável 2 — Matriz de Ferramentas (Versões, Commits, Licenças e Independência Real)

| Ferramenta | Repositório / Origem | Commit / Versão | Licença | Papel Real na Auditoria | Independência Real | Classificação / Limitações |
|---|---|---|---|---|---|---|
| **CZAR Studio + LibOrbisPkg** | `OpenOrbis/LibOrbisPkg` + patch auditado (21 linhas) | `92a4e7f8e5914cb06b52ba3ae6b0675da438a051` | LGPL-3.0 | Empacotador PKG Linux (`pkg_ps4_theme`, `IROTag.SS_THEME=2`) + `pkg_validate` / `pkg_extract` | **SHARED_BUILDER_FAMILY** (não conta como validador independente de si mesmo!) | `APROVADO` como construtor Linux; validado externamente por 3 ferramentas independentes |
| **Imxnxl Original (Grupo B)** | `Imxnxl/PS4-Ultimate-Theme-Creator` | `aa595aa6cda8e906c7f21086b829a6b23223784e` | MIT | Gerador de referência original para XML, DDS DXT1a, `plane.mdx`, `index.xml`, VAG, PNG, `param.sfx`, `.gp4` | **REFERÊNCIA UPSTREAM** | `APROVADO` nos ativos Python/Linux; `orbis-pub-cmd.exe` e `at9tool.exe` = **`NÃO EXECUTADO`** (binários proprietários Windows da Sony SDK legalmente excluídos) |
| **czar-python-pkg-inspector** | `app/forge/pkg_inspector.py` + `ps4_contract.py` + `digital_twin.py` | Branch `feature/czar-studio-gold-master-v1` | Projeto | Leitor binário CNT puro Python, 7 digests SHA-256, Sc0, Contrato PS4 (9 regras) e Digital Twin (10 ciclos RAF + 3 ciclos ATRAC9) | **INDEPENDENTE DO C#/.NET** | `APROVADO` (7/7 PKGs); explicita `SEMANTIC_SIMULATION_ONLY` |
| **pearlxcore/OrbisPkgTool** | `https://github.com/pearlxcore/OrbisPkgTool` | `3de6da849b64a8ad0d5ba00dd4e8919934b868b0` (compilado em `.NET 8.0`) | MIT | Validador independiente de 8 estágios (`validate`), leitor `info` e extrator (`extract`) | **100% INDEPENDENTE** (implementação própria em C#, sem código do `LibOrbisPkg`) | **`APROVADO`** como validador/extrator (7/7 PKGs, 8/8 estágios); **`FERRAMENTA INCOMPATÍVEL`** como construtor de temas PS4 (`PkgBuilder.cs:801,908,1017` fixa `CATEGORY="gd"/"gp"` e omite `IRO_TAG=2`) |
| **xXJSONDeruloXx/ps4-pkg-tools** | `https://github.com/xXJSONDeruloXx/ps4-pkg-tools` | C++23 ShadPS4 7.0 (compilado nativo) | GPL-2.0 | Descriptografador e extrator PFS/PFSC nativo em C++23 | **100% INDEPENDENTE** (zero código C#/.NET ou Python compartilhado) | **`APROVADO`** como extrator independente (7/7 PKGs, paridade SHA-256 100%) |
| **psgfx-at9 / vgmstream-cli / FFmpeg** | `elripalda/psgfx` (`5ee7aa9e`) + `vgmstream r2168` + FFmpeg `5.1.9` | Instalados via bootstrap | GPL-3.0 / ISC / LGPL | Codificação ATRAC9 144 kbps + decodificação independente multi-ciclo e medição EBU R128 | **DECODIFICADORES INDEPENDENTES** do codificador `psgfx-at9` | **`APROVADO`** (todos os `bgm_home.at9` decodificados por ambos) |

---

## Entregáveis 3, 4 e 5 — Corpus Reproduzível, Comparação Diferencial A/B/C (18 Dimensões) e Paridade Multi-Ferramenta

Script reproduzível: [`scripts/audit_fortaleza_corpus.py`](../scripts/audit_fortaleza_corpus.py) (saídas privadas fora do Git em `~/.gemini/czar-operacao-fortaleza/fortaleza_corpus_audit.json`).

### Resultados Consolidados nos 7 PKGs (6 Grupo A + 1 PKG Original Congelado do Drive)

| Pacote | Tamanho (B) | SHA-256 | Arquivos (Sc0+Image0) | A vs B (`Imxnxl` `aa595aa6`) | Inspector + Contrato PS4 | Digital Twin (`SEMANTIC_SIMULATION_ONLY`) | `OrbisPkgTool` (C#/.NET 8-estágios) | `ps4-pkg-tool` (C++23 ShadPS4) | Paridade SHA-256 entre os 3 Extratores | Release Gate |
|---|---:|---|---:|---|---|---|---|---|---|---|
| **`A1_static_simple`** | 2.031.616 | `f4b1415c49e3…ef8c` | 7 | **`APROVADO`** (6/6 byte-idêntico, 0 divergências) | `APROVADO` · 9/9 | `APROVADO` (static) | `APROVADO` (8/8 PASS) | `APROVADO` | **7/7 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`A2_static_icons`** | 3.997.696 | `4688a4f4111a…162c` | 35 | **`APROVADO`** (6/6 não-ícones byte-idêntico + 28 ícones RGBA `MAE=0.000`*) | `APROVADO` · 9/9 | `APROVADO` (9 fn + 9 glow + 10 ct) | `APROVADO` (8/8 PASS) | `APROVADO` | **35/35 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`A3_dynamic_raf`** | 5.111.808 | `729397c60508…9f0b` | 19 | **`APROVADO`** (**18/18 byte-idêntico**, incl. 12/12 DDS + `plane.mdx` + `index.xml`) | `APROVADO` · 9/9 | `APROVADO` (12f × 10 ciclos) | `APROVADO` (8/8 PASS) | `APROVADO` | **19/19 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`A4_dynamic_full`** | 7.471.104 | `976691c2f003…7fbc` | 51 | **`APROVADO`** (**21/21** não-ícones byte-idêntico, incl. 12 DDS + 3 VAG + MDX + XML + 28 ícones `MAE=0.000`*) | `APROVADO` · 9/9 | `APROVADO` (12f×10c · BGM 3c · 3 VAG) | `APROVADO` (8/8 PASS) | `APROVADO` | **51/51 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`A5_zenitsu_real`** | 14.286.848 | `804a186f9937…9d7c` | 49 | **`APROVADO`** (**19/19** não-ícones byte-idêntico, incl. 13/13 DDS 720p reais + 28 ícones `MAE=0.000`*) | `APROVADO` · 9/9 | `APROVADO` (13f×10c · BGM 3c · `-22.10 LKFS`) | `APROVADO` (8/8 PASS) | `APROVADO` | **49/49 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`A6_crown_of_thorns_real`** | 12.386.304 | `1a53b07bf692…1863` | 35 | **`APROVADO`** (**22/22** não-ícones byte-idêntico, incl. 13/13 DDS 720p reais + 3 VAG + 11 ícones `MAE=0.000`*) | `APROVADO` · 9/9 | `APROVADO` (13f×10c · BGM 3c · 3 VAG) | `APROVADO` (8/8 PASS) | `APROVADO` | **35/35 (100% exato)** | `PHYSICAL_TEST_PENDING` |
| **`FROZEN_DRIVE_ZENITSU`** *(original intacto)* | 13.762.560 | `9647b10bcdc9…f363` | 60 | `NÃO EXECUTADO` (binário externo pronto, sem reconstrução B) | `APROVADO` · 9/9 | `APROVADO` (24f×10c · BGM 3c `170.67s`, `0` clips, clique `0.0056`, `-22.10 LKFS`) | `APROVADO` (8/8 PASS) | `APROVADO` | **60/60 (100% exato)** | `SEMANTICALLY_VALIDATED` *(sem bundle de origem; pendente de teste físico)* |

> **\*Nota sobre a diferença documentada nos PNGs transparentes de ícones (A vs B):** No CZAR Studio, `app/media.py:_render_transparent_rgba` usa Pillow `LANCZOS` para preservar canal alfa limpo em ícones RGBA sem halos escuros, enquanto o `Imxnxl` original usa FFmpeg para gravar o PNG. Em todos os ícones medidos (`A2`, `A4`, `A5`, `A6`), os pixels RGBA decodificados apresentaram **`rgba_mae = 0.000`** (idênticos pixel a pixel) e modo `RGBA` idêntico — diferindo apenas nos headers/chunks zlib do contêiner PNG. Todos os demais arquivos (`theme.xml`, `index.xml`, `plane.mdx`, `00..NN.dds`, `se_*.vag`, `preview.png`, `icon0.png`, `pic1.png`, `homescreen.png`, `functionscreen.png`) foram **100% byte a byte idênticos**!

### Cobertura das 18 Dimensões Diferenciais (Fase 3)
1. **Cabeçalho binário CNT:** `magic=7fCNT`, `drm_type=0xF`, `content_type=0x1B (AC)`, `iro_tag=2 (SS_THEME)` em offset `0x98`, zero IDs duplicados.
2. **Digests criptográficos:** `HeaderDigest`, `HeaderSignature`, `body_digest`, `digest_table_hash`, `pfs_image_digest`, `pfs_signed_digest`, `sc_entries1_hash`, `sc_entries2_hash` e digests por entrada 100% verificados em Python e confirmados pelos 8 estágios do `OrbisPkgTool`.
3. **`Sc0/param.sfo`:** `CATEGORY=ac`, `IRO_TAG=2`, `FORMAT=obs`, `TITLE_ID`, `CONTENT_ID`, `VERSION=01.00`, `bgmhome_loud` preservado, ausência estrita de `APP_TYPE`/`APP_VER`, paridade PFS interno ↔ Sc0 verificada.
4. **`theme.gp4`:** `volume_type=pkg_ps4_theme`, `content_id`, `passcode`, anti-traversal e anti-duplicata.
5. **Árvore `Image0/`:** 100% dentro da lista branca canônica PS4, zero órfãos, zero symlinks.
6. **`theme.xml`:** `format-version=4.0`, `label`, `themecolor` `0..7`, todas as 6 cores/dimmers ARGB, coerência `homebgm-enable`, **100% byte-idêntico** ao Imxnxl.
7. **`scene/background.raf/index.xml`:** validação 1:1 `<material>`/`<actor>`/`<sequence>`, ordem temporal exata `f00..f(N-1)->f00`, `wait` estritamente em segundos `[0.01, 10.0]`, **100% byte-idêntico** ao Imxnxl.
8. **`scene/background.raf/plane.mdx`:** `mdx.parse` estrutural completo (`ROOT`, `MODEL`, `GEOMETRY`, `MESH`, `ARRAYS`), **100% byte-idêntico** ao Imxnxl.
9. **`*.dds` DXT1a:** cabeçalho (`dwMipMapCount=0`, `dwPitchOrLinearSize`, `DXT1`), decodificação real `dxt1.decode_dds`, orçamento ≤ 6 MiB, **100% byte-idêntico** ao Imxnxl em todos os 50 quadros DDS dos temas `A3`–`A6` após correção da Fase 10.
10. **Apresentação (`preview.png`, `icon0.png`, `pic1.png`):** `740×416`, `512×512`, `1920×1080`, **100% byte-idêntico** ao Imxnxl.
11. **Ícones de função + glows:** `128×128 RGBA` pareados 1:1 com `152×152 RGBA`, `rgba_mae = 0.000` vs Imxnxl.
12. **Ícones de conteúdo:** `512×512 RGBA`, `rgba_mae = 0.000` vs Imxnxl, auditoria contra duplicatas acidentais entre slots.
13. **Fundos estáticos vs dinâmicos:** exclusividade estrita (`texture/background/` obrigatório em estáticos, proibido em dinâmicos).
14. **`sound/bgm_home.at9`:** RIFF WAVE ATRAC9 `48 kHz` estéreo `144 kbps` (`fe740bf0`), `fact` + `smpl` (`playCount=0`, limites de loop dentro do comprimento decodificado), `vgmstream-cli` 3 ciclos completos + FFmpeg + `bgmhome_loud`.
15. **Efeitos `.vag`:** `VAGp` big-endian `48 kHz`, decodificável por FFmpeg `adpcm_psx`, **100% byte-idêntico** ao Imxnxl.
16. **Paridade entre extratores independentes:** **256/256 arquivos** extraídos com SHA-256 idêntico entre `LibOrbisPkg`, `OrbisPkgTool` (C#/.NET) e `ps4-pkg-tool` (C++23 ShadPS4).
17. **Contrato de Compatibilidade PS4 (`app/forge/ps4_contract.py`):** 9 regras formais (`PS4-CNT-001`..`PS4-SFX-001`) + divergências comunitárias documentadas (`OrbisPkgTool` Dlc label, `wait` segundos vs ms, ATRAC9 144 vs 168 kbps).
18. **PS4 Theme Digital Twin (`app/forge/digital_twin.py`):** simulação pós-extração em 10 ciclos RAF + 3 ciclos ATRAC9 + VAG + ícones, explicitamente rotulado `SEMANTIC_SIMULATION_ONLY` (`physical_execution_proven=False`).

---

## Entregáveis 6, 7, 8 e 9 — Suíte Adversarial, Digital Twin, Correções e Biblioteca de Casos

### Novas Suítes Permanentes da Operação Fortaleza (`26/26 PASSED`)
1. [`tests/test_fortaleza_contract.py`](../tests/test_fortaleza_contract.py) (`3 passed`) — Contrato de Compatibilidade PS4 (Fase 5).
2. [`tests/test_fortaleza_digital_twin.py`](../tests/test_fortaleza_digital_twin.py) (`3 passed`) — PS4 Theme Digital Twin (Fase 6).
3. [`tests/test_fortaleza_adversarial.py`](../tests/test_fortaleza_adversarial.py) (`17 passed`) — Fase 7 + Fase 10, incluindo **13 PKGs reais com digests criptográficos 100% válidos empacotados via `LibOrbisPkg`** contendo ataques semânticos internos (`theme.xml` incompleto/versão errada, DDS ausente/órfão/corrompido, `<sequence>` fora de ordem, `wait="100"` em ms, `plane.mdx` truncado com magic válido, `homebgm-enable` contraditório, PNG sem alfa, arquivo órfão fora da lista branca, ATRAC9 decodificável com `smpl` `loop_end=99.000.000`, troca de ícones de mesmo tamanho `ct:disc`↔`ct:browser`), além de `CATEGORY=gd`, `IRO_TAG=0`, IDs CNT duplicados, orçamento > 6 MiB, > 48 quadros, bomba XML `DOCTYPE/ENTITY`, SVG XSS/XXE, GP4 path-traversal/duplicata, divergência PFS interno ↔ Sc0, timeout de subprocesso e regressão do bloco de autovetor menor do DXT1.
4. [`tests/test_fortaleza_manifest_and_gates.py`](../tests/test_fortaleza_manifest_and_gates.py) (`3 passed`) — Fase 8 & 11: recusa fail-closed do `apply_loop_to_project` contra qualquer adulteração (`ddsHashes`/`files` incompletos, `renderKey` forjado, `recipe` divergente, `deluxe_frames/*.png` alterado, DDS trocado *dentro* de `background.raf.zip` mesmo com hash externo forjado), invalidação automática de aprovação ao editar o projeto (`stale_vs_project=True` → `REGRESSION_DETECTED`), e verificação dos **7 estados de Release Gate** + **não-herança de `PHYSICALLY_VERIFIED`**.

### Verificação Total de Regressão (Zero Falhas)
- **Suíte Operação Fortaleza + Zenitsu:** `32 passed, 0 failed` (`36.04 s`)
- **Suíte Forge Checkpoints A–E + Gold Master E + Inspector + Audio/SFO/Icons:** `40 passed, 0 failed` (`124.00 s`)
- **Suíte Ampliada (Audit Remediation + Perceptual Supremacy + Ouroboros Proposals + Compositor + Health/Setup + Doctor + Projects):** `45 passed, 3 subtests passed, 0 failed` (`467.71 s`)
- **Total verificado nesta entrega:** **`111 testes únicos aprovados, 0 falhas`** (sem regressões).

---

## Entregáveis 10 e 11 — Roteiro de Teste Físico, Limitações Conhecidas e Evidências Pendentes

- **Roteiro executável PS4 Slim `12.52` com matriz de isolamento por fator único:** [`docs/OPERACAO_FORTALEZA_ROTEIRO_PS4.md`](OPERACAO_FORTALEZA_ROTEIRO_PS4.md).
- **Limitações honestas e explícitas:**
  1. **Ausência de prova física em console PS4 Slim `12.52`:** Toda validação desta entrega ocorreu em Linux. Os 6 PKGs do corpus permanecem em **`PHYSICAL_TEST_PENDING`** e o PKG original do Drive permanece em **`SEMANTICALLY_VALIDATED`** (pendente de ensaio físico). O selo **`PHYSICALLY_VERIFIED`** jamais será herdado entre builds e exige execução presencial do roteiro sobre um SHA-256 exato.
  2. **Ausência legal de `orbis-pub-cmd.exe` e `at9tool.exe`:** Por serem binários proprietários Windows do SDK Sony, o selamento final `.pkg`/`.at9` pelo repositório `Imxnxl` está explicitamente registrado como **`NÃO EXECUTADO`**. A equivalência foi provada byte a byte em todos os ativos abertos e reforçada por 3 ferramentas independentes (`czar-python-pkg-inspector`, `OrbisPkgTool`, `ps4-pkg-tool` C++23).
  3. **`OrbisPkgTool` como construtor:** Classificado como **`FERRAMENTA INCOMPATÍVEL`** para gerar temas PS4 (`PkgBuilder.cs` limita-se a `gd`/`gp` sem `IRO_TAG=2`), porém **`APROVADO`** como validador (8 estágios) e extrator independente.
  4. **Aprovação visual/auditiva subjetiva na TV:** O Digital Twin (`SEMANTIC_SIMULATION_ONLY`) confirma ausência de erros estruturais, ordem de quadros, 10 ciclos RAF, 3 ciclos ATRAC9, pico/clipping e ausência de troca acidental de ícones, mas a estética final na TV e o comportamento real do ShellUI 12.52 só podem ser confirmados no console físico.

# Checkpoint 0 — Auditoria Inicial (MISSÃO CZAR — OUROBOROS ABSOLUTE)

## 1. Estado Inicial do Repositório e Ponto de Recuperação

- **Repositório**: `czarsazevedo-gif/czar-ps4-theme-studio`
- **Branch única de trabalho**: `feature/czar-studio-gold-master-v1` (sem criação de branch alternativa, sem merge, sem alteração de branches anteriores)
- **Último HEAD anteriormente informado**: `6efc9fbf8ee54a3b6c3a84d01c6107485e3d67d1`
- **HEAD real local e remoto antes da intervenção**: `2710937615a0cedd998869b4b8ba0a750a3eea6b` (`feat(ops): auto-provision Codespaces tools, add one-tap UI activation and live 3-state (green/gray/red) status lights`)
- **Branches anteriores preservadas intocadas**:
  - `feature/czar-audio-pkg-forge-v1` → `4ea9c75628909e86222acebe9ce90a1cae573ef3`
  - `feature/czar-ouroboros-perceptual-v2` → `315f0cbf6678d69c12cdcb172ed8783e543d9792`
- **Tag anotada de segurança (recuperação)**: `backup/pre-ouroboros-absolute-20261120` → `2710937615a0cedd998869b4b8ba0a750a3eea6b` (publicada em `origin`)
- **Como recuperar o estado anterior à missão (sem perda de histórico)**:
  ```bash
  git fetch --tags origin
  # Inspecionar estado exato anterior em modo somente leitura:
  git checkout backup/pre-ouroboros-absolute-20261120
  # Ou reverter commits em cima da branch preservando histórico (sem reset --hard / force-push):
  git switch feature/czar-studio-gold-master-v1
  git revert --no-commit backup/pre-ouroboros-absolute-20261120..HEAD
  ```

---

## 2. Inventário Inicial do Ambiente (Linux / Debian 12 Bookworm x86_64)

- **SO / Kernel**: Debian GNU/Linux 12 (`bookworm`), Linux `6.12.105+` x86_64
- **Recursos**: `2` vCPU, `7.8 GiB` RAM (`~7.0 GiB` livre), `19 GiB` livres em `/home/antigravity`, `9.8 GiB` em `/tmp`
- **Ferramentas já presentes**:
  - Sistema/build: `git`, `curl`, `wget`, `openssl`, `sha256sum`, `tar`, `unzip`, `zip`, `jq`, `make`, `gcc`, `g++`, `clang`, `cmake`, `ninja`, `pkg-config`, `autoconf`, `automake`, `cargo`, `rustc`, `node`, `npm`, `chromium`
  - Mídia: `ffmpeg` (`5.1.9`, decodificadores `atrac9`, `gif`, `h264`, `vp9`, `av1`, `flac`, `mp3`, `pcm_s16le`; filtros `scale`, `zscale`, `fps`, `minterpolate`, `signalstats`, `blackdetect`, `freezedetect`, `cropdetect`, `ebur128`; `libvmaf` ausente no build padrão Debian), `ffprobe`, `convert` (ImageMagick `6.9.11-60`)
  - CZAR Forge em `tools/`: `tools/bin/psgfx-at9`, `tools/bin/liborbispkg/pkgtool`, `tools/dotnet/dotnet` (.NET 8 SDK)
  - Python `/home/antigravity/.venv` (`3.11.2`): `fastapi 0.165.0`, `uvicorn 0.54.0`, `httpx 0.28.1`, `numpy 2.4.6`, `scipy 1.17.1`, `pillow 13.0.0`, `opencv-python-headless 5.1.0.95` (sem conflito), `scikit-image 0.26.0`, `imageio 2.38.2`, `pydantic 2.14.4`, `python-multipart 0.0.34`, `pytest 9.2.0`
- **Lacunas identificadas para o Checkpoint 1 (`CZAR COMPLETE TOOLCHAIN`)**:
  - `gifsicle` — ausente (será compilado do upstream `kohler/gifsicle` em `tools/bin/gifsicle`)
  - `vgmstream-cli` — ausente (será compilado do upstream `vgmstream/vgmstream` com suporte nativo ATRAC9/VAG em `tools/bin/vgmstream-cli`)
  - `hyperfine` — ausente (será instalado/compilado em `tools/bin/hyperfine` com verificação de integridade)
  - `tools/bin/magick` — link validado DXT1a apontando para `/usr/bin/convert`
  - SVG / Tabler Icons — avaliar rasterização limpa com alfa (`convert` vs renderizador dedicado) e suporte completo a SVG em `128×128`, `152×152` e `512×512`
  - Python `.venv`: `av` (PyAV), `pytest-asyncio`, `playwright` (reutilizando `/usr/bin/chromium`)
  - Scripts unificados ausentes: `scripts/bootstrap_czar_full.sh`, `scripts/doctor_czar_full.sh`, `docs/czar-full-toolchain.md`, `docs/czar-toolchain-lock.md`, `docs/licenses-and-third-party.md`

---

## 3. Auditoria Arquitetural dos Motores Atuais e Defeitos Históricos Mapeados

### 3.1 Multiplicidade de motores a consolidar em um único `OUROBOROS`
1. `app/sentinela.py` (Sentinela v1 — obsoleto frente ao motor perceptivo)
2. `app/circulo.py` (Círculo v1 — obsoleto frente à busca global Video Textures)
3. `app/genesis.py` (Gênesis v1 — apenas translação periódica simples; superado pela estratégia interna de síntese periódica)
4. `app/deluxe/*` (Sentinela Supreme, Círculo Supreme, Gênesis Supreme, Reconstructor, Calibrator, Finish, Regions, Pipeline, Router — contém os algoritmos comprovadamente superiores que serão absorvidos como estratégias internas organizadas de `app/ouroboros_engine/`)
5. `app/static/ouroboros.html`, `app/static/ouroboros.js`, `app/static/ouroboros.css` (laboratório paralelo separado da aba Fundo Animado)

### 3.2 Defeitos históricos confirmados na leitura do código
1. **Seção 7 (GIFs com atrasos variáveis)**: `app/ouroboros_media.py` e `app/deluxe/calibrator.py` não utilizam `gifsicle` para inspecionar atrasos individuais por quadro, métodos de descarte, quadros parciais/repetidos e transparência; `app/raf.py` emite `<case wait="...">` uniforme (gerador canônico preservado), exigindo suporte explícito a **Modo A** (preservar temporização quando uniforme ou reportar limitação honesta) e **Modo B** (reconstruir cadência contínua ao longo da linha do tempo cumulativa real sem duplicação enganosa).
2. **Seção 11 (Avaliação pós-ATRAC9 ignorava loop interno)**: `app/forge/audio_loop.py:evaluate_post_at9_loop()` decodificava a faixa inteira e media `_seam_metrics(pcm, sr)` nas pontas `0..N`, ignorando `smpl.loop_start` / `smpl.loop_end` (compensados por `fact.encoder_delay`) quando o loop estava em um intervalo interno.
3. **Seção 12 (Divergência SFO não comparada no inspetor PKG)**: `app/forge/pkg_inspector.py:553-560` comparava apenas `Sc0/icon0.png` e `Sc0/pic1.png` contra o bundle esperado, omitindo comparação de campos entre `expected_content_dir/sce_sys/param.sfo` e `Sc0/param.sfo` extraído.
4. **Seção 14 (Benchmark com zero vídeos retornava exit 0)**: `scripts/benchmark_ouroboros_corpus.py:main()` encerrava com código `0` mesmo quando `entries` estava vazio ou quando `0` vídeos concluíam com sucesso.
5. **Seções 23–26 (Três propostas automáticas + coordenação de aplicação sem 409 espúrio)**: atualmente o fluxo exige navegar para um laboratório externo e renderizar uma configuração por vez; será substituído por um único fluxo na aba **Fundo animado** que gera automaticamente as **3 propostas independentes** (`1280×720`, `960×540`, `640×360`), cada uma com player DDS real, 10 ciclos, tela cheia, inspeção de junção e botão direto **APLICAR ESTE LOOP AO MEU TEMA** com coordenação de jobs e vínculo criptográfico.

---

## 4. Integridade Canônica e Baseline de Testes
- `app/dxt1.py`, `app/dds.py`, `app/mdx.py`, `app/raf.py`: `0` alterações frente a `02ef1552b44074678f8874304c5df9ee077a2162`.
- Projetos existentes que possuam metadados `deluxe` / `deluxe_dds` / `deluxe_frames` serão mantidos 100% compatíveis na leitura, prévia do Compositor e exportação RAF/Bundle/PKG.
- Baseline pré-intervenção executado (`test_projects.py`, `test_library.py`, `test_compositor.py`, `test_ouroboros_compat.py`, `test_system_health_and_setup.py`, `test_gold_master_checkpoint_c/d/e.py`): **24 passed (+ 9 subtests passed), 0 failed**.


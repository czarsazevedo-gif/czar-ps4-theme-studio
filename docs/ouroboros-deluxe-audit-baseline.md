# Ouroboros Cinematic Deluxe · Checkpoint 0 — Auditoria e Linha de Base

## 1. Repositório, Isolamento e Controle de Versão

| Item | Valor verificado |
| --- | --- |
| Repositório proprietário | `https://github.com/czarsazevedo-gif/czar-ps4-theme-studio` |
| Branch de referência | `origin/feature/ouroboros-genesis-v1` |
| HEAD remoto verificado | `2c356cda25eed8d32cdb9b22b3072dc4f52c48b9` |
| Branch exclusiva desta missão | `feature/czar-ouroboros-deluxe-antigravity-v1` |
| Branch paralela preservada | `feature/czar-ouroboros-supreme-v1` (não criada/alterada por esta missão) |
| Base de equivalência canônica | `8a9ef7f2ca990cea2ed01439285a90f0770876ea` (30 entradas ZIP idênticas) |

Regras de isolamento ativas:
- Nenhum merge em `main`, `feature/ouroboros-genesis-v1` ou `feature/czar-ouroboros-supreme-v1`.
- Evolução **100% aditiva** em `/api/ouroboros` e nos manifests de projeto (`schemaVersion: 1`), preservando todas as rotas, payloads e exportações canônicas existentes.
- Fora de escopo nesta missão: ATRAC9, empacotamento PKG, geração de ícones e efeitos sonoros.

---

## 2. Ambiente de Bancada e Hardware Verificado

| Componente | Especificação medida |
| --- | --- |
| SO | Debian 12 (Linux x86_64) |
| CPU | Intel Xeon Platinum 8481C · 2 vCPUs (2 cores, 1 thread/core) |
| Memória | 7,8 GiB RAM (7,0 GiB disponíveis, sem swap) |
| Python de teste (`.venv`) | CPython 3.12.16 (alinhado ao `.devcontainer/devcontainer.json` `python:1-3.12-bookworm`) |
| Python do sistema | CPython 3.11.2 (nota: `test_ouroboros_compat.py` usa `tarfile.extractall(..., filter="data")`, disponível em >= 3.11.4 / 3.12) |
| FFmpeg / FFprobe | FFmpeg 5.1.9-0+deb12u1 |
| Filtros FFmpeg disponíveis | `fps`, `scale`, `zscale`, `minterpolate`, `signalstats`, `blackdetect`, `freezedetect`, `cropdetect`, `ssim`, `psnr`, `deshake`, `mpdecimate`, `tblend`, `tmix` |
| `libvmaf` / `vmaf` | **Indisponível** no FFmpeg do sistema (sem `libvmaf` compilado e sem binário `vmaf`). Avaliação perceptual usará SSIM, MS-SSIM, PSNR, métricas de fluxo/gradiente/flicker/ghosting/banding próprias em CPU e FFmpeg `ssim`/`psnr` quando aplicável |
| Navegador para testes touch | `/usr/bin/chromium` + Playwright 1.65.2 |

---

## 3. Confirmação dos Requisitos Canônicos (`app/config.py`, `app/dxt1.py`, `app/raf.py`, `app/theme.py`)

Confirmado diretamente no código canônico:

| Resolução | Bytes por quadro DDS DXT1a (`128 + blocos*8`) | Teto efetivo (`max_frames_for`) | Total máximo DDS | Limite RAF (`RAF_SIZE_LIMIT`) |
| --- | ---: | ---: | ---: | ---: |
| `1280 × 720` | 460.928 B | 13 quadros | 5.992.064 B | 6.291.456 B (6 MiB) |
| `960 × 540` | 259.328 B | 24 quadros | 6.223.872 B | 6.291.456 B (6 MiB) |
| `640 × 360` | 115.328 B | 48 quadros | 5.535.744 B | 6.291.456 B (6 MiB) |

- Teto independente de quadros: `MAX_FRAME_COUNT = 48`.
- Formato DDS: `FourCC = DXT1`, sem mipmaps (`dwMipMapCount = 0`), cabeçalho de 128 bytes, modo 4 cores opaco (`p0 > p1`) ou plano (`p0 == p1`) após `flatten_alpha`.
- Cena RAF (`app/raf.py`): `plane.mdx` (referenciado no XML como `plane.fbx`), um material e um ator por quadro, `<sequence>` com `<case wait="...">` expresso em **segundos decimais** (`MIN_WAIT_SECONDS = 0.02`, `MAX_WAIT_SECONDS = 5.0`), duração do ciclo `= N × wait`.

---

## 4. Auditoria de Dependências (Atuais e Candidatas para Checkpoints 1–4)

### 4.1. Dependências já declaradas (`requirements.txt`)
| Pacote | Versão testada | Licença | Papel |
| --- | --- | --- | --- |
| `fastapi` | 0.165.0 | MIT | API HTTP |
| `uvicorn` | 0.54.0 | BSD-3-Clause | Servidor ASGI |
| `python-multipart` | 0.0.34 | Apache-2.0 | Upload multipart |
| `numpy` | 2.5.5 | BSD-3-Clause | Tensores, DXT1, análise numérica |
| `pydantic` | 2.14.4 | MIT | Validação de schemas |
| `Pillow` | 13.0.0 | MIT-CMU | Imagens, WebP, composição raster |

### 4.2. Pilha de Visão Computacional auditada para incorporação (a partir do Checkpoint 1)
| Pacote | Versão auditada | Licença | Papel técnico no Deluxe |
| --- | --- | --- | --- |
| `opencv-python-headless` | 5.1.0.95 | Apache-2.0 | Optical flow denso (Farneback / DIS), Lucas-Kanade, homografia/estabilização afim, remapeamento (`remap`), `inpaint`, `seamlessClone` (Poisson), pirâmides Laplacianas/Gaussianas, máscaras morfológicas |
| `scipy` | 1.18.1 | BSD-3-Clause | Filtragem temporal/espacial, `ndimage`, otimização numérica, suavização de sinais e campos de movimento |
| `scikit-image` | 0.26.0 | BSD-3-Clause | SSIM / métricas perceptuais auxiliares, registro subpixel (`phase_cross_correlation`), segmentação regional |

### 4.3. Dependências de desenvolvimento / teste (não obrigatórias em produção)
- `pytest` 9.2.0 (MIT), `playwright` 1.65.2 (Apache-2.0), `httpx` 0.28.1 (BSD-3-Clause).

### 4.4. Avaliação de modelos neurais (PyTorch / RAFT / RIFE / etc.)
- **Decisão inicial:** não incorporar como dependência obrigatória. A VM dispõe de 2 vCPUs sem GPU; métodos clássicos vetorizados (OpenCV DIS/Farneback + consistência frente/trás + advecção Euleriana dual-phase + costura multibanda/Poisson + inpainting espaço-temporal) operam em CPU em poucos segundos e permitem validação determinística pós-DDS. Modelos neurais só serão reavaliados se houver evidência experimental de limitação insuperável pelos métodos clássicos com ganho real em CPU.

---

## 5. Linha de Base Experimental e Resultados de Testes

### 5.1. Suíte de testes da referência (Python 3.12.16)
| Suíte | Resultado | Observações |
| --- | --- | --- |
| `test_sentinela`, `test_circulo`, `test_genesis`, `test_ouroboros_api`, `test_projects`, `test_library`, `test_compositor` | **21 OK, 0 falhas** (2 skipped: mídias privadas não definidas) | Tempo conjunto ~69,7 s em 2 vCPUs |
| `test_ouroboros_compat` | **1 OK** (14,6 s) | `6 variações; 30 entradas canônicas idênticas` contra `8a9ef7f` |
| `test_dxt1.py` | **OK** | Gradiente 720p: PSNR 43,40 dB, maxerr 4, meanerr 1,35, 0 blocos `p0 < p1` |
| `test_ouroboros_browser.py` (Chromium touch) | **OK** | 1480×924 e 924×1480, prévia DDS, 10 voltas, volta ao editor, reinício |
| `test_genesis_browser.py` (Chromium touch) | **OK** | 1480×924 e 924×1480, prévia DDS, 10 voltas, download RAF |
| `docs/examples/genesis/SHA256SUMS` | **4/4 OK** | `lua-sintetica.mp4` e RAFs 1280/960/640 íntegros |

Status de fontes reais:
- `docs/examples/genesis/lua-sintetica.mp4`: presente (160×90, 1,0 s, 10 quadros, H.264).
- `tests/sample.mp4`: gerada via `tests/make_fixtures.py` (854×480, 7,30 s).
- `flower.mp4`: **não localizado** no repositório, histórico Git ou issues (permanecerá suportado via `CZAR_ACCEPTANCE_VIDEO` caso seja disponibilizado).

### 5.2. Benchmarks de linha de base (2 vCPUs Intel Xeon Platinum 8481C)

#### Codificação e decodificação DXT1a (`app/dxt1.py`, 1 quadro aleatório)
| Resolução | Encode DXT1a | Decode DXT1a | Bytes/quadro | Custo estimado por teto de quadros (single-thread) |
| --- | ---: | ---: | ---: | ---: |
| `1280 × 720` | 617,3 ms | 40,6 ms | 460.928 B | ~8,0 s (13 quadros) |
| `960 × 540` | 167,1 ms | 22,3 ms | 259.328 B | ~4,0 s (24 quadros) |
| `640 × 360` | 73,3 ms | 9,6 ms | 115.328 B | ~3,5 s (48 quadros) |

#### Exemplos Gênesis V1 (`docs/examples/genesis/`) — medição direta pós-decodificação DDS
| Arquivo | Quadros | `wait` | Soma DDS | MAE interno mediano | MAE na junção | CPU no teste (`test_genesis.py`) | RSS de pico |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `lua-1280.raf.zip` | 12 | 0,100 s | 5.531.136 B | 3,885 | 3,895 | 5,40 s | ~213 MiB |
| `lua-960.raf.zip` | 24 | 0,050 s | 6.223.872 B | 2,114 | 2,113 | 5,49 s | ~213 MiB |
| `lua-640.raf.zip` | 48 | 0,025 s | 5.535.744 B | 1,134 | 1,133 | 4,57 s | ~213 MiB |

#### Busca Círculo atual (`app/circulo.py`)
| Fonte | Duração | Tempo de busca | Sentinela | Veredito nos 3 grupos |
| --- | ---: | ---: | --- | --- |
| `tests/sample.mp4` | 7,30 s | 6,67 s | `['A', 'C', 'D']`, `pingPong=False` | `RECONSTRUÇÃO NECESSÁRIA` (3/3) |
| `lua-sintetica.mp4` | 1,00 s | 0,50 s | `['A', 'C']`, `pingPong=False` | `RECONSTRUÇÃO NECESSÁRIA` (3/3) |

---

## 6. Gargalos, Regressões e Lacunas Identificadas na Auditoria

1. **Sentinela limitada (`app/sentinela.py`)**: grade fixa 6×6 em 96×54 com deslocamento inteiro `[-4, +4]`. Não resolve movimento subpixel, deslocamento rápido, rotação, divergência, câmera móvel, segmentação de múltiplas regiões nem consistência frente/trás (oclusão). Não possui os 5 níveis progressivos (A–E).
2. **Círculo sem custo multicritério calibrado (`app/circulo.py`)**:
   - `assess()` reduz até mesmo os DDS decodificados de 720p/540p/360p de volta para 96×54 antes de medir — mascarando banding DXT1a, perda de nitidez, ghosting fino e costuras espaciais.
   - Falta penalidade de duração curta implausível e falta emissão das 5 categorias nomeadas (melhor natural, corrigível, sintetizado, duração convincente, equilíbrio).
   - Cadências limitadas a `{8, 10, 12, 16}` FPS, sem calibração sobre a grade real de `wait` (`25, 33.333, 40, 50, 66.667, 75, 100, 125, 150` ms).
3. **Ausência de motor de reconstrução (Ouroboros Supreme)**: fontes que recebem `RECONSTRUÇÃO NECESSÁRIA` não têm pipeline escalonado (correção fotométrica → compensação de movimento com check frente/trás → reconstrução local / peças-chave de transição / costura multibanda e Poisson → síntese de textura dinâmica unidirecional por advecção dual-phase → composição multirregião independente).
4. **Gênesis V1 restrito (`app/genesis.py`)**: exige fonte 16:9 e faixas estritamente horizontais/verticais com repetição exata (`MAE < 1,8`). Não suporta máscaras de forma livre, fluxo 2D não uniforme nem texturas naturais quase-periódicas.
5. **Ausência do editor regional (Cinematic Content Editor)**: sem desenho/edição de máscaras touch, estratégias por região (`protect`, `animate`, `remove_transient`, `fix_seam`, `adjust_motion`), undo/redo ou persistência de receitas regionais.
6. **Ausência de Deluxe Finish e validação perceptual nativa pós-DDS**: sem mitigação de flicker/ghosting/banding DXT1a em espaço de cor adequado e sem avaliação científica multicritério em resolução de saída decodificada.
7. **Integração incompleta com o projeto e exportador (`app/main.py`, `app/projects.py`, `app/static/ouroboros.js`)**: a página Ouroboros hoje não consegue aplicar um candidato/loop Deluxe ao fundo animado do projeto ativo com persistência transacional e exportação RAF idêntica à prévia.
8. **Player de 10 ciclos (`app/static/ouroboros.js`)**: inicializa `begin = performance.now()` fora do primeiro callback de `requestAnimationFrame` (causando leve jitter inicial no quadro 0) e não separa telemetria de apresentação do navegador (refresh rate / quadros pulados) de defeitos do conteúdo.
9. **Gargalo de CPU na codificação DDS**: `circulo.preview` e `genesis.render` codificam DDS sequencialmente (`dds.encode_frame`), desperdiçando o paralelismo já existente em `dds.encode_sequence` (~1,7× mais rápido em 2 vCPUs com saída byte-a-byte idêntica).
10. **Avisos pré-existentes observados**: `ResourceWarning` de `SpooledTemporaryFile` em `test_library.py` e `StarletteDeprecationWarning` de `TestClient` com `httpx` (não introduzidos por esta missão).

---

## 7. Plano Modular de Arquitetura (Checkpoints 1 a 6 — 100% Aditivo)

Para maximizar clareza e evitar conflitos com `feature/czar-ouroboros-supreme-v1`, os novos motores viverão em módulos dedicados sob `app/deluxe/` integrados de forma aditiva em `app/ouroboros.py`, `app/main.py` e `app/static/`:

- **Checkpoint 1 — Consistência do Loop**:
  - `app/deluxe/metrics.py`: suíte científica multicritério (16 métricas circulares + pós-DDS em resolução nativa: SSIM, PSNR, fluxo óptico, velocidade, aceleração, luminância, croma, bordas, flicker, ghosting, nitidez/textura, pixels protegidos, banding DXT1a, congelamento, reversão, cadência).
  - `app/deluxe/sentinela_supreme.py`: níveis A–E (exploração global, segmentação regional, fluxo direcional denso + câmera + oclusão, periodicidade real vs simetria/pulsação, reavaliação por resolução).
  - `app/deluxe/circulo_supreme.py`: busca multicritério por duração/qualidade/orçamento com as 5 categorias nomeadas e receitas reproduzíveis.
  - `tests/fixtures_deluxe.py`: gerador da suíte sintética obrigatória (Cenários A–R).
- **Checkpoint 2 — Reconstrução & Gênesis Supreme**:
  - `app/deluxe/genesis_supreme.py`: fluxo 2D, máscaras livres, advecção periódica Euleriana dual-phase unidirecional (zero ping-pong), síntese por patches e preservação de regiões protegidas.
  - `app/deluxe/reconstructor.py`: escada de 6 níveis (corte natural → fotometria em Lab/linear → compensação de fluxo frente/trás → costura local multibanda/Poisson + peças-chave de transição do próprio conteúdo → textura dinâmica → composição multirregião independente).
- **Checkpoint 3 — Edição Cinematográfica Regional**:
  - `app/deluxe/regions.py`: modelo de máscaras/regiões persistido no projeto, estratégias (`protect`, `animate`, `remove_transient`, `fix_seam`, `adjust_motion`), remoção espaço-temporal de objetos transitórios e composição temporal sem vazamento de bordas.
- **Checkpoint 4 — Calibrador Temporal & Deluxe Finish**:
  - `app/deluxe/calibrator.py`: varredura dos intervalos canônicos (`25` a `150` ms) com recomendações separadas (fluidez, definição, continuidade, duração convincente, equilíbrio).
  - `app/deluxe/finish.py`: pipeline de acabamento cinematográfico (continuidade temporal, anti-ghosting, anti-flicker seletivo, consistência fotométrica em luz linear/Lab, preservação de textura, avaliação de banding/dither DXT1a) + ciclo obrigatório `render → DDS DXT1a → decode → reavaliar → rejeitar se piorar`.
- **Checkpoint 5 — Integração Transacional & UI Touch Galaxy Tab**:
  - Rotas aditivas `/api/ouroboros/deluxe/*`, aplicação transacional ao fundo animado do projeto ativo (`job` com quadros lossless verificados + receita/máscaras persistidas), equivalência byte-a-byte entre prévia e `background.raf.zip` exportado pelo editor principal, e interface touch cinematográfica com player de 10 ciclos e telemetria real de apresentação.
- **Checkpoint 6 — Validação Final, Benchmarks e Relatório**:
  - Execução integral de Cenários A–R + `lua-sintetica.mp4` + `sample.mp4`, benchmarks CPU/RAM/tempo, regressão de 100% da suíte anterior (incluindo compatibilidade `8a9ef7f` e Playwright touch), documentação técnica final e publicação na branch exclusiva.

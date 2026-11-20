# CZAR ATRAC9 Studio — Engenharia de Áudio para Temas PS4

Este documento detalha o pipeline de áudio integrado do **CZAR Forge**, cobrindo importação, acabamento de loop musical, controle profissional de volume, codificação ATRAC9 nativa em Linux, validação independente e efeitos sonoros VAG.

## 1. Parâmetros canônicos do PS4 (música de menu)

A música de fundo do menu inicial vive obrigatoriamente em `Image0/sound/bgm_home.at9` (com `<homebgm-enable>True</homebgm-enable>` em `Image0/theme.xml`). O perfil validado empiricamente em temas reais de console é:

| Parâmetro | Valor exigido | Observação |
| --- | --- | --- |
| Codec | **ATRAC9** (`SubFormat GUID 47E142D2-36BA-4D8D-88FC-61654F8C836C`) | Nunca renomear WAV PCM para `.at9` |
| Taxa de amostragem | **48.000 Hz** | Fixo |
| Canais | **2 (Estéreo)** | `dwChannelMask = 0x00000003` |
| Bitrate | **144 kbps** (`nAvgBytesPerSec = 18000`) | **Nunca** substituir silenciosamente por 168 kbps |
| Block Align | **384 bytes** (4 quadros × 96 bytes/quadro) | `wSamplesPerBlock = 1024` |
| `configData` (`fmt ` bytes 44..47) | **`fe 74 0b f0`** | Verificado contra tema oficial Sony |
| Chunks RIFF | `fmt `, `fact`, `smpl`, `data` | `fact` com `sample_count` + `encoder_delay`; `smpl` com `playCount = 0` (loop infinito) |
| Duração máxima | 300 s (5 minutos) | Limite prático do Studio para tamanho do pacote |

## 2. Codificador ATRAC9 nativo em Linux (`psgfx-at9`)

- Em vez de exigir `at9tool.exe` (Windows / Sony SDK), o Studio suporta o codificador aberto **`elripalda/psgfx`** (`src/lib/at9` + `tools/at9check`, commit fixado `5ee7aa9e7d91c9f518322847d13a6bda5ba97837`, licença **GPL-3.0-or-later**).
- A ferramenta é instalada **externamente** em `tools/bin/psgfx-at9` (ignorado no Git) por `./scripts/install_forge_tools.sh at9` e invocada via subprocesso (`--encode-raw`), mantendo separação de código e licença em relação ao repositório MIT (veja [`docs/licensing.md`](licensing.md)).
- Se o proprietário optar por executar no Windows com `at9tool.exe` em `tools/`, a detecção legada continua funcionando como alternativa.

## 3. Validação independente (sem autoaprovação)

O codificador nunca aprova sozinho o próprio arquivo. Toda saída `.at9` (gerada ou importada) passa por [`validate_at9`](../app/forge/audio_validation.py):

1. **Parser RIFF binário independente** (`parse_at9_riff`): verifica cabeçalho `RIFF`/`WAVE`, truncamento, `wFormatTag = 0xFFFE`, GUID ATRAC9, `48000 Hz`, `2 ch`, `18000 B/s`, `blockAlign = 384`, `configData = fe740bf0`, alinhamento de superframes em `data`, `fact` e bloco `smpl` (`playCount = 0`, `loop_end > loop_start`).
2. **FFprobe independente**: confirma `codec_name == "atrac9"`, taxa de amostragem, canais e duração.
3. **Decodificação completa de ida e volta (`FFmpeg`)**: decodifica todos os superframes de volta para PCM estéreo 48 kHz, mede amostras decodificadas, pico dBFS, clipping e ausência de truncamento; compara energia RMS, duração e similaridade espectral por magnitude (respeitando o caráter com perdas do codec ATRAC9).
4. **`vgmstream-cli` (opcional)**: quando presente no `PATH`, adiciona uma terceira leitura independente de metadados.

## 4. CZAR Audio Loop Finisher

Implementado em [`app/forge/audio_loop.py`](../app/forge/audio_loop.py):

- **Análise musical**: mede silêncio inicial/final (`-55 dBFS`), densidade de transientes (fluxo espectral) perto das fronteiras para evitar cortar ataques musicais, regularidade rítmica por autocorrelação de envelope, descontinuidade de amplitude e derivada (risco de clique), continuidade de energia (dB), correlação de fase e similaridade espectral entre fim e início.
- **Modos**:
  - `whole`: preserva a música integral (com microajuste opcional de cruzamento por zero apenas se houver risco mensurável de clique).
  - `auto_refined`: busca ajustes suaves de fronteira (±220 ms no fim, ±15 ms no início com proteção de transientes) maximizando fase + timbre + continuidade de energia.
  - `manual_excerpt`: recorte preciso por tempo (`start_sec` / `end_sec`) ou amostra (`start_sample` / `end_sample`).
- **Acabamento seletivo**: crossfade equal-power (`crossfade_ms`) nunca é aplicado indiscriminadamente; fades de entrada/saída exigem autorização explícita do usuário (`allow_fade=true`).
- **Transparência**: quando não há costura convincente, o relatório informa `convincing: false` e lista os motivos.
- **Avaliação pós-ATRAC9**: após a codificação, a qualidade da costura é medida novamente sobre o `.at9` decodificado pelo FFmpeg, e uma prévia audível da costura (`fim → início`) é servida em `/api/audio/{job_id}/seam-preview`.

## 5. Controle de volume profissional e `bgmhome_loud`

Implementado em [`app/forge/audio_volume.py`](../app/forge/audio_volume.py) e [`app/forge/audio_validation.py`](../app/forge/audio_validation.py):

- Medição nativa em Linux via **FFmpeg `ebur128=peak=true`** (ITU-R BS.1770-4 / EBU R128, onde LUFS e LKFS são numericamente idênticos): mede LUFS integrado, True Peak (dBTP), LRA (LU), pico dBFS, RMS, faixa dinâmica e contagem de amostras clipadas.
- Quatro modos explícitos (a normalização **nunca** ocorre silenciosamente):
  1. **Volume original (`original`)**: `0,0 dB`, preserva o nível da fonte.
  2. **Música ambiente (`ambient`)**: alvo suave de `-28,0 LUFS` (em referência aos `-32,00 LKFS` de temas oficiais PS4) com limitador de pico verdadeiro a `-1,5 dBTP`.
  3. **Ajuste manual (`manual`)**: ganho de `-24,0 dB` a `+12,0 dB` com limitador suave em reforços positivos.
  4. **Normalização assistida (`assisted`)**: alvo escolhido pelo usuário (`-36,0` a `-12,0 LUFS`) com prévia obrigatória antes/depois.
- O valor real medido no arquivo final `.at9` é persistido no projeto e declarado em `param.sfo` (`PUBTOOLINFO` → `bgmhome_loud=<valor>`).
- **Diferença entre navegador e PS4**: o navegador reproduz o áudio decodificado em escala linear sem aplicar a curva de atenuação/nivelamento do sistema operacional do PS4 baseada em `bgmhome_loud`. O volume final percebido no console depende também da TV/headset e das configurações de áudio do PS4.

## 6. Efeitos sonoros VAG (PS-ADPCM)

- Preserva integralmente o codificador canônico [`app/vag.py`](../app/vag.py) (mono 48 kHz, recorte em 5 s, cabeçalho `VAGp`).
- Slots canônicos suportados (idênticos ao projeto original):
  - `sound/se_decide.vag` (Confirmar)
  - `sound/se_cursor.vag` (Mover cursor)
  - `sound/se_cancel.vag` (Cancelar)
- Suportam importação, ajuste de ganho (`/api/audio/{job_id}/gain`), prévia reproduzível no navegador (`/api/audio/{job_id}/preview`), persistência por projeto (com isolamento em duplicações) e inclusão automática no bundle/PKG.

## 7. Fluxo "CONVERTER E APLICAR MÚSICA"

1. Enviar WAV, MP3, FLAC ou `.at9` via `POST /api/forge/audio/inspect` (ou diretamente em `/api/audio/bgm/upload` / Biblioteca).
2. Ouvir prévia original, ajustar Loop Finisher e modo de volume e testar em `POST /api/forge/audio/preview-process` (Antes / Depois / Costura).
3. Acionar **CONVERTER E APLICAR MÚSICA** (`POST /api/forge/audio/convert-apply`), que codifica em ATRAC9 real 144 kbps, valida com FFmpeg + parser RIFF, avalia o loop pós-ATRAC9, mede `bgmhome_loud`, salva os metadados completos (`original_sha256`, `converted_sha256`, `derived_id`, codec, taxa, canais, pontos de loop, parâmetros de volume/conversão) e atualiza o projeto.

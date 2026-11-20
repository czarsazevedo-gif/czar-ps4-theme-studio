# CZAR FORGE — Arquitetura, Auditoria e Contratos

Documento técnico da missão **CZAR FORGE**, responsável pelo caminho final que transforma os recursos produzidos no Czar PS4 Theme Studio em um tema PS4 completo (música ATRAC9 validada, efeitos VAG, fundos estáticos/animados RAF canônicos, ícones, `theme.xml`, `param.sfo`, GP4, bundle validado e, quando tecnicamente disponível, pacote `.pkg` inspecionado).

## 1. Referência de estado inicial (Checkpoint A)

- **Branch base verificada no remoto**: `feature/czar-ouroboros-deluxe-antigravity-v1` (`02ef1552b44074678f8874304c5df9ee077a2162`).
- **Branch desta missão**: `feature/czar-audio-pkg-forge-v1` (sem merge, sem alteração em branches anteriores).
- **Referência canônica upstream**: `Imxnxl/PS4-Ultimate-Theme-Creator` (`aa595aa6cda8e906c7f21086b829a6b23223784e`).
- **Baseline de testes verificado antes das alterações**: 33 testes automatizados existentes passando (`test_projects.py`, `test_library.py`, `test_compositor.py`, `test_deluxe_checkpoint1.py`, `test_deluxe_checkpoint5.py`).

## 2. Auditoria real do pipeline de áudio e causas raiz encontradas

A inspeção do código-fonte atual identificou defeitos concretos e reproduzíveis (não hipotéticos):

| # | Defeito real encontrado | Localização exata | Causa raiz | Impacto observado |
| --- | --- | --- | --- | --- |
| 1 | **HTTP 404 em rotas de catálogo estático com `?project=`** | `app/projects.py:136-143`, `app/static/app.js:714-717`, `app/static/app.js:1490-1499`, `app/static/projects.js:22-27` | `Projects.mediaUrl()` anexa `?project=<id>` a toda rota `/api/*` (exceto `/api/projects*`). O middleware `scope_jobs` intercepta qualquer caminho iniciado por `/api/audio/`, `/api/package/`, `/api/convert/` e exige `project_path()` **antes** de `Projects.boot()` criar/validar o cookie de sessão. Assim, `GET /api/audio/slots?project=<id>` e `GET /api/package/defaults?project=<id>` retornam **HTTP 404** quando a sessão ainda não tem cookie ou o ID pertence a outra sessão/Codespace. | `bootSfx()` lança exceção não tratada em `boot()`, abortando a inicialização de `sfx.catalog`, `bootPackage()`, `Projects.boot()` e `Library.boot()`, deixando a aba Áudio e a Biblioteca quebradas. |
| 2 | **HTTP 404 ao trocar idioma dentro de um projeto** | `app/static/app.js:769-771` | `pintarSlots()` chama `paintSlot(slotId, '/api/slot/' + jobId + '/preview')` sem passar por `Projects.mediaUrl()`, fazendo o navegador requisitar a prévia fora do escopo do projeto. | Imagens de slots quebram com HTTP 404 após troca de idioma. |
| 3 | **Ausência de rota de prévia de áudio e de player na aba Áudio** | `app/main.py` (sem rota `/api/audio/{job_id}/preview`), `app/static/index.html:332-362`, `app/static/library.js:160` | Não existe endpoint para decodificar/streamar prévia reproduzível no Chrome Android de `.at9` ou `.vag` aplicados ao projeto. | Usuário não consegue ouvir a música nem os efeitos aplicados no Studio. |
| 4 | **Upload de WAV/MP3/FLAC para música falha (HTTP 415/422) no Linux e cria job órfão em erro** | `app/at9.py:43-91`, `app/main.py:510-545, 574-577, 634-656` | `app/at9.py` procura exclusivamente `at9tool.exe` / `ps4_at9tool.exe` (Windows proprietário). Em Linux, `at9.available()` é sempre `False`. Além disso, `audio_upload()` cria o diretório `audio-<id>` antes de validar/converter, deixando diretórios órfãos em falhas 422; e `library.js:187` oculta o destino `bgm` para WAV/MP3/FLAC. | Impossível importar WAV/MP3/FLAC como música de menu no Codespace Linux. |
| 5 | **Loudness (`bgmhome_loud`) sempre `None` no Linux** | `app/pkg.py:57-75`, `app/main.py:654, 673` | `pkg.calc_loudness()` depende exclusivamente de `orbis-pub-cmd.exe file_calc_loudness`. | `bgmhome_loud` não é medido nem escrito em `param.sfo` no Linux; UI não mostra comparação com `-32.00 LKFS`. |
| 6 | **Restauração de efeitos SFX frágil por texto traduzido e falha de projeto ao restaurar ganho sem encoder** | `app/static/app.js:746-750, 900-924` | Restauração de SFX busca a linha pelo texto traduzido (`.sfx-name`) em vez de `[data-slot]`; troca de idioma apaga metadados da linha SFX; reabrir projeto com `bgmGain != 0` sem codificador aborta a abertura do projeto inteiro. | Perda visual de SFX ou bloqueio na reabertura de projeto salvo. |
| 7 | **`_add_animated_scene` recodifica RAF Deluxe no pacote** | `app/main.py:887-921` vs `app/main.py:344-386` | `_write_raf_export` reaproveita `deluxe_dds` byte a byte, mas `_add_animated_scene` (usado em `/api/package/build`) ignora `deluxe_dds` e reextrai quadros via FFmpeg. | Quebra a equivalência byte a byte entre o RAF aprovado no Ouroboros/Deluxe e o bundle/PKG final. |
| 8 | **Empacotador PKG indisponível no Linux e `IRO_TAG` zerado em referências open-source** | `app/pkg.py:39-55`, `OpenOrbis/LibOrbisPkg` (`PkgBuilder.cs:275`), `pearlxcore/OrbisPkgTool` (`PkgBuilder.cs:598`) | `app/pkg.py` exige `orbis-pub-cmd.exe` (Windows). Nas ferramentas livres analisadas, o campo binário `iro_tag` no offset `0x98` do cabeçalho PKG é gravado como `0` (`IROTag.None`) em vez de `2` (`IROTag.SS_THEME`). | Sem correção estrutural no cabeçalho PKG (`0x98 = 2`) e no `param.sfo` (`IRO_TAG = 2`, `CATEGORY = ac`), o PS4 confunde o tema com aplicativo comum (`CE-37695-0`) ou falha ao aplicar (`CE-36329-3`). |

## 3. Contratos dos checkpoints

- **Checkpoint A (concluído)**: Auditoria completa, contratos Pydantic em `app/forge/contracts.py`, auditoria de licenças em `docs/licensing.md` e testes de contrato/reprodução em `tests/test_forge_checkpoint_a.py`.
- **Checkpoint B**: Correção das causas raiz de áudio (404/sessão/catálogo/órfãos/restauração), codificador ATRAC9 nativo Linux via ferramenta externa GPL-3.0-or-later (`psgfx`), validador independente (FFmpeg + parser RIFF/fmt/fact/smpl), **CZAR Audio Loop Finisher**, medidor profissional de loudness/volume (`bgmhome_loud`), prévia de áudio no navegador, persistência completa no projeto e botão **"CONVERTER E APLICAR MÚSICA"**.
- **Checkpoint C**: **CZAR Bundle Validator** completo (`Image0/theme.xml`, `texture/`, `scene/background.raf/`, `sound/`, `Sc0/param.sfo`, `Sc0/icon0.png`, `Sc0/pic1.png`, GP4, equivalência RAF byte a byte com Ouroboros/Deluxe, integridade e dimensões de ícones).
- **Checkpoint D**: **CZAR PKG Forge** nativo Linux com `volume_type=pkg_ps4_theme`, `IRO_TAG=2` (no `param.sfo` **e** no cabeçalho binário PKG em `0x98`, com hashes/assinaturas consistentes), `CATEGORY=ac`, passcode `0*32`, validador/extrator independente de PKG comparando hashes contra o bundle e jobs assíncronos com progresso/cancelamento/resultados atômicos.
- **Checkpoint E**: Interface web touch premium (pt-BR, Galaxy Tab S10 Ultra Chrome), suíte completa de integração (cenários A–Z + ponta a ponta), regressões do núcleo canônico, documentação completa (`docs/*.md`, `README.md`, `CHANGELOG.md`) e roteiro de aceitação física para PS4 Slim 12.52.

## 4. Proteção explícita do núcleo canônico

Preservados integralmente sem alterações destrutivas:
- Motores de movimento: Ouroboros, Genesis, Sentinela, Círculo, Temporal Calibrator, Deluxe Finish.
- Compositor visual, Biblioteca de mídia imutável, Projetos persistentes (`schemaVersion: 1`), Autosave, Sessões privadas.
- Geometria canônica da prévia, DXT1a DDS, `plane.mdx`, `index.xml`, limite de 48 quadros e 6 MiB de DDS.
- Upload multipart canônico de até 512 MiB (sem upload fragmentado).

# CZAR PKG Forge — Empacotamento Nativo de Temas PS4 em Linux

Este documento descreve a engenharia do **CZAR PKG Forge**, responsável por construir arquivos `.pkg` de temas PlayStation 4 diretamente em Linux (GitHub Codespaces), sem depender de Windows nem de binários proprietários da Sony.

## 1. Requisitos específicos de temas PS4

Diferente de um aplicativo PS4 (`pkg_ps4_app` / `CATEGORY=gd`), um **tema PS4** exige o conjunto exato abaixo para não ser tratado como aplicativo (erro `CE-37695-0`) nem travar ao aplicar (`CE-36329-3`):

| Campo | Onde vive | Valor exigido | Consequência se incorreto |
| --- | --- | --- | --- |
| `volume_type` | Projeto `.gp4` | **`pkg_ps4_theme`** | Empacotador monta estrutura de aplicativo com PlayGo/keystone indevidos |
| `content_type` | Cabeçalho binário PKG (`0x74`, uint32 BE) | **`0x1B`** (`ContentType.AC`) | `0x1A` (GD) trata o pacote como jogo/aplicativo |
| `iro_tag` | Cabeçalho binário PKG (`0x98`, uint32 BE) | **`2`** (`IROTag.SS_THEME`) | `0` faz o PS4 tentar abrir como app (`CE-37695-0`) |
| `CATEGORY` | `Sc0/param.sfo` | **`ac`** | `gd` instala, aparece na lista, mas trava ao aplicar (`CE-36329-3`) |
| `IRO_TAG` | `Sc0/param.sfo` | **`2`** (`int32`) | Sem `IRO_TAG=2`, o console ignora o tipo de tema |
| `FORMAT` | `Sc0/param.sfo` | **`obs`** | Padrão canônico PS4 |
| `APP_TYPE` / `APP_VER` | `Sc0/param.sfo` | **Ausentes** | Exclusivos de aplicativos |
| `PUBTOOLINFO` | `Sc0/param.sfo` | `c_date=YYYYMMDD[,bgmhome_loud=-XX.XX],img0_l0_size=...` | Nível real da música usado pelo console para balancear o volume |
| `passcode` | GP4 / criptografia PFS | **`00000000000000000000000000000000`** (32 zeros) | O passcode não viaja no PKG; instaladores FPKG esperam 32 zeros |
| `keystone` / `chunk_info` | GP4 / PFS | **Ausentes** | Temas não usam save data nem PlayGo chunks |

## 2. Solução estrutural para o problema crítico do `IRO_TAG`

A auditoria do `OpenOrbis/LibOrbisPkg` (commit `92a4e7f8e5914cb06b52ba3ae6b0675da438a051`, LGPL-3.0) e do `pearlxcore/OrbisPkgTool` (commit `3de6da849b64a8ad0d5ba00dd4e8919934b868b0`, MIT) revelou que **ambas** as implementações gravavam `iro_tag = 0` (`IROTag.None`) no offset `0x98` do cabeçalho binário PKG.

Além disso, no `OpenOrbis/LibOrbisPkg`:
1. `PkgBuilder.cs:275` definia `iro_tag = IROTag.None` (0).
2. `PkgBuilder.cs:342` sobrescrevia `sfo["PUBTOOLINFO"]`, apagando `bgmhome_loud=-XX.XX`.
3. `Gp4Validator.cs` rejeitava `VolumeType.pkg_ps4_theme` em `checkPkgVolumeType` (apesar de já aceitá-lo em `checkCategoryMatchesPkgType`).
4. `Gp4Validator.cs` e `PfsProperties.cs` não normalizavam barras invertidas `\` de arquivos GP4 gerados para Windows quando executados em Linux.

### Por que NÃO corrigir `0x98` por hex-patch após a construção?
No formato PS4 PKG:
- `HeaderDigest` (`0xFE0..0x1000`) é `SHA-256(pkg[0x000:0xFE0])` — cobre o offset `0x98`!
- `HeaderSignature` (`0x1000..0x1100`) é a assinatura RSA-2048 de `SHA-256(pkg[0x000:0x1000])` — também cobre o offset `0x98`!

Portanto, o **CZAR PKG Forge** aplica o patch auditável [`scripts/patches/liborbispkg-ps4-theme-iro-tag.py`](../scripts/patches/liborbispkg-ps4-theme-iro-tag.py) no código LGPL-3.0 clonado externamente em `tools/src/LibOrbisPkg/` **antes** da compilação com .NET 8 (`scripts/install_forge_tools.sh pkg`). Assim:
- `iro_tag = IROTag.SS_THEME (2)` é definido dentro de `PkgBuilder.BuildPkg()` **antes** de `WriteHeader()`, `HeaderDigest` e `HeaderSignature`.
- `bgmhome_loud` é preservado intacto em `Sc0/param.sfo`.
- O binário resultante (`tools/bin/liborbispkg/pkgtool`) permanece fora do repositório Git (respeitando a fronteira de licença MIT ↔ LGPL-3.0) e é invocado pelo adaptador [`app/forge/pkg_forge.py`](../app/forge/pkg_forge.py).

## 3. Execução assíncrona, concorrência e resiliência

Implementado em [`app/forge/pkg_router.py`](../app/forge/pkg_router.py):

- **Pool limitado**: `ThreadPoolExecutor(max_workers=2)` evita bloqueio do servidor FastAPI/Uvicorn e limita consumo de RAM.
- **Single-flight por projeto**: impede construções concorrentes sobre o mesmo projeto (HTTP 409).
- **Cancelamento real**: `POST /api/forge/pkg/jobs/{task_id}/cancel` sinaliza interrupção e encerra imediatamente o subprocesso `pkgtool`.
- **Escrita atômica e recuperação após reinício**: resultados validados são publicados em `pkg-forge-v1/<pkg_id>/` por renomeação atômica (`report.json` + `<CONTENT_ID>.pkg`). Falhas limpam temporários sem sobrescrever PKGs válidos anteriores nem corromper o projeto.
- **Download em streaming**: `GET /api/forge/pkg/{pkg_id}/download` envia o arquivo diretamente do disco via `FileResponse`, sem carregar arquivos grandes na memória do navegador Chrome Android.

## 4. Instalação reproduzível no Codespaces

```bash
./scripts/install_forge_tools.sh all
```

Ou separadamente:
- Apenas codificador ATRAC9 nativo (`psgfx-at9`): `./scripts/install_forge_tools.sh at9`
- Apenas empacotador PKG nativo (.NET 8 + `LibOrbisPkg`): `./scripts/install_forge_tools.sh pkg`

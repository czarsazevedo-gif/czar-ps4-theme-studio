# Licenciamento e Fronteiras de Ferramentas Externas — CZAR Forge

Este documento registra a auditoria de licenças de todas as dependências e ferramentas externas avaliadas ou invocadas pelo **CZAR Forge**, estabelecendo fronteiras claras para que o repositório principal (licença MIT) não incorpore inadvertidamente código incompatível.

## 1. Licença do repositório principal

- **Czar PS4 Theme Studio** deriva de [Imxnxl/PS4-Ultimate-Theme-Creator](https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator) e adota a **MIT License** ([`LICENSE`](../LICENSE)), cobrindo exclusivamente o código-fonte do repositório (`app/`, `tests/`, `scripts/`, `docs/`).
- Nenhum binário proprietário da Sony (`orbis-pub-cmd.exe`, `sc.exe`, `di.exe`, `libatrac9.dll`, `at9tool.exe`) é incluído, versionado ou baixado automaticamente pelo repositório.

## 2. Auditoria de ferramentas externas livres

| Ferramenta | Repositório | Commit/Versão fixada | Licença | Papel no CZAR Forge | Como é integrada |
| --- | --- | --- | --- | --- | --- |
| **PSGFX (`src/lib/at9` + `tools/at9check`)** | `https://github.com/elripalda/psgfx` | `5ee7aa9e7d91c9f518322847d13a6bda5ba97837` | **GPL-3.0-or-later** (tabelas LibAtrac9: MIT; minimp3: CC0; dr_wav/dr_flac: Unlicense/MIT-0) | Codificador ATRAC9 nativo Linux (48 kHz, estéreo, 144 kbps, `configData fe 74 0b f0`, `fact` + `smpl`) | Compilado externamente pelo usuário/Codespace via `scripts/install_forge_tools.sh` em `tools/bin/` (ignorado no Git) e invocado como executável CLI separado em subprocesso. **Nenhum código GPL é copiado ou ligado no repositório MIT.** |
| **FFmpeg / FFprobe** | `https://github.com/FFmpeg/FFmpeg` | Pacote Debian Bookworm (`5.1.9`) | **GPL-2.0-or-later / LGPL-2.1-or-later** (conforme build Debian `--enable-gpl`) | Decodificador independente ATRAC9/VAG/WAV/MP3/FLAC, verificação de ida e volta e medidor EBU R128 (`ebur128`) | Binário de sistema invocado por subprocesso. |
| **OpenOrbis/LibOrbisPkg** | `https://github.com/OpenOrbis/LibOrbisPkg` | `92a4e7f8e5914cb06b52ba3ae6b0675da438a051` | **LGPL-3.0** | Empacotador PS4 Theme PKG nativo Linux (`pkg_build`, `pkg_validate`, `pkg_extract`) com patch auditável [`scripts/patches/liborbispkg-ps4-theme-iro-tag.py`](../scripts/patches/liborbispkg-ps4-theme-iro-tag.py) | Compilado externamente pelo usuário/Codespace via `scripts/install_forge_tools.sh pkg` em `tools/bin/liborbispkg/` (ignorado no Git) e invocado como executável CLI separado em subprocesso. **Nenhum código LGPL é copiado no repositório MIT.** |
| **maxton/LibOrbisPkg** | `https://github.com/maxton/LibOrbisPkg` | Upstream original | **LGPL-3.0** | Referência histórica do formato PKG/PFS/SFO | Apenas referência de especificação. |
| **pearlxcore/OrbisPkgTool** | `https://github.com/pearlxcore/OrbisPkgTool` | `3de6da849b64a8ad0d5ba00dd4e8919934b868b0` | **MIT** | Referência de validação 8-estágios e empacotamento FPKG (identificado `WriteBe32(hdr, 0x98, 0)` em `PkgBuilder.cs:598`) | Referência de especificação / opcional externo em .NET 10. |
| **vgmstream** | `https://github.com/vgmstream/vgmstream` | Opcional externo | **ISC / MIT** | Validação independente adicional de `.at9` / `.vag` quando presente no `PATH` | Opcional via subprocesso (`vgmstream-cli`). |

## 3. Obrigações aplicáveis à GPL-3.0-or-later (`elripalda/psgfx`)

A separação em executáveis independentes **não resolve automaticamente** todas as questões de licenciamento sob a GPL-3.0:

1. **Não incorporação no repositório MIT**: nenhum arquivo `.cpp`/`.h` de `elripalda/psgfx` ou `blackbearreloaded/ps5-at9-converter` é copiado, traduzido ou incluído neste repositório. O script `scripts/install_forge_tools.sh` clona o repositório upstream no commit fixado fora do código versionado (`tools/src/` e `tools/bin/`, ambos no `.gitignore`) e compila a ferramenta CLI com `g++`.
2. **Interface desacoplada (subprocesso por arquivos/stdin)**: o Czar Studio funciona sem o executável externo (aceitando `.at9` pronto ou `at9tool.exe` fornecido pelo proprietário) e comunica-se exclusivamente por linha de comando padrão (arquivo PCM/WAV de entrada → arquivo `.at9` de saída), sem compartilhamento de memória, símbolos ou estruturas internas.
3. **Interpretação jurídica e redistribuição**:
   - Para **uso privado / execução local no Codespace do proprietário**, a GPL-3.0 não exige distribuição pública do código.
   - Se alguém **redistribuir** uma imagem, container ou pacote contendo simultaneamente o Czar Studio e o binário compilado do `psgfx`/`at9check`, deverá cumprir integralmente a **GPL-3.0-or-later** para o binário GPL (fornecer o texto da licença GPL-3.0, avisos de copyright de BlackBearReloaded / PSGFX / LibAtrac9 / dr_libs / minimp3 e o **Código-Fonte Correspondente** exato, incluindo eventuais patches aplicados ao binário CLI).
   - Caso uma interpretação estrita considere a combinação distribuída como obra derivada única (por dependência funcional estreita), a licença MIT do Czar Studio é **compatível com a GPL-3.0** na direção MIT → GPL-3.0 (a obra combinada redistribuída pode ser distribuída sob GPL-3.0-or-later, mantendo o código do Czar Studio sob MIT no repositório).
4. **Sem mistura com binários proprietários**: binários proprietários da Sony nunca devem ser redistribuídos em conjunto com ferramentas GPL/LGPL.

## 4. Obrigações aplicáveis à LGPL-3.0 (`OpenOrbis/LibOrbisPkg`)

1. **Separação estrita**: o código-fonte de `OpenOrbis/LibOrbisPkg` e os artefatos compilados (`tools/src/LibOrbisPkg/`, `tools/bin/liborbispkg/`, `tools/dotnet/`) ficam fora do controle de versão (`.gitignore`). O repositório MIT armazena apenas o script instalador (`scripts/install_forge_tools.sh`) e o patch auditável mínimo (`scripts/patches/liborbispkg-ps4-theme-iro-tag.py`).
2. **Escopo do patch**: o patch ajusta exclusivamente quatro pontos necessários para temas PS4 válidos em Linux (`IROTag.SS_THEME = 2` no offset `0x98` antes do cálculo de `HeaderDigest`/`HeaderSignature`, preservação de `bgmhome_loud` em `PUBTOOLINFO`, aceitação de `pkg_ps4_theme` no `Gp4Validator` e normalização de separadores `\` → `/` em caminhos GP4).
3. **Obrigações em caso de redistribuição**: qualquer redistribuição do binário compilado `pkgtool` deve acompanhar a licença LGPL-3.0 / GPL-3.0, os avisos de copyright do projeto OpenOrbis/LibOrbisPkg e o código-fonte completo modificado (upstream no commit `92a4e7f8e5914cb06b52ba3ae6b0675da438a051` + patch aplicado). Para uso privado local no Codespace, não há redistribuição.


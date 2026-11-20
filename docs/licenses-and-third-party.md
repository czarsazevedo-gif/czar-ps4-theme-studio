# Licenças e Componentes de Terceiros — Czar PS4 Theme Studio

Este documento consolida a auditoria de licenças do repositório **Czar PS4 Theme Studio** e de todas as ferramentas externas preparadas pelo `scripts/bootstrap_czar_full.sh`. Consulte também [`docs/licensing.md`](licensing.md) para detalhes históricos das fronteiras GPL/LGPL do CZAR Forge.

## 1. Código-fonte do repositório

- **Licença**: MIT ([`LICENSE`](../LICENSE)), derivada de `Imxnxl/PS4-Ultimate-Theme-Creator`.
- **Política zero-proprietário**: nenhum binário proprietário da Sony (`orbis-pub-cmd.exe`, `at9tool.exe`, `libatrac9.dll`, `sc.exe`, `di.exe`) é versionado, baixado ou redistribuído.

## 2. Fronteira de Execução de Ferramentas Externas (subproc em `tools/bin/`, fora do Git)

Todas as ferramentas compiladas ou extraídas por `scripts/bootstrap_czar_full.sh` residem em `tools/bin/`, `tools/src/`, `tools/dotnet/`, `tools/cache/` e `tools/icons/` (todos listados no `.gitignore`), sendo executadas exclusivamente como processos externos independentes (CLI/subprocesso) sem incorporação de código-fonte copyleft no repositório MIT.

| Projeto | Licença | Integração | Aviso / Licença copiada em `tools/bin/` |
| --- | --- | --- | --- |
| **FFmpeg / FFprobe** | GPL-2.0-or-later / LGPL-2.1-or-later | CLI do sistema | Pacote Debian Bookworm |
| **Gifsicle** (`kohler/gifsicle`) | GPL-2.0 | CLI externo (`tools/bin/gifsicle`) | `tools/bin/GIFSICLE-LICENSE.txt` |
| **PSGFX** (`elripalda/psgfx`) | GPL-3.0-or-later (tabelas LibAtrac9: MIT) | CLI externo (`tools/bin/psgfx-at9`) | `tools/bin/PSGFX-LICENSE.txt`, `PSGFX-AT9-NOTICES.md` |
| **vgmstream** (`vgmstream/vgmstream`) | ISC / MIT | CLI externo (`tools/bin/vgmstream-cli`) | `tools/bin/VGMSTREAM-LICENSE.txt` |
| **OpenOrbis/LibOrbisPkg** | LGPL-3.0 | CLI externo (`tools/bin/liborbispkg/pkgtool`) + patch auditável | `tools/bin/LIBORBISPkg-LICENSE.txt` |
| **Hyperfine** (`sharkdp/hyperfine`) | MIT / Apache-2.0 | CLI externo (`tools/bin/hyperfine`) | `tools/bin/HYPERFINE-LICENSE-MIT.txt` |
| **Tabler Icons** (`tabler/tabler-icons`) | MIT | Assets SVG em `tools/icons/tabler/` | `tools/icons/tabler/LICENSE` |
| **ImageMagick** | ImageMagick License | CLI externo (`tools/bin/magick`) | Pacote Debian Bookworm |
| **OpenCV** (`opencv-python-headless`) | Apache-2.0 | Biblioteca Python no `.venv` | Metadados wheel |
| **SciPy / NumPy / scikit-image** | BSD-3-Clause | Bibliotecas Python no `.venv` | Metadados wheel |
| **Pillow** | MIT-CMU | Biblioteca Python no `.venv` | Metadados wheel |
| **PyAV** (`av`) | BSD-3-Clause (empacota FFmpeg LGPL/GPL conforme wheel) | Biblioteca Python no `.venv` | Metadados wheel |
| **Playwright / Chromium** | Apache-2.0 / BSD-3-Clause | Testes automatizados | Metadados wheel / Debian |
| **FastAPI / Uvicorn / Pydantic / HTTPX / pytest** | MIT / BSD-3-Clause | Bibliotecas Python no `.venv` | Metadados wheel |

## 3. Ferramentas avaliadas e descartadas (ou mantidas opcionais)

- **`opencv-python` / `opencv-contrib-python`**: descartados no mesmo `.venv` para evitar conflito de binários compartilhados com `opencv-python-headless` (já provê Farneback, DIS, warp, remap, homografia e filtros necessários).
- **`lucide-icons/lucide`**: não duplicado por ora, pois `tabler-icons` (MIT) já cobre integralmente o conjunto de ícones SVG de referência sem redundância.
- **`resvg`**: avaliado conforme Seção 13; dispensado como dependência obrigatória após teste empírico comprovar rasterização SVG RGBA limpa sem halo (`min_rgb = 255` em pixels opacos e semitransparentes) via ImageMagick + Pillow.
- **`libvmaf`**: mantido opcional (ausente no pacote FFmpeg padrão do Debian 12; avaliação perceptual completa utiliza SSIM/MS-SSIM em float32, PyAV, fluxo óptico, direção, velocidade, ghosting, flicker, banding e avaliação pós-DXT1a).
- **Redistribuição**: para uso local/privado no Codespace não há redistribuição; qualquer redistribuição de containers contendo binários GPL/LGPL compilados deve acompanhar os textos de licença e o código-fonte correspondente (incluindo o patch do LibOrbisPkg).

# Lock Reproduzível — CZAR Complete Toolchain

Todas as versões, commits Git e checksums SHA-256 fixados em `scripts/bootstrap_czar_full.sh` e `requirements-lock.txt`.

## 1. Ferramentas Nativas Externas (instaladas em `tools/bin/` — fora do Git)

| Ferramenta | Repositório Upstream | Tag / Commit Fixado | Verificação de Integridade | Licença |
| --- | --- | --- | --- | --- |
| **PSGFX (`psgfx-at9`)** | `https://github.com/elripalda/psgfx` | `5ee7aa9e7d91c9f518322847d13a6bda5ba97837` | Commit SHA-1 imutável + autoteste RIFF/FFmpeg | GPL-3.0-or-later |
| **LibOrbisPkg (`pkgtool`)** | `https://github.com/OpenOrbis/LibOrbisPkg` | `92a4e7f8e5914cb06b52ba3ae6b0675da438a051` | Commit SHA-1 imutável + patch auditável [`liborbispkg-ps4-theme-iro-tag.py`](../scripts/patches/liborbispkg-ps4-theme-iro-tag.py) | LGPL-3.0 |
| **.NET 8 SDK** | `https://dot.net/v1/dotnet-install.sh` (canal `8.0`) | Canal LTS `8.0` em `tools/dotnet/` | Instalação isolada em espaço de usuário | MIT |
| **Gifsicle** | `https://github.com/kohler/gifsicle` | `v1.98` (`8ff3057b736141b406be621830b851fdf4285689`) | Commit SHA-1 imutável + teste de delays 50/150 ms | GPL-2.0 |
| **vgmstream-cli** | `https://github.com/vgmstream/vgmstream` | `24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce` | Commit SHA-1 imutável (`-DUSE_ATRAC9=ON -DBUILD_STATIC=ON`) + decodificação `.at9` | ISC / MIT |
| **Hyperfine** | `https://github.com/sharkdp/hyperfine` | `v2.0.0` (`hyperfine-v2.0.0-x86_64-unknown-linux-gnu.tar.gz`) | SHA-256 `ae2beda2ac99c098427e4f755244552daf20e514422889cca6d44421e2d93c87` | MIT / Apache-2.0 |
| **Tabler Icons** | `https://github.com/tabler/tabler-icons` | `v3.9.0` (`0bd42617ab4c3eae2774a548b9d429046c6b197e`) | Commit SHA-1 imutável (sparse checkout `icons/outline` + `LICENSE`) | MIT |
| **ImageMagick (`magick`)** | Pacote Debian Bookworm `imagemagick` (`6.9.11-60`) | Link `tools/bin/magick` → `magick`/`convert` validado | Autoteste real DXT1a + rasterização SVG RGBA sem halo | ImageMagick License (Apache-2.0-like) |

## 2. Ambiente Python Isolado

Lock completo congelado em [`requirements-lock.txt`](../requirements-lock.txt) (Python `3.11.2`, Debian 12 Bookworm x86_64):
- **Core API**: `fastapi==0.165.0`, `uvicorn==0.54.0`, `pydantic==2.14.4`, `python-multipart==0.0.34`, `starlette==1.10.0`
- **Visão / Matemática / Vídeo**: `numpy==2.4.6`, `scipy==1.17.1`, `opencv-python-headless==5.1.0.95`, `scikit-image==0.26.0`, `pillow==13.0.0`, `av==16.1.0`, `imageio==2.38.2`
- **Testes / Browser**: `pytest==9.2.0`, `pytest-asyncio==1.5.0`, `httpx==0.28.1`, `playwright==1.65.2`

> **Garantia anti-conflito OpenCV**: apenas `opencv-python-headless` é instalado; `opencv-python` e `opencv-contrib-python` não são misturados no mesmo ambiente.

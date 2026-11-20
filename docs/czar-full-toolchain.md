# CZAR Complete Toolchain — Instalação Integral e Verificação Funcional

Este documento descreve a toolchain unificada do **Czar PS4 Theme Studio** na branch `feature/czar-studio-gold-master-v1`.

## 1. Fluxo Único de Instalação e Execução

```bash
git switch feature/czar-studio-gold-master-v1
git pull --ff-only
./scripts/bootstrap_czar_full.sh --install
./scripts/doctor_czar_full.sh --json
./start.sh
```

- **`scripts/bootstrap_czar_full.sh --install`**: instalador integral e **idempotente**. Detecta SO, distribuição Linux, arquitetura, versão do Python, núcleos de CPU, RAM e espaço em disco; configura o ambiente virtual Python isolado (`.venv` ou `$CZAR_VENV`); compila/instala em `tools/bin/` (ignorado pelo Git) todas as ferramentas nativas com commits e hashes fixados; nunca baixa binários proprietários da Sony e nunca desativa verificações de assinatura GPG.
- **`scripts/doctor_czar_full.sh`** (e `--json`): verificador funcional real. Não se limita a `command -v` — executa testes reais de decodificação, inspeção de GIF com atrasos variáveis, fluxo óptico, otimização SciPy, PyAV/SSIM, codificação ATRAC9 + validação independente `vgmstream-cli`, VAG, LibOrbisPkg/.NET, rasterização SVG/PNG sem halo (128×128, 152×152, 512×512), Compositor, RAF/DXT1a, PKG/SFO, Playwright+Chromium, Hyperfine e Ouroboros.
- **`start.sh`**: detecta automaticamente o `.venv` e `tools/bin/`, inicia o servidor Uvicorn na porta `8000` (privada no Codespaces) com sonda `/healthz`.

---

## 2. Componentes da Toolchain

| Componente | Papel no Studio | Localização / Origem | Obrigatório? |
| --- | --- | --- | --- |
| **Python + `.venv`** | Runtime backend, API e processamento científico | `.venv` / `$CZAR_VENV` (`requirements-lock.txt`) | Sim |
| **FastAPI + Uvicorn + HTTPX** | Servidor web assíncrono e cliente de testes | `.venv` | Sim |
| **FFmpeg + FFprobe** | Decodificação de vídeo/áudio, VFR/CFR, EBU R128 (`ebur128`), `atrac9` | Sistema (`/usr/bin/ffmpeg`, `/usr/bin/ffprobe`) | Sim |
| **Gifsicle (`v1.98`)** | Inspeção real de atrasos individuais de GIF, descarte, transparência e `--unoptimize` | `tools/bin/gifsicle` (compilado do upstream) | Sim |
| **OpenCV (`opencv-python-headless`)** | Fluxo óptico, estabilização, máscaras, warping e análise de movimento | `.venv` (sem pacotes OpenCV conflitantes) | Sim |
| **SciPy** | Otimização de junções, regularização, sinais temporais e periodicidade | `.venv` | Sim |
| **Avaliação Visual (`scikit-image` + `PyAV`)** | SSIM/MS-SSIM e acesso exato a pacotes/PTS de quadros | `.venv` | Sim |
| **PSGFX (`psgfx-at9`)** | Codificação ATRAC9 nativa Linux (48 kHz, estéreo, 144 kbps, `fact` + `smpl`) | `tools/bin/psgfx-at9` (compilado do upstream) | Sim |
| **vgmstream-cli** | Decodificação independente e conferência de `loop start` / `loop end` em `.at9` e `.vag` | `tools/bin/vgmstream-cli` (compilado com `USE_ATRAC9=ON`) | Sim |
| **VAG (`app/vag.py`)** | Efeitos sonoros Sony ADPCM (`se_cursor`, `se_decide`, `se_cancel`) | Interno MIT | Sim |
| **LibOrbisPkg + .NET 8 SDK** | Empacotamento nativo PS4 Theme PKG (`pkg_ps4_theme`, `IRO_TAG=2`, `CATEGORY=ac`), `pkg_validate` e `pkg_extract` | `tools/bin/liborbispkg/pkgtool` + `tools/dotnet/dotnet` | Sim |
| **Ícones (Pillow + ImageMagick + Tabler Icons)** | Rasterização SVG/PNG sem halo em `128×128`, `152×152` glow e `512×512` | `.venv` + `tools/bin/magick` + `tools/icons/tabler/` | Sim |
| **Hyperfine (`v2.0.0`)** | Benchmarks reproduzíveis com estatísticas de tempo | `tools/bin/hyperfine` (SHA-256 verificado) | Sim |
| **Playwright + Chromium + Node.js** | Testes reais E2E e ergonomia touch/S Pen em navegador real | `.venv` + `/usr/bin/chromium` + `node` | Sim |
| **libvmaf** | Métrica perceptual auxiliar opcional no FFmpeg | Build FFmpeg (opcional — ausente no Debian Bookworm padrão) | Opcional |

---

## 3. Idempotência e Recuperação

- Executar `./scripts/bootstrap_czar_full.sh --install` repetidamente reutiliza qualquer binário em `tools/bin/` cujo autoteste já passe, evitando recompilações desnecessárias e sem corromper o ambiente.
- Se o Codespaces apresentar erro de chave GPG no arquivo externo `/etc/apt/sources.list.d/yarn.list`, o instalador **não** desativa a verificação GPG — ele informa exatamente como o administrador pode corrigir o repositório do sistema se algum pacote `apt` estiver ausente.
- Para reverter o código ao ponto anterior à missão **OUROBOROS ABSOLUTE**:
  ```bash
  git fetch --tags origin
  git checkout backup/pre-ouroboros-absolute-20261120
  ```

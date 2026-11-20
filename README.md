# Czar PS4 Theme Studio

## OUROBOROS ABSOLUTE — Motor Único Unificado & Toolchain Completa (`feature/czar-studio-gold-master-v1`)

Na branch **`feature/czar-studio-gold-master-v1`**, o Studio consolida toda a engenharia audiovisual, de loops animados, áudio ATRAC9/VAG, ícones PNG/SVG sem halo e empacotamento PKG PS4 nativo em Linux (GitHub Codespaces), pronto para uso touch/S Pen no Chrome do **Galaxy Tab S10 Ultra**.

### Instalação integral, verificação e inicialização (Codespaces / Linux)

```bash
git fetch origin
git checkout feature/czar-studio-gold-master-v1
git pull origin feature/czar-studio-gold-master-v1

./scripts/bootstrap_czar_full.sh --install
./scripts/doctor_czar_full.sh
./start.sh
```

Abra a porta **8000** com visibilidade **Privada** no painel **Portas** (sonda leve em `GET /healthz`). O `bootstrap_czar_full.sh --install` é 100% idempotente, instala e valida em espaço de usuário (`tools/bin/`, `tools/dotnet/`, `tools/icons/` e venv Python isolado) todas as ferramentas necessárias — **FFmpeg/FFprobe, Gifsicle 1.98, OpenCV 5.1, SciPy 1.17, scikit-image, PyAV 16.1, psgfx-at9, vgmstream-cli, LibOrbisPkg + .NET 8 SDK, ImageMagick, Tabler Icons MIT, Playwright + Chromium e Hyperfine 2.0** — e executa o diagnóstico funcional completo (`21/21` itens obrigatórios).

### Fluxo Unificado no Studio

1. **OUROBOROS (motor único na aba Fundo animado)**: ao importar qualquer vídeo ou GIF, o motor unificado [`app/ouroboros_engine/`](app/ouroboros_engine/) executa diagnóstico inteligente (21 campos + 5 categorias oficiais de aptidão), inspeção real de GIFs via `gifsicle` (Modo A preservação vs Modo B reconstrução temporal contínua para atrasos variáveis como `50/150 ms`) e gera automaticamente **3 propostas independentes** dentro do limite canônico PS4 (`6 MiB`, `<= 48` quadros, DXT1a real):
   - **Proposta 1 (`1280×720`)** — *Máxima Qualidade Visual*
   - **Proposta 2 (`960×540`)** — *Melhor Equilíbrio*
   - **Proposta 3 (`640×360`)** — *Maior Flexibilidade Temporal*
   Cada cartão oferece player real dos quadros DDS decodificados, modo **10 Ciclos**, **Inspecionar junção** + mapa de costura, **Tela cheia** e aplicação direta **`APLICAR ESTE LOOP AO MEU TEMA`** com vínculo criptográfico `expectedRenderKey` (`HTTP 409` contra trocas de take). Todos os motores concorrentes legados (`sentinela.py`, `circulo.py`, `genesis.py`, `app/deluxe/`, `ouroboros.html/js/css`) foram removidos.
2. **Ícones PNG & SVG sem halo (aba Ícones e Compositor)**: suporte nativo a `.png`, `.svg` (incluindo Tabler Icons, com guarda anti-XXE/XSS) e redimensionamento Lanczos com alfa pré-multiplicado em `128×128`, `152×152` (glow nativo) e `512×512` sem halo escuro.
3. **CZAR ATRAC9 Studio (aba Áudio)**: importe `WAV`, `MP3`, `FLAC` ou `.at9` PS4 válido (pass-through sem recompressão desnecessária), ajuste loop e volume (`original`, `ambient`, `manual`, `assisted` com EBU R128 `bgmhome_loud`) e codifique em ATRAC9 real (48 kHz estéreo, 144 kbps, `configData fe 74 0b f0`, `fact` + `smpl`) com validação independente via **FFmpeg + RIFF + `vgmstream-cli`** e avaliação exata do intervalo interno `[loop_start, loop_end]` (incluindo verificação de 3 ciclos contínuos). Efeitos `se_decide`, `se_cursor` e `se_cancel` são convertidos para VAG com prévia reproduzível.
4. **CZAR FORGE (aba Empacotar)**: utilize **VALIDAR MEU TEMA**, **EXPORTAR RECURSOS**, **GERAR PKG**, **BAIXAR PKG** e **VER RELATÓRIO**. O empacotador nativo Linux (`LibOrbisPkg`) gera `.pkg` (`pkg_ps4_theme`, `IRO_TAG=2`, `CATEGORY=ac`, passcode `0×32`) com verificação criptográfica, extração de `Image0`/`Sc0`, comparação estrita de todos os campos de `Sc0/param.sfo` (permitindo apenas metadados documentados do empacotador) e conferência SHA-256 arquivo a arquivo contra o bundle.
5. **Benchmarks reproduzíveis**:
   ```bash
   ./scripts/benchmark_hyperfine.sh
   ```
6. **Compatibilidade física no PS4 Slim (firmware 12.52) e Galaxy Tab S10 Ultra**: toda validação em Linux e Chromium headless comprova integridade estrutural, criptográfica, visual e de interface, mas **não substitui testes físicos nos aparelhos reais**. Todo pacote permanece em estado **`PENDENTE DE TESTE FÍSICO`** até conclusão do roteiro em [`docs/acceptance-ps4.md`](docs/acceptance-ps4.md).

Documentação técnica:
- [Toolchain Completa CZAR](docs/czar-full-toolchain.md) · [Lock de Versões/Commits/Hashes](docs/czar-toolchain-lock.md) · [Licenças e Terceiros](docs/licenses-and-third-party.md)
- [Auditoria Ouroboros Absolute](docs/ouroboros-absolute-audit.md) · [Relatório Gold Master](docs/gold-master-report.md)
- [Arquitetura CZAR FORGE](docs/czar-forge.md) · [ATRAC9 Studio](docs/atrac9-studio.md) · [PKG Forge](docs/pkg-forge.md) · [Validação PKG](docs/pkg-validation.md)
- [Roteiro de aceitação física no PS4 Slim 12.52 e Galaxy Tab S10 Ultra](docs/acceptance-ps4.md)

---

## Fases anteriores preservadas

Para usar o **diagnóstico Ouroboros 3.4**, abra a branch `feature/studio-infinite-motion-v1` no GitHub. Em **Code → Codespaces**, crie um Codespace nessa branch e aguarde o devcontainer. Execute `./start.sh` e abra a porta **8000** com visibilidade **Privada**. O endereço encaminhado costuma ter a forma `https://<nome-do-codespace>-8000.app.github.dev`.

No editor, abra um projeto, importe um vídeo e toque em **Diagnóstico de loops**. Selecione o vídeo da biblioteca ou o fundo animado salvo. **Analisar movimento e ciclos** mostra o diagnóstico da fonte e os cortes por resolução. **Conferir DDS** mede os bytes reais e reproduz os quadros decodificados. O diagnóstico trabalha em uma área privada do projeto. Para exportar um RAF pelo compositor, use o fluxo da 3.3. [Resultados e aceitação no tablet](docs/ouroboros-circulo.md) · [Estado dos checkpoints](docs/ouroboros-status.md)

Para testar a fase 3.3 no Chrome do tablet, abra a branch `feature/studio-visual-compositor-v1`. Em **Code → Codespaces**, crie um Codespace nessa branch. Aguarde a instalação de Python, FFmpeg e dependências pelo devcontainer, execute `./start.sh` no terminal e abra a porta **8000** no painel **Portas**. Confira se a visibilidade está **Privada**. Em um Codespace existente, execute antes `python -m pip install -r requirements.txt`.

Abra um projeto e use **Compor** para montar os fundos, sobrepor imagens a uma fonte animada e enquadrar ícones. **Prévia PS4** renderiza as camadas antes de mostrá-las nas três telas. Em **Pacote**, **Exportar recursos do tema (ZIP)** baixa os recursos processados, independentemente da disponibilidade de um empacotador PKG. [Compositor e roteiro de aceitação](docs/compositor-v1.md)

Em **Fundo**, escolha imagem estática ou animação. Toque no seletor de arquivo para enviar a mídia inteira. Em uma animação, escolha a resolução **de saída**, o número de quadros e o intervalo entre eles. **Exportar** entrega um ZIP com `scene/background.raf/`. Os fundos, ícones e sons usam os slots canônicos. **Pré-visualização** mostra a tela inicial, o menu superior e a seleção de tema.

Os projetos da versão 0.1.0 continuam na instalação em que foram salvos. Para reabri-los, use a branch e o Codespace em que foram criados. Um Codespace novo tem disco próprio. A recuperação grava seus trabalhos em `workspace/recovery/`. O navegador guarda referências para recuperar a sessão na mesma origem.

O código original é de Imxnxl, sob a licença do repositório. ATRAC9 requer áudio pronto ou codificador compatível fornecido pelo proprietário. A exportação RAF funciona sem o empacotador externo. [Referência original](https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator/tree/aa595aa6cda8e906c7f21086b829a6b23223784e)

Consulte o [registro de recuperação](docs/recuperacao.md), o [guia de projetos](docs/projetos-v1.md), o [guia da biblioteca](docs/biblioteca-v1.md) e o [histórico de mudanças](CHANGELOG.md).

Na branch `feature/ouroboros-genesis-v1`, abra **Diagnóstico de loops → Gênesis** para sintetizar, visualizar e exportar faixas com translação periódica. [Demonstração da lua, RAFs e roteiro no tablet](docs/genesis-v1.md)

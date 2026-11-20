# Czar PS4 Theme Studio

## CZAR FORGE — Áudio ATRAC9, Validação de Bundle e Empacotamento PKG Nativo Linux (`feature/czar-audio-pkg-forge-v1`)

Na branch **`feature/czar-audio-pkg-forge-v1`**, o Studio reúne o pipeline completo de finalização de temas PS4 em Linux (GitHub Codespaces), pronto para uso touch no Chrome do **Galaxy Tab S10 Ultra**:

1. **Preparar ferramentas externas nativas Linux (no Codespace)**:
   ```bash
   python3 -m pip install -r requirements.txt
   ./scripts/install_forge_tools.sh all
   PYTHONPATH=. python3 scripts/diagnose_forge.py
   ./start.sh
   ```
   Abra a porta **8000** com visibilidade **Privada** no painel **Portas**.
2. **CZAR ATRAC9 Studio (aba Áudio)**: importe `WAV`, `MP3`, `FLAC` ou `.at9` PS4 válido, ajuste loop (**CZAR Audio Loop Finisher** com prévia da costura e comparação antes/depois) e volume (`original`, `ambient`, `manual`, `assisted` com medição EBU R128 `bgmhome_loud`), e toque em **CONVERTER E APLICAR MÚSICA** para codificar em ATRAC9 real (48 kHz, estéreo, 144 kbps, `configData fe 74 0b f0`, bloco `smpl`) com validação independente via FFmpeg. Efeitos `se_decide`, `se_cursor` e `se_cancel` são convertidos para VAG com prévia reproduzível.
3. **CZAR FORGE (aba Empacotar)**: utilize os botões **VALIDAR MEU TEMA**, **EXPORTAR RECURSOS**, **GERAR PKG**, **BAIXAR PKG** e **VER RELATÓRIO**. O empacotador nativo Linux gera `.pkg` (`pkg_ps4_theme`, `IRO_TAG=2` no `param.sfo` e no offset `0x98` do cabeçalho binário, `CATEGORY=ac`, passcode `0×32`) com verificação criptográfica, extração de `Image0` e comparação SHA-256 arquivo a arquivo contra o bundle.
4. **Compatibilidade física no PS4 Slim (firmware 12.52)**: a validação em Linux comprova estrutura, metadados e hashes, mas **não substitui o teste físico no console**. Todo pacote permanece em estado **`PENDENTE DE TESTE FÍSICO`** até conclusão do roteiro em [`docs/acceptance-ps4.md`](docs/acceptance-ps4.md).

Documentação técnica da forja:
- [Arquitetura e auditoria CZAR FORGE](docs/czar-forge.md)
- [CZAR ATRAC9 Studio e Loop Finisher](docs/atrac9-studio.md)
- [CZAR PKG Forge (Linux nativo e IRO_TAG=2)](docs/pkg-forge.md)
- [Validação independente de PKG e hashes](docs/pkg-validation.md)
- [Licenciamento e fronteiras GPL-3.0 / LGPL-3.0](docs/licensing.md)
- [Roteiro de aceitação física no PS4 Slim 12.52](docs/acceptance-ps4.md)

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

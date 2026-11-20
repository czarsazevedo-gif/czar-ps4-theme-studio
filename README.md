# Czar PS4 Theme Studio

Para usar o **diagnóstico Ouroboros 3.4**, abra a branch `feature/studio-infinite-motion-v1` no GitHub. Em **Code → Codespaces**, crie um Codespace nessa branch e aguarde o devcontainer. Execute `./start.sh` e abra a porta **8000** com visibilidade **Privada**. O endereço encaminhado costuma ter a forma `https://<nome-do-codespace>-8000.app.github.dev`.

No editor, abra um projeto, importe um vídeo e toque em **Diagnóstico de loops**. Selecione o vídeo da biblioteca ou o fundo animado salvo. **Analisar movimento e ciclos** mostra o diagnóstico da fonte e os cortes por resolução. **Conferir DDS** mede os bytes reais e reproduz os quadros decodificados. O diagnóstico trabalha em uma área privada do projeto. Para exportar um RAF pelo compositor, use o fluxo da 3.3. [Resultados e aceitação no tablet](docs/ouroboros-circulo.md) · [Estado dos checkpoints](docs/ouroboros-status.md)

Para testar a fase 3.3 no Chrome do tablet, abra a branch `feature/studio-visual-compositor-v1`. Em **Code → Codespaces**, crie um Codespace nessa branch. Aguarde a instalação de Python, FFmpeg e dependências pelo devcontainer, execute `./start.sh` no terminal e abra a porta **8000** no painel **Portas**. Confira se a visibilidade está **Privada**. Em um Codespace existente, execute antes `python -m pip install -r requirements.txt`.

Abra um projeto e use **Compor** para montar os fundos, sobrepor imagens a uma fonte animada e enquadrar ícones. **Prévia PS4** renderiza as camadas antes de mostrá-las nas três telas. Em **Pacote**, **Exportar recursos do tema (ZIP)** baixa os recursos processados, independentemente da disponibilidade de um empacotador PKG. [Compositor e roteiro de aceitação](docs/compositor-v1.md)

Em **Fundo**, escolha imagem estática ou animação. Toque no seletor de arquivo para enviar a mídia inteira. Em uma animação, escolha a resolução **de saída**, o número de quadros e o intervalo entre eles. **Exportar** entrega um ZIP com `scene/background.raf/`. Os fundos, ícones e sons usam os slots canônicos. **Pré-visualização** mostra a tela inicial, o menu superior e a seleção de tema.

Os projetos da versão 0.1.0 continuam na instalação em que foram salvos. Para reabri-los, use a branch e o Codespace em que foram criados. Um Codespace novo tem disco próprio. A recuperação grava seus trabalhos em `workspace/recovery/`. O navegador guarda referências para recuperar a sessão na mesma origem.

O código original é de Imxnxl, sob a licença do repositório. ATRAC9 requer áudio pronto ou codificador compatível fornecido pelo proprietário. A exportação RAF funciona sem o empacotador externo. [Referência original](https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator/tree/aa595aa6cda8e906c7f21086b829a6b23223784e)

Consulte o [registro de recuperação](docs/recuperacao.md), o [guia de projetos](docs/projetos-v1.md), o [guia da biblioteca](docs/biblioteca-v1.md) e o [histórico de mudanças](CHANGELOG.md).

Na branch `feature/ouroboros-genesis-v1`, abra **Diagnóstico de loops → Gênesis** para sintetizar, visualizar e exportar faixas com translação periódica. [Demonstração da lua, RAFs e roteiro no tablet](docs/genesis-v1.md)

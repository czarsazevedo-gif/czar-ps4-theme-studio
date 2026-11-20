# Czar PS4 Theme Studio

No Chrome do tablet, abra a branch `feature/studio-infinite-motion-v1` e crie um Codespace nela em **Code → Codespaces**. Na primeira abertura, o devcontainer instala FFmpeg e compila os módulos livres do OpenCV usados pelo laboratório em CPU. Aguarde o fim da preparação, execute `./start.sh` no terminal e abra a porta **8000**, com visibilidade **Privada**, no painel **Portas**. O endereço encaminhado segue a forma `https://<nome-do-codespace>-8000.app.github.dev`. Para atualizar as dependências de um Codespace já preparado para esta branch, execute `./.devcontainer/install-motion.sh`.

Em um projeto, selecione **Fundo → Animado** e importe um vídeo, ou aplique um da Biblioteca. Em **Loops**, procure junções, selecione um candidato e toque em **Renderizar e verificar**. O monitor toca os próprios quadros DDS e permite comparar a fonte, o corte e o refinamento. **Aplicar ao tema** usa a mesma cena na prévia PS4 e no ZIP de recursos. [Laboratório, medições e aceitação no tablet](docs/infinite-motion-v1.md)

Abra um projeto e use **Compor** para montar os fundos, sobrepor imagens a uma fonte animada e enquadrar ícones. **Prévia PS4** renderiza as camadas antes de mostrá-las nas três telas. Em **Pacote**, **Exportar recursos do tema (ZIP)** baixa os recursos processados, independentemente da disponibilidade de um empacotador PKG. [Compositor e roteiro de aceitação](docs/compositor-v1.md)

Em **Fundo**, escolha imagem estática ou animação. Toque no seletor de arquivo para enviar a mídia inteira. Em uma animação, escolha a resolução **de saída**, o número de quadros e o intervalo entre eles. **Exportar** entrega um ZIP com `scene/background.raf/`. Os fundos, ícones e sons usam os slots canônicos. **Pré-visualização** mostra a tela inicial, o menu superior e a seleção de tema.

Os projetos da versão 0.1.0 continuam na instalação em que foram salvos. Para reabri-los, use a branch e o Codespace em que foram criados. Um Codespace novo tem disco próprio. A recuperação grava seus trabalhos em `workspace/recovery/`. O navegador guarda referências para recuperar a sessão na mesma origem.

O código original é de Imxnxl, sob a licença do repositório. ATRAC9 requer áudio pronto ou codificador compatível fornecido pelo proprietário. A exportação RAF funciona sem o empacotador externo. [Referência original](https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator/tree/aa595aa6cda8e906c7f21086b829a6b23223784e)

Consulte o [registro de recuperação](docs/recuperacao.md), o [guia de projetos](docs/projetos-v1.md), o [guia da biblioteca](docs/biblioteca-v1.md) e o [histórico de mudanças](CHANGELOG.md).

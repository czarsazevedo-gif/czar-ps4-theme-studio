# Czar PS4 Theme Studio · recuperação

No Chrome do tablet, abra a branch `recovery/canonical-codespaces` deste repositório. Em **Code → Codespaces**, crie um Codespace nessa branch. Aguarde a instalação de Python e FFmpeg pelo devcontainer, execute `./start.sh` no terminal e abra a porta **8000** no painel **Portas**. Confira se a visibilidade está **Privada**. O processamento ocorre no Linux do Codespace.

Em **Fundo**, escolha imagem estática ou animação. Toque no seletor de arquivo para enviar a mídia inteira. Em uma animação, escolha a resolução **de saída**, o número de quadros e o intervalo entre eles. **Exportar** entrega um ZIP com `scene/background.raf/`. Os fundos, ícones e sons usam os slots canônicos. **Pré-visualização** mostra a tela inicial, o menu superior e a seleção de tema.

Os projetos da versão 0.1.0 continuam na instalação em que foram salvos. Para reabri-los, use a branch e o Codespace em que foram criados. Um Codespace novo tem disco próprio. A recuperação grava seus trabalhos em `workspace/recovery/`. O navegador guarda referências para recuperar a sessão na mesma origem.

O código original é de Imxnxl, sob a licença do repositório. ATRAC9 requer áudio pronto ou codificador compatível fornecido pelo proprietário. A exportação RAF funciona sem o empacotador externo. [Referência original](https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator/tree/aa595aa6cda8e906c7f21086b829a6b23223784e)

Consulte o [registro de recuperação](docs/recuperacao.md) para conferir as verificações e registrar o teste no tablet.

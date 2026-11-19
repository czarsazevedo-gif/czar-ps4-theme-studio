# Projetos · fase 3.1

O painel Projetos permite criar, abrir, renomear, salvar, duplicar e fechar trabalhos. A lista é ordenada pela última alteração. O nome do projeto é independente do título do tema na aba Pacote. A sessão avulsa continua disponível quando nenhum projeto está aberto.

## Persistência

- Cada manifesto JSON tem `schemaVersion: 1`, ID, nome, horários UTC, revisão e estado do editor. O estado referencia os jobs originais de fundos, ícones, áudio e animação. O servidor valida os campos, o orçamento da animação e a existência e o tipo dos jobs no projeto.
- O servidor mantém os dados em `workspace/studio-projects-v1/` com uma pasta por sessão e projeto. Um cookie persistente de primeira parte identifica a sessão no mesmo perfil e endereço do navegador. Os endpoints canônicos recebem o ID do projeto e resolvem os arquivos no diretório privado correspondente. Os projetos e mídias anteriores continuam em seus diretórios.
- O autosave aguarda uma pausa breve de edição. O servidor confere a revisão sob bloqueio e troca o manifesto por um arquivo completo gravado no mesmo volume. O editor mostra **Salvo** após a confirmação. Uma revisão desatualizada recebe HTTP 409. O rascunho fica na aba, e uma nova aba pode consultar o último manifesto salvo.
- A duplicação cria outro manifesto e outros diretórios de processamento. O Linux compartilha somente os originais imutáveis por hard links no mesmo volume. Miniaturas estáticas e áudio processado são copiados. A animação restaura os quadros pelo processador original. Exportações geradas antes da duplicação podem ser geradas novamente a partir da cópia.

## Teste no Galaxy Tab

1. Abra a branch `feature/studio-projects-v1` em um Codespace de teste. Aguarde a preparação do contêiner, execute `./start.sh` e abra a porta privada 8000 no Chrome.
2. Toque em **Novo**, dê um nome e importe um PNG. Ajuste um ícone, uma cor ou o título do tema. Espere **Salvo**, recarregue a página e confira o fundo, o ícone e as cores nas três telas.
3. Importe um GIF ou MP4. Ajuste resolução, número de quadros e temporização. Exporte o RAF. Duplique o projeto, altere a cópia e use **Fechar** e **Abrir** para confirmar que o original mantém seus ajustes.
4. Pare e reabra **o mesmo Codespace**, inicie o servidor de novo e reabra o projeto pelo mesmo endereço encaminhado e perfil do Chrome. Use uma segunda aba para conferir o aviso de conflito ao editar o mesmo projeto.

## Limites e riscos

O vínculo com os projetos depende dos dados persistidos do Codespace e do cookie do perfil do navegador. Criar outro Codespace, apagar o cookie, mudar de perfil ou de endereço não transfere os projetos. Não há migração automática. Rascunhos ainda sem confirmação do servidor dependem do armazenamento da aba para sobreviver à recarga. O consumo de disco cresce com as importações e os derivados. A duplicação depende de suporte a hard links no mesmo volume. Em abas concorrentes, a revisão protege o manifesto e o processamento de um projeto é serializado. Ao reabrir, o editor aplica os parâmetros confirmados do manifesto.

## Bancada

Com as fixtures de `python tests/make_fixtures.py`, execute `PYTHONPATH=. python tests/test_projects.py -v` (requer `httpx` para os testes) e `python tests/test_projects_browser.py` (requer Playwright e Chromium). A suíte de navegador inicia e reinicia Uvicorn num workspace temporário. O roteiro no Galaxy Tab cobre a reabertura real do Codespace e o encaminhamento privado.

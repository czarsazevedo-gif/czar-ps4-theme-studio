# Roteiro de Aceitação Física — PS4 Slim (Firmware 12.52)

> **AVISO OBRIGATÓRIO DE HONESTIDADE TÉCNICA**
>
> Testes executados em Linux (GitHub Codespaces), validação binária/criptográfica de PKG, extração de `Image0` e decodificação ATRAC9 via FFmpeg **NÃO substituem o teste físico no console PS4**.
>
> Todo pacote `.pkg` gerado pelo **CZAR FORGE** distingue explicitamente três estágios:
> 1. **`PACOTE GERADO`** (arquivo `.pkg` produzido; extração completa de `Image0` não executada),
> 2. **`PACOTE VALIDADO ESTRUTURALMENTE`** (cabeçalho binário `IRO_TAG=2`, `CATEGORY=ac`, `Sc0`, `Image0` extraído e SHA-256 100% conferido contra o bundle em Linux),
> 3. **`PACOTE TESTADO NO PS4`** (apenas após o proprietário concluir este roteiro no **PS4 Slim — firmware 12.52**; nunca marcado automaticamente).

---

## 1. Pré-requisitos no computador / tablet (Galaxy Tab S10 Ultra + Chrome)

1. Abrir o projeto no Czar PS4 Theme Studio na porta **8000 (Privada)** do Codespace.
2. Na aba **Empacotar (CZAR FORGE)**, clicar em **VALIDAR MEU TEMA** e confirmar que todas as verificações estão aprovadas (`0 erros`).
3. Clicar em **GERAR PKG** e aguardar a conclusão com status **`PACOTE VALIDADO ESTRUTURALMENTE`** e **`PACOTE TESTADO NO PS4: Pendente (PS4 Slim 12.52)`**.
4. Clicar em **VER RELATÓRIO COMPLETO** e registrar:
   - `CONTENT_ID` e `TITLE_ID`
   - `TIPO DE TEMA: pkg_ps4_theme`
   - `IRO_TAG: cabeçalho 0x98 = 2 · SFO = 2`
   - `CATEGORY: ac`
   - `bgmhome_loud` (quando houver música) ou `Não aplicável` (quando sem música)
   - `Extração ↔ Bundle (SHA-256): 100% correspondente`
   - `SHA-256 do PKG`
5. Clicar em **BAIXAR PKG** e salvar o arquivo `.pkg` no armazenamento do dispositivo.
6. Copiar o `.pkg` para a raiz de um pendrive USB formatado em **exFAT** ou **FAT32** (verificar que o tamanho em bytes no pendrive é idêntico ao reportado pelo CZAR FORGE).

---

## 1B. Checklist de Aceitação Física no Galaxy Tab S10 Ultra (Chrome Android · Toque e S Pen)

> **Status atual: `PENDENTE DE TESTE FÍSICO NO TABLET`** (validado em Chromium headless com toque emulado em `1480×924` e `924×1480`, sem substituir o aparelho físico).

- [ ] **Paisagem (`1480×924`) e Retrato (`924×1480`)**: alternar orientação girando o tablet; confirmar ausência de rolagem horizontal indesejada, sobreposição de texto ou botões cortados nas 6 abas e no Ouroboros Deluxe.
- [ ] **Toque com o dedo e precisão com S Pen**: abrir o guia **COMO ESCOLHER UM VÍDEO PARA UM LOOP PERFEITO**, operar sliders de quadros/wait/ganho, desenhar máscaras regionais no Ouroboros Deluxe e mover/redimensionar/rotacionar camadas no Compositor sem perda de foco ou disparo duplo.
- [ ] **Importação e diagnóstico não-bloqueante**: importar vídeo real do armazenamento do tablet, confirmar que o diagnóstico automático de 21 campos roda em segundo plano sem travar o player e pode ser cancelado sem afetar o vídeo.
- [ ] **Prévia de áudio no Chrome Android**: reproduzir música ATRAC9 decodificada, prévia de costura do loop e efeitos sonoros VAG diretamente no tablet.
- [ ] **Download e persistência após recarregar aba**: baixar `background.raf.zip` e `.pkg` no Chrome Android; recarregar a página e confirmar que o projeto salvo e o histórico de PKGs reaparecem intactos.

---

## 2. Checklist de Teste Físico no PS4 Slim (Firmware 12.52)

> **Status atual: `PENDENTE DE TESTE FÍSICO NO CONSOLE`**

Marque cada item durante a execução real no console:

### Instalação e reconhecimento
- [ ] **Transferência / detecção USB**: o arquivo `.pkg` aparece listado corretamente no instalador de pacotes do PS4 (sem arquivo corrompido ou invisível).
- [ ] **Instalação sem erro de tipo**: a instalação conclui **sem** erro `CE-37695-0` (indício de `IRO_TAG` incorreto ou `content_type` errado) e **sem** erro de hash/integridade (`CE-36244-9` / `CE-34706-0`).
- [ ] **Aparição exclusiva em Configurações → Temas**: o pacote aparece em **Configurações → Temas** como tema instalável (e **NÃO** como aplicativo ou jogo na tela inicial).
- [ ] **Miniatura e tela de seleção**: `Sc0/icon0.png` (512×512), `Sc0/pic1.png` (1920×1080) e `Image0/texture/preview.png` (740×416) exibem corretamente sem distorção ou erro de cor.

### Aplicação e visual
- [ ] **Aplicação do tema**: selecionar **Aplicar** conclui sem erro `CE-36329-3` ou travamento da interface.
- [ ] **Fundo animado (quando aplicável)**: a animação `scene/background.raf/` reproduz com fluidez, sem congelamento, sem corte abrupto e sem artefatos DXT1a inesperados.
- [ ] **Fundo estático (quando aplicável)**: `texture/background/home.png` e `function.png` aparecem nítidos em 1920×1080.
- [ ] **Ícones de função (menu superior)**: os ícones 128×128 e seus brilhos de foco 152×152 (RGBA) aparecem corretamente e destacam o item focado.
- [ ] **Ícones de conteúdo (linha principal)**: os ícones 512×512 (RGBA) aparecem corretamente sem borda preta indevida.
- [ ] **Cores e legibilidade**: `themecolor`, `fontcolor`, `fontshadowcolor` e `focuscolor` mantêm boa leitura na tela inicial e em Configurações.

### Áudio (Música ATRAC9 e Efeitos VAG)
- [ ] **Reprodução da música**: `sound/bgm_home.at9` inicia automaticamente na tela inicial (48 kHz, estéreo, 144 kbps, ATRAC9 real).
- [ ] **Qualidade do loop prolongado**: deixar o PS4 parado na tela inicial por **pelo menos 3 ciclos completos** da música; verificar ausência de cliques, estalos, silêncio inesperado, salto brusco de volume ou truncamento.
- [ ] **Volume confortável na TV**: o volume da música no menu está adequado para uso contínuo (nem estourado/incômodo, nem inaudível), respeitando `bgmhome_loud` e o modo escolhido no CZAR ATRAC9 Studio.
- [ ] **Efeitos sonoros VAG**: testar navegação direcional (`se_cursor.vag`), confirmação (`se_decide.vag`) e retorno (`se_cancel.vag`); confirmar resposta imediata sem distorção ou atraso.

### Estabilidade e ciclo de vida
- [ ] **Navegação entre telas**: alternar repetidamente entre Tela Inicial, Menu Superior (Notificações, Amigos, Configurações) e Biblioteca; confirmar retorno limpo do fundo e da música.
- [ ] **Troca de tema**: aplicar outro tema do sistema e voltar para o tema gerado pelo CZAR FORGE; confirmar reaplicação limpa.
- [ ] **Reinício do console**: reiniciar o PS4 Slim 12.52 com o tema ativo; confirmar que o tema permanece aplicado, animado e com música funcionando após o boot.
- [ ] **Remoção limpa (opcional)**: testar exclusão em Configurações → Temas sem deixar ícones fantasma.

---

## 3. Registro do resultado físico

| Campo | Registro |
| --- | --- |
| Console / Firmware | PS4 Slim — Firmware 12.52 |
| Data do ensaio | _Preencher após teste físico_ |
| `CONTENT_ID` testado | _Preencher_ |
| `SHA-256` do `.pkg` | _Preencher_ |
| Resultado físico | `PENDENTE` / `APROVADO` / `REPROVADO` |
| Observações / códigos de erro (se houver) | _Preencher_ |

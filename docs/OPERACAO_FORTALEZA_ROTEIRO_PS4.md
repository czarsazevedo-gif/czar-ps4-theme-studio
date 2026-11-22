# OPERAÇÃO FORTALEZA — Roteiro Executável de Ensaio Físico no PS4 Slim (Firmware 12.52)

> **REGRA INEGOCIÁVEL DE VERACIDADE:** Nenhuma validação estrutural, comparação multi-ferramenta ou simulação no PS4 Theme Digital Twin (`SEMANTIC_SIMULATION_ONLY`) constitui prova de compatibilidade física. O estado `PHYSICALLY_VERIFIED` **só** pode ser registrado após a execução presencial deste roteiro em console físico, vinculado estritamente ao **SHA-256 exato** do arquivo `.pkg` instalado e ao `inputs_fingerprint` da revisão testada. Qualquer reconstrução ou alteração do tema volta automaticamente a `PHYSICAL_TEST_PENDING`.

---

## 1. Pré-requisitos e Integridade do Pacote Antes do Console

1. **Identificação do arquivo-alvo da primeira instalação:**
   - **Candidato prioritário (original do Google Drive preservado intacto fora do Git):**
     - Arquivo: `UP9000-CUSA00000_00-ZENITSUSOUL00000.pkg`
     - SHA-256 esperado: `9647b10bcdc99001500b0b0a49e3941c80d48322ceb31ec94ca7fbc0d79ef363`
     - Cópia congelada na bancada Linux: `~/.gemini/czar-zenitsu-audit/frozen_for_ps4/UP9000-CUSA00000_00-ZENITSUSOUL00000.pkg`
   - **Candidatos de controle isolado (gerados pela Operação Fortaleza fora do Git em `~/.gemini/czar-operacao-fortaleza/builds/`, todos aprovados em `czar-python-pkg-inspector`, `LibOrbisPkg`, `pearlxcore/OrbisPkgTool` e `ps4-pkg-tool` C++23 ShadPS4 com 100% de paridade SHA-256):**
     - `A1_static_simple` (`2.031.616 B`, SHA-256 `f4b1415c49e305b6917596ac079e9f396890c3f46cdc1deed6feb3b56156ef8c`) — controle mínimo estático (isola contêiner CNT + `IRO_TAG=2` + `CATEGORY=ac` + `theme.xml` + `param.sfo`).
     - `A2_static_icons` (`3.997.696 B`, SHA-256 `4688a4f4111af9a488985dafb9c9deec561e5f974941eb583c3145cf081d162c`) — controle estático + 9/9 ícones de função + 9/9 glows + 10/10 ícones de conteúdo (isola PNG RGBA e dimensões).
     - `A3_dynamic_raf` (`5.111.808 B`, SHA-256 `729397c6050802b1036ec1d53c58af467ad6f558300b4093715f22956c4c9f0b`) — controle dinâmico apenas com RAF 12×540p (isola `plane.mdx` + `index.xml` + DXT1a `.dds`).
     - `A4_dynamic_full` (`7.471.104 B`, SHA-256 `976691c2f0037c215b542dc8068748b7c6c6e63fa62ef85ec4080cdae0e67fbc`) — controle dinâmico completo com ícones + ATRAC9 144 kbps + 3 VAGs (isola áudio + loop).
     - `A5_zenitsu_real` (`14.286.848 B`, SHA-256 `804a186f9937e85596a9a9ad6a04c450cfdc9f4564ffb2e4d7203feb59639d7c`) — reconstrução limpa Gold Master a partir das 23 mídias originais do Drive.
     - `A6_crown_of_thorns_real` (`12.386.304 B`, SHA-256 `1a53b07bf692b176216ab4a2d00da69c43abf3d3190ea21de49f9a2b763f1863`) — tema dinâmico 720p real (13 quadros) + ícones + ATRAC9 + 3 VAGs.

2. **Conferência obrigatória no pendrive (exFAT / FAT32) antes de inserir no PS4:**
   ```bash
   sha256sum /mnt/usb/UP9000-CUSA00000_00-ZENITSUSOUL00000.pkg
   ```
   - Confirme que o hash no pendrive é **idêntico caractere por caractere** ao SHA-256 do relatório.
   - Ejete o pendrive com segurança (`sync && umount`) para evitar truncamento no sistema de arquivos.

3. **Registro obrigatório do ambiente do console (preencher antes de instalar):**
   - Modelo exato do console: `PS4 Slim (ex.: CUH-2215B)`
   - Firmware exato em *Configurações → Sistema → Informações do sistema*: `12.52`
   - Método/ambiente de instalação FPKG: `Debug Settings / GoldHEN / Package Installer (especificar versão)`
   - Tema ativo anterior ao ensaio: `(anotar para teste de troca/reversão)`

---

## 2. Procedimento Passo a Passo e Checklist Observável (14 Pontos)

Marque cada item exclusivamente com `APROVADO`, `REPROVADO` ou `NÃO OBSERVADO`, anotando qualquer código `CE-*` / `NP-*` e registrando foto/vídeo curto no Galaxy Tab S10 Ultra ou celular.

| # | Etapa no PS4 Slim 12.52 | Critério Objetivo de Aprovação | Resultado | Evidência / Observação |
|---|---|---|---|---|
| **P01** | **Instalação do PKG** via *Debug Settings → Game → Package Installer* | Instalação completa sem erro (`CE-34706-0`, `CE-36434-0`, `CE-37695-0`, `CE-36244-9`); notificação de tema instalado (não aparece como aplicativo corrompido na Home). | `[ ]` | |
| **P02** | **Listagem em *Configurações → Temas → Selecionar tema*** | O tema aparece na lista com nome legível (`TITLE` do `param.sfo`), miniatura `icon0.png` nítida (512×512) e sem ícone genérico quebrado. | `[ ]` | |
| **P03** | **Tela de pré-visualização do tema** | `preview.png` (740×416) / `pic1.png` (1920×1080) renderizam corretamente, sem distorção, sem faixa preta anômala e sem artefatos de cor. | `[ ]` | |
| **P04** | **Aplicação do tema** | Aplicar o tema conclui sem congelamento, reinício de ShellUI ou erro `CE-37695-0`. | `[ ]` | |
| **P05** | **Fundo da Home Screen (RAF Dinâmico ou Estático)** | Em tema dinâmico: animação RAF inicia imediatamente, reproduz **>= 10 ciclos completos** sem travamento, inversão de quadros, tela preta, piscadas ou velocidade absurda (confirma `wait` em segundos e `plane.mdx` corretos). Em estático: `homescreen.png` nítido 1080p. | `[ ]` | |
| **P06** | **Tela de Funções (seta para cima) e Configurações** | Transição Home ↔ barra superior fluida; dimmer (`functionscreen-dimmer`) e fundo secundário legíveis; nenhum crash ou queda de FPS ao abrir *Configurações* e *Biblioteca*. | `[ ]` | |
| **P07** | **Ícones de Função (9 slots × 128×128 + `_glow` 152×152)** | Percorrer TODOS os ícones presentes (*PS Store*, *Notificações*, *Amigos*, *Mensagens*, *Party*, *Perfil*, *Troféus*, *Configurações*, *Energia*): transparência limpa (sem halo quadrado preto), ícone correspondente ao slot correto (zero trocas) e efeito `_glow` visível ao focar. | `[ ]` | |
| **P08** | **Ícones de Conteúdo (10 slots × 512×512)** | Verificar na barra de conteúdo (*Disco*, *Navegador*, *Pasta*, *Galeria de Capturas*, *Biblioteca*, *Ao Vivo do PlayStation*, *Reprodutor de Mídia*, *TV e Vídeo*, *Novidades*): arte correta no slot correto, transparência preservada, sem cortes/trocas. | `[ ]` | |
| **P09** | **Tipografia, Contraste e Dimmers (`theme.xml`)** | `fontcolor`, `fontshadowcolor`, `focuscolor` e dimmers tornam os rótulos perfeitamente legíveis sobre o fundo animado/estático. | `[ ]` | |
| **P10** | **Música de Fundo (`sound/bgm_home.at9` — ATRAC9 144 kbps)** | Música começa automaticamente na Home; **ouvir >= 3 voltas completas do loop** verificando: ausência de mudo, ausência de truncamento, ausência de clique forte/chiado na emenda do `smpl` e volume equilibrado (`bgmhome_loud` em `param.sfo`). | `[ ]` | |
| **P11** | **Efeitos Sonoros VAG (`se_decide`, `se_cursor`, `se_cancel`)** | Navegar esquerda/direita (`se_cursor.vag`), confirmar com **X** (`se_decide.vag`) e voltar com **O** (`se_cancel.vag`): cada efeito toca instantaneamente, limpo, sem ruído metálico ou atraso. | `[ ]` | |
| **P12** | **Troca entre temas** | Alternar para um tema padrão da Sony (ex.: *Fluxo*) e voltar para o tema testado: recarregamento limpo de RAF, ícones e ATRAC9 sem vazamento visual ou sonoro. | `[ ]` | |
| **P13** | **Persistência após Modo de Repouso e Reinicialização** | Colocar o console em Modo de Repouso (ou reiniciar, conforme compatível com o ambiente 12.52) e retornar à Home: tema continua íntegro ou reaplicável sem erro. | `[ ]` | |
| **P14** | **Desinstalação limpa** | Em *Configurações → Temas → [Tema] → Options → Excluir*: remoção completa sem deixar entradas fantasmas no banco de dados. | `[ ]` | |

---

## 3. Matriz de Isolamento por Fator Único (Em Caso de Qualquer Falha)

Se o tema completo falhar em qualquer ponto (`P01`–`P14`), **NÃO adivinhe a causa**. Instale os pacotes de controle abaixo na ordem exata para isolar o subsistema responsável em 1 passo por fator:

| Sintoma Observado no PS4 12.52 | Controle Isolado a Instalar | O que o Resultado Prova |
|---|---|---|
| Erro na instalação (`CE-37695-0`, `CE-36434-0`, aparece como App em vez de Tema) | `A1_static_simple.pkg` | Se `A1` falhar: problema está no **contêiner CNT / `IRO_TAG` / `param.sfo` / patch `LibOrbisPkg`**. Se `A1` passar: contêiner está íntegro e o defeito está em um ativo interno do tema completo. |
| `A1` passa, mas ícones aparecem pretos, distorcidos ou derrubam ShellUI | `A2_static_icons.pkg` | Isola **PNG RGBA / transparência / pareamento `128×128` + `_glow 152×152` / `512×512`**. |
| `A1` passa, mas fundo fica preto, congelado, invertido ou trava na Home | `A3_dynamic_raf.pkg` | Isola **`scene/background.raf/`** (`plane.mdx`, `index.xml`, `wait` em segundos, `00..NN.dds` DXT1a). |
| `A3` passa, mas música falha, trava na emenda do loop ou não toca | `A4_dynamic_full.pkg` (e depois uma versão sem `bgm_home.at9`) | Isola **`bgm_home.at9` (`psgfx-at9` 144 kbps / `fact` / `smpl` / `bgmhome_loud`)** vs **efeitos `.vag`**. |
| `A4` passa, mas apenas o tema real Zenitsu/Crown falha | `A5_zenitsu_real.pkg` / `A6_crown_of_thorns_real.pkg` | Isola **ativo específico do projeto real** (ex.: duração do MP3, resolução 720p 13 quadros, tamanho próximo de 6 MiB, ou ícone específico). |

---

## 4. Registro Oficial de Evidência Física (Promoção a `PHYSICALLY_VERIFIED`)

Após concluir os 14 pontos com `APROVADO`, salve o registro abaixo (fora do Git ou no histórico de auditoria do proprietário) para validar o objeto `PhysicalVerificationRecord` em [`app/forge/release_gates.py`](file:///home/antigravity/czarsazevedo-gif/czar-ps4-theme-studio/app/forge/release_gates.py):

```json
{
  "pkg_sha256": "<SHA-256 EXATO DO .PKG INSTALADO NO PS4>",
  "inputs_fingerprint": "<inputs_fingerprint DO report.json / integrity_manifest.json>",
  "console_model": "PS4 Slim CUH-XXXX",
  "firmware": "12.52",
  "install_method": "USB exFAT + Debug Settings / GoldHEN",
  "passed": true,
  "recorded_at": "YYYY-MM-DDTHH:MM:SSZ",
  "checkpoints": {
    "P01_install": "APROVADO",
    "P02_list": "APROVADO",
    "P03_preview": "APROVADO",
    "P04_apply": "APROVADO",
    "P05_background_raf_10cycles": "APROVADO",
    "P06_function_screen": "APROVADO",
    "P07_function_icons_glows": "APROVADO",
    "P08_content_icons": "APROVADO",
    "P09_typography_dimmers": "APROVADO",
    "P10_bgm_atrac9_3loops": "APROVADO",
    "P11_vag_sfx": "APROVADO",
    "P12_theme_switch": "APROVADO",
    "P13_rest_reboot": "APROVADO",
    "P14_uninstall": "APROVADO"
  },
  "notes": "Link/referência das fotos ou vídeo curto gravado durante o ensaio."
}
```

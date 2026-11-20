# Histórico de mudanças

## CZAR FORGE · Áudio ATRAC9, Validação de Bundle e PKG Nativo Linux (`feature/czar-audio-pkg-forge-v1`)

- **Auditoria e correções reais de áudio/404**: rotas de catálogo estático (`/api/audio/slots`, `/api/package/defaults`, `/api/convert/targets`, `/api/forge/status`) isentas do filtro `?project=` antes do boot de sessão; corrigido 404 em `pintarSlots()` ao trocar idioma; removidos diretórios órfãos `audio-*` em falhas de upload; restauração de efeitos VAG por `data-slot`; prévias reproduzíveis em WAV para música e SFX.
- **CZAR ATRAC9 Studio & Audio Loop Finisher**: suporte a WAV, MP3, FLAC e `.at9` PS4 válido com codificador nativo Linux (`elripalda/psgfx` GPL-3.0-or-later instalado externamente via `scripts/install_forge_tools.sh at9`, 48 kHz estéreo, estritamente 144 kbps, `configData fe 74 0b f0`, `fact` + `smpl`), validação independente via FFmpeg + parser RIFF + ida e volta perceptual, controle profissional de volume (`original`, `ambient`, `manual`, `assisted` + EBU R128 `bgmhome_loud`), análise/refino de loop com prévia de costura e botão **CONVERTER E APLICAR MÚSICA**.
- **CZAR Bundle Validator & `param.sfo` binário**: gerador/leitor binário PSF v1.1 nativo (`IRO_TAG=2`, `CATEGORY=ac`, `bgmhome_loud`), preservação da equivalência RAF byte a byte com Ouroboros/Deluxe em `_add_animated_scene`, validação exaustiva de `theme.xml`, texturas, ícones 128×128/152×152/512×512, RAF (`<=48` quadros, `<=6 MiB`), áudio e `.gp4`.
- **CZAR PKG Forge & Inspetor Independente**: empacotamento nativo Linux (`OpenOrbis/LibOrbisPkg` LGPL-3.0 externo + patch auditável gravando `IRO_TAG=2` no offset `0x98` do cabeçalho binário antes de `HeaderDigest`/`HeaderSignature` e preservando `bgmhome_loud`), inspetor independente em Python puro verificando cabeçalho, digests, `Sc0/param.sfo`, `pkg_validate`, extração de `Image0` e SHA-256 arquivo a arquivo contra o bundle, além de jobs assíncronos com progresso, cancelamento e persistência pós-reinício.
- **Interface Touch & Aceitação PS4**: painel CZAR FORGE em português brasileiro com resumo de 8 indicadores, botões `VALIDAR MEU TEMA`, `EXPORTAR RECURSOS`, `GERAR PKG`, `BAIXAR PKG`, `VER RELATÓRIO`, flush automático de autosave antes de exportar, diagnóstico reproduzível (`scripts/diagnose_forge.py`), suíte A–Z + E2E de 19 etapas e roteiro de aceitação física para PS4 Slim 12.52 (`docs/acceptance-ps4.md`, status explícito `PENDENTE DE TESTE FÍSICO`).

## Fase 3.4 · fundação Ouroboros em branch de desenvolvimento

- ARCA preservou o protótipo interrompido em um commit recuperável e documentou a base canônica.
- Sentinela mede trajetórias, simetria temporal, regiões de atividade e mudanças de luz em CPU.
- Círculo procura cortes naturais com os orçamentos de 720p, 540p e 360p. A página de diagnóstico permite repetir um candidato, inspecionar a junção e conferir o resultado do DDS nativo.
- O diagnóstico usa originais já importados, trabalhos canceláveis e cache por conteúdo no projeto. Um ensaio de regressão compara as entradas de seis ZIPs RAF com a versão 3.3.
- A aceitação física no Galaxy Tab e as fontes privadas Lua e Coroa continuam pendentes. [Ensaios da fundação](docs/ouroboros-circulo.md)

## Fase 3.3 · compositor visual

- Composição raster para fundos e slots de ícones, com enquadramento, edição por toque e valores numéricos, e histórico por aba.
- Sobreposições estáticas aplicadas aos quadros RGBA antes da geração canônica de DDS.
- Estado de composição versionado no projeto e fontes imutáveis da biblioteca.
- Exportação ZIP dos recursos canônicos do tema, além do fluxo RAF e do empacotamento opcional.
- Ensaios de pixels de PNG, comparação com DDS gerado a partir da prévia e regressões do editor anterior.

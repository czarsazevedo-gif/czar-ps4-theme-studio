# Auditoria da base

A implementação parte do commit `aa595aa6cda8e906c7f21086b829a6b23223784e`.
O checkout foi reconstruído com os blobs do GitHub quando o clone direto
estava bloqueado. O Git confirmou a mesma árvore e o mesmo hash do commit.

| Componente original | Decisão |
| --- | --- |
| FastAPI e JS sem build step | Manter, com submódulos para persistência, composição e UI. Os fluxos originais ficam disponíveis no modo legado local. |
| `dxt1.py`, `dds.py`, `mdx.py`, `raf.py`, `vag.py` | Reusar encoders, gerador de cena e parsers. |
| `theme.py` | Reusar nomes, LF e limites. Remover o gerador sem chamadas que incluía `target`. |
| `theme_xml.py`, `config.py` | Reusar slots, caminhos, cores, tamanhos e defaults. |
| `pkg.py` | Reusar o perfil de tema, as regras, a fonte SFX dos campos e o gerador GP4. Acrescentar um codificador PSF para a etapa Linux que antes dependia de `sfo_create`. |
| `media.py` | Reusar a descoberta de FFmpeg e manter os fluxos anteriores. As entradas do Studio usam contêiner forçado, protocolos limitados, verificação e timeout. Pillow compõe recortes, camadas e glow. |
| `jobs.py` | Preservar o modo legado, separando a retenção de jobs da pasta de projetos. |
| Preview original | Manter no legado. Usar no Studio a disposição relativa das três telas e a seleção inicial do segundo ícone da tela inicial, com canvas de composição. |
| MIT e avisos | Manter créditos e separar a etapa proprietária do editor. |

## Baseline no Linux

No código original, DXT1, HTTP, conversão, UI principal e UI de slots passaram.
A UI de preview passou suas verificações visuais e falhou em uma asserção
que presumia o botão de empacotar ativo sem o empacotador. Os testes de slots,
tema e pacote chegaram à etapa de construção e pararam por falta da ferramenta.
A suíte de áudio tentou ler a fixture AT9 em um teste de slot desconhecido e
encerrou antes de chegar ao trecho que a tornava opcional. As verificações
dependentes foram condicionadas à disponibilidade. A construção e extração
real continuam sendo executadas pelas suítes legadas quando as dependências
existem no ambiente compatível.

## Verificação do Studio

- `./test.sh` passou DXT1, seis casos de API e segurança, dois viewports
  touch e os ramos disponíveis das suítes originais.
- Outro teste executou `./start.sh` na porta 8000, verificou `/healthz` e repetiu
  os cenários de tablet no servidor inicializado pelo script.
- `tests/studio_api.py` verifica o RAF de MP4 e GIF, o bundle, a reimportação,
  VAG, WAV, SFO, isolamento, origens, CSRF, ZIP Slip, bomba simulada, limites
  e cancelamento. Os elementos dependentes de AT9 pronto e pacote executável
  requerem um ambiente externo.
- O encaminhamento real em Codespaces, o navegador no tablet físico e o
  console PS4 ficam na matriz como pendentes de validação.

## Empacotamento pesquisado

O projeto aberto [LibOrbisPkg](https://github.com/maxton/LibOrbisPkg) publica
seu código sob GPL-3.0. Suas definições reconhecem formatos de volume de temas,
mas o [construtor consultado](https://github.com/maxton/LibOrbisPkg/blob/master/LibOrbisPkg/PKG/PkgBuilder.cs)
inicializa `iro_tag` do cabeçalho como `None`. O material consultado também
não demonstrou um fluxo reproduzível para este volume com os metadados e
fixtures exigidos, por isso o Studio opera em Status C. Wine e instalação física
não foram verificados.

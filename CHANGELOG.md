# Histórico de mudanças

## Fase 3.4 · Czar Infinite Motion

- Laboratório de loops com seleção temporal, leitura de timestamps, busca de junções e medição de direção, velocidade, luminosidade e diferenças entre quadros.
- Corte manual e automático, verificação de recorrência de textura e alinhamento local com retângulo protegido. A decisão de usar um refinamento passa por uma nova comparação dos DDS.
- Timeline touch, histórico, reprodução contínua e inspeção do corte, do refinamento e do RAF decodificado. O loop aplicado segue para a prévia PS4 e para a árvore do tema com suas camadas estáticas.
- Operações de CPU com progresso, cancelamento, cache privado por conteúdo e parâmetros, publicação atômica e recuperação de arquivos derivados. A configuração opcional continua no manifesto de projeto v1.
- Comparação binária com a fase 3.3, ensaios sintéticos de direção e testes de API, Chromium touch e reinício do servidor. [Resultados e limites da fase](docs/infinite-motion-v1.md)

## Fase 3.3 · compositor visual

- Composição raster para fundos e slots de ícones, com enquadramento, edição por toque e valores numéricos, e histórico por aba.
- Sobreposições estáticas aplicadas aos quadros RGBA antes da geração canônica de DDS.
- Estado de composição versionado no projeto e fontes imutáveis da biblioteca.
- Exportação ZIP dos recursos canônicos do tema, além do fluxo RAF e do empacotamento opcional.
- Ensaios de pixels de PNG, comparação com DDS gerado a partir da prévia e regressões do editor anterior.

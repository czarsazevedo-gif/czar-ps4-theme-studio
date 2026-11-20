# Validação Independente de Bundles e Pacotes PKG — CZAR Forge

No **CZAR Forge**, gerar um arquivo com extensão `.pkg` nunca é tratado como prova de sucesso. Toda construção passa por validação independente antes e depois do empacotamento.

## 1. Validação pré-PKG: CZAR Bundle Validator (`app/forge/bundle_validator.py`)

Antes de acionar o empacotador, a árvore montada do tema é auditada integralmente:

1. **`Image0/theme.xml`**: XML bem-formado, `<format-version>4.0</format-version>`, `<themecolor>` 0–7, cores `#AARRGGBB`, e consistência estrita entre `<homebgm-enable>` e a existência de `Image0/sound/bgm_home.at9`.
2. **Exclusividade estático vs. animado**:
   - Tema estático: exige `texture/background/homescreen.png` e `functionscreen.png` (1920×1080 PNG íntegros).
   - Tema animado: proíbe PNGs mortos em `texture/background/` e valida `scene/background.raf/` (`NN.dds` DXT1a, resolução canônica, `<= 48` quadros, `<= 6 MiB`, `plane.mdx` com cabeçalho `XDM.00.1MSP`, `index.xml` com materiais e `<case>` sincronizados).
3. **Ícones e miniaturas**:
   - `texture/preview.png`: 740×416 PNG.
   - `Sc0/icon0.png`: 512×512 PNG.
   - `Sc0/pic1.png`: 1920×1080 PNG.
   - `texture/function_icon/<slot>.png` (128×128 RGBA) + `<slot>_glow.png` (152×152 RGBA), somente nos 9 slots canônicos, sempre em pares.
   - `texture/content_icon/<slot>.png` (512×512 RGBA), somente nos 10 slots canônicos.
4. **Áudio**:
   - `Image0/sound/bgm_home.at9`: validação independente via `validate_at9` (parser RIFF binário + FFprobe + decodificação completa FFmpeg: 48 kHz, estéreo, 144 kbps, `configData fe 74 0b f0`, `fact`, `smpl` com `playCount=0`).
   - `Image0/sound/se_{decide,cursor,cancel}.vag`: cabeçalho `VAGp` 48 kHz + verificação FFprobe (`adpcm_psx`).
5. **`Sc0/param.sfo`**: parser PSF v1.1 independente (`app/forge/sfo.py`) confirma `CATEGORY='ac'`, `IRO_TAG=2`, `FORMAT='obs'`, ausência de `APP_TYPE`/`APP_VER`, coerência `TITLE_ID` ↔ `CONTENT_ID` e presença/ausência coerente de `bgmhome_loud`.
6. **`.gp4`**: verifica `volume_type=pkg_ps4_theme`, ausência de `chunk_info`/`storage_type` e correspondência 1-para-1 de todos os arquivos em disco.

## 2. Validação pós-PKG independente (`app/forge/pkg_inspector.py`)

Após o `.pkg` ser gerado pelo empacotador externo (`liborbispkg-linux`), um **leitor binário PS4 PKG independente escrito em Python puro** inspeciona o arquivo diretamente byte a byte:

1. **Cabeçalho binário (`0x000..0x1100`)**:
   - Assinatura `\x7fCNT` (`0x00`), `drm_type = 0xF` (`0x70`).
   - **`content_type = 0x1B` (`AC`) em `0x74`** (reprova `0x1A` GD).
   - **`iro_tag = 2` (`IROTag.SS_THEME`) em `0x98`** (reprova `0` ou qualquer valor divergente).
   - `package_size == tamanho real do arquivo`.
2. **Integridade criptográfica SHA-256**:
   - `HeaderDigest` (`0xFE0..0x1000`) == `SHA-256(pkg[0x000:0xFE0])` — prova que `0x98` não foi adulterado após a geração!
   - `HeaderSignature` (`0x1000..0x1100`) presente (não zerada).
   - `body_digest` (`0x160`), `digest_table_hash` (`0x140`), `sc_entries1_hash` (`0x100`), `sc_entries2_hash` (`0x120`).
   - `pfs_image_digest` (`0x440`) e `pfs_signed_digest` (`0x460`).
   - Verificação individual do digest SHA-256 de cada entrada na tabela `DIGESTS` (`0x001`).
3. **Extração direta de `Sc0` (Python puro)**:
   - Extrai `Sc0/param.sfo`, `Sc0/icon0.png` e `Sc0/pic1.png` diretamente dos offsets da tabela de entradas.
   - Confirma `CATEGORY='ac'`, `IRO_TAG=2`, `CONTENT_ID` idêntico ao cabeçalho PKG e `bgmhome_loud` preservado quando há música.
4. **Validação auxiliar + extração completa de `Image0` e comparação SHA-256**:
   - Executa `pkg_validate` (hash/assinaturas) e `pkg_extract --passcode <passcode>`.
   - Calcula SHA-256 de **todos** os arquivos extraídos de `Image0/` e `Sc0/` e compara byte a byte com o bundle de origem (exigindo 0 divergências para `extracted_match = True`).
5. **Regra de honestidade sobre compatibilidade física**:
   - O relatório gerado pela bancada Linux marca **sempre** `COMPATIBILIDADE FÍSICA: Pendente` (`physical_compatibility: "pending"`).
   - Somente ensaios no PS4 Slim 12.52 real (veja [`docs/acceptance-ps4.md`](acceptance-ps4.md)) comprovam a compatibilidade física.

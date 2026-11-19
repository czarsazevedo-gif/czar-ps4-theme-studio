/* Translations for the interface.
 *
 * No build step here either: the dictionary is a plain object and the markup
 * carries `data-i18n` keys. Switching language re-walks the DOM.
 *
 * Backend error messages are NOT translated: they come from the API in English
 * and are shown as-is. They are edge cases (a rejected file, a tool missing),
 * and routing every one of them through a code table would add machinery for
 * very little gain.
 */

const I18N = {
  "pt-BR": {
    "app.name": "Czar PS4 Theme Studio",
    "app.sub": "Fundos, ícones e sons para temas do PS4",
    "app.by": "Ferramenta original de",
    "lang.label": "Idioma",
    "tab.backgrounds": "Fundo",
    "tab.icons": "Ícones",
    "tab.sounds": "Áudio",
    "tab.preview": "Pré-visualização",
    "tab.package": "Pacote",
    "bg.mode.aria": "Tipo de fundo",
    "bg.static": "Imagem estática",
    "bg.static.hint": "Uma imagem para a tela inicial e outra para o menu superior",
    "bg.animated": "Fundo animado",
    "bg.animated.hint": "Um vídeo ou GIF atrás do menu",
    "bg.drop.title": "Escolha um vídeo ou GIF",
    "bg.drop.hint": "Toque para escolher um arquivo MP4, MOV, MKV, WebM, AVI ou GIF",
    "bg.play.title": "Reproduzir ou pausar",
    "bg.play.aria": "Reproduzir",
    "bg.pause.aria": "Pausar",
    "bg.perloop": "por ciclo",
    "bg.efffps": "FPS efetivo",
    "bg.perframe": "por quadro",
    "bg.resolution": "Resolução de saída",
    "bg.frames": "Quadros",
    "bg.wait": "Intervalo entre quadros",
    "bg.wait.note": "ms por quadro.",
    "bg.budget": "Tamanho em",
    "bg.encoder": "Codificador",
    "bg.export": "Exportar",
    "bg.change": "Trocar arquivo",
    "bg.slots.lead": "Os espaços vazios usam o <b>fundo da tela inicial</b>.",
    "bg.slots.raf": "A animação aparece nas duas telas. A imagem do tema fornece a miniatura, o ícone e a imagem de apresentação.",
    "bg.size.note": "{bytes} por quadro · máximo de {max} ({limit}).",
    "bg.size.byweight": "limite de tamanho",
    "bg.size.bycount": "limite de quadros",
    "bg.budget.max": "Limite atingido para {label}.",
    "bg.budget.each": "{bytes} por quadro.",
    "icons.bulkFn": "Usar uma imagem em todos os ícones de função",
    "icons.bulkCt": "Usar uma imagem em todos os ícones de conteúdo",
    "icons.clear": "Limpar todos",
    "icons.function": "Ícones de função",
    "icons.content": "Ícones de conteúdo",
    "snd.music": "Música do menu",
    "snd.pick": "Escolher áudio",
    "snd.remove": "Remover",
    "snd.none": "A tela inicial usará a música do sistema",
    "snd.none.sfx": "O sistema usará o som padrão",
    "snd.volume": "Volume",
    "snd.sfx": "Efeitos sonoros",
    "snd.sfx.when": "Ao mover, confirmar e cancelar",
    "snd.sfx.trim": "Corte em 5 segundos.",
    "snd.any": "Conversão automática dos formatos aceitos.",
    "snd.at9only": "Escolha um arquivo <code>.at9</code> pronto a 48 kHz. A conversão depende de um codificador externo compatível.",
    "snd.gain.plain": "Aplicado ao soltar o controle.",
    "snd.gain.vs": "{loud} LKFS · {compared}.",
    "snd.gain.same": "nível próximo ao de um tema de referência",
    "snd.gain.above": "{n} dB acima do tema de referência",
    "snd.gain.below": "{n} dB abaixo do tema de referência",
    "snd.converting": "Convertendo…",
    "snd.reencoding": "Recodificando…",
    "pv.lead": "Confira o contraste antes de empacotar.",
    "pv.aria": "Tela",
    "pv.home": "Tela inicial",
    "pv.home.hint": "Jogos com os ícones de função na parte superior",
    "pv.func": "Menu superior",
    "pv.func.hint": "Ícones de função ampliados",
    "pv.apply": "Seleção de tema",
    "pv.apply.hint": "Miniatura e cor do sistema",
    "pv.empty": "Importe um fundo para visualizar o tema",
    "pv.applyBtn": "Aplicar",
    "pv.settings": "Configurações",
    "pv.note.empty": "Importe a imagem do tema na aba Fundo.",
    "pv.note.apply": "O painel esquerdo usa texture/preview.png. O direito usa a cor do sistema.",
    "pv.note.raf": "A cena aparece nas duas telas. ",
    "pv.note.icons": "{own} de {total} ícones de função personalizados.",
    "pv.settingsRows": ["Guia do usuário","Gerenciamento de dados","Acessibilidade","Gerenciamento da conta","Controle parental","Configurações de login","Rede","Notificações"],
    "pv.tiledesc": "Veja como o nome e a descrição do item aparecem sobre o fundo.",
    "pkg.missing": "Empacotador indisponível neste servidor. A edição e a exportação RAF continuam disponíveis.",
    "pkg.assets": "Recursos do tema",
    "pkg.assets.note": "Os recursos são gerados ao empacotar.",
    "pkg.contents": "Conteúdo do pacote",
    "pkg.contents.note": "Caminhos esperados pelo console.",
    "pkg.title": "Título",
    "pkg.titleId": "ID do título",
    "pkg.titleId.note": "Quatro letras e cinco dígitos.",
    "pkg.contentId": "ID do conteúdo",
    "pkg.contentId.note": "Gerado a partir do título e do ID.",
    "pkg.color": "Cor do sistema",
    "pkg.color.note": "Obrigatória no <code>theme.xml</code>.",
    "pkg.textColors": "Cores do texto",
    "pkg.text": "Texto",
    "pkg.shadow": "Sombra",
    "pkg.focus": "Foco",
    "pkg.format": "Formato",
    "pkg.passcode": "Chave do pacote",
    "pkg.passcode.value": "32 zeros",
    "pkg.fixed.note": "A chave usada para criptografar o pacote deve corresponder à esperada pelo instalador.",
    "pkg.build": "Gerar .pkg",
    "pkg.hint.noTools": "Empacotador indisponível.",
    "pkg.hint.ready": "O download começa quando o pacote estiver pronto.",
    "pkg.hint.needHome": "Falta a imagem do tema.",
    "chk.homeBg": "Fundo da tela inicial",
    "chk.themeImg": "Imagem do tema",
    "chk.required": "Obrigatório",
    "chk.needed": "Necessário",
    "chk.otherBg": "Outros fundos",
    "chk.own": "{n} personalizados",
    "chk.copyHome": "Usar o fundo da tela inicial",
    "chk.fnIcons": "Ícones de função",
    "chk.ctIcons": "Ícones de conteúdo",
    "chk.default": "Padrão",
    "chk.sfx": "Efeitos sonoros",
    "chk.systemOnes": "Sons do sistema",
    "chk.music": "Música do menu",
    "chk.systemMusic": "Música do sistema",
    "chk.animated": "Fundo animado",
    "chk.optional": "Opcional",
    "chk.loadedNotUsed": "Carregado enquanto o fundo estático está selecionado",
    "chk.nOf": "{n} de {total}",
    "slot.required": "Obrigatório",
    "slot.optional": "Opcional",
    "slot.copyOf": "Cópia de {from}",
    "slot.copyTheme": "Usa a imagem do tema",
    "slot.empty": "Sem imagem",
    "slot.remove": "Remover",
    "slot.coveredByRaf": "Coberto pelo fundo animado",
    "slot.themePurpose": "Miniatura · ícone · apresentação",
    "misc.session": "{n} arquivos recuperados da sessão anterior.",
    "misc.sessionExpired": " {n} arquivos já expiraram.",
    "misc.appliedTo": "Imagem aplicada a {n} ícones.",
    "misc.myTheme": "Meu tema",
    "bg.result.ok": "Concluído · {n} quadros em {s}s",
    "bg.src.meta": "{w}×{h} · {d}s · {codec} · extração a {fps} FPS",
    "bg.size.opt": "{label} · até {max} quadros",
    "tree.themeXml": "Descritor do tema",
    "tree.fnIcons": "Ícones de função",
    "tree.ctIcons": "Ícones de conteúdo",
    "tree.animated": "Fundo animado",
    "tree.animated.note": "Quadros, plano e cena",
    "pkg.result.ok": "Concluído · {size} em {s}s",
    "pkg.download": "Baixar .pkg",
  },
  en: {
    "app.name": "PS4 Ultimate Theme Creator",
    "app.sub": "Backgrounds, icons and sounds — with .pkg packaging",
    "app.by": "Tool by",
    "lang.label": "Language",

    "tab.backgrounds": "Background",
    "tab.icons": "Icons",
    "tab.sounds": "Sounds",
    "tab.preview": "Preview",
    "tab.package": "Package",

    // --- background ---
    "bg.mode.aria": "Background type",
    "bg.static": "Still image",
    "bg.static.hint": "one image for the home screen and another for the top menu",
    "bg.animated": "Animated background",
    "bg.animated.hint": "a video or gif moving behind the menu",
    "bg.drop.title": "Drop a video or GIF here",
    "bg.drop.hint": "or click to pick one — mp4, mov, mkv, webm, avi, gif",
    "bg.play.title": "Play / pause",
    "bg.play.aria": "Play",
    "bg.pause.aria": "Pause",
    "bg.perloop": "per loop",
    "bg.efffps": "effective fps",
    "bg.perframe": "per frame",
    "bg.resolution": "Resolution",
    "bg.frames": "Frames",
    "bg.wait": "Wait per frame",
    "bg.wait.note": "ms per frame.",
    "bg.budget": "Size in",
    "bg.encoder": "Encoder",
    "bg.export": "Export",
    "bg.change": "Change file",
    "bg.slots.lead": "Empty slots copy from the <b>home background</b>.",
    "bg.slots.raf":
      "The scene is drawn on <b>both</b> screens, so the still backgrounds are "
      + "not used. The theme image is still needed — the thumbnail, the icon "
      + "and the store shot come from it.",
    "bg.size.note": "{bytes} per frame · max {max} ({limit}).",
    "bg.size.byweight": "weight decides",
    "bg.size.bycount": "frame count decides",
    "bg.budget.max": "At the maximum for {label}.",
    "bg.budget.each": "{bytes} per frame.",

    // --- icons ---
    "icons.bulkFn": "Apply one image to every function icon",
    "icons.bulkCt": "Apply one image to every content icon",
    "icons.clear": "Clear all",
    "icons.function": "Function icons",
    "icons.content": "Content icons",

    // --- sounds ---
    "snd.music": "Menu music",
    "snd.pick": "Choose audio",
    "snd.remove": "Remove",
    "snd.none": "no music — the home screen will use the system one",
    "snd.none.sfx": "no sound — the system one will be used",
    "snd.volume": "Volume",
    "snd.sfx": "Sound effects",
    "snd.sfx.when": "on move, confirm and cancel",
    "snd.sfx.trim": "Trimmed to 5 s.",
    "snd.any": "Any format: converted automatically.",
    "snd.at9only":
      "Only <code>.at9</code> at 48 kHz. With <code>at9tool.exe</code> in "
      + "<code>tools/</code> any format would be converted.",
    "snd.gain.plain": "Applied to the file on release.",
    "snd.gain.vs": "{loud} LKFS — {compared}.",
    "snd.gain.same": "about the same as a real theme",
    "snd.gain.above": "{n} dB above a real theme",
    "snd.gain.below": "{n} dB below a real theme",
    "snd.converting": "converting…",
    "snd.reencoding": "re-encoding…",

    // --- preview ---
    "pv.lead": "Approximate, to check contrast before packaging.",
    "pv.aria": "Screen",
    "pv.home": "Home screen",
    "pv.home.hint": "the game row, with the function icons on top",
    "pv.func": "Top menu",
    "pv.func.hint": "pressing up: the function icons enlarged",
    "pv.apply": "Apply theme",
    "pv.apply.hint": "where the thumbnail and the system colour show up",
    "pv.empty": "Upload the background to see the preview",
    "pv.applyBtn": "Apply",
    "pv.settings": "Settings",
    "pv.note.empty": "Upload the theme image in the Background tab.",
    "pv.note.apply":
      "The left panel is texture/preview.png; the right one, the system colour.",
    "pv.note.raf": "The scene is drawn on both screens. ",
    "pv.note.icons": "{own} of {total} function icons are yours.",
    "pv.settingsRows": [
      "User's Guide", "Data Management", "Accessibility", "Account Management",
      "Parental Controls", "Login Settings", "Network", "Notifications",
    ],
    "pv.tiledesc":
      "This is how the selected item's name and description sit on your background.",

    // --- package ---
    "pkg.missing":
      "The Fake PKG Tools were not found in the project folder, so packaging "
      + "is unavailable. Everything else still works.",
    "pkg.assets": "Theme assets",
    "pkg.assets.note": "Regenerated when packaging: no need to export anything first.",
    "pkg.contents": "Package contents",
    "pkg.contents.note": "The names the console looks for.",
    "pkg.title": "Title",
    "pkg.titleId": "Title ID",
    "pkg.titleId.note": "4 letters and 5 digits.",
    "pkg.contentId": "Content ID",
    "pkg.contentId.note": "Filled in from the title and the Title ID.",
    "pkg.color": "System colour",
    "pkg.color.note": "Required in the <code>theme.xml</code>.",
    "pkg.textColors": "Text colours",
    "pkg.text": "text",
    "pkg.shadow": "shadow",
    "pkg.focus": "focus",
    "pkg.format": "Format",
    "pkg.passcode": "Passcode",
    "pkg.passcode.value": "32 zeros",
    "pkg.fixed.note":
      "Fixed: this is what the console accepts as a theme and what the "
      + "installer expects. The passcode encrypts the package and is <b>not "
      + "stored inside it</b>, so a different one could not be installed.",
    "pkg.build": "Build .pkg",
    "pkg.hint.noTools": "The Fake PKG Tools are not available.",
    "pkg.hint.ready": "The .pkg downloads when it finishes.",
    "pkg.hint.needHome": "The theme image is missing.",

    // --- checklist ---
    "chk.homeBg": "Home background",
    "chk.themeImg": "Theme image",
    "chk.required": "required",
    "chk.needed": "needed",
    "chk.otherBg": "Other backgrounds",
    "chk.own": "{n} custom",
    "chk.copyHome": "copy from the home one",
    "chk.fnIcons": "Function icons",
    "chk.ctIcons": "Content icons",
    "chk.default": "default",
    "chk.sfx": "Sound effects",
    "chk.systemOnes": "the system ones",
    "chk.music": "Menu music",
    "chk.systemMusic": "the system one",
    "chk.animated": "Animated background",
    "chk.optional": "optional",
    "chk.loadedNotUsed": "loaded, but the mode is still image",
    "chk.nOf": "{n} of {total}",

    // --- slots ---
    "slot.required": "required",
    "slot.optional": "optional",
    "slot.copyOf": "copies {from}",
    "slot.copyTheme": "copies the theme image",
    "slot.empty": "no image",
    "slot.remove": "Remove",
    "slot.coveredByRaf": "hidden by the animated background",
    "slot.themePurpose": "thumbnail · icon · store shot",

    // --- misc ---
    "misc.session": "Recovered {n} files from the previous session.",
    "misc.sessionExpired": " {n} had already expired.",
    "misc.appliedTo": "Applied to {n} icons.",
    "misc.myTheme": "My Theme",
    "bg.result.ok": "Done — {n} frames in {s}s",
    "bg.src.meta": "{w}×{h} · {d}s · {codec} · extracted at {fps} fps",
    "bg.size.opt": "{label} — up to {max} frames",
    "tree.themeXml": "theme descriptor",
    "tree.fnIcons": "function icons",
    "tree.ctIcons": "content icons",
    "tree.animated": "animated background",
    "tree.animated.note": "frames + plane + scene",
    "pkg.result.ok": "Done — {size} in {s}s",
    "pkg.download": "Download .pkg",
  },

  es: {
    "app.name": "PS4 Ultimate Theme Creator",
    "app.sub": "Fondos, iconos y sonidos — con empaquetado a .pkg",
    "app.by": "Herramienta de",
    "lang.label": "Idioma",

    "tab.backgrounds": "Fondo",
    "tab.icons": "Iconos",
    "tab.sounds": "Sonidos",
    "tab.preview": "Vista previa",
    "tab.package": "Empaquetar",

    "bg.mode.aria": "Tipo de fondo",
    "bg.static": "Imagen fija",
    "bg.static.hint": "una imagen para la home y otra para el menú superior",
    "bg.animated": "Fondo animado",
    "bg.animated.hint": "un vídeo o gif que se mueve detrás del menú",
    "bg.drop.title": "Arrastra un vídeo o GIF aquí",
    "bg.drop.hint": "o haz clic para elegirlo — mp4, mov, mkv, webm, avi, gif",
    "bg.play.title": "Reproducir / pausar",
    "bg.play.aria": "Reproducir",
    "bg.pause.aria": "Pausar",
    "bg.perloop": "por vuelta",
    "bg.efffps": "fps efectivos",
    "bg.perframe": "por frame",
    "bg.resolution": "Resolución",
    "bg.frames": "Frames",
    "bg.wait": "Wait por frame",
    "bg.wait.note": "ms por frame.",
    "bg.budget": "Tamaño en",
    "bg.encoder": "Encoder",
    "bg.export": "Exportar",
    "bg.change": "Cambiar archivo",
    "bg.slots.lead": "Los huecos vacíos copian del <b>fondo de la home</b>.",
    "bg.slots.raf":
      "La escena se pinta en <b>las dos</b> pantallas: los fondos fijos no se "
      + "usan. La imagen del tema sí hace falta — de ella salen la miniatura, "
      + "el icono y la ficha.",
    "bg.size.note": "{bytes} por frame · máximo {max} (manda {limit}).",
    "bg.size.byweight": "el peso",
    "bg.size.bycount": "el número de frames",
    "bg.budget.max": "Al máximo a {label}.",
    "bg.budget.each": "{bytes} por frame.",

    "icons.bulkFn": "Aplicar una imagen a todos los iconos de función",
    "icons.bulkCt": "Aplicar una imagen a todos los iconos de contenido",
    "icons.clear": "Vaciar todos",
    "icons.function": "Iconos de función",
    "icons.content": "Iconos de contenido",

    "snd.music": "Música del menú",
    "snd.pick": "Elegir audio",
    "snd.remove": "Quitar",
    "snd.none": "sin música — la home usará la del sistema",
    "snd.none.sfx": "sin sonido — se usará el del sistema",
    "snd.volume": "Volumen",
    "snd.sfx": "Efectos de sonido",
    "snd.sfx.when": "al moverte, aceptar y cancelar",
    "snd.sfx.trim": "Se recortan a 5 s.",
    "snd.any": "Cualquier formato: se convierte solo.",
    "snd.at9only":
      "Solo <code>.at9</code> a 48 kHz. Con <code>at9tool.exe</code> en "
      + "<code>tools/</code> se convertiría cualquier formato.",
    "snd.gain.plain": "Se aplica al archivo al soltar.",
    "snd.gain.vs": "{loud} LKFS — {compared}.",
    "snd.gain.same": "más o menos como un tema real",
    "snd.gain.above": "{n} dB por encima de un tema real",
    "snd.gain.below": "{n} dB por debajo de un tema real",
    "snd.converting": "convirtiendo…",
    "snd.reencoding": "recodificando…",

    "pv.lead": "Aproximado, para ver el contraste antes de empaquetar.",
    "pv.aria": "Pantalla",
    "pv.home": "Pantalla principal",
    "pv.home.hint": "la fila de juegos, con los iconos de función arriba",
    "pv.func": "Menú superior",
    "pv.func.hint": "al pulsar arriba: los iconos de función en grande",
    "pv.apply": "Aplicar tema",
    "pv.apply.hint": "donde se ve la miniatura y el color del sistema",
    "pv.empty": "Sube el fondo para ver la vista previa",
    "pv.applyBtn": "Aplicar",
    "pv.settings": "Ajustes",
    "pv.note.empty": "Sube la imagen del tema en la pestaña Fondo.",
    "pv.note.apply":
      "El panel izquierdo es texture/preview.png; el derecho, el color del sistema.",
    "pv.note.raf": "La escena se pinta en las dos pantallas. ",
    "pv.note.icons": "{own} de {total} iconos de función son tuyos.",
    "pv.settingsRows": [
      "Guía del usuario", "Gestión de datos", "Accesibilidad", "Gestión de cuenta",
      "Control parental", "Inicio de sesión", "Red", "Notificaciones",
    ],
    "pv.tiledesc":
      "Así se ve el nombre y la descripción del elemento seleccionado sobre tu fondo.",

    "pkg.missing":
      "No se encontraron las Fake PKG Tools en la carpeta del proyecto, así que "
      + "no se puede empaquetar. El resto de la app funciona igual.",
    "pkg.assets": "Assets del tema",
    "pkg.assets.note": "Se regeneran al empaquetar: no exportes nada antes.",
    "pkg.contents": "Contenido del pkg",
    "pkg.contents.note": "Los nombres que busca la consola.",
    "pkg.title": "Título",
    "pkg.titleId": "Title ID",
    "pkg.titleId.note": "4 letras y 5 dígitos.",
    "pkg.contentId": "Content ID",
    "pkg.contentId.note": "Se rellena solo con el título y el Title ID.",
    "pkg.color": "Color del sistema",
    "pkg.color.note": "Obligatorio en el <code>theme.xml</code>.",
    "pkg.textColors": "Colores del texto",
    "pkg.text": "texto",
    "pkg.shadow": "sombra",
    "pkg.focus": "foco",
    "pkg.format": "Formato",
    "pkg.passcode": "Passcode",
    "pkg.passcode.value": "32 ceros",
    "pkg.fixed.note":
      "Fijos: es lo que la consola acepta como tema y lo que espera el "
      + "instalador. El passcode cifra el pkg y <b>no va dentro</b>, así que "
      + "con otro no se podría instalar.",
    "pkg.build": "Empaquetar .pkg",
    "pkg.hint.noTools": "Las Fake PKG Tools no están disponibles.",
    "pkg.hint.ready": "El .pkg se descarga al terminar.",
    "pkg.hint.needHome": "Falta la imagen del tema.",

    "chk.homeBg": "Fondo de la home",
    "chk.themeImg": "Imagen del tema",
    "chk.required": "obligatorio",
    "chk.needed": "hace falta",
    "chk.otherBg": "Otros fondos",
    "chk.own": "{n} propios",
    "chk.copyHome": "copian de la home",
    "chk.fnIcons": "Iconos de función",
    "chk.ctIcons": "Iconos de contenido",
    "chk.default": "por defecto",
    "chk.sfx": "Efectos de sonido",
    "chk.systemOnes": "los del sistema",
    "chk.music": "Música del menú",
    "chk.systemMusic": "la del sistema",
    "chk.animated": "Fondo animado",
    "chk.optional": "opcional",
    "chk.loadedNotUsed": "cargado, pero el modo es imagen fija",
    "chk.nOf": "{n} de {total}",

    "slot.required": "obligatorio",
    "slot.optional": "opcional",
    "slot.copyOf": "copia de {from}",
    "slot.copyTheme": "copia del tema",
    "slot.empty": "sin imagen",
    "slot.remove": "Quitar",
    "slot.coveredByRaf": "lo tapa el fondo animado",
    "slot.themePurpose": "miniatura · icono · ficha",

    "misc.session": "Se recuperaron {n} archivos de la sesión anterior.",
    "misc.sessionExpired": " {n} ya habían caducado.",
    "misc.appliedTo": "Aplicado a {n} iconos.",
    "misc.myTheme": "Mi Tema",
    "bg.result.ok": "Listo — {n} frames en {s}s",
    "bg.src.meta": "{w}×{h} · {d}s · {codec} · extraído a {fps} fps",
    "bg.size.opt": "{label} — hasta {max} frames",
    "tree.themeXml": "descriptor del tema",
    "tree.fnIcons": "iconos de función",
    "tree.ctIcons": "iconos de contenido",
    "tree.animated": "fondo animado",
    "tree.animated.note": "frames + plano + escena",
    "pkg.result.ok": "Listo — {size} en {s}s",
    "pkg.download": "Descargar .pkg",
  },
};

const LANG_KEY = "ps4theme.lang";

/** Active language. Browser first, then whatever was chosen before. */
let LANG = (() => {
  const saved = localStorage.getItem(LANG_KEY);
  if (saved && I18N[saved]) return saved;
  return "pt-BR";
})();

const lang = () => LANG;

/** Looks up a key and fills in {placeholders}. Falls back to English, and to
 *  the key itself, so a missing string is visible instead of blank. */
function t(key, vars) {
  const s = I18N[LANG]?.[key] ?? I18N.en[key] ?? key;
  if (!vars || typeof s !== "string") return s;
  return s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? vars[k] : m));
}

/** Rewrites every marked node. `data-i18n` sets text, `data-i18n-html` allows
 *  the few strings that carry <b> or <code>, and `data-i18n-attr` handles
 *  title/aria-label/placeholder as "attr:key" pairs. */
function applyI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-html]").forEach((el) => {
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    el.dataset.i18nAttr.split(",").forEach((par) => {
      const [attr, key] = par.split(":").map((x) => x.trim());
      if (attr && key) el.setAttribute(attr, t(key));
    });
  });
  document.documentElement.lang = LANG;
  document.title = t("app.name");
}

function setLang(code) {
  if (!I18N[code]) return;
  LANG = code;
  localStorage.setItem(LANG_KEY, code);
  applyI18n();
  document.dispatchEvent(new CustomEvent("langchange"));
}

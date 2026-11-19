const dictionaries = {
  "pt-BR": {
    panels: { project: "Projeto", background: "Fundo", animation: "Animação", layers: "Camadas",
      icons: "Ícones", audio: "Áudio", colors: "Cores", preview: "Preview", validate: "Validar", export: "Exportar" },
    slots: { home: "Fundo inicial", function: "Fundo do menu superior", preview: "Miniatura do tema", icon0: "Ícone do tema",
      community: "Comunidades", event: "Eventos", friend: "Amigos", message: "Mensagens", notification: "Notificações",
      party: "Grupo", power: "Energia", setting: "Configurações", trophy: "Troféus", browser: "Navegador", disc: "Disco",
      discoverlay: "Sobreposição de disco", folder: "Pasta", gallery: "Galeria", library: "Biblioteca",
      livefromps: "Live from PlayStation", shareplay: "Share Play", tvvideo: "TV e vídeo", usbmusic: "Música USB",
      bgm: "Música do menu", se_decide: "Confirmar", se_cursor: "Cursor", se_cancel: "Cancelar" },
    colors: ["Azul", "Rosa", "Vermelho", "Azul-marinho", "Cinza", "Dourado", "Azul-aço", "Roxo"],
    presets: ["Qualidade", "Equilibrado", "Movimento"],
    statuses: { valid: "Válido", warning: "Aviso", error: "Erro", unverified: "Não verificável" }
  }
};
export const locale = "pt-BR";
export const words = dictionaries[locale];
export const dateLabel = (value) => new Date(value).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" });
export const number = (value, digits = 2) => new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(value);

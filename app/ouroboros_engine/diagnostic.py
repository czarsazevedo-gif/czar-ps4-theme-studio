"""Intelligent post-import media diagnostic builder (Pilar II — O Guia do Loop Perfeito).

Transforms real Sentinela Supreme + Círculo Supreme measurements into an honest,
evidence-backed diagnostic covering all 21 required fields and 5 official aptitude categories.
Never fabricates favorable verdicts for static, reversed, or unanalyzed media.
"""

from __future__ import annotations

import math

from .analyzer import (
    CLASS_CORRECTABLE,
    CLASS_NATURAL,
    CLASS_REGIONAL,
    CLASS_SYNTHESIS,
    CLASS_UNSUITABLE,
)

# Official 5 aptitude categories (Section 11)
APT_NATURAL = "FAVORÁVEL A LOOP NATURAL"
APT_CORRECTABLE = "CORRIGÍVEL"
APT_SYNTHESIS = "ADEQUADO À SÍNTESE"
APT_CONDITIONAL = "DIFÍCIL / RESULTADO CONDICIONAL"
APT_UNSUITABLE = "INADEQUADO AO RESULTADO DESEJADO"


def _fmt_sec(sec: float) -> str:
    return f"{sec:.2f} s"


def _direction_label(vec: list[float], speed: float, coh: float) -> str:
    if speed < 0.15:
        return "quase sem deslocamento espacial mensurável"
    vx, vy = float(vec[0]), float(vec[1])
    if coh < 0.35:
        return "multidirecional / turbulento"
    angle = math.degrees(math.atan2(vy, vx))
    if -22.5 <= angle < 22.5:
        return "horizontal para a direita"
    if 22.5 <= angle < 67.5:
        return "diagonal para baixo-direita"
    if 67.5 <= angle < 112.5:
        return "vertical para baixo"
    if 112.5 <= angle < 157.5:
        return "diagonal para baixo-esquerda"
    if angle >= 157.5 or angle < -157.5:
        return "horizontal para a esquerda"
    if -157.5 <= angle < -112.5:
        return "diagonal para cima-esquerda"
    if -112.5 <= angle < -67.5:
        return "vertical para cima"
    return "diagonal para cima-direita"


def _pick_primary_take(analysis: dict) -> dict | None:
    cats = analysis.get("categories") or {}
    for key in ("best_natural", "best_balance", "best_correctable", "best_convincing_duration", "best_synthesized"):
        if cats.get(key):
            return cats[key]
    for grp in analysis.get("groups") or []:
        if grp.get("candidates"):
            return grp["candidates"][0]
    return None


def build_media_diagnostic(analysis: dict, *, filename: str = "") -> dict:
    """Build the full 21-field evidence-backed media diagnostic from a real `search_supreme()` result."""
    sent = analysis.get("sentinela") or {}
    src = sent.get("source") or {}
    la = sent.get("levelA") or {}
    lb = sent.get("levelB") or {}
    lc = sent.get("levelC") or {}
    ld = sent.get("levelD") or {}
    lq = sent.get("levelQuality") or {}
    cam = lc.get("cameraMotion") or {}

    name = filename or analysis.get("name") or "vídeo importado"
    dur = float(src.get("duration") or la.get("durationSec") or 0.0)
    w = int(src.get("width") or lq.get("sourceWidth") or 0)
    h = int(src.get("height") or lq.get("sourceHeight") or 0)
    codec = str(src.get("codec") or "desconhecido")
    fps_nom = float(src.get("fps") or 0.0)
    fps_meas = float(src.get("sampleFps") or fps_nom or 0.0)
    is_vfr = bool(src.get("isVFR"))
    dup_ts = int(src.get("duplicateTimestamps") or 0)
    drop_ts = int(src.get("droppedTimestamps") or 0)

    cuts = la.get("cuts") or []
    freezes = la.get("freezes") or []
    med_mae = float(la.get("medianMae") or 0.0)
    mean_spd = float(lc.get("meanSpeedPx") or la.get("meanSpeedPx") or 0.0)
    sp_coh = float(lc.get("spatialCoherence") or 0.0)
    tmp_cons = float(lc.get("temporalConsistency") or 0.0)
    occ_mean = float(lc.get("occlusionMeanFraction") or 0.0)
    dom_vec = lc.get("dominantVec") or [0.0, 0.0]

    static_frac = float(lb.get("staticFraction") or 0.0)
    prog_frac = float(lb.get("progressiveFraction") or 0.0)
    osc_frac = float(lb.get("oscillatoryFraction") or 0.0)
    trans_frac = float(lb.get("transientFraction") or 0.0)
    mixed_sd = bool(lb.get("hasMixedStaticDynamic"))
    multi_spd = bool(lb.get("hasIndependentSpeeds"))

    cam_moving = bool(cam.get("moving") or la.get("cameraMoving"))
    cam_class = str(cam.get("cameraClass") or la.get("cameraClass") or ("panoramica_ou_travelling" if cam_moving else "estavel"))
    light_stab = str(la.get("lightingStability") or ("flicker_artificial" if ld.get("artificialFlicker") else "consistente"))

    true_rec = bool(ld.get("trueRecurrence"))
    period_sec = ld.get("periodSeconds")
    ping_pong = bool(ld.get("reverseMotion"))
    spatial_rep = ld.get("spatialRepetition") or {}
    lum_pulse = bool(ld.get("luminousPulsation"))
    art_flicker = bool(ld.get("artificialFlicker"))

    sharp_lbl = str(lq.get("sharpnessLabel") or "adequada")
    noise_lbl = str(lq.get("noiseLabel") or "baixo")
    comp_lbl = str(lq.get("compressionLabel") or "limpa")
    crop_needed = bool(lq.get("cropNeeded"))
    portrait_src = bool(lq.get("portraitSource"))
    motion_blur_risk = bool(lq.get("motionBlurRisk"))

    best_take = _pick_primary_take(analysis)
    take_cls = best_take["costs"]["classification"] if best_take else CLASS_UNSUITABLE
    take_cost = float(best_take["costs"]["totalCost"]) if best_take else 99.0
    take_em = (best_take.get("evaluation") or {}).get("metrics") if best_take else {}
    take_fc = take_em.get("flowContinuity") or {}
    take_vs = take_em.get("visualSimilarity") or {}
    take_static = bool(best_take["costs"].get("isStatic")) if best_take else (med_mae < 0.25 and mean_spd < 0.12)

    sent_cls = str(sent.get("classification") or CLASS_UNSUITABLE)
    conf_val = float(sent.get("confidence") or 0.0)
    conf_label = str(sent.get("confidenceLabel") or ("forte" if conf_val >= 0.72 else ("moderada" if conf_val >= 0.45 else "limitada")))
    inconclusive = conf_val < 0.45 or (best_take is None)

    # --- Classify into the exact 5 official aptitude categories (Section 11) ---
    is_static_scene = bool(
        take_static
        or (static_frac > 0.94 and med_mae < 0.35 and mean_spd < 0.15 and float(la.get("luminanceRangeL") or 0.0) < 1.5)
    )
    if is_static_scene:
        aptidao = APT_UNSUITABLE
        resumo = "O vídeo apresenta movimento desprezível ou cena quase estática. Ausência de movimento não caracteriza um bom loop animado."
    elif (
        (sent_cls == CLASS_NATURAL or take_cls == CLASS_NATURAL)
        and not cuts
        and not ping_pong
        and not cam_moving
        and not art_flicker
        and (not take_fc.get("directionMeasured") or float(take_fc.get("seamDirectionCosine", 1.0)) >= 0.45)
    ):
        aptidao = APT_NATURAL
        resumo = "Seu vídeo possui boas características para looping contínuo com recorrência compatível com o orçamento RAF do PS4."
    elif (
        (sent_cls in {CLASS_SYNTHESIS, CLASS_REGIONAL} or spatial_rep.get("detected"))
        and not ping_pong
        and len(cuts) <= 1
        and not cam_moving
        and (prog_frac >= 0.15 or spatial_rep.get("detected") or mixed_sd)
    ):
        aptidao = APT_SYNTHESIS
        resumo = "Seu vídeo possui movimento contínuo ou regiões distintas adequadas à síntese periódica / reconstrução regional sem movimento reverso."
    elif (
        sent_cls == CLASS_CORRECTABLE
        or (best_take is not None and take_cls == CLASS_CORRECTABLE and not ping_pong and len(cuts) <= 1)
    ) and not (cam_moving and float(la.get("cameraMedianDispPx") or 0.0) >= 4.0):
        aptidao = APT_CORRECTABLE
        resumo = "O material apresenta descontinuidades tratáveis na junção, mas oferece base viável para refinamento local ou ponte de transição."
    elif best_take is not None and not is_static_scene and (cam_moving or trans_frac >= 0.08 or len(cuts) >= 1 or osc_frac >= 0.20 or take_cost < 24.0):
        aptidao = APT_CONDITIONAL
        resumo = "Seu vídeo apresenta dificuldades importantes para looping imperceptível; o resultado dependerá de recorte, reconstrução local ou decisão artística."
    else:
        aptidao = APT_UNSUITABLE
        resumo = "Trajetória sem periodicidade convincente ou com rupturas severas; o resultado em loop tende a apresentar reinício perceptível."

    # --- Evidence-backed lists (strictly derived from real measurements!) ---
    pontos_fortes: list[str] = []
    pontos_fracos: list[str] = []
    o_que_falta: list[str] = []
    corrigivel: list[str] = []
    nao_garantido: list[str] = []

    if not cam_moving and cam_class == "estavel":
        pontos_fortes.append("Câmera estável (sem panorâmica ou deslocamento global relevante).")
    elif cam_class == "tremor_leve":
        pontos_fracos.append("Pequeno tremor de câmera detectado; pode exigir estabilização.")
    else:
        pontos_fracos.append(f"Movimento global de câmera detectado ({cam_class.replace('_', ' ')}).")

    if light_stab == "consistente":
        pontos_fortes.append("Iluminação e exposição consistentes ao longo da gravação.")
    elif light_stab == "variacao_suave":
        pontos_fracos.append("Variação gradual de luminosidade/exposição ao longo do vídeo.")
    elif light_stab == "flicker_artificial":
        pontos_fracos.append("Oscilação rápida de brilho (flicker artificial) detectada.")
    else:
        pontos_fracos.append("Mudança brusca de iluminação detectada.")

    if w >= 1900 and h >= 1060:
        pontos_fortes.append(f"Boa resolução de origem ({w}×{h}), favorável à redução limpa para 720p/540p/360p.")
    elif w >= 1260 and h >= 700:
        pontos_fortes.append(f"Resolução HD ({w}×{h}) adequada aos perfis canônicos do PS4.")
    elif w > 0:
        pontos_fracos.append(f"Resolução de origem inferior a 720p ({w}×{h}); ampliação pode suavizar detalhes.")

    if not crop_needed:
        pontos_fortes.append("Proporção compatível com 16:9 do PS4 (sem necessidade de recorte severo).")
    elif portrait_src:
        pontos_fracos.append(f"Vídeo vertical ({w}×{h}): exigirá recorte central para enquadrar em 16:9 no PS4.")
    else:
        pontos_fracos.append(f"Proporção diferente de 16:9 ({w}×{h}): haverá recorte ou re-enquadramento.")

    if sharp_lbl in {"alta", "adequada"}:
        pontos_fortes.append(f"Nitidez {sharp_lbl} e compressão {comp_lbl.replace('_', ' ')}.")
    else:
        pontos_fracos.append(f"Nitidez reduzida ({sharp_lbl})" + (" com risco de motion blur." if motion_blur_risk else "."))

    if comp_lbl == "alta_compressao":
        pontos_fracos.append("Sinais de compressão/blocos 8×8 elevados na fonte, aumentando risco de banding pós-DXT1a.")
    if noise_lbl == "alto":
        pontos_fracos.append("Ruído espacial elevado que pode amplificar ruído temporal após codificação DDS.")

    if not is_vfr and dup_ts == 0 and drop_ts == 0:
        pontos_fortes.append(f"Cadência temporal regular ({fps_meas:.2f} FPS, CFR).")
    else:
        det = []
        if is_vfr:
            det.append("taxa de quadros variável (VFR)")
        if dup_ts:
            det.append(f"{dup_ts} intervalo(s) duplicado(s)")
        if drop_ts:
            det.append(f"{drop_ts} queda(s) de quadro")
        pontos_fracos.append("Irregularidade de timestamps: " + ", ".join(det) + ".")

    if not cuts:
        pontos_fortes.append("Ausência de cortes abruptos de cena na fonte.")
    else:
        pontos_fracos.append(f"{len(cuts)} corte(s) abrupto(s) detectado(s) na gravação.")

    if freezes:
        pontos_fracos.append(f"{len(freezes)} instante(s) congelado(s) ou quase duplicado(s) detectado(s).")

    if ping_pong:
        pontos_fracos.append("Reversão estrutural / padrão ping-pong detectado na trajetória.")

    if prog_frac >= 0.15 and sp_coh >= 0.55 and not cam_moving:
        pontos_fortes.append(f"Movimento predominantemente unidirecional ({_direction_label(dom_vec, mean_spd, sp_coh)}) com boa coerência ({sp_coh:.2f}).")
    if mixed_sd:
        pontos_fortes.append(f"Separação clara entre área estática ({static_frac * 100:.0f}%) e dinâmica — favorece proteção regional.")
    if true_rec and period_sec:
        pontos_fortes.append(f"Recorrência natural detectada com período aproximado de {period_sec:.2f} s.")
    elif spatial_rep.get("detected"):
        pontos_fortes.append(f"Textura com repetição espacial coerente no eixo {spatial_rep.get('axis')}.")
    elif not is_static_scene:
        pontos_fracos.append("Ausência de periodicidade natural exata entre início e fim.")

    if trans_frac >= 0.03:
        pontos_fracos.append(f"Atividade transitória localizada ({trans_frac * 100:.1f}% do quadro) — possível objeto atravessando a cena.")
    if occ_mean > 0.22:
        pontos_fracos.append(f"Taxa elevada de oclusões no fluxo óptico ({occ_mean * 100:.0f}%).")
    if dur < 3.0:
        pontos_fracos.append(f"Duração curta ({dur:.2f} s) limita a variedade de trechos candidatos.")
    elif 10.0 <= dur <= 45.0:
        pontos_fortes.append(f"Boa duração ({dur:.2f} s) para comparar múltiplos trechos candidatos.")

    if not pontos_fortes:
        pontos_fortes.append("Arquivo decodificável e válido para inspeção no Studio.")

    # --- O que falta ---
    if is_static_scene:
        o_que_falta.append("Movimento contínuo real (água, nuvens, partículas, luz ou textura viva) para justificar um fundo animado.")
    if not true_rec and not spatial_rep.get("detected") and not is_static_scene:
        o_que_falta.append("Um intervalo com maior correspondência de aparência e direção de movimento entre o último e o primeiro quadro.")
    if cam_moving:
        o_que_falta.append("Câmera fixa ou estabilizada para evitar deslocamento global do enquadramento no fechamento do ciclo.")
    if trans_frac >= 0.03:
        o_que_falta.append("Trecho livre de objetos únicos atravessando a cena ou máscara regional para remoção/proteção.")
    if cuts:
        o_que_falta.append("Seleção restrita a um segmento contínuo sem cortes internos.")
    if dur < 4.0:
        o_que_falta.append("Mais segundos de gravação contínua (como referência inicial, 15–30 s costumam oferecer mais ciclos e trechos limpos).")
    if not o_que_falta:
        o_que_falta.append("Validação visual final dos quadros decodificados em DDS por pelo menos 10 ciclos contínuos.")

    # --- O que pode ser corrigido no Studio ---
    if not is_static_scene:
        if light_stab in {"variacao_suave", "mudanca_brusca"}:
            corrigivel.append("Harmonização fotométrica circular em CIE Lab (elimina degrau de brilho/cor na costura).")
        if art_flicker:
            corrigivel.append("Supressão seletiva de flicker no Ouroboros Deluxe Finish.")
        if cam_moving:
            corrigivel.append("Estabilização de câmera afim por RANSAC (quando o deslocamento for moderado).")
        if trans_frac >= 0.03 or mixed_sd:
            corrigivel.append("Proteção estática alinhada a blocos DXT1 4×4 e reconstrução/remoção regional.")
        if prog_frac >= 0.15 or spatial_rep.get("detected"):
            corrigivel.append("Síntese periódica 2D (Gênesis Supreme) ou ponte de transição direcional alinhada ao fluxo.")
        else:
            corrigivel.append("Comparação multicritério de trechos naturais e ponte de transição direcional (Level 4).")
        corrigivel.append("Dither ordenado estático pós-DXT1a caso surja banding em gradientes suaves.")
    else:
        corrigivel.append("Uso como fundo estático 1920×1080 (background.dds) em vez de cena animada RAF.")

    # --- O que não pode ser garantido ---
    nao_garantido.append("Fechamento invisível de movimentos únicos sem repetição (ex.: veículos, personagens ou objetos isolados cruzando a tela).")
    if cam_moving:
        nao_garantido.append("Compensação sem artefatos de paralaxe 3D, zoom forte ou panorâmica contínua longa.")
    if comp_lbl == "alta_compressao" or w < 1280:
        nao_garantido.append("Recuperação de detalhes inexistentes na gravação original após compressão DXT1a.")
    nao_garantido.append("Aprovação final estética e compatibilidade física no PS4 Slim 12.52 antes do teste real no console.")

    # --- Best take recommendation ---
    if best_take is not None and not is_static_scene:
        bt_sz = best_take["size"]
        bt_frames = int(best_take["frames"])
        bt_wait_ms = round(float(best_take["wait"]) * 1000.0, 1)
        bt_fps = round(float(best_take["fps"]), 1)
        bt_dur = float(best_take["duration"])
        bt_mb = float(best_take["estimatedBytes"]) / (1024.0 * 1024.0)
        melhor_trecho = f"{_fmt_sec(best_take['start'])} → {_fmt_sec(best_take['end'])} (duração útil {bt_dur:.2f} s)"
        melhor_resolucao = f"{bt_sz[0]}×{bt_sz[1]} · {bt_frames} quadros ({bt_mb:.2f} MiB / 6,00 MiB RAF)"
        melhor_cadencia = f"{bt_wait_ms:.1f} ms/quadro ({bt_fps:.1f} FPS efetivos)"

        if aptidao == APT_NATURAL:
            melhor_estrategia = "Corte natural + Ouroboros Deluxe Finish (validação pós-DDS)"
            rec_mode = "natural"
        elif aptidao == APT_SYNTHESIS and mixed_sd:
            melhor_estrategia = "Reconstrução regional (proteger estático + sintetizar movimento)"
            rec_mode = "reconstruct_auto"
        elif aptidao == APT_SYNTHESIS:
            melhor_estrategia = "Síntese periódica unidirecional (Gênesis Supreme) / Reconstrução automática"
            rec_mode = "reconstruct_auto"
        elif cam_moving:
            melhor_estrategia = "Estabilização de câmera + Ponte de transição direcional (Level 4)"
            rec_mode = "reconstruct_auto"
        else:
            melhor_estrategia = "Reconstrução automática (compara corte natural, fluxo, ponte direcional e síntese)"
            rec_mode = "reconstruct_auto"

        if aptidao == APT_NATURAL:
            qualidade_esperada = "Alta — costura natural consistente com as transições internas; confirmar 10 ciclos após DDS."
        elif aptidao == APT_SYNTHESIS:
            qualidade_esperada = "Boa a alta em texturas contínuas — movimento unidirecional preservado; inspecionar nitidez e bordas após DDS."
        elif aptidao == APT_CORRECTABLE:
            qualidade_esperada = "Moderada a boa — melhora perceptível sobre o corte bruto, podendo exigir ajuste fino de trecho ou resolução."
        else:
            qualidade_esperada = "Condicional — a junção pode continuar perceptível; recomenda-se testar outro trecho, máscara regional ou fonte alternativa."
    else:
        melhor_trecho = "Nenhum trecho animado convincente identificado."
        melhor_resolucao = "Considere fundo estático 1920×1080 ou importar outro vídeo."
        melhor_cadencia = "Não aplicável"
        melhor_estrategia = "Trocar material de origem ou usar imagem estática"
        rec_mode = "natural"
        qualidade_esperada = "Baixa para loop animado — o material não atende aos requisitos mínimos de movimento contínuo."

    if aptidao in {APT_NATURAL, APT_SYNTHESIS, APT_CORRECTABLE}:
        proximo_passo = "Abrir no Ouroboros Deluxe para comparar candidatos, renderizar em modo automático e conferir 10 ciclos pós-DDS antes de aplicar."
    elif aptidao == APT_CONDITIONAL:
        proximo_passo = "Abrir no Ouroboros Deluxe para inspecionar candidatos/regiões manualmente ou importar um vídeo com câmera mais estável e movimento repetitivo."
    else:
        proximo_passo = "Selecionar outro vídeo com movimento contínuo mais favorável (veja o guia 'Como escolher um vídeo para um loop perfeito') ou usar modo Estático."

    fps_text = (
        f"{fps_meas:.2f} FPS medidos (nominal {fps_nom:.2f} FPS · {'VFR irregular' if is_vfr else 'CFR regular'})"
        if fps_nom > 0
        else f"{fps_meas:.2f} FPS"
    )
    if dup_ts or drop_ts:
        fps_text += f" · {dup_ts} dup / {drop_ts} quedas"

    dur_note = (
        "curta (pouca variedade de trechos)"
        if dur < 3.0
        else ("boa variedade para busca de ciclos" if dur >= 10.0 else "moderada para busca de ciclos")
    )
    res_text = f"{w}×{h} ({'16:9 nativa' if not crop_needed else ('vertical — recorte 16:9 necessário' if portrait_src else 'recorte 16:9 necessário')}) · codec {codec}"
    qual_text = f"Nitidez {sharp_lbl}, ruído {noise_lbl}, compressão {comp_lbl.replace('_', ' ')}" + (" · risco de motion blur" if motion_blur_risk else "")
    mov_text = (
        f"{_direction_label(dom_vec, mean_spd, sp_coh)} · velocidade média {mean_spd:.2f} px/passo · "
        f"coerência espacial {sp_coh:.2f} · consistência temporal {tmp_cons:.2f}"
    )
    estab_text = f"Câmera: {cam_class.replace('_', ' ')} · Iluminação: {light_stab.replace('_', ' ')}"
    if true_rec and period_sec:
        per_text = f"Recorrência natural detectada (~{period_sec:.2f} s)"
    elif spatial_rep.get("detected"):
        per_text = f"Repetição espacial detectada no eixo {spatial_rep.get('axis')} (adequada à síntese)"
    elif lum_pulse:
        per_text = "Pulsação luminosa periódica detectada sem deslocamento estrutural"
    else:
        per_text = "Sem periodicidade natural exata detectada na janela analisada"

    conf_text = f"{conf_label} ({round(conf_val * 100)}%)" + (" — avaliação inconclusiva; recomenda-se inspeção visual" if inconclusive else "")

    return {
        "aptidao": aptidao,
        "resumo": resumo,
        "inconclusivo": inconclusive,
        "confidence": round(conf_val, 3),
        "confidenceLabel": conf_label,
        "recommendedMode": rec_mode,
        "fields": {
            "material": name,
            "duracao": f"{dur:.2f} s ({dur_note})",
            "resolucao": res_text,
            "fps": fps_text,
            "qualidade": qual_text,
            "movimento": mov_text,
            "estabilidade": estab_text,
            "periodicidade": per_text,
            "aptidao": aptidao,
            "pontos_fortes": pontos_fortes,
            "pontos_fracos": pontos_fracos,
            "o_que_falta": o_que_falta,
            "o_que_pode_ser_corrigido": corrigivel,
            "o_que_nao_pode_ser_garantido": nao_garantido,
            "melhor_trecho": melhor_trecho,
            "melhor_resolucao": melhor_resolucao,
            "melhor_cadencia": melhor_cadencia,
            "melhor_estrategia": melhor_estrategia,
            "qualidade_esperada": qualidade_esperada,
            "confianca": conf_text,
            "proximo_passo": proximo_passo,
        },
        "bestTake": (
            {
                "start": best_take["start"],
                "end": best_take["end"],
                "frames": best_take["frames"],
                "size": best_take["size"],
                "wait": best_take["wait"],
                "duration": best_take["duration"],
                "classification": take_cls,
                "totalCost": take_cost,
                "seamRatio": take_vs.get("seamRatio"),
                "seamDirectionCosine": take_fc.get("seamDirectionCosine"),
            }
            if best_take
            else None
        ),
    }

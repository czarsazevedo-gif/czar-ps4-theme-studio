"""16-metric scientific circular loop evaluation suite, including native-resolution post-DDS validation."""

from __future__ import annotations

import math
import time
from typing import Sequence

import cv2
import numpy as np
from skimage.metrics import structural_similarity as skimage_ssim

from .. import dds, dxt1
from ..config import ANIMATED_SIZES, MAX_FRAME_COUNT, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION as DELUXE_VERSION
from .bc1_backends import DEFAULT_OUROBOROS_BC1_BACKEND, encode_sequence_bc1, extract_bc1_endpoints
from .flow import compute_bidirectional_flow, flow_kinematics, to_gray_u8


def _as_rgb_stack(frames: Sequence[np.ndarray] | np.ndarray) -> np.ndarray:
    arr = np.asarray(frames)
    if arr.ndim != 4 or arr.shape[0] < 4:
        raise ValueError("A avaliação circular exige ao menos 4 quadros RGB/RGBA.")
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.shape[-1] != 3:
        raise ValueError("Quadros devem ter 3 ou 4 canais.")
    return arr.astype(np.uint8, copy=False)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    diff = a.astype(np.float32) - b.astype(np.float32)
    mse = float(np.mean(diff * diff))
    if mse <= 1e-9:
        return 99.0
    return float(10.0 * math.log10((255.0 * 255.0) / mse))


def _ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Exact uniform-window SSIM matching skimage (ddof=1) using OpenCV float32 boxFilter (~3.5x faster)."""
    h, w = a.shape[:2]
    win = min(7, h if h % 2 == 1 else h - 1, w if w % 2 == 1 else w - 1)
    if win < 3:
        return 1.0 - float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) / 255.0
    af = a.astype(np.float32, copy=False)
    bf = b.astype(np.float32, copy=False)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    k = (win, win)
    n_el = float(win * win)
    cov_norm = n_el / (n_el - 1.0)
    ux = cv2.boxFilter(af, -1, k, borderType=cv2.BORDER_REFLECT)
    uy = cv2.boxFilter(bf, -1, k, borderType=cv2.BORDER_REFLECT)
    uxx = cv2.boxFilter(af * af, -1, k, borderType=cv2.BORDER_REFLECT)
    uyy = cv2.boxFilter(bf * bf, -1, k, borderType=cv2.BORDER_REFLECT)
    uxy = cv2.boxFilter(af * bf, -1, k, borderType=cv2.BORDER_REFLECT)
    vx = np.maximum(0.0, (uxx - ux * ux) * cov_norm)
    vy = np.maximum(0.0, (uyy - uy * uy) * cov_norm)
    vxy = (uxy - ux * uy) * cov_norm
    smap = ((2.0 * ux * uy + c1) * (2.0 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    pad = win // 2
    if pad > 0 and smap.shape[0] > 2 * pad and smap.shape[1] > 2 * pad:
        return float(smap[pad:-pad, pad:-pad].mean())
    return float(smap.mean())


def _ssim_and_ms_ssim_gray(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Compute full-res SSIM and 3-scale MS-SSIM in a single pass (eliminates duplicate scale-0 work)."""
    weights = (0.4, 0.35, 0.25)
    scores = []
    ca, cb = a, b
    s0 = 1.0
    for idx, w_scale in enumerate(weights):
        s = _ssim_gray(ca, cb)
        if idx == 0:
            s0 = s
        scores.append((w_scale, s))
        if min(ca.shape[:2]) < 24:
            break
        ca = cv2.pyrDown(ca)
        cb = cv2.pyrDown(cb)
    total_w = sum(w_s for w_s, _ in scores)
    return s0, float(sum(w_s * s for w_s, s in scores) / max(total_w, 1e-6))


def _ms_ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Fast 3-scale multi-scale SSIM approximation on grayscale frames."""
    return _ssim_and_ms_ssim_gray(a, b)[1]


def _robust_z(value: float, samples: np.ndarray, floor: float = 0.25) -> float:
    if len(samples) == 0:
        return 0.0
    med = float(np.median(samples))
    mad = float(np.median(np.abs(samples - med)))
    scale = max(floor, 1.4826 * mad)
    return float((value - med) / scale)


def _edge_map(gray: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(gx, gy)


def _laplacian_sharpness(gray: np.ndarray) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    return float(np.var(lap))


def _banding_index(rgb: np.ndarray) -> float:
    """Estimate false-contour banding in smooth gradient areas.

    In smooth regions (low coarse gradient and low high-frequency texture residual),
    true smooth gradients have tiny uniform macroscopic steps while quantized/DXT1a-banded
    regions exhibit flat 4x4 plateaus broken by sudden contour steps along block boundaries.
    """
    gray = to_gray_u8(rgb).astype(np.float32)
    smooth = cv2.GaussianBlur(gray, (9, 9), 2.0)
    gx_s = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    gy_s = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    coarse_grad = np.hypot(gx_s, gy_s)

    # Smooth-gradient mask: gentle tone ramp AND low high-frequency texture (excludes hair/line-art halos)
    hf_res = cv2.boxFilter(np.abs(gray - smooth), -1, (5, 5))
    gradient_zone = (coarse_grad >= 0.18) & (coarse_grad <= 3.8) & (hf_res <= 3.2)
    if int(gradient_zone.sum()) < 64:
        return 0.0

    # Macroscopic 2x2-smoothed pixel differences (ignores 1-LSB blue-noise dither while preserving 4x4 block steps)
    macro = cv2.boxFilter(gray, -1, (2, 2))
    dx = np.abs(np.diff(macro, axis=1, prepend=macro[:, :1]))
    dy = np.abs(np.diff(macro, axis=0, prepend=macro[:1, :]))
    step = np.maximum(dx, dy)

    z_steps = step[gradient_zone]
    z_coarse = coarse_grad[gradient_zone]
    # Plateau fraction (near-zero macroscopic step despite non-zero coarse gradient)
    plateau_frac = float((z_steps < 0.35).mean())
    # Excess jump ratio: where macroscopic step jumps > 2.5x the underlying smooth slope (false contour edge)
    false_contour_frac = float((z_steps > np.maximum(2.4, 2.5 * z_coarse)).mean())
    # Block-boundary energy ratio (4x4 DXT1 grid emphasis inside smooth gradient zones)
    bx = dx[:, 4::4]
    by = dy[4::4, :]
    in_x = dx[:, 2::4]
    in_y = dy[2::4, :]
    m = min(bx.shape[1], in_x.shape[1])
    n = min(by.shape[0], in_y.shape[0])
    block_ratio = 0.0
    if m > 0 and n > 0:
        m_x = gradient_zone[:, 4::4][:, :m] & gradient_zone[:, 2::4][:, :m]
        m_y = gradient_zone[4::4, :][:n, :] & gradient_zone[2::4, :][:n, :]
        if int(m_x.sum()) >= 16 and int(m_y.sum()) >= 16:
            b_edge = 0.5 * (float(bx[:, :m][m_x].mean()) + float(by[:n, :][m_y].mean()))
            b_inner = 0.5 * (float(in_x[:, :m][m_x].mean()) + float(in_y[:n, :][m_y].mean()))
            block_ratio = max(0.0, (b_edge - b_inner) / max(b_inner + 0.5, 0.5))

    # Scale into an interpretable [0, 1+] banding score
    score = plateau_frac * false_contour_frac * 6.5 + 0.30 * block_ratio
    return float(max(0.0, score))


def _border_empty_fraction(rgb: np.ndarray) -> float:
    """Detect pure-black/empty border strips (e.g. from uncompensated warping)."""
    gray = to_gray_u8(rgb)
    h, w = gray.shape
    band = max(2, min(8, min(h, w) // 20))
    interior_mean = float(gray[band:-band, band:-band].mean()) if h > 2 * band and w > 2 * band else float(gray.mean())
    if interior_mean < 12.0:
        return 0.0
    borders = np.concatenate([
        gray[:band, :].ravel(),
        gray[-band:, :].ravel(),
        gray[:, :band].ravel(),
        gray[:, -band:].ravel(),
    ])
    return float((borders < 3).mean())


def _resize_for_flow(frames: np.ndarray, max_width: int) -> tuple[np.ndarray, float]:
    h, w = frames.shape[1:3]
    if w <= max_width:
        return frames, 1.0
    scale = max_width / float(w)
    nh = max(32, int(round(h * scale / 2)) * 2)
    nw = max(32, int(round(w * scale / 2)) * 2)
    resized = np.stack([cv2.resize(f, (nw, nh), interpolation=cv2.INTER_AREA) for f in frames])
    return resized, nw / float(w)


def _detect_reversals(
    grays: np.ndarray,
    flows_fwd: list[np.ndarray],
    confs: list[np.ndarray],
    internal_mae: np.ndarray,
) -> dict:
    """Detect internal turnaround reversals (ping-pong) vs legitimate luminous pulsation."""
    n = len(grays)
    events = []
    # For each candidate pivot, check if past flow opposes future reversed flow AND frames mirror
    for pivot in range(2, n - 2):
        for hold in (0, 1):
            span = min(4, pivot, n - 1 - pivot - hold)
            if span < 2:
                continue
            opp_scores = []
            mirror_ratios = []
            for k in range(span):
                f_past = flows_fwd[pivot - 1 - k]
                f_fut = flows_fwd[pivot + hold + k]
                c_valid = (confs[pivot - 1 - k] > 0.3) & (confs[pivot + hold + k] > 0.3)
                mag_p = np.hypot(f_past[..., 0], f_past[..., 1])
                mag_f = np.hypot(f_fut[..., 0], f_fut[..., 1])
                moving = c_valid & (mag_p > 0.35) & (mag_f > 0.35)
                if int(moving.sum()) < max(16, int(0.015 * grays.shape[1] * grays.shape[2])):
                    continue
                cos = (
                    f_past[..., 0][moving] * f_fut[..., 0][moving]
                    + f_past[..., 1][moving] * f_fut[..., 1][moving]
                ) / (mag_p[moving] * mag_f[moving] + 1e-6)
                opp_scores.append(float((cos < -0.35).mean()))

                # Compare mirrored frame similarity (zero-mean local contrast cancels light drift)
                a = grays[pivot - 1 - k].astype(np.float32)
                b = grays[pivot + hold + 1 + k].astype(np.float32)
                diff = a - b
                mir_err = float(np.abs(diff - diff.mean()).mean())
                step_err = max(1.0, float(internal_mae[pivot - 1 - k]))
                mirror_ratios.append(mir_err / step_err)

            if len(opp_scores) >= 2 and float(np.mean(opp_scores)) >= 0.60:
                m_ratio = float(np.mean(mirror_ratios)) if mirror_ratios else 9.9
                events.append(
                    {
                        "pivot": pivot,
                        "hold": hold,
                        "opposition": round(float(np.mean(opp_scores)), 3),
                        "mirrorRatio": round(m_ratio, 3),
                        "mirrored": bool(m_ratio < 0.72),
                    }
                )
                break
    return {
        "detected": bool(any(e["mirrored"] for e in events)),
        "turnEvents": events,
    }


def _evaluate_kinematic_trajectories(
    small_grays: np.ndarray,
    flows_fwd: list[np.ndarray],
    confs: list[np.ndarray],
    mae_all: np.ndarray,
    *,
    seam_ssim: float,
    wait: float,
    flow_scale: float = 1.0,
    mean_psnr: float | None = None,
) -> dict:
    """Evaluate Lagrangian trajectory closure, intra-cycle phase-handoff burst, and regional pose continuity.

    Confronts TWO distinct calculation engines:
      1. O-KTCS (Ouroboros Kinematic & Trajectory Closure Score, 0..10):
         8x6 regional flow integration, open-loop vs closed-loop drift, tail/bridge pose snap-back, and MAD wave.
      2. INDEP-KLT (Independent External Verification Tool, 0..10):
         Shi-Tomasi + Pyramidal Lucas-Kanade sparse particle tracking with FB check + Fourier phaseCorrelate.
    """
    n, hs, ws = small_grays.shape
    gh, gw = 6, 8
    ch, cw = max(1, hs // gh), max(1, ws // gw)

    cell_vecs = np.zeros((n, gh, gw, 2), dtype=np.float32)
    cell_mags = np.zeros((n, gh, gw), dtype=np.float32)
    cell_mad = np.zeros((n, gh, gw), dtype=np.float32)
    for i in range(n):
        j = (i + 1) % n
        fw = flows_fwd[i] * float(flow_scale)
        cf = confs[i]
        d_img = cv2.absdiff(small_grays[j], small_grays[i])
        for r in range(gh):
            for c in range(gw):
                ys, xs = slice(r * ch, min(hs, (r + 1) * ch)), slice(c * cw, min(ws, (c + 1) * cw))
                cell_mad[i, r, c] = float(d_img[ys, xs].mean())
                w_c = cf[ys, xs]
                u_c = fw[ys, xs, 0]
                v_c = fw[ys, xs, 1]
                mag_c = np.hypot(u_c, v_c)
                sel = (w_c > 0.25) & (mag_c > 0.15)
                if int(sel.sum()) >= 8:
                    vx = float(np.median(u_c[sel]))
                    vy = float(np.median(v_c[sel]))
                    cell_vecs[i, r, c] = (vx, vy)
                    cell_mags[i, r, c] = float(math.hypot(vx, vy))

    # 1. O-KTCS Trajectory Closure & Regional Pose Snap-Back
    # Distinguish stationary flowing texture (where wrap MAD <= 1.05x internal MAD and wrap velocity aligns with internal flow)
    # from unclosed rigid structural displacement (where wrap exhibits visual excess or directional drop/reversal).
    net_disp = cell_vecs[:-1].sum(axis=0)
    net_dist = np.hypot(net_disp[..., 0], net_disp[..., 1])
    total_path = cell_mags[:-1].sum(axis=0)
    mov_cells = total_path > 1.8

    med_cell_mad = np.median(cell_mad[:-1], axis=0)
    mad_ratio_cell = cell_mad[-1] / np.maximum(med_cell_mad, 0.8)
    v_med = np.median(cell_vecs[:-1], axis=0)
    v_seam = cell_vecs[-1]
    v_med_mag = np.hypot(v_med[..., 0], v_med[..., 1])
    v_seam_mag = np.hypot(v_seam[..., 0], v_seam[..., 1])
    cos_cell = (v_med[..., 0] * v_seam[..., 0] + v_med[..., 1] * v_seam[..., 1]) / (v_med_mag * v_seam_mag + 1e-5)
    vis_excess = np.clip((mad_ratio_cell - 1.02) / 0.55, 0.0, 1.0)
    dir_drop = np.clip((0.75 - cos_cell) / 0.75, 0.0, 1.0)
    struct_weight = np.maximum(vis_excess, dir_drop)

    eff_net_dist = net_dist * struct_weight
    prog_unclosed = np.where(mov_cells, (net_dist / np.maximum(total_path, 1e-4)) * struct_weight, 0.0)
    max_unclosed_px = float(eff_net_dist[mov_cells].max()) if mov_cells.any() else 0.0
    p90_unclosed_ratio = float(np.percentile(prog_unclosed[mov_cells], 90)) if mov_cells.any() else 0.0

    frame_mad_max_min = float(np.max(mae_all) / max(float(np.min(mae_all)), 0.15))
    k_body = max(2, int(n * 0.6))
    v_body = np.median(cell_vecs[:k_body], axis=0)
    tail_vecs = cell_vecs[k_body:]
    b_mag = np.hypot(v_body[..., 0], v_body[..., 1])
    cell_rev_sev = np.zeros((gh, gw), dtype=np.float32)
    for r in range(gh):
        for c in range(gw):
            if b_mag[r, c] < 0.22:
                continue
            worst_dot = 1.0
            for t_idx in range(len(tail_vecs)):
                tv = tail_vecs[t_idx, r, c]
                t_mag = float(math.hypot(float(tv[0]), float(tv[1])))
                if t_mag > 0.15:
                    cos_t = float((tv[0] * v_body[r, c, 0] + tv[1] * v_body[r, c, 1]) / (t_mag * b_mag[r, c] + 1e-5))
                    worst_dot = min(worst_dot, cos_t)
            cell_rev_sev[r, c] = max(0.0, -worst_dot) * float(b_mag[r, c])
    max_tail_rev = float(cell_rev_sev.max())
    rev_cells_cnt = int((cell_rev_sev > 0.18).sum())

    # 2. Independent External Tracker (Shi-Tomasi + Pyramidal Lucas-Kanade + Fourier Phase Correlation)
    p0 = cv2.goodFeaturesToTrack(small_grays[0], maxCorners=180, qualityLevel=0.01, minDistance=max(4, min(hs, ws) // 32))
    surv_rate = 1.0
    klt_unclosed_ratio = 0.0
    klt_unclosed_px = 0.0
    if p0 is not None and len(p0) >= 6:
        cur = p0.copy()
        alive = np.ones(len(p0), dtype=bool)
        traj = [p0.reshape(-1, 2)]
        for i in range(n):
            j = (i + 1) % n
            nxt, st1, _ = cv2.calcOpticalFlowPyrLK(small_grays[i], small_grays[j], cur, None)
            bck, st2, _ = cv2.calcOpticalFlowPyrLK(small_grays[j], small_grays[i], nxt, None)
            fb = np.hypot((cur - bck)[..., 0], (cur - bck)[..., 1]).ravel()
            alive &= (st1.ravel() == 1) & (st2.ravel() == 1) & (fb < 1.8)
            cur = nxt
            traj.append(cur.reshape(-1, 2))
        traj_arr = np.stack(traj)
        surv_rate = float(alive.mean())
        if int(alive.sum()) >= 4:
            tr_a = traj_arr[:, alive, :]
            steps_v = np.diff(tr_a[:-1], axis=0)
            path_len = np.hypot(steps_v[..., 0], steps_v[..., 1]).sum(axis=0)
            net_err = np.hypot(tr_a[-2, :, 0] - tr_a[0, :, 0], tr_a[-2, :, 1] - tr_a[0, :, 1])
            mov_p = path_len > 1.2
            if mov_p.any():
                # Map each particle's starting coordinate to its (r, c) cell struct_weight
                p_xs = np.clip((tr_a[0, :, 0] // cw).astype(int), 0, gw - 1)
                p_ys = np.clip((tr_a[0, :, 1] // ch).astype(int), 0, gh - 1)
                p_sw = struct_weight[p_ys, p_xs]
                eff_p_err = net_err * p_sw
                klt_unclosed_ratio = float(np.percentile((eff_p_err[mov_p]) / np.maximum(path_len[mov_p], 1e-4), 85))
                klt_unclosed_px = float(np.percentile(eff_p_err[mov_p], 90))

    klt_step_p95: list[float] = []
    pc_peaks: list[float] = []
    small_f32 = [g.astype(np.float32) for g in small_grays]
    for i in range(n):
        j = (i + 1) % n
        pa = cv2.goodFeaturesToTrack(small_grays[i], maxCorners=140, qualityLevel=0.01, minDistance=max(4, min(hs, ws) // 32))
        if pa is not None and len(pa) >= 4:
            pb, st1, _ = cv2.calcOpticalFlowPyrLK(small_grays[i], small_grays[j], pa, None)
            good = st1.ravel() == 1
            d = np.hypot((pb[good] - pa[good])[..., 0], (pb[good] - pa[good])[..., 1]).ravel() if good.any() else np.array([0.0])
            klt_step_p95.append(float(np.percentile(d, 95)))
        else:
            klt_step_p95.append(0.0)
        (_, _), resp = cv2.phaseCorrelate(small_f32[i], small_f32[j])
        pc_peaks.append(float(resp))

    med_klt_p95 = float(np.median(klt_step_p95[:-1])) if len(klt_step_p95) > 1 else 0.25
    klt_burst = float(max(klt_step_p95) / max(med_klt_p95, 0.25)) if klt_step_p95 else 1.0
    klt_burst_step = int(np.argmax(klt_step_p95)) if klt_step_p95 else 0
    pc_drop = float(np.median(pc_peaks) - min(pc_peaks)) if pc_peaks else 0.0

    fps = 1.0 / max(float(wait), 1e-4)
    psnr_val = float(mean_psnr) if mean_psnr is not None else 38.0
    s_closure = 10.0 - min(7.5, max(0.0, p90_unclosed_ratio - 0.20) * 6.5 + max(0.0, max_unclosed_px - 6.0) * 0.055)
    s_kinematic = 10.0 - min(7.5, max_tail_rev * 0.65 + max(0.0, frame_mad_max_min - 1.45) * 1.85)
    s_seam_cad = min(10.0, 5.5 * float(seam_ssim) + 4.5 * min(fps / 12.0, 1.0))
    s_dds = min(10.0, max(0.0, (psnr_val - 28.0) / 1.4))
    ouroboros_score = round(0.35 * s_closure + 0.35 * s_kinematic + 0.15 * s_seam_cad + 0.15 * s_dds, 2)

    ind_closure = 10.0 - min(7.5, max(0.0, klt_unclosed_ratio - 0.20) * 6.5 + max(0.0, klt_unclosed_px - 6.0) * 0.08)
    ind_burst = 10.0 - min(7.5, max(0.0, klt_burst - 1.35) * 1.45 + pc_drop * 12.0 + (1.0 - surv_rate) * 4.5)
    ind_cad_dds = 0.5 * s_seam_cad + 0.5 * s_dds
    independent_score = round(0.40 * ind_closure + 0.40 * ind_burst + 0.20 * ind_cad_dds, 2)

    raw_avg = 0.5 * (ouroboros_score + independent_score)
    # Kinematic dominance cap: open trajectory or violent intra-cycle burst caps the definitive score honestly.
    # For smooth closed orbits of rigid objects, Lagrangian KLT closure tracks the moving object accurately even
    # when Eulerian grid cells do not cancel to zero, provided survival, seam SSIM, and step continuity are all clean.
    if surv_rate >= 0.45 and ind_closure >= 8.2 and klt_burst <= 1.8 and float(seam_ssim) >= 0.90:
        eff_closure = ind_closure
    else:
        eff_closure = min(s_closure, ind_closure if surv_rate > 0.35 else s_closure)
    kin_cap = eff_closure + 2.4
    if klt_burst > 4.0:
        kin_cap = min(kin_cap, 10.0 - min(4.5, (klt_burst - 2.0) * 0.35))
    definitive_score = round(min(raw_avg, max(3.5, kin_cap)), 2)

    return {
        "ouroborosKtcsScore": ouroboros_score,
        "independentKltScore": independent_score,
        "definitiveLoopScore": definitive_score,
        "subScores": {
            "trajectoryClosure": round(s_closure, 2),
            "poseContinuity": round(s_kinematic, 2),
            "seamAndCadence": round(s_seam_cad, 2),
            "postDdsFidelity": round(s_dds, 2),
            "independentClosure": round(ind_closure, 2),
            "independentBurst": round(ind_burst, 2),
        },
        "p90UnclosedTrajectoryRatio": round(p90_unclosed_ratio, 4),
        "maxUnclosedDisplacementPx": round(max_unclosed_px, 2),
        "tailPoseReversalSeverity": round(max_tail_rev, 3),
        "tailPoseReversalCells": rev_cells_cnt,
        "intraCycleMadWaveRatio": round(frame_mad_max_min, 3),
        "kltParticleSurvivalRate": round(surv_rate, 4),
        "kltUnclosedTrajectoryRatio": round(klt_unclosed_ratio, 4),
        "kltMaxStepBurstRatio": round(klt_burst, 3),
        "kltWorstBurstStep": klt_burst_step,
        "phaseCorrelationDrop": round(pc_drop, 4),
        "intraCycleJumpDetected": bool(klt_burst > 3.8 and frame_mad_max_min > 2.4),
        "poseSnapBackDetected": bool(max_tail_rev > 4.2 and rev_cells_cnt >= 14),
    }


def evaluate_loop(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    protected_mask: np.ndarray | None = None,
    reference_frames: Sequence[np.ndarray] | np.ndarray | None = None,
    source_motion_profile: dict | None = None,
    timestamps: Sequence[float] | None = None,
    flow_max_width: int = 240,
    evaluate_dds: bool = False,
    backend: str = "native",
) -> dict:
    """Compute the full 16-metric scientific circular evaluation on a frame sequence (optimized streaming memory)."""
    t_start = time.perf_counter()
    rgb = _as_rgb_stack(frames)
    n, h, w, _ = rgb.shape
    grays = np.stack([to_gray_u8(f) for f in rgb])

    # Fast 4x4 regional L* and global a*/b* statistics without allocating a full (N, H, W, 3) float32 Lab stack!
    reg_L = np.empty((n, 4, 4), dtype=np.float32)
    ab_means = np.empty((n, 2), dtype=np.float32)
    for i in range(n):
        small_f = cv2.resize(rgb[i], (64, 36), interpolation=cv2.INTER_AREA)
        lab_s = cv2.cvtColor(small_f, cv2.COLOR_RGB2LAB)
        L_s = lab_s[..., 0].astype(np.float32) * (100.0 / 255.0)
        reg_L[i] = L_s.reshape(4, 9, 4, 16).mean(axis=(1, 3))
        ab_means[i] = lab_s[..., 1:3].mean(axis=(0, 1)).astype(np.float32) - 128.0
    L_means = reg_L.mean(axis=(1, 2))

    # --- 1. Visual similarity (internal vs circular seam) ---
    mae_all = np.empty(n, dtype=np.float32)
    psnr_all = np.empty(n, dtype=np.float32)
    ssim_all = np.empty(n, dtype=np.float32)
    ms_ssim_all = np.empty(n, dtype=np.float32)
    edge_diff_all = np.empty(n, dtype=np.float32)
    lum_step_all = np.empty(n, dtype=np.float32)
    chroma_step_all = np.empty(n, dtype=np.float32)

    edges = np.stack([_edge_map(g) for g in grays])
    sharpness_all = np.array([_laplacian_sharpness(g) for g in grays], dtype=np.float32)
    gh_b, gw_b = min(8, max(2, h // 8)), min(8, max(2, w // 8))
    bh_b, bw_b = h // gh_b, w // gw_b
    hc_b, wc_b = gh_b * bh_b, gw_b * bw_b
    blk_mae = np.empty((n, gh_b, gw_b), dtype=np.float32)
    blk_edg = np.empty((n, gh_b, gw_b), dtype=np.float32)

    px_count_3 = float(h * w * 3)
    for i in range(n):
        j = (i + 1) % n
        d_u8 = cv2.absdiff(rgb[i], rgb[j])
        m_c = cv2.mean(d_u8)
        mae_all[i] = float((m_c[0] + m_c[1] + m_c[2]) / 3.0)
        n2 = float(cv2.norm(d_u8, cv2.NORM_L2))
        mse = (n2 * n2) / px_count_3
        psnr_all[i] = 99.0 if mse <= 1e-9 else float(10.0 * math.log10((255.0 * 255.0) / mse))
        ssim_all[i], ms_ssim_all[i] = _ssim_and_ms_ssim_gray(grays[i], grays[j])
        de = np.abs(edges[i] - edges[j])
        edge_diff_all[i] = float(de.mean())
        lum_step_all[i] = float(L_means[j] - L_means[i])
        ab_i = ab_means[i]
        ab_j = ab_means[j]
        chroma_step_all[i] = float(np.hypot(ab_j[0] - ab_i[0], ab_j[1] - ab_i[1]))
        blk_mae[i] = cv2.resize(d_u8[:hc_b, :wc_b], (gw_b, gh_b), interpolation=cv2.INTER_AREA).mean(axis=-1)
        blk_edg[i] = cv2.resize(de[:hc_b, :wc_b], (gw_b, gh_b), interpolation=cv2.INTER_AREA)

    int_mae = mae_all[:-1]
    seam_mae = float(mae_all[-1])
    int_ssim = ssim_all[:-1]
    seam_ssim = float(ssim_all[-1])
    int_ms_ssim = ms_ssim_all[:-1]
    seam_ms_ssim = float(ms_ssim_all[-1])
    int_psnr = psnr_all[:-1]
    seam_psnr = float(psnr_all[-1])
    med_mae = float(np.median(int_mae))
    med_ssim = float(np.median(int_ssim))

    visual_seam_ratio = float(seam_mae / max(med_mae, 0.5))
    visual_z = _robust_z(seam_mae, int_mae, floor=0.4)
    ssim_drop = float(max(0.0, med_ssim - seam_ssim))

    # --- 2, 3, 4. Dense bidirectional optical flow, velocity, acceleration ---
    small_rgb, flow_scale = _resize_for_flow(rgb, flow_max_width)
    small_grays = np.stack([to_gray_u8(f) for f in small_rgb])
    flows_fwd: list[np.ndarray] = []
    confs: list[np.ndarray] = []
    occ_fracs: list[float] = []
    foldovers: list[float] = []
    speeds = np.empty(n, dtype=np.float32)
    mean_vecs = np.empty((n, 2), dtype=np.float32)

    for i in range(n):
        j = (i + 1) % n
        fr = compute_bidirectional_flow(small_rgb[i], small_rgb[j])
        # Rescale flow magnitudes back to native pixel units
        scaled_fwd = fr.fwd / max(flow_scale, 1e-6)
        flows_fwd.append(scaled_fwd)
        confs.append(fr.confidence)
        occ_fracs.append(float(fr.occluded.mean()))
        kin = flow_kinematics(scaled_fwd, fr.confidence > 0.25)
        foldovers.append(kin["foldoverFraction"])
        speeds[i] = kin["meanSpeed"]
        mean_vecs[i] = kin["meanVec"]

    # Flow continuity at seam: compare F[N-1]->F[0] against adjacent F[N-2]->F[N-1] and F[0]->F[1]
    f_before = flows_fwd[n - 2]
    f_seam = flows_fwd[n - 1]
    f_after = flows_fwd[0]
    c_seam = confs[n - 1]
    c_adj = np.minimum(confs[n - 2], confs[0])

    # Internal flow-to-flow acceleration norms
    int_acc = []
    for i in range(n - 2):
        w_mask = (confs[i] > 0.25) & (confs[i + 1] > 0.25)
        diff_v = flows_fwd[i + 1] - flows_fwd[i]
        dv = np.hypot(diff_v[..., 0], diff_v[..., 1])
        int_acc.append(float(dv[w_mask].mean()) if w_mask.any() else float(dv.mean()))
    int_acc_arr = np.asarray(int_acc, dtype=np.float32)

    valid_seam = (c_seam > 0.25) & (c_adj > 0.25)
    f_expected = 0.5 * (f_before + f_after)
    f_err_map = np.hypot(f_seam[..., 0] - f_expected[..., 0], f_seam[..., 1] - f_expected[..., 1])
    seam_flow_err = float(f_err_map[valid_seam].mean()) if valid_seam.any() else float(f_err_map.mean())
    med_int_acc = float(np.median(int_acc_arr)) if len(int_acc_arr) else 0.0
    flow_err_ratio = float(seam_flow_err / max(med_int_acc, 0.25))

    # Direction & reversal at seam (require genuine structural motion, not DXT1a/photometric noise)
    min_moving_px = max(36, int(0.015 * valid_seam.size))
    opp_fracs = []
    cos_means = []
    for adj in (f_before, f_after):
        ma = np.hypot(adj[..., 0], adj[..., 1])
        ms = np.hypot(f_seam[..., 0], f_seam[..., 1])
        moving = valid_seam & (ma > 0.45) & (ms > 0.45)
        if int(moving.sum()) >= min_moving_px:
            cos = (adj[..., 0][moving] * f_seam[..., 0][moving] + adj[..., 1][moving] * f_seam[..., 1][moving]) / (
                ma[moving] * ms[moving] + 1e-6
            )
            opp_fracs.append(float((cos < -0.25).mean()))
            cos_means.append(float(np.clip(cos, -1.0, 1.0).mean()))
    seam_opp_frac = max(opp_fracs, default=0.0)
    seam_dir_cosine = min(cos_means, default=1.0)
    direction_measured = bool(opp_fracs)

    # Velocity & acceleration continuity
    int_speeds = speeds[:-1]
    med_speed = float(np.median(int_speeds))
    seam_speed = float(speeds[-1])
    adj_speed = 0.5 * (float(speeds[n - 2]) + float(speeds[0]))
    vel_jump_rel = float(abs(seam_speed - adj_speed) / max(adj_speed, med_speed, 0.35))

    acc_before_seam = float(np.hypot(*(mean_vecs[n - 1] - mean_vecs[n - 2])))
    acc_seam_after = float(np.hypot(*(mean_vecs[0] - mean_vecs[n - 1])))
    seam_acc = max(acc_before_seam, acc_seam_after)
    int_vec_acc = np.hypot(
        np.diff(mean_vecs[:-1, 0]), np.diff(mean_vecs[:-1, 1])
    ) if n > 3 else np.array([0.0], dtype=np.float32)
    acc_ratio = float(seam_acc / max(float(np.median(int_vec_acc)), 0.15))

    # --- 5, 6. Luminance & Chromatic continuity ---
    int_lum_abs = np.abs(lum_step_all[:-1])
    seam_lum_abs = float(abs(lum_step_all[-1]))
    lum_ratio = float(seam_lum_abs / max(float(np.median(int_lum_abs)), 0.4))
    # Derivative continuity for smooth photometric pulsation
    lum_slope_err = float(abs(lum_step_all[-1] - 0.5 * (lum_step_all[-2] + lum_step_all[0])))

    int_chroma = chroma_step_all[:-1]
    seam_chroma = float(chroma_step_all[-1])
    chroma_ratio = float(seam_chroma / max(float(np.median(int_chroma)), 0.4))

    # --- 7. Edge consistency & localized 8x8 seam burst (catches transient objects) ---
    int_edge = edge_diff_all[:-1]
    seam_edge = float(edge_diff_all[-1])
    edge_ratio = float(seam_edge / max(float(np.median(int_edge)), 0.8))
    edge_z = _robust_z(seam_edge, int_edge, floor=0.8)

    base_blk_mae = np.quantile(blk_mae[:-1], 0.85, axis=0)
    base_blk_edg = np.quantile(blk_edg[:-1], 0.85, axis=0)
    local_seam_excess = float(np.max((blk_mae[-1] - base_blk_mae) / (base_blk_mae + 4.5)))
    local_edge_excess = float(np.max((blk_edg[-1] - base_blk_edg) / (base_blk_edg + 8.0)))

    # --- 8. Flicker (distinguishes smooth natural lighting waves from high-freq artificial flicker) ---
    dL_circ = np.array([L_means[(i + 1) % n] - L_means[i] for i in range(n)], dtype=np.float32)
    d2L_circ = np.array([dL_circ[(i + 1) % n] - dL_circ[i] for i in range(n)], dtype=np.float32)
    sign_flips = np.array(
        [(dL_circ[i] * dL_circ[(i - 1) % n] < -0.04) for i in range(n)], dtype=bool
    )
    alternation_rate = float(sign_flips.mean())
    hf_lum_energy = float(np.abs(d2L_circ[sign_flips]).mean()) if sign_flips.any() else 0.0
    flicker_score = float(alternation_rate * hf_lum_energy)
    reg_dL = np.roll(reg_L, -1, axis=0) - reg_L
    reg_d2L = np.roll(reg_dL, -1, axis=0) - reg_dL
    reg_flip = (reg_dL * np.roll(reg_dL, 1, axis=0)) < -0.09
    reg_flicker = float(np.abs(reg_d2L[reg_flip]).mean() * reg_flip.mean()) if reg_flip.any() else 0.0
    flicker_score = max(flicker_score, reg_flicker)

    # --- 9. Ghosting detector (Intervention 06 / CZAR-ABS-AUD-007: moving-region Laplacian dip + midpoint blend) ---
    sharp_ref = max(float(np.percentile(sharpness_all, 85)), 1.0)
    sharp_min_ratio = float(sharpness_all.min() / sharp_ref)
    seam_sharp_ratio = float(min(sharpness_all[0], sharpness_all[-1]) / sharp_ref)
    laps = np.stack([cv2.Laplacian(g, cv2.CV_32F, ksize=3) for g in grays])
    blend_scores = []
    double_edge_scores = []
    for i in range(n):
        prev_f = grays[(i - 1) % n].astype(np.float32)
        curr_f = grays[i].astype(np.float32)
        next_f = grays[(i + 1) % n].astype(np.float32)
        span_diff = float(np.abs(next_f - prev_f).mean())
        if span_diff < 2.0:
            blend_scores.append(0.0)
            double_edge_scores.append(0.0)
            continue
        moving_px = np.abs(next_f - prev_f) > max(12.0, span_diff * 1.6)
        if int(moving_px.sum()) < 32:
            blend_scores.append(0.0)
            double_edge_scores.append(0.0)
            continue

        # On a real crossfade curr = 0.5*(prev+next), moving-region Laplacian variance drops sharply (~0.5x)
        # whereas on clean translated motion / periodic waves, moving-region variance stays ~1.0x!
        mov_var_prev = float(np.var(laps[(i - 1) % n][moving_px]))
        mov_var_next = float(np.var(laps[(i + 1) % n][moving_px]))
        mov_var_curr = float(np.var(laps[i][moving_px]))
        mov_var_nbr = max(0.5 * (mov_var_prev + mov_var_next), 1.0)
        moving_sharp_dip = max(0.0, 1.0 - mov_var_curr / mov_var_nbr)

        nbr_sharp = max(0.5 * (float(sharpness_all[(i - 1) % n]) + float(sharpness_all[(i + 1) % n])), 1.0)
        local_sharp_dip = max(0.0, 1.0 - float(sharpness_all[i]) / nbr_sharp)
        frame_sharp_dip = max(0.0, 1.0 - float(sharpness_all[i]) / sharp_ref)

        e_prev = edges[(i - 1) % n]
        e_curr = edges[i]
        e_next = edges[(i + 1) % n]
        prev_only_edge = (e_prev > 35.0) & (e_next < 10.0)
        next_only_edge = (e_next > 35.0) & (e_prev < 10.0)
        dbl_e = 0.0
        if local_sharp_dip > 0.08 and moving_sharp_dip > 0.16 and int(prev_only_edge.sum()) >= 16 and int(next_only_edge.sum()) >= 16:
            p_echo = float((e_curr[prev_only_edge] / (e_prev[prev_only_edge] + 1e-3)).mean())
            n_echo = float((e_curr[next_only_edge] / (e_next[next_only_edge] + 1e-3)).mean())
            dbl_e = float(np.clip(min(p_echo, n_echo) * 1.6, 0.0, 1.0)) if (p_echo > 0.28 and n_echo > 0.28) else 0.0
        double_edge_scores.append(dbl_e)

        # Require genuine local sharpness dip (>16%) AND moving-region Laplacian drop (>22%),
        # OR strong double-edge echo (>0.45) with moving-region sharpness drop (>20%).
        if ((local_sharp_dip > 0.16 and moving_sharp_dip > 0.22) or (dbl_e > 0.45 and moving_sharp_dip > 0.20)):
            mid_avg = 0.5 * (prev_f + next_f)
            rel_mid = float(
                np.abs(curr_f[moving_px] - mid_avg[moving_px]).mean()
                / (np.abs(next_f[moving_px] - prev_f[moving_px]).mean() + 1e-5)
            )
            sharp_evidence = min(1.0, max((local_sharp_dip - 0.08) * 3.8, (moving_sharp_dip - 0.12) * 3.2))
            blend_evidence = max(0.0, (0.24 - rel_mid) / 0.24)
            blend_scores.append(blend_evidence * max(sharp_evidence, dbl_e))
        else:
            blend_scores.append(0.0)
    ghosting_blend_peak = float(max(blend_scores)) if blend_scores else 0.0
    double_edge_peak = float(max(double_edge_scores)) if double_edge_scores else 0.0
    ghosting_score = float(max(0.0, max(0.0, 0.84 - seam_sharp_ratio) * 0.75 + ghosting_blend_peak * 0.90))
    ghosting_detected = bool(
        ghosting_score > 0.42 and (ghosting_blend_peak > 0.35 or double_edge_peak > 0.48)
    )

    # --- 10. Texture preservation / sharpness ---
    med_sharp = float(np.median(sharpness_all))
    sharp_retention = 1.0
    ref_border_empty = 0.0
    ref_arr: np.ndarray | None = None
    if reference_frames is not None:
        ref_arr = _as_rgb_stack(reference_frames)
        ref_sharp = float(np.median([_laplacian_sharpness(to_gray_u8(f)) for f in ref_arr]))
        sharp_retention = float(med_sharp / max(ref_sharp, 1.0))
        ref_border_empty = float(max(_border_empty_fraction(f) for f in ref_arr))

    # --- 11. Static / protected region integrity ---
    static_max_diff = 0.0
    static_mean_diff = 0.0
    static_changed_frac = 0.0
    if protected_mask is not None:
        pmask = np.asarray(protected_mask, dtype=bool)
        if pmask.shape != (h, w):
            pmask = cv2.resize(pmask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        if pmask.any():
            base_px = rgb[0][pmask].astype(np.int16)
            diffs = np.abs(rgb[:, pmask].astype(np.int16) - base_px[None, :, :])
            static_max_diff = float(diffs.max())
            static_mean_diff = float(diffs.mean())
            static_changed_frac = float((diffs.max(axis=-1) > 0).mean())

    # --- 12. Reconstruction artifacts (CZAR-ABS-AUD-011: differential border check vs reference, vignette-safe) ---
    border_empty = float(max(_border_empty_fraction(f) for f in rgb))
    if ref_arr is not None:
        # Only count newly blackened border pixels that were NOT already dark vignette (<14) in reference!
        b_chk = max(2, min(8, min(h, w) // 20))
        ref_g0 = to_gray_u8(ref_arr[0])
        if ref_g0.shape != (h, w):
            ref_g0 = cv2.resize(ref_g0, (w, h), interpolation=cv2.INTER_AREA)
        ref_b = np.concatenate([
            ref_g0[:b_chk, :].ravel(),
            ref_g0[-b_chk:, :].ravel(),
            ref_g0[:, :b_chk].ravel(),
            ref_g0[:, -b_chk:].ravel(),
        ])
        new_black_fracs = []
        for gi in grays:
            cur_b = np.concatenate([
                gi[:b_chk, :].ravel(),
                gi[-b_chk:, :].ravel(),
                gi[:, :b_chk].ravel(),
                gi[:, -b_chk:].ravel(),
            ])
            new_black_fracs.append(float(((cur_b < 3) & (ref_b >= 14)).mean()))
        border_empty_delta = max(0.0, float(max(new_black_fracs)) if new_black_fracs else 0.0)
    else:
        border_empty_delta = border_empty
    max_foldover = float(max(foldovers)) if foldovers else 0.0
    banding_pre = float(np.mean([_banding_index(rgb[i]) for i in np.linspace(0, n - 1, min(n, 6), dtype=int)]))

    # --- Spatial per-region metrics (Intervention 17) ---
    acc_f = np.zeros((h, w, 3), dtype=np.float32)
    acc_sq = np.zeros((h, w, 3), dtype=np.float32)
    for i in range(n):
        cv2.accumulate(rgb[i], acc_f)
        cv2.accumulateSquare(rgb[i], acc_sq)
    mean_f = acc_f / float(n)
    temp_std_map = np.sqrt(np.maximum(0.0, acc_sq / float(n) - mean_f * mean_f)).mean(axis=-1)
    seam_abs_map = cv2.absdiff(rgb[-1], rgb[0]).mean(axis=-1)
    static_reg_mask = temp_std_map <= 2.0
    moving_reg_mask = ~static_reg_mask
    static_reg_seam_mae = float(seam_abs_map[static_reg_mask].mean()) if static_reg_mask.any() else 0.0
    static_reg_shimmer = float(temp_std_map[static_reg_mask].mean()) if static_reg_mask.any() else 0.0
    moving_reg_seam_mae = float(seam_abs_map[moving_reg_mask].mean()) if moving_reg_mask.any() else 0.0
    moving_area_frac = float(moving_reg_mask.mean())
    b_strip = max(2, min(8, min(h, w) // 20))
    border_seam_mae = float(
        np.concatenate(
            [
                seam_abs_map[:b_strip, :].ravel(),
                seam_abs_map[-b_strip:, :].ravel(),
                seam_abs_map[:, :b_strip].ravel(),
                seam_abs_map[:, -b_strip:].ravel(),
            ]
        ).mean()
    )
    hotspot_excess_map = (blk_mae[-1] - base_blk_mae) / (base_blk_mae + 4.5)
    worst_idx = int(np.argmax(hotspot_excess_map))
    worst_r, worst_c = divmod(worst_idx, gw_b)
    spatial_regions = {
        "wholeFrame": {"seamMae": round(seam_mae, 4), "seamSsim": round(seam_ssim, 4), "seamRatio": round(visual_seam_ratio, 4)},
        "staticRegions": {
            "areaFraction": round(1.0 - moving_area_frac, 4),
            "meanTempStd": round(static_reg_shimmer, 4),
            "seamMae": round(static_reg_seam_mae, 4),
        },
        "movingRegions": {
            "areaFraction": round(moving_area_frac, 4),
            "seamMae": round(moving_reg_seam_mae, 4),
        },
        "protectedObjects": {
            "checked": protected_mask is not None,
            "maxAbsDiff": round(static_max_diff, 3),
            "meanAbsDiff": round(static_mean_diff, 4),
        },
        "boundaries": {
            "seamMae": round(border_seam_mae, 4),
            "emptyFraction": round(border_empty, 5),
            "emptyDeltaVsSource": round(border_empty_delta, 5),
        },
        "seamHotspot": {
            "gridSize": [gw_b, gh_b],
            "worstCell": [int(worst_c), int(worst_r)],
            "worstCellMae": round(float(blk_mae[-1, worst_r, worst_c]), 4),
            "localSeamExcess": round(local_seam_excess, 4),
            "localEdgeExcess": round(local_edge_excess, 4),
        },
    }

    # --- 13. Reversals (Ping-Pong) ---
    rev_info = _detect_reversals(small_grays, flows_fwd, confs, int_mae)
    has_reversal = bool(rev_info["detected"] or (direction_measured and seam_opp_frac > 0.45))

    # --- 14. Freezes / duplicate frames ---
    structural_motion = bool(med_speed > 0.25 and med_mae > 0.6)
    exact_dups = [i for i in range(n) if np.array_equal(rgb[i], rgb[(i + 1) % n])]
    near_stalls = [
        i for i in range(n)
        if structural_motion and float(mae_all[i]) < max(0.15, 0.14 * med_mae)
    ]
    freeze_indices = sorted(set(exact_dups + near_stalls))
    seam_frozen = (n - 1) in freeze_indices

    # --- 14b. Temporal & Motion Preservation Fidelity (prevents static/over-damped false approvals!) ---
    cy0, cy1 = int(h * 0.15), max(int(h * 0.15) + 1, int(h * 0.80))
    cx0, cx1 = int(w * 0.20), max(int(w * 0.20) + 1, int(w * 0.80))
    seq_t_std = float(temp_std_map.mean())
    seq_t_std_p90 = float(np.percentile(temp_std_map, 90))
    seq_focal_std = float(temp_std_map[cy0:cy1, cx0:cx1].mean())
    seq_active_area = float((temp_std_map > 2.5).mean())
    seq_focal_active = float((temp_std_map[cy0:cy1, cx0:cx1] > 2.5).mean())

    scale_to_180 = 180.0 / float(max(h, 1))
    fmag_stack = np.stack([np.hypot(fl[..., 0], fl[..., 1]) for fl in flows_fwd], axis=0)
    int_fmag = fmag_stack[:-1] if len(fmag_stack) > 1 else fmag_stack
    seq_flow_p95_180 = float(np.percentile(int_fmag, 95) * scale_to_180)
    fh_s, fw_s = int_fmag.shape[1:3]
    fcy0, fcy1 = int(fh_s * 0.15), max(int(fh_s * 0.15) + 1, int(fh_s * 0.80))
    fcx0, fcx1 = int(fw_s * 0.20), max(int(fw_s * 0.20) + 1, int(fw_s * 0.80))
    seq_focal_flow_p95_180 = float(np.percentile(int_fmag[:, fcy0:fcy1, fcx0:fcx1], 95) * scale_to_180)
    seq_flow_active_frac = float(((int_fmag * scale_to_180) > 0.30).mean())
    sparse_scale = max(moving_area_frac, 0.15) if moving_area_frac < 0.35 else 1.0
    near_dup_pairs = int(
        sum(
            1
            for mv in mae_all
            if (med_mae > 0.6 and float(mv) < max(0.30 * sparse_scale, 0.20 * med_mae))
            or (seq_t_std > 1.5 and float(mv) / sparse_scale < 0.28)
        )
    )

    # Compare against raw interval reference frames (catches synthesis/reconstruction motion collapse!)
    raw_exc_ret = 1.0
    raw_foc_ret = 1.0
    raw_subj_ret = 1.0
    raw_area_ret = 1.0
    raw_step_ret = 1.0
    ref_t_std_val = None
    reconstruction_motion_damped = False
    reconstruction_severely_damped = False
    if ref_arr is not None and len(ref_arr) >= 2:
        rh, rw = ref_arr.shape[1:3]
        r_acc = np.zeros((rh, rw, 3), dtype=np.float32)
        r_sq = np.zeros((rh, rw, 3), dtype=np.float32)
        for rf in ref_arr:
            cv2.accumulate(rf, r_acc)
            cv2.accumulateSquare(rf, r_sq)
        r_m = r_acc / float(len(ref_arr))
        ref_std_map = np.sqrt(np.maximum(0.0, r_sq / float(len(ref_arr)) - r_m * r_m)).mean(axis=-1)
        if ref_std_map.shape != (h, w):
            ref_std_map = cv2.resize(ref_std_map, (w, h), interpolation=cv2.INTER_AREA)
        ref_t_std_val = float(ref_std_map.mean())
        ref_foc_std = float(ref_std_map[cy0:cy1, cx0:cx1].mean())
        ref_act_area = float((ref_std_map > 2.5).mean())
        ref_p75 = float(np.percentile(ref_std_map, 75))
        subj_m = ref_std_map >= max(3.0, ref_p75)
        ref_subj_std = float(ref_std_map[subj_m].mean()) if subj_m.any() else ref_t_std_val
        seq_subj_std = float(temp_std_map[subj_m].mean()) if subj_m.any() else seq_t_std
        seq_p75 = float(np.percentile(temp_std_map, 75))
        out_subj_m = temp_std_map >= max(3.0, seq_p75)
        seq_own_subj_std = float(temp_std_map[out_subj_m].mean()) if out_subj_m.any() else seq_t_std
        ref_steps = [float(cv2.absdiff(ref_arr[i], ref_arr[i + 1]).mean()) for i in range(len(ref_arr) - 1)]
        ref_med_mae = float(np.median(ref_steps)) if ref_steps else 0.0

        raw_exc_ret = float(seq_t_std / max(ref_t_std_val, 1.0)) if ref_t_std_val >= 1.5 else 1.0
        raw_foc_ret = float(seq_focal_std / max(ref_foc_std, 1.0)) if ref_foc_std >= 1.5 else 1.0
        if ref_subj_std >= 2.5:
            raw_subj_fixed_ret = float(seq_subj_std / max(ref_subj_std, 1.0))
            raw_subj_dist_ret = float(seq_own_subj_std / max(ref_subj_std, 1.0))
            # Respect genuine spatial advection (drifting clouds/water) when total & focal motion are preserved,
            # while still vetoing any real motion collapse or frozen focal character.
            raw_subj_ret = float(max(raw_subj_fixed_ret, min(raw_subj_dist_ret, raw_exc_ret, raw_foc_ret)))
        else:
            raw_subj_ret = 1.0
        raw_area_ret = float(seq_active_area / max(ref_act_area, 0.04)) if ref_act_area >= 0.05 else 1.0
        raw_step_ret = float(med_mae / max(ref_med_mae, 0.4)) if ref_med_mae >= 0.8 else 1.0
        reconstruction_motion_damped = bool(
            ref_t_std_val >= 2.2
            and (
                raw_exc_ret < 0.72
                or raw_subj_ret < 0.68
                or raw_area_ret < 0.62
                or raw_step_ret < 0.55
            )
        )
        reconstruction_severely_damped = bool(
            ref_t_std_val >= 2.2
            and (
                raw_exc_ret < 0.60
                or raw_subj_ret < 0.56
                or raw_area_ret < 0.48
                or raw_step_ret < 0.42
            )
        )

    # Compare against full-source motion baseline (catches dead-zone interval selection!)
    src_exc_ret = 1.0
    src_glob_ret = 1.0
    src_foc_ret = 1.0
    src_area_ret = 1.0
    src_flow_ret = 1.0
    source_motion_starved = False
    source_severely_starved = False
    if source_motion_profile is not None:
        src_local_std = float(
            source_motion_profile.get("localWindowStdMedian")
            or source_motion_profile.get("temporalStdMean")
            or 0.0
        )
        src_global_std = float(source_motion_profile.get("temporalStdMean") or 0.0)
        src_local_foc = float(
            source_motion_profile.get("localFocalStdMedian")
            or source_motion_profile.get("focalTemporalStdMean")
            or 0.0
        )
        src_local_area = float(
            source_motion_profile.get("localActiveAreaMedian")
            or source_motion_profile.get("activeAreaFraction")
            or 0.0
        )
        src_flow_p95 = float(source_motion_profile.get("flowP95Norm180") or 0.0)

        src_local_win = float(source_motion_profile.get("localWindowSec") or 2.0)
        loop_dur_s = max(0.25, float(n) * float(wait))
        # Excursion over a shorter window (< localWindowSec) naturally spans less drift than a 2s window;
        # normalize gently (exponent 0.75, floor 0.62) while still catching true dead-zone starvation!
        dur_norm = min(1.0, max(0.62, (loop_dur_s / max(src_local_win, 0.5)) ** 0.75))
        eff_src_local_std = src_local_std * dur_norm
        eff_src_local_foc = src_local_foc * dur_norm

        src_exc_ret = float(seq_t_std / max(eff_src_local_std, 1.0)) if eff_src_local_std >= 1.8 else 1.0
        src_glob_ret = float(seq_t_std / max(src_global_std, 1.0)) if src_global_std >= 1.8 else 1.0
        src_foc_ret = float(seq_focal_std / max(eff_src_local_foc, 1.0)) if eff_src_local_foc >= 1.8 else 1.0
        src_area_ret = float(seq_active_area / max(src_local_area, 0.04)) if src_local_area >= 0.06 else 1.0
        src_flow_ret = float(seq_flow_p95_180 / max(src_flow_p95, 0.25)) if src_flow_p95 >= 0.35 else 1.0

        source_motion_starved = bool(
            eff_src_local_std >= 2.0
            and (
                src_exc_ret < 0.58
                or (src_foc_ret < 0.56 and src_exc_ret < 0.72)
                or src_area_ret < 0.45
            )
        )
        source_severely_starved = bool(
            eff_src_local_std >= 2.0 and (src_exc_ret < 0.46 or src_area_ret < 0.32)
        )

    is_practically_static = bool(med_mae < 0.25 and med_speed < 0.12 and float(np.ptp(L_means)) < 1.2)
    intrinsic_starved = bool(
        not is_practically_static
        and seq_t_std < 1.15
        and seq_active_area < 0.03
        and seq_flow_p95_180 < 0.25
        and float(np.ptp(L_means)) < 1.5
    )
    motion_starved = bool(is_practically_static or intrinsic_starved or source_motion_starved or reconstruction_motion_damped)

    ret_terms: list[float] = []
    if ref_arr is not None and (ref_t_std_val or 0.0) >= 1.5:
        raw_eff = min(min(raw_exc_ret, raw_subj_ret), 0.5 * raw_area_ret + 0.5 * raw_step_ret)
        ret_terms.append(min(1.0, raw_eff))
    if source_motion_profile is not None and float(source_motion_profile.get("localWindowStdMedian") or 0.0) >= 1.8:
        src_eff = min(src_exc_ret, 0.6 * src_foc_ret + 0.4 * src_area_ret)
        ret_terms.append(min(1.0, src_eff))
    effective_retention = min(ret_terms) if ret_terms else 1.0

    motion_fidelity_score = float(np.clip(10.0 - max(0.0, 0.90 - effective_retention) * 12.5, 0.5, 10.0))
    if near_dup_pairs > 0:
        motion_fidelity_score = max(0.5, motion_fidelity_score - min(3.0, near_dup_pairs * 0.75))
    if is_practically_static:
        motion_fidelity_score = min(motion_fidelity_score, 1.5)
    elif intrinsic_starved:
        motion_fidelity_score = min(motion_fidelity_score, 3.5)

    temporal_fidelity = {
        "temporalStdMean": round(seq_t_std, 4),
        "temporalStdP90": round(seq_t_std_p90, 4),
        "focalTemporalStdMean": round(seq_focal_std, 4),
        "activeAreaFraction": round(seq_active_area, 4),
        "focalActiveAreaFraction": round(seq_focal_active, 4),
        "flowP95Norm180": round(seq_flow_p95_180, 4),
        "focalFlowP95Norm180": round(seq_focal_flow_p95_180, 4),
        "flowActiveAreaFraction": round(seq_flow_active_frac, 4),
        "nearDuplicatePairs": near_dup_pairs,
        "rawToOutputExcursionRetention": round(min(raw_exc_ret, 3.0), 4),
        "rawToOutputFocalRetention": round(min(raw_foc_ret, 3.0), 4),
        "rawToOutputActiveSubjectRetention": round(min(raw_subj_ret, 3.0), 4),
        "rawToOutputActiveAreaRetention": round(min(raw_area_ret, 3.0), 4),
        "rawToOutputStepEnergyRetention": round(min(raw_step_ret, 3.0), 4),
        "sourceExcursionRetention": round(min(src_exc_ret, 3.0), 4),
        "sourceGlobalExcursionRatio": round(min(src_glob_ret, 3.0), 4),
        "sourceFocalRetention": round(min(src_foc_ret, 3.0), 4),
        "sourceActiveAreaRetention": round(min(src_area_ret, 3.0), 4),
        "sourceFlowP95Retention": round(min(src_flow_ret, 3.0), 4),
        "effectiveMotionRetention": round(effective_retention, 4),
        "motionFidelityScore": round(motion_fidelity_score, 2),
        "intrinsicMotionStarved": intrinsic_starved,
        "sourceMotionStarved": source_motion_starved,
        "sourceSeverelyStarved": source_severely_starved,
        "reconstructionMotionDamped": reconstruction_motion_damped,
        "reconstructionSeverelyDamped": reconstruction_severely_damped,
        "motionStarved": motion_starved,
        "checkedAgainstSource": source_motion_profile is not None,
        "checkedAgainstRawInterval": ref_arr is not None,
    }

    # --- 15. Cadence regularity ---
    speed_cv = float(np.std(speeds) / max(float(np.mean(speeds)), 0.15)) if structural_motion else 0.0
    timing_jitter = 0.0
    if timestamps is not None and len(timestamps) >= 3:
        dts = np.diff(np.asarray(timestamps, dtype=np.float64))
        dts = dts[dts > 0]
        if len(dts) >= 2:
            timing_jitter = float(np.max(np.abs(dts / np.median(dts) - 1.0)))

    # --- 16. Post-DDS evaluation (optional inline) ---
    dds_info = None
    if evaluate_dds:
        dds_info = _measure_dds_roundtrip(rgb, backend=backend)

    # --- 17. Lagrangian Trajectory Closure & Dual-Engine Score (O-KTCS vs INDEP-KLT) ---
    traj_kin = _evaluate_kinematic_trajectories(
        small_grays,
        flows_fwd,
        confs,
        mae_all,
        seam_ssim=seam_ssim,
        wait=wait,
        flow_scale=flow_scale,
        mean_psnr=(dds_info or {}).get("meanPsnr"),
    )
    traj_kin["subScores"]["motionFidelity"] = round(motion_fidelity_score, 2)
    if motion_starved:
        traj_kin["definitiveLoopScore"] = round(
            min(float(traj_kin["definitiveLoopScore"]), min(5.8, motion_fidelity_score + 0.3)), 2
        )
    elif motion_fidelity_score < 8.0:
        traj_kin["definitiveLoopScore"] = round(
            min(float(traj_kin["definitiveLoopScore"]), motion_fidelity_score + 0.6), 2
        )

    # --- Hard-cut detection inside sequence (including seam) ---
    cut_thresh = max(16.0, 3.0 * med_mae + 6.0)
    cut_indices = [
        i for i in range(n)
        if float(mae_all[i]) > cut_thresh and float(ssim_all[i]) < max(0.65, med_ssim - 0.18)
    ]

    # --- Aggregate diagnostic summary (never a blind single number!) ---
    issues: list[str] = []
    if is_practically_static:
        issues.append("Cena praticamente estática (ausência de movimento não indica bom loop).")
    elif intrinsic_starved:
        issues.append("Movimento temporal insuficiente para caracterizar animação contínua perceptível.")
    if reconstruction_motion_damped:
        issues.append(
            f"Perda severa de movimento original durante a reconstrução "
            f"(excursão {raw_exc_ret * 100:.0f}%, sujeito ativo {raw_subj_ret * 100:.0f}%)."
        )
    if source_motion_starved:
        issues.append(
            f"Trecho selecionado com atividade temporal muito inferior à fonte original "
            f"(excursão {src_exc_ret * 100:.0f}%, foco {src_foc_ret * 100:.0f}%)."
        )
    if has_reversal:
        issues.append("Reversão estrutural / ping-pong detectada.")
        traj_kin["definitiveLoopScore"] = round(min(float(traj_kin["definitiveLoopScore"]), 5.2), 2)
    if cut_indices:
        issues.append(f"Corte abrupto detectado nos índices {cut_indices}.")
    if freeze_indices:
        issues.append(f"Congelamento / quadros duplicados nos índices {freeze_indices}.")
    if (visual_seam_ratio > 1.75 and visual_z > 3.0 and ssim_drop > 0.03) or local_seam_excess > 1.8:
        issues.append("Descontinuidade visual significativa na junção circular.")
    if direction_measured and (flow_err_ratio > 2.2 and seam_flow_err > 1.0 or seam_dir_cosine < 0.35):
        issues.append("Descontinuidade de fluxo óptico na junção.")
    if structural_motion and vel_jump_rel > 0.55:
        issues.append("Ruptura de velocidade na junção.")
    if structural_motion and acc_ratio > 3.0 and seam_acc > 1.2:
        issues.append("Salto brusco de aceleração na junção.")
    if lum_ratio > 2.8 and seam_lum_abs > 3.5:
        issues.append("Salto de luminância na junção.")
    if chroma_ratio > 2.8 and seam_chroma > 4.0:
        issues.append("Salto cromático na junção.")
    if (edge_ratio > 1.9 and edge_z > 3.2 and seam_edge > 6.0) or local_edge_excess > 2.2:
        issues.append("Inconsistência estrutural / de bordas na junção.")
    if flicker_score > 1.8 and alternation_rate > 0.45:
        issues.append("Flicker artificial de alta frequência detectado.")
    if ghosting_detected:
        issues.append("Sinal de ghosting / mistura dupla detectado.")
    if border_empty > 0.08 and border_empty_delta > 0.04:
        issues.append("Bordas vazias/pretas introduzidas por reconstrução.")
    if static_max_diff > 0 and protected_mask is not None:
        issues.append(f"Região protegida alterada (diff máx {static_max_diff:.1f}).")
    if traj_kin["intraCycleJumpDetected"]:
        issues.append(
            f"Salto intra-ciclo de trajetória/fase detectado no passo {traj_kin['kltWorstBurstStep']} "
            f"(KLT burst {traj_kin['kltMaxStepBurstRatio']:.1f}x)."
        )

    # Confidence reflects measurable signal strength & FB flow reliability
    mean_conf = float(np.mean([c.mean() for c in confs]))
    mean_occ = float(np.mean(occ_fracs))
    conf_score = float(np.clip(mean_conf * (1.0 - 0.5 * mean_occ), 0.0, 1.0))

    # Independent 7-axis audit proof (visual, kinematic, motion preservation, freeze, jump, cadence, 10-cycle merged later)
    visual_axis_ok = bool(
        not ((visual_seam_ratio > 1.75 and visual_z > 3.0 and ssim_drop > 0.03) or local_seam_excess > 1.8)
        and not ghosting_detected
        and not (flicker_score > 1.8 and alternation_rate > 0.45)
    )
    kinematic_axis_ok = bool(
        not has_reversal
        and not (direction_measured and (flow_err_ratio > 2.2 and seam_flow_err > 1.0 or seam_dir_cosine < 0.35))
        and not (structural_motion and vel_jump_rel > 0.55)
        and not (structural_motion and acc_ratio > 3.0 and seam_acc > 1.2)
    )
    motion_axis_ok = bool(not motion_starved and not is_practically_static)
    freeze_axis_ok = bool(not freeze_indices and near_dup_pairs == 0)
    jump_axis_ok = bool(not cut_indices and not traj_kin["intraCycleJumpDetected"])
    cadence_fps = round(1.0 / max(float(wait), 1e-6), 4)
    cadence_axis_ok = bool(cadence_fps >= 3.0 and timing_jitter <= 0.25 and speed_cv <= 0.95)
    independent_axes = {
        "visualContinuity": {
            "passed": visual_axis_ok,
            "seamSsim": round(seam_ssim, 4),
            "seamRatio": round(visual_seam_ratio, 4),
            "localSeamExcess": round(local_seam_excess, 4),
        },
        "kinematicContinuity": {
            "passed": kinematic_axis_ok,
            "ouroborosKtcsScore": traj_kin["ouroborosKtcsScore"],
            "independentKltScore": traj_kin["independentKltScore"],
            "seamDirectionCosine": round(seam_dir_cosine, 4),
            "velocityJump": round(vel_jump_rel, 4),
            "accelRatio": round(acc_ratio, 4),
        },
        "motionPreservation": {
            "passed": motion_axis_ok,
            "motionFidelityScore": temporal_fidelity["motionFidelityScore"],
            "temporalStdMean": temporal_fidelity["temporalStdMean"],
            "focalTemporalStdMean": temporal_fidelity["focalTemporalStdMean"],
            "activeAreaFraction": temporal_fidelity["activeAreaFraction"],
            "rawToOutputExcursionRetention": temporal_fidelity["rawToOutputExcursionRetention"],
            "sourceExcursionRetention": temporal_fidelity["sourceExcursionRetention"],
            "sourceFocalRetention": temporal_fidelity["sourceFocalRetention"],
            "motionStarved": motion_starved,
            "reconstructionMotionDamped": reconstruction_motion_damped,
        },
        "freezeIntegrity": {
            "passed": freeze_axis_ok,
            "detected": bool(freeze_indices),
            "freezeIndices": freeze_indices,
            "nearDuplicatePairs": near_dup_pairs,
        },
        "jumpIntegrity": {
            "passed": jump_axis_ok,
            "intraCycleJumpDetected": bool(traj_kin["intraCycleJumpDetected"]),
            "kltMaxStepBurstRatio": traj_kin["kltMaxStepBurstRatio"],
            "cutDetected": bool(cut_indices),
        },
        "cadenceIntegrity": {
            "passed": cadence_axis_ok,
            "fps": cadence_fps,
            "waitSec": float(wait),
            "speedCV": round(speed_cv, 4),
            "timingJitter": round(timing_jitter, 4),
        },
    }

    return {
        "version": DELUXE_VERSION,
        "frames": n,
        "size": [w, h],
        "wait": float(wait),
        "duration": round(n * float(wait), 6),
        "elapsedMs": round((time.perf_counter() - t_start) * 1000.0, 1),
        "metrics": {
            "visualSimilarity": {
                "seamMae": round(seam_mae, 4),
                "internalMedianMae": round(med_mae, 4),
                "seamRatio": round(visual_seam_ratio, 4),
                "localSeamExcess": round(local_seam_excess, 4),
                "seamZScore": round(visual_z, 3),
                "seamPsnr": round(seam_psnr, 2),
                "internalMedianPsnr": round(float(np.median(int_psnr)), 2),
                "seamSsim": round(seam_ssim, 4),
                "internalMedianSsim": round(med_ssim, 4),
                "seamMsSsim": round(seam_ms_ssim, 4),
                "internalMedianMsSsim": round(float(np.median(int_ms_ssim)), 4),
                "ssimDrop": round(ssim_drop, 4),
            },
            "flowContinuity": {
                "seamFlowErrorPx": round(seam_flow_err, 4),
                "internalMedianAccPx": round(med_int_acc, 4),
                "flowErrorRatio": round(flow_err_ratio, 4),
                "seamDirectionCosine": round(seam_dir_cosine, 4),
                "seamOppositeFraction": round(seam_opp_frac, 4),
                "directionMeasured": direction_measured,
                "meanOcclusionFraction": round(mean_occ, 4),
                "seamOcclusionFraction": round(occ_fracs[-1], 4),
                "meanFlowConfidence": round(mean_conf, 4),
            },
            "velocityContinuity": {
                "seamSpeedPx": round(seam_speed, 4),
                "internalMedianSpeedPx": round(med_speed, 4),
                "adjacentSpeedPx": round(adj_speed, 4),
                "relativeJump": round(vel_jump_rel, 4),
            },
            "accelerationContinuity": {
                "seamAccelPx": round(seam_acc, 4),
                "internalMedianAccelPx": round(float(np.median(int_vec_acc)), 4),
                "accelRatio": round(acc_ratio, 4),
            },
            "luminanceContinuity": {
                "seamDeltaL": round(float(lum_step_all[-1]), 4),
                "internalMedianAbsDeltaL": round(float(np.median(int_lum_abs)), 4),
                "lumRatio": round(lum_ratio, 4),
                "slopeErrorL": round(lum_slope_err, 4),
                "rangeL": round(float(np.ptp(L_means)), 4),
            },
            "chromaticContinuity": {
                "seamDeltaChroma": round(seam_chroma, 4),
                "internalMedianChroma": round(float(np.median(int_chroma)), 4),
                "chromaRatio": round(chroma_ratio, 4),
            },
            "edgeConsistency": {
                "seamEdgeDiff": round(seam_edge, 4),
                "internalMedianEdgeDiff": round(float(np.median(int_edge)), 4),
                "edgeRatio": round(edge_ratio, 4),
                "localEdgeExcess": round(local_edge_excess, 4),
                "edgeZScore": round(edge_z, 3),
            },
            "flicker": {
                "score": round(flicker_score, 4),
                "alternationRate": round(alternation_rate, 4),
                "highFreqLumEnergy": round(hf_lum_energy, 4),
                "artificialFlicker": bool(flicker_score > 1.8 and alternation_rate > 0.45),
            },
            "ghosting": {
                "score": round(ghosting_score, 4),
                "blendPeak": round(ghosting_blend_peak, 4),
                "doubleEdgePeak": round(double_edge_peak, 4),
                "sharpnessMinRatio": round(sharp_min_ratio, 4),
                "seamSharpnessRatio": round(seam_sharp_ratio, 4),
                "detected": ghosting_detected,
            },
            "texturePreservation": {
                "medianSharpness": round(med_sharp, 2),
                "minSharpness": round(float(sharpness_all.min()), 2),
                "sharpnessRetention": round(sharp_retention, 4),
            },
            "staticIntegrity": {
                "checked": protected_mask is not None,
                "maxAbsDiff": round(static_max_diff, 3),
                "meanAbsDiff": round(static_mean_diff, 4),
                "changedPixelFraction": round(static_changed_frac, 6),
            },
            "reconstructionArtifacts": {
                "emptyBorderFraction": round(border_empty, 5),
                "emptyBorderDelta": round(border_empty_delta, 5),
                "maxFlowFoldover": round(max_foldover, 5),
                "bandingIndex": round(banding_pre, 4),
            },
            "spatialRegions": spatial_regions,
            "temporalFidelity": temporal_fidelity,
            "independentAxes": independent_axes,
            "trajectoryKinematics": traj_kin,
            "reversals": {
                "detected": has_reversal,
                "internalMirrorDetected": rev_info["detected"],
                "seamOppositeFraction": round(seam_opp_frac, 4),
                "turnEvents": rev_info["turnEvents"],
            },
            "freezes": {
                "detected": bool(freeze_indices),
                "seamFrozen": seam_frozen,
                "indices": freeze_indices,
                "exactDuplicates": exact_dups,
                "nearDuplicatePairs": near_dup_pairs,
            },
            "cadenceRegularity": {
                "speedCV": round(speed_cv, 4),
                "timingJitter": round(timing_jitter, 4),
                "fps": cadence_fps,
            },
            "cuts": {
                "detected": bool(cut_indices),
                "indices": cut_indices,
            },
            "ddsDelta": dds_info,
        },
        "issues": issues,
        "confidence": round(conf_score, 3),
        "confidenceLabel": "forte" if conf_score >= 0.55 else ("moderada" if conf_score >= 0.30 else "limitada"),
        "passed": len(issues) == 0,
    }


def _measure_dds_roundtrip(rgb: np.ndarray, *, backend: str = DEFAULT_OUROBOROS_BC1_BACKEND) -> dict:
    """Encode RGB sequence to canonical DDS DXT1a, decode it, and measure perceptual spatial & temporal deltas
    (Interventions 09, 19, 20, 21).
    """
    n, h, w, _ = rgb.shape
    blobs, enc_meta = encode_sequence_bc1(rgb, backend=backend)
    encode_ms = float(enc_meta.get("encodeMs", 0.0))

    t1 = time.perf_counter()
    decoded_rgba = [dxt1.decode_dds(b) for b in blobs]
    decoded_rgb = np.stack([a[..., :3] for a in decoded_rgba])
    min_alpha = int(min(int(a[..., 3].min()) for a in decoded_rgba)) if decoded_rgba else 255
    decode_ms = (time.perf_counter() - t1) * 1000.0

    total_bytes = sum(len(b) for b in blobs)
    expected_bytes = n * dds_bytes_for(w, h)
    valid_headers = all(b[:4] == b"DDS " and b[84:88] == b"DXT1" for b in blobs)
    within_budget = (
        total_bytes <= RAF_SIZE_LIMIT
        and n <= MAX_FRAME_COUNT
        and ((w, h) not in ANIMATED_SIZES or n <= max_frames_for((w, h)))
    )

    px_count_3 = float(h * w * 3)
    psnrs: list[float] = []
    maes: list[float] = []
    for i in range(n):
        d_u8 = cv2.absdiff(rgb[i], decoded_rgb[i])
        m_c = cv2.mean(d_u8)
        maes.append(float((m_c[0] + m_c[1] + m_c[2]) / 3.0))
        n2 = float(cv2.norm(d_u8, cv2.NORM_L2))
        mse = (n2 * n2) / px_count_3
        psnrs.append(99.0 if mse <= 1e-9 else float(10.0 * math.log10((255.0 * 255.0) / mse)))
    ssims = [_ssim_gray(to_gray_u8(rgb[i]), to_gray_u8(decoded_rgb[i])) for i in range(n)]

    sample_idx = np.linspace(0, n - 1, min(n, 6), dtype=int)
    band_pre = float(np.mean([_banding_index(rgb[i]) for i in sample_idx]))
    band_post = float(np.mean([_banding_index(decoded_rgb[i]) for i in sample_idx]))
    band_inc = band_post - band_pre

    # Intervention 20: Perceptual CIELAB color, edge, smooth-gradient & highlight fidelity (sampled <=6 frames)
    de_list: list[float] = []
    de_p95_list: list[float] = []
    dl_list: list[float] = []
    dc_list: list[float] = []
    edge_psnr_list: list[float] = []
    grad_err_list: list[float] = []
    hi_err_list: list[float] = []

    for i in sample_idx:
        s_lab = cv2.cvtColor(rgb[i], cv2.COLOR_RGB2LAB).astype(np.float32)
        d_lab = cv2.cvtColor(decoded_rgb[i], cv2.COLOR_RGB2LAB).astype(np.float32)
        dL = (d_lab[..., 0] - s_lab[..., 0]) * (100.0 / 255.0)
        da = d_lab[..., 1] - s_lab[..., 1]
        db = d_lab[..., 2] - s_lab[..., 2]
        de = np.sqrt(dL * dL + da * da + db * db)
        de_list.append(float(de.mean()))
        de_p95_list.append(float(np.percentile(de, 95)))
        dl_list.append(float(np.abs(dL).mean()))
        dc_list.append(float(np.hypot(da, db).mean()))

        s_gray = to_gray_u8(rgb[i]).astype(np.float32)
        e_map = _edge_map(s_gray.astype(np.uint8))
        e_mask = e_map > 28.0
        rgb_diff = rgb[i].astype(np.float32) - decoded_rgb[i].astype(np.float32)
        if int(e_mask.sum()) >= 32:
            e_mse = float(np.mean((rgb_diff[e_mask]) ** 2))
            edge_psnr_list.append(99.0 if e_mse <= 1e-9 else float(10.0 * math.log10((255.0 * 255.0) / e_mse)))
        else:
            edge_psnr_list.append(psnrs[i])

        smooth_g = cv2.GaussianBlur(s_gray, (9, 9), 2.0)
        c_grad = np.hypot(cv2.Sobel(smooth_g, cv2.CV_32F, 1, 0, ksize=3) / 8.0, cv2.Sobel(smooth_g, cv2.CV_32F, 0, 1, ksize=3) / 8.0)
        g_mask = (c_grad >= 0.15) & (c_grad <= 4.0)
        grad_err_list.append(float(np.abs(rgb_diff[g_mask]).mean()) if int(g_mask.sum()) >= 32 else maes[i])

        hi_mask = (s_lab[..., 0] * (100.0 / 255.0)) >= 78.0
        hi_err_list.append(float(np.abs(rgb_diff[hi_mask]).mean()) if int(hi_mask.sum()) >= 32 else maes[i])

    # Intervention 21: Temporal stability of the FULL decoded DDS loop (static shimmer, endpoint churn, chroma osc, wrap)
    acc_s = np.zeros((h, w, 3), dtype=np.float32)
    acc_s2 = np.zeros((h, w, 3), dtype=np.float32)
    for i in range(n):
        cv2.accumulate(rgb[i], acc_s)
        cv2.accumulateSquare(rgb[i], acc_s2)
    m_s = acc_s / float(n)
    src_tstd = np.sqrt(np.maximum(0.0, acc_s2 / float(n) - m_s * m_s)).mean(axis=-1)
    static_px = src_tstd < 1.5
    shimmer_steps = []
    chroma_osc_steps = []
    edge_alt_steps = []
    wrap_shimmer = 0.0
    if int(static_px.sum()) >= 32:
        for i in range(n):
            j = (i + 1) % n
            d_st = cv2.absdiff(decoded_rgb[j], decoded_rgb[i])
            step_d = float(d_st[static_px].mean())
            shimmer_steps.append(step_d)
            if i == n - 1:
                wrap_shimmer = step_d
        # Sample chroma oscillation on static regions across <=8 consecutive pairs
        c_idx = np.linspace(0, n - 1, min(n, 8), dtype=int)
        c_labs = [cv2.cvtColor(decoded_rgb[i], cv2.COLOR_RGB2LAB).astype(np.float32) for i in c_idx]
        for k in range(len(c_labs) - 1):
            dab = np.hypot(
                c_labs[k + 1][..., 1][static_px] - c_labs[k][..., 1][static_px],
                c_labs[k + 1][..., 2][static_px] - c_labs[k][..., 2][static_px],
            )
            chroma_osc_steps.append(float(dab.mean()))

    # Measure BC1 4x4 endpoint change rate in static blocks
    ep_change_rate = 0.0
    if n >= 2:
        bh, bw = (h + 3) // 4, (w + 3) // 4
        pad_h, pad_w = (-h) % 4, (-w) % 4
        sp_pad = np.pad(static_px, ((0, pad_h), (0, pad_w)), mode="edge") if (pad_h or pad_w) else static_px
        static_blk = sp_pad.reshape(bh, 4, bw, 4).all(axis=(1, 3)).ravel()
        if int(static_blk.sum()) >= 4:
            eps = [extract_bc1_endpoints(b) for b in blobs]
            rates = []
            for i in range(n):
                j = (i + 1) % n
                changed = (eps[j][static_blk, 0] != eps[i][static_blk, 0]) | (eps[j][static_blk, 1] != eps[i][static_blk, 1])
                rates.append(float(changed.mean()))
            ep_change_rate = float(np.mean(rates))

    # Edge alternation rate on decoded loop
    dec_edges = [float(_edge_map(to_gray_u8(decoded_rgb[i])).mean()) for i in range(n)]
    de_diff = np.array([dec_edges[(i + 1) % n] - dec_edges[i] for i in range(n)], dtype=np.float32)
    edge_alt_rate = float(((de_diff * np.roll(de_diff, 1)) < -0.05).mean()) if n >= 3 else 0.0

    mean_psnr_val = float(np.mean(psnrs))
    # Honest provenance-aware banding risk: flag genuine compression-introduced banding rather than dark cel-shading in source
    banding_risk = bool(
        (band_inc > 0.14)
        or (band_inc > 0.08 and band_post > 0.38 and mean_psnr_val < 38.5)
        or (band_pre < 0.25 and band_post > 0.36)
    )

    return {
        "backend": enc_meta.get("actualBackend", backend),
        "requestedBackend": enc_meta.get("requestedBackend", backend),
        "fallbackUsed": bool(enc_meta.get("fallbackUsed", False)),
        "reusedBlocks": int(enc_meta.get("reusedBlocks", 0)),
        "wrapStabilizedBlocks": int(enc_meta.get("wrapStabilizedBlocks", 0)),
        "reuseRatio": float(enc_meta.get("reuseRatio", 0.0)),
        "alphaSafe": min_alpha == 255,
        "totalBytes": total_bytes,
        "expectedBytes": expected_bytes,
        "withinBudget": within_budget,
        "validHeaders": valid_headers,
        "encodeMs": round(encode_ms, 1),
        "decodeMs": round(decode_ms, 1),
        "meanPsnr": round(mean_psnr_val, 2),
        "minPsnr": round(float(np.min(psnrs)), 2),
        "meanSsim": round(float(np.mean(ssims)), 4),
        "meanMae": round(float(np.mean(maes)), 4),
        "meanLabDeltaE": round(float(np.mean(de_list)), 4),
        "p95LabDeltaE": round(float(np.mean(de_p95_list)), 4),
        "lumDeltaL": round(float(np.mean(dl_list)), 4),
        "chromaDeltaC": round(float(np.mean(dc_list)), 4),
        "edgePsnr": round(float(np.mean(edge_psnr_list)), 2),
        "gradientError": round(float(np.mean(grad_err_list)), 4),
        "highlightError": round(float(np.mean(hi_err_list)), 4),
        "bandingPreDds": round(band_pre, 4),
        "bandingPostDds": round(band_post, 4),
        "bandingIncrease": round(band_inc, 4),
        "bandingRisk": banding_risk,
        "compressedTemporalQuality": {
            "staticBlockShimmerMad": round(float(np.mean(shimmer_steps)) if shimmer_steps else 0.0, 4),
            "wrapStaticShimmerMad": round(wrap_shimmer, 4),
            "endpointChangeRate": round(ep_change_rate, 4),
            "chromaOscillation": round(float(np.mean(chroma_osc_steps)) if chroma_osc_steps else 0.0, 4),
            "edgeAlternationRate": round(edge_alt_rate, 4),
        },
        "distinctFrames": len(set(blobs)),
        "_decoded": decoded_rgb,
        "_blobs": blobs,
    }


def evaluate_post_dds(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    protected_mask: np.ndarray | None = None,
    reference_frames: Sequence[np.ndarray] | np.ndarray | None = None,
    source_motion_profile: dict | None = None,
    timestamps: Sequence[float] | None = None,
    backend: str = DEFAULT_OUROBOROS_BC1_BACKEND,
    return_blobs: bool = False,
) -> dict:
    """Mandatory post-DDS evaluation: encode -> decode -> re-evaluate all metrics on decoded DDS."""
    rgb = _as_rgb_stack(frames)
    dds_rt = _measure_dds_roundtrip(rgb, backend=backend)
    decoded_rgb = dds_rt.pop("_decoded")
    blobs = dds_rt.pop("_blobs")

    # Compare protected pixels against the DDS-encoded reference frame 0 (canonical DXT1a behavior)
    post_report = evaluate_loop(
        decoded_rgb,
        wait,
        protected_mask=protected_mask,
        reference_frames=reference_frames if reference_frames is not None else rgb,
        source_motion_profile=source_motion_profile,
        timestamps=timestamps,
        evaluate_dds=False,
    )
    post_report["metrics"]["ddsDelta"] = dds_rt
    tk = post_report["metrics"].get("trajectoryKinematics")
    tf = post_report["metrics"].get("temporalFidelity") or {}
    if tk is not None and dds_rt.get("meanPsnr") is not None:
        s_dds = round(min(10.0, max(0.0, (float(dds_rt["meanPsnr"]) - 28.0) / 1.4)), 2)
        sub = tk["subScores"]
        sub["postDdsFidelity"] = s_dds
        o_sc = round(
            0.35 * sub["trajectoryClosure"]
            + 0.35 * sub["poseContinuity"]
            + 0.15 * sub["seamAndCadence"]
            + 0.15 * s_dds,
            2,
        )
        i_sc = round(
            0.40 * sub["independentClosure"]
            + 0.40 * sub["independentBurst"]
            + 0.20 * (0.5 * sub["seamAndCadence"] + 0.5 * s_dds),
            2,
        )
        raw_avg = 0.5 * (o_sc + i_sc)
        surv_r = float(tk.get("kltParticleSurvivalRate", 1.0))
        k_burst = float(tk.get("kltMaxStepBurstRatio", 1.0))
        s_ssim = float((post_report["metrics"].get("visualSimilarity") or {}).get("seamSsim", 0.0))
        has_rev_post = bool((post_report["metrics"].get("reversals") or {}).get("detected", False))
        if surv_r >= 0.45 and sub["independentClosure"] >= 8.2 and k_burst <= 1.8 and s_ssim >= 0.90:
            eff_cl = sub["independentClosure"]
        else:
            eff_cl = min(sub["trajectoryClosure"], sub["independentClosure"] if surv_r > 0.35 else sub["trajectoryClosure"])
        kin_cap = eff_cl + 2.4
        if k_burst > 4.0:
            kin_cap = min(kin_cap, 10.0 - min(4.5, (k_burst - 2.0) * 0.35))
        if has_rev_post:
            kin_cap = min(kin_cap, 5.2)
        def_sc = min(raw_avg, max(3.5, kin_cap))
        mf_sc = float(tf.get("motionFidelityScore", 10.0))
        if tf.get("motionStarved"):
            def_sc = min(def_sc, min(5.8, mf_sc + 0.3))
        elif mf_sc < 8.0:
            def_sc = min(def_sc, mf_sc + 0.6)
        tk["ouroborosKtcsScore"] = o_sc
        tk["independentKltScore"] = i_sc
        tk["definitiveLoopScore"] = round(def_sc, 2)

    if not dds_rt["withinBudget"]:
        post_report["issues"].append("Sequência DDS excede o orçamento RAF canônico.")
    if not dds_rt["validHeaders"]:
        post_report["issues"].append("Cabeçalho DDS DXT1 inválido.")
    if not dds_rt.get("alphaSafe", True):
        post_report["issues"].append("Bloco DXT1a gerou pixel transparente indevido em quadro opaco.")
    if dds_rt["bandingRisk"]:
        post_report["issues"].append("Risco de banding/posterização após codificação DXT1a.")
    post_report["passed"] = len(post_report["issues"]) == 0
    post_report["evaluatedOn"] = "decoded_dds_native_resolution"
    if return_blobs:
        return {"report": post_report, "blobs": blobs, "decoded": decoded_rgb}
    return post_report


def evaluate_ten_cycles(eval_report: dict) -> dict:
    """Audit continuous 10-cycle playback (no artificial pauses) from a circular post-DDS evaluation."""
    n = int(eval_report.get("frames") or 0)
    wait = float(eval_report.get("wait") or 0.1)
    cycle_sec = round(n * wait, 4)
    total_sec = round(cycle_sec * 10.0, 3)
    m = eval_report.get("metrics") or {}
    vs = m.get("visualSimilarity") or {}
    fc = m.get("flowContinuity") or {}
    vc = m.get("velocityContinuity") or {}
    rv = m.get("reversals") or {}
    fz = m.get("freezes") or {}
    fl = m.get("flicker") or {}
    gh = m.get("ghosting") or {}
    tf = m.get("temporalFidelity") or {}

    sr = m.get("spatialRegions") or {}
    seam_hs = sr.get("seamHotspot") or {}
    static_r = sr.get("staticRegions") or {}
    moving_r = sr.get("movingRegions") or {}

    dir_ok = (not fc.get("directionMeasured")) or (
        float(fc.get("seamDirectionCosine", 1.0)) >= 0.40 and float(fc.get("seamOppositeFraction", 0.0)) <= 0.28
    )
    hotspot_visible = float(seam_hs.get("localSeamExcess", vs.get("localSeamExcess", 0.0))) > 1.8
    seam_visible = bool(
        float(vs.get("seamRatio", 1.0)) > 1.75
        or hotspot_visible
        or (fc.get("directionMeasured") and not dir_ok)
        or float(vc.get("relativeJump", 0.0)) > 0.55
    )
    motion_ok = not bool(tf.get("motionStarved", False))
    rep_per_min = round(60.0 / max(cycle_sec, 0.05), 1)
    repetition_risk = "alta" if cycle_sec < 0.9 else ("moderada" if cycle_sec < 1.6 else "baixa")
    continuous_ok = bool(
        eval_report.get("passed")
        and not seam_visible
        and motion_ok
        and not rv.get("detected")
        and not fz.get("detected")
        and not fl.get("artificialFlicker")
        and not gh.get("detected")
    )
    ten_axis = {
        "passed": continuous_ok,
        "cycles": 10,
        "tenCycleDurationSec": total_sec,
        "seamVisibleRisk": seam_visible,
        "directionalContinuityOk": dir_ok,
        "motionPreservationOk": motion_ok,
    }
    if isinstance(m.get("independentAxes"), dict):
        m["independentAxes"]["tenCycleIntegrity"] = ten_axis
    return {
        "cycles": 10,
        "framesPerCycle": n,
        "totalFramesPlayed": n * 10,
        "wrapTransitions": 10,
        "cycleDurationSec": cycle_sec,
        "tenCycleDurationSec": total_sec,
        "repetitionsPerMinute": rep_per_min,
        "repetitionRisk": repetition_risk,
        "seamVisibleRisk": seam_visible,
        "directionalContinuityOk": dir_ok,
        "motionPreservationOk": motion_ok,
        "continuousTenCyclePass": continuous_ok,
        "spatialAudit": {
            "wholeFrame": sr.get("wholeFrame"),
            "staticRegions": static_r,
            "movingRegions": moving_r,
            "protectedObjects": sr.get("protectedObjects"),
            "boundaries": sr.get("boundaries"),
            "seamHotspot": seam_hs,
            "hotspotVisibleRisk": hotspot_visible,
        },
    }


def classify_artifact_provenance(
    final_issues: list[str],
    raw_issues: list[str] | set[str],
    dds_delta: dict | None = None,
    *,
    strategy_used: str | None = None,
) -> dict:
    """Intervention 18: Separate original source defects vs reconstruction defects vs DXT1a compression defects."""
    raw_set = set(raw_issues or ())
    original_defects: list[str] = []
    reconstruction_defects: list[str] = []
    compression_defects: list[str] = []

    comp_markers = ("DXT1a", "banding", "posterização", "orçamento RAF", "Cabeçalho DDS", "shimmer")
    for iss in final_issues or []:
        if any(tok in iss for tok in comp_markers):
            compression_defects.append(iss)
        elif iss in raw_set:
            original_defects.append(iss)
        elif strategy_used in {"natural", "level1_natural_cut", "level1_natural"}:
            original_defects.append(iss)
        else:
            reconstruction_defects.append(iss)

    return {
        "originalDefects": original_defects,
        "reconstructionDefects": reconstruction_defects,
        "compressionDefects": compression_defects,
        "bandingPreDds": (dds_delta or {}).get("bandingPreDds"),
        "bandingPostDds": (dds_delta or {}).get("bandingPostDds"),
        "bandingIncrease": (dds_delta or {}).get("bandingIncrease"),
    }


def compare_before_after(before: dict, after: dict) -> dict:
    """Scientific before/after comparison matrix. Rejects refinements that worsen perceptual, directional, or motion fidelity."""
    bm = before["metrics"]
    am = after["metrics"]

    b_spd = float(bm["velocityContinuity"]["internalMedianSpeedPx"])
    a_spd = float(am["velocityContinuity"]["internalMedianSpeedPx"])
    motion_retention = float(a_spd / max(b_spd, 0.15)) if b_spd >= 0.25 else 1.0

    b_tf = bm.get("temporalFidelity") or {}
    a_tf = am.get("temporalFidelity") or {}
    b_tstd = float(b_tf.get("temporalStdMean", 0.0))
    a_tstd = float(a_tf.get("temporalStdMean", b_tstd))
    excursion_retention = float(a_tstd / max(b_tstd, 1.0)) if b_tstd >= 2.0 else 1.0

    b_area = float(b_tf.get("activeAreaFraction", 0.0))
    a_area = float(a_tf.get("activeAreaFraction", b_area))
    active_area_retention = float(a_area / max(b_area, 0.04)) if b_area >= 0.06 else 1.0

    b_fp95 = float(b_tf.get("flowP95Norm180", 0.0))
    a_fp95 = float(a_tf.get("flowP95Norm180", b_fp95))
    flow_p95_retention = float(a_fp95 / max(b_fp95, 0.25)) if b_fp95 >= 0.45 else 1.0

    a_subj_ret = float(a_tf.get("rawToOutputActiveSubjectRetention", 1.0))

    b_tk = bm.get("trajectoryKinematics") or {}
    a_tk = am.get("trajectoryKinematics") or {}

    comparisons = {
        "seamMae": (bm["visualSimilarity"]["seamMae"], am["visualSimilarity"]["seamMae"], "lower"),
        "seamSsim": (bm["visualSimilarity"]["seamSsim"], am["visualSimilarity"]["seamSsim"], "higher"),
        "seamFlowErrorPx": (bm["flowContinuity"]["seamFlowErrorPx"], am["flowContinuity"]["seamFlowErrorPx"], "lower"),
        "seamDirectionCosine": (bm["flowContinuity"]["seamDirectionCosine"], am["flowContinuity"]["seamDirectionCosine"], "higher"),
        "seamOppositeFraction": (bm["flowContinuity"]["seamOppositeFraction"], am["flowContinuity"]["seamOppositeFraction"], "lower"),
        "velocityJump": (bm["velocityContinuity"]["relativeJump"], am["velocityContinuity"]["relativeJump"], "lower"),
        "edgeRatio": (bm["edgeConsistency"]["edgeRatio"], am["edgeConsistency"]["edgeRatio"], "lower"),
        "seamDeltaL": (abs(bm["luminanceContinuity"]["seamDeltaL"]), abs(am["luminanceContinuity"]["seamDeltaL"]), "lower"),
        "seamDeltaChroma": (bm["chromaticContinuity"]["seamDeltaChroma"], am["chromaticContinuity"]["seamDeltaChroma"], "lower"),
        "flickerScore": (bm["flicker"]["score"], am["flicker"]["score"], "lower"),
        "ghostingScore": (bm["ghosting"]["score"], am["ghosting"]["score"], "lower"),
        "sharpnessRetention": (bm["texturePreservation"]["sharpnessRetention"], am["texturePreservation"]["sharpnessRetention"], "higher"),
        "motionRetention": (1.0, round(min(motion_retention, 2.0), 4), "higher"),
        "excursionRetention": (1.0, round(min(excursion_retention, 2.0), 4), "higher"),
        "activeAreaRetention": (1.0, round(min(active_area_retention, 2.0), 4), "higher"),
        "definitiveLoopScore": (
            float(b_tk.get("definitiveLoopScore", 6.0)),
            float(a_tk.get("definitiveLoopScore", 6.0)),
            "higher",
        ),
    }

    matrix = {}
    improvements = []
    regressions = []
    for name, (b_val, a_val, better) in comparisons.items():
        delta = round(a_val - b_val, 4)
        improved = (delta < -1e-4) if better == "lower" else (delta > 1e-4)
        worsened = (delta > 1e-4) if better == "lower" else (delta < -1e-4)
        matrix[name] = {
            "before": round(b_val, 4),
            "after": round(a_val, 4),
            "delta": delta,
            "better": better,
            "status": "improved" if improved else ("regressed" if worsened else "unchanged"),
        }
        if improved:
            improvements.append(name)
        elif worsened:
            regressions.append(name)

    # Hard rejection triggers: never accept reversals, freezes, ghosting, blur, protected drift,
    # directional degradation, intra-cycle phase-handoff jumps, or artificial motion/excursion dampening!
    hard_reject = []
    if am["reversals"]["detected"] and not bm["reversals"]["detected"]:
        hard_reject.append("Introduziu reversão artificial.")
    if am["freezes"]["detected"] and not bm["freezes"]["detected"]:
        hard_reject.append("Introduziu congelamento/duplicação.")
    b_ndup = int(b_tf.get("nearDuplicatePairs", 0))
    a_ndup = int(a_tf.get("nearDuplicatePairs", 0))
    if a_ndup > b_ndup and a_ndup >= 1:
        hard_reject.append(f"Introduziu pares de quadros quase congelados ({b_ndup} -> {a_ndup}).")
    if am["ghosting"]["detected"] and not bm["ghosting"]["detected"]:
        hard_reject.append("Introduziu ghosting perceptível.")
    if a_tk.get("intraCycleJumpDetected") and not b_tk.get("intraCycleJumpDetected"):
        hard_reject.append("Introduziu salto intra-ciclo de trajetória/fase.")
    if am["texturePreservation"]["sharpnessRetention"] < 0.72 and (
        am["texturePreservation"]["sharpnessRetention"] < bm["texturePreservation"]["sharpnessRetention"] - 0.12
    ):
        hard_reject.append("Destruiu nitidez/textura para disfarçar a costura.")
    if am["staticIntegrity"]["checked"] and am["staticIntegrity"]["maxAbsDiff"] > bm["staticIntegrity"]["maxAbsDiff"] + 2.0:
        hard_reject.append("Corrompeu região estática protegida.")

    # Directional degradation gate (prevents improving static appearance while breaking motion direction!)
    if am["flowContinuity"]["directionMeasured"] and bm["flowContinuity"]["directionMeasured"]:
        b_cos = float(bm["flowContinuity"]["seamDirectionCosine"])
        a_cos = float(am["flowContinuity"]["seamDirectionCosine"])
        b_opp = float(bm["flowContinuity"]["seamOppositeFraction"])
        a_opp = float(am["flowContinuity"]["seamOppositeFraction"])
        if (a_cos < b_cos - 0.18 and a_cos < 0.50) or (a_opp > b_opp + 0.14 and a_opp > 0.25):
            hard_reject.append("Piorou a continuidade direcional do movimento na junção.")

    # Artificial motion-dampening gates (never shrink genuine movement or temporal excursion to fake seam smoothness!)
    if b_spd >= 0.45 and a_spd < b_spd * 0.62 and (bm["visualSimilarity"]["internalMedianMae"] >= 1.2):
        hard_reject.append("Reduziu artificialmente a amplitude de movimento para mascarar a costura.")
    if b_tstd >= 2.2 and excursion_retention < 0.72:
        hard_reject.append(
            f"Reduziu excessivamente a excursão temporal original ({excursion_retention * 100:.0f}% retido)."
        )
    if a_tf.get("checkedAgainstRawInterval") and b_tstd >= 2.2 and a_subj_ret < 0.68:
        hard_reject.append(
            f"Atenuou o movimento nas áreas ativas do sujeito ({a_subj_ret * 100:.0f}% retido)."
        )
    if b_area >= 0.08 and active_area_retention < 0.65:
        hard_reject.append(
            f"Encolheu a área visual ativa em movimento ({active_area_retention * 100:.0f}% retida)."
        )
    if b_fp95 >= 0.55 and flow_p95_retention < 0.62:
        hard_reject.append(
            f"Reduziu a velocidade de pico do fluxo óptico ({flow_p95_retention * 100:.0f}% retida)."
        )

    def _perceptual_cost(rep: dict) -> float:
        rm = rep["metrics"]
        fc = rm["flowContinuity"]
        tk_r = rm.get("trajectoryKinematics") or {}
        tf_r = rm.get("temporalFidelity") or {}
        dir_pen = (
            (1.0 - max(-1.0, min(1.0, float(fc["seamDirectionCosine"])))) * 1.15
            + float(fc["seamOppositeFraction"]) * 2.10
            if fc["directionMeasured"]
            else 0.0
        )
        traj_pen = (
            max(0.0, float(tk_r.get("kltMaxStepBurstRatio", 1.0)) - 1.5) * 0.35
            + max(0.0, float(tk_r.get("tailPoseReversalSeverity", 0.0)) - 0.8) * 0.22
            + max(0.0, float((rm["visualSimilarity"]).get("localSeamExcess", 0.0)) - 0.35) * 0.45
        )
        motion_pen = max(0.0, 9.0 - float(tf_r.get("motionFidelityScore", 10.0))) * 0.45
        return (
            float(rm["visualSimilarity"]["seamRatio"])
            + float(fc["flowErrorRatio"]) * 0.60
            + dir_pen
            + traj_pen
            + motion_pen
            + float(rm["velocityContinuity"]["relativeJump"]) * 0.85
            + max(0.0, float(rm["edgeConsistency"]["edgeRatio"]) - 1.0) * 0.60
            + float(rm["ghosting"]["score"]) * 2.00
            + float(rm["flicker"]["score"]) * 0.50
            + len(rep["issues"]) * 1.50
        )

    b_seam_cost = _perceptual_cost(before)
    a_seam_cost = _perceptual_cost(after)

    accepted = (not hard_reject) and (a_seam_cost <= b_seam_cost + 0.05)
    return {
        "accepted": accepted,
        "hardRejectReasons": hard_reject,
        "beforeCompositeCost": round(b_seam_cost, 4),
        "afterCompositeCost": round(a_seam_cost, 4),
        "improvements": improvements,
        "regressions": regressions,
        "matrix": matrix,
    }


def check_prohibited_artifacts(eval_report: dict, *, strategy_used: str | None = None) -> dict:
    """Explicitly check every prohibited loop artifact from Section 20.

    Prohibited approvals:
      - ping-pong / artificial reversal
      - disguised freeze / duplicate stall / frozen static scene
      - artificial elimination or severe damping of original motion
      - gross ghosting / destructive crossfade (strategy-aware — Intervention 06)
      - excessive blur / texture loss
      - protected-region deformation
      - abrupt position or velocity jump (including intra-cycle phase-handoff burst)
    """
    m = eval_report.get("metrics") or {}
    vs = m.get("visualSimilarity") or {}
    fc = m.get("flowContinuity") or {}
    vc = m.get("velocityContinuity") or {}
    gh = m.get("ghosting") or {}
    tp = m.get("texturePreservation") or {}
    st = m.get("staticIntegrity") or {}
    rv = m.get("reversals") or {}
    fz = m.get("freezes") or {}
    tk = m.get("trajectoryKinematics") or {}
    tf = m.get("temporalFidelity") or {}
    issues = list(eval_report.get("issues") or [])

    violations: list[str] = []
    if any("praticamente estática" in iss for iss in issues):
        violations.append("Cena congelada / praticamente estática (sem movimento real no ciclo).")
    if tf.get("intrinsicMotionStarved"):
        violations.append("Atividade temporal insuficiente (movimento quase imperceptível).")
    if tf.get("reconstructionSeverelyDamped"):
        violations.append(
            f"Reconstrução eliminou severamente o movimento original "
            f"(excursão retida {float(tf.get('rawToOutputExcursionRetention', 0.0)) * 100:.0f}%)."
        )
    if tf.get("sourceSeverelyStarved"):
        violations.append(
            f"Trecho sem fidelidade temporal frente à fonte "
            f"(excursão {float(tf.get('sourceExcursionRetention', 0.0)) * 100:.0f}% da mediana original)."
        )
    if rv.get("detected"):
        violations.append("Inversão artificial / efeito ping-pong detectado.")
    if fz.get("detected"):
        violations.append("Congelamento ou repetição disfarçada detectada.")
    if tk.get("intraCycleJumpDetected"):
        violations.append(
            f"Salto intra-ciclo de trajetória/fase detectado no passo {tk.get('kltWorstBurstStep')} "
            f"(KLT burst {float(tk.get('kltMaxStepBurstRatio', 0.0)):.1f}x)."
        )
    if gh.get("detected") or (float(gh.get("score", 0.0)) > 0.42 and float(gh.get("blendPeak", 0.0)) > 0.30):
        if strategy_used in {"natural", "level1_natural_cut", "level1_natural"}:
            violations.append("Ghosting / dupla exposição presente na mídia de origem.")
        else:
            violations.append("Ghosting grosseiro ou crossfade destrutivo na junção.")
    if float(tp.get("sharpnessRetention", 1.0)) < 0.70:
        violations.append("Perda excessiva de nitidez/textura.")
    if st.get("checked") and float(st.get("maxAbsDiff", 0.0)) > 6.0:
        violations.append("Deformação indevida em região estática protegida.")
    if fc.get("directionMeasured") and (
        float(fc.get("seamDirectionCosine", 1.0)) < 0.30 or float(fc.get("seamOppositeFraction", 0.0)) > 0.35
    ):
        violations.append("Inversão ou quebra severa da direção do movimento na junção.")
    if float(vs.get("seamRatio", 1.0)) > 2.2 or float(vc.get("relativeJump", 0.0)) > 0.85:
        violations.append("Salto abrupto de posição ou velocidade no fechamento do ciclo.")

    return {
        "clean": len(violations) == 0,
        "violations": violations,
    }


def classify_loop_verdict(
    eval_report: dict,
    ten_cycle: dict | None = None,
    *,
    strategy_used: str | None = None,
) -> dict:
    """Honest verdict classification: APROVADO, CONDICIONAL, or REPROVADO (never falsifies quality)."""
    prohibited = check_prohibited_artifacts(eval_report, strategy_used=strategy_used)
    issues = list(eval_report.get("issues") or [])
    m = eval_report.get("metrics") or {}
    dds_d = m.get("ddsDelta") or {}
    tf = m.get("temporalFidelity") or {}
    within_budget = bool(dds_d.get("withinBudget", True))
    distinct = int(dds_d.get("distinctFrames", eval_report.get("frames", 0)))
    ten_ok = bool(ten_cycle.get("continuousTenCyclePass", False)) if ten_cycle else bool(eval_report.get("passed"))

    if not within_budget or distinct < 2:
        return {
            "verdict": "REPROVADO",
            "approved": False,
            "summary": "Sequência inválida para PS4 (estática, congelada ou fora do orçamento de 6 MiB).",
            "prohibited": prohibited,
        }
    if not prohibited["clean"]:
        return {
            "verdict": "REPROVADO",
            "approved": False,
            "summary": prohibited["violations"][0],
            "prohibited": prohibited,
        }
    if eval_report.get("passed") and ten_ok and len(issues) == 0 and not tf.get("motionStarved"):
        return {
            "verdict": "APROVADO",
            "approved": True,
            "summary": "Loop contínuo aprovado após codificação DXT1a real, fidelidade temporal e auditoria de 10 ciclos.",
            "prohibited": prohibited,
        }
    return {
        "verdict": "CONDICIONAL",
        "approved": False,
        "summary": (
            f"Resultado condicional pós-DDS ({len(issues)} alerta(s)): "
            + (issues[0] if issues else "inspecione a junção e a fidelidade temporal antes de aplicar.")
        ),
        "prohibited": prohibited,
    }


def evaluate_raf_zip(raf_zip_path, *, protected_mask: np.ndarray | None = None) -> dict:
    """Independently decode an exported `background.raf.zip` (DXT1a DDS + plane.mdx + index.xml) and validate."""
    import re
    import zipfile
    from pathlib import Path

    p = Path(raf_zip_path)
    if not p.is_file():
        raise FileNotFoundError(f"Arquivo RAF ausente: {p}")

    with zipfile.ZipFile(p, "r") as zf:
        names = zf.namelist()
        xml_entries = [n for n in names if n.endswith("/index.xml") or n == "index.xml"]
        mdx_entries = [n for n in names if n.endswith("/plane.mdx") or n == "plane.mdx"]
        dds_entries = sorted(n for n in names if n.endswith(".dds"))
        if not xml_entries or not mdx_entries or not dds_entries:
            raise ValueError("RAF ZIP incompleto: exige index.xml, plane.mdx e arquivos .dds.")

        xml_text = zf.read(xml_entries[0]).decode("utf-8")
        m_wait = re.search(r'<case\s+wait="([0-9.]+)"', xml_text)
        wait_sec = float(m_wait.group(1)) if m_wait else 0.1
        dds_blobs = [zf.read(n) for n in dds_entries]

    total_bytes = sum(len(b) for b in dds_blobs)
    decoded_frames = []
    for blob in dds_blobs:
        if blob[:4] != b"DDS " or blob[84:88] != b"DXT1":
            raise ValueError("Bloco DDS dentro do RAF ZIP não possui cabeçalho DXT1 válido.")
        rgba = dxt1.decode_dds(blob)
        decoded_frames.append(rgba[..., :3])

    arr = np.stack(decoded_frames, axis=0)
    ev = evaluate_loop(arr, wait_sec, protected_mask=protected_mask)
    ten = evaluate_ten_cycles(ev)
    verdict = classify_loop_verdict(ev, ten)
    return {
        "evaluatedFromRafZip": True,
        "frames": len(dds_blobs),
        "size": [int(arr.shape[2]), int(arr.shape[1])],
        "wait": wait_sec,
        "ddsBytes": total_bytes,
        "withinBudget": total_bytes <= RAF_SIZE_LIMIT and len(dds_blobs) <= MAX_FRAME_COUNT,
        "evaluation": ev,
        "tenCycleAudit": ten,
        "verdict": verdict,
    }


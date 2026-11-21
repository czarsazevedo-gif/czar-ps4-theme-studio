"""Dense optical flow, forward-backward occlusion checks, subpixel phase shift, and camera motion."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from threading import Lock

import cv2
import numpy as np


_FLOW_CACHE_MAX = 256
_FLOW_CACHE: OrderedDict[tuple, "FlowResult"] = OrderedDict()
_FLOW_CACHE_LOCK = Lock()


def clear_flow_cache() -> None:
    """Clear the bounded in-memory optical flow LRU cache."""
    with _FLOW_CACHE_LOCK:
        _FLOW_CACHE.clear()


def _frame_digest(gray: np.ndarray) -> bytes:
    """Fast collision-resistant key for a uint8 grayscale frame."""
    h = hashlib.blake2b(digest_size=16)
    h.update(gray.shape[0].to_bytes(4, "little"))
    h.update(gray.shape[1].to_bytes(4, "little"))
    h.update(memoryview(np.ascontiguousarray(gray)))
    return h.digest()


@dataclass(frozen=True)
class FlowResult:
    """Bidirectional dense optical flow with occlusion and multi-factor confidence estimates."""

    fwd: np.ndarray        # (H, W, 2) float32, dx, dy from a -> b
    bwd: np.ndarray        # (H, W, 2) float32, dx, dy from b -> a
    fb_error: np.ndarray   # (H, W) float32, forward-backward inconsistency in px
    occluded: np.ndarray   # (H, W) bool, True where correspondence is unreliable/occluded
    confidence: np.ndarray # (H, W) float32 in [0, 1]


def to_gray_u8(frame: np.ndarray) -> np.ndarray:
    """Convert RGB or RGBA uint8 frame to single-channel luminance uint8."""
    if frame.ndim == 2:
        return frame.astype(np.uint8, copy=False)
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_RGBA2GRAY)
    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)


def compute_flow_pair(
    gray_a: np.ndarray,
    gray_b: np.ndarray,
    *,
    method: str = "farneback",
) -> np.ndarray:
    """Compute dense optical flow (H, W, 2) float32 from gray_a to gray_b."""
    a = gray_a.astype(np.uint8, copy=False)
    b = gray_b.astype(np.uint8, copy=False)
    if method == "dis" and hasattr(cv2, "DISOpticalFlow_create"):
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        return dis.calc(a, b, None).astype(np.float32, copy=False)
    if method == "dis_fast" and hasattr(cv2, "DISOpticalFlow_create"):
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        return dis.calc(a, b, None).astype(np.float32, copy=False)
    return cv2.calcOpticalFlowFarneback(
        a,
        b,
        None,
        pyr_scale=0.5,
        levels=4,
        winsize=15,
        iterations=4,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    ).astype(np.float32, copy=False)


def _sample_flow(flow: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinearly sample a 2-channel flow field at floating-point coordinates (x, y)."""
    mx = x.astype(np.float32, copy=False)
    my = y.astype(np.float32, copy=False)
    u = cv2.remap(flow[..., 0], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    v = cv2.remap(flow[..., 1], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.stack([u, v], axis=-1)


def compute_bidirectional_flow(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    *,
    method: str = "farneback",
    fb_alpha: float = 0.05,
    fb_beta: float = 0.75,
    multi_factor: bool = True,
) -> FlowResult:
    """Compute forward & backward dense flow with Sundaram occlusion and Intervention 15 multi-factor confidence.

    Combines:
      1. Forward-backward consistency error
      2. Local Sobel texture strength (penalizes flat untextured regions)
      3. Post-warp photometric residual |I_b(x + u) - I_a(x)|
      4. Multi-estimator agreement (DIS vs Farnebäck) when OpenCV DIS is available
    """
    ga = to_gray_u8(frame_a)
    gb = to_gray_u8(frame_b)
    cache_key = (_frame_digest(ga), _frame_digest(gb), method, round(fb_alpha, 4), round(fb_beta, 4), multi_factor)
    with _FLOW_CACHE_LOCK:
        hit = _FLOW_CACHE.get(cache_key)
        if hit is not None:
            _FLOW_CACHE.move_to_end(cache_key)
            return hit

    h, w = ga.shape
    fwd = compute_flow_pair(ga, gb, method=method)
    bwd = compute_flow_pair(gb, ga, method=method)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    tx = xx + fwd[..., 0]
    ty = yy + fwd[..., 1]
    out_of_bounds = (tx < 0) | (tx > w - 1) | (ty < 0) | (ty > h - 1)

    tx_c = np.clip(tx, 0, w - 1).astype(np.float32, copy=False)
    ty_c = np.clip(ty, 0, h - 1).astype(np.float32, copy=False)
    bwd_at_fwd = _sample_flow(bwd, tx_c, ty_c)
    fb_vec = fwd + bwd_at_fwd
    fb_err = np.hypot(fb_vec[..., 0], fb_vec[..., 1]).astype(np.float32)

    mag_f = np.hypot(fwd[..., 0], fwd[..., 1])
    mag_b = np.hypot(bwd_at_fwd[..., 0], bwd_at_fwd[..., 1])
    # Sundaram et al. forward-backward consistency threshold
    thresh = fb_alpha * (mag_f + mag_b) + fb_beta
    occluded = out_of_bounds | (fb_err > thresh)

    # 1. Local texture gradient strength (flat areas cannot yield reliable optical flow)
    gx = cv2.Sobel(ga, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(ga, cv2.CV_32F, 0, 1, ksize=3)
    grad = np.hypot(gx, gy)
    grad_smooth = cv2.GaussianBlur(grad, (5, 5), 0)
    tex_conf = np.clip(grad_smooth / 22.0, 0.0, 1.0)

    # 2. Forward-backward consistency confidence
    fb_conf = np.exp(-fb_err / np.maximum(thresh, 0.5))

    if multi_factor:
        # 3. Post-warp photometric residual |I_b(x + u) - I_a(x)|
        warped_gb = cv2.remap(gb.astype(np.float32), tx_c, ty_c, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        photo_err = np.abs(warped_gb - ga.astype(np.float32))
        photo_err_s = cv2.GaussianBlur(photo_err, (3, 3), 0)
        photo_conf = np.exp(-photo_err_s / 24.0)

        # 4. Multi-estimator agreement (DIS vs Farnebäck) when DIS is available
        agree_conf = 1.0
        if method == "farneback" and hasattr(cv2, "DISOpticalFlow_create") and min(h, w) >= 24:
            fwd_dis = compute_flow_pair(ga, gb, method="dis_fast")
            agree_err = np.hypot(fwd[..., 0] - fwd_dis[..., 0], fwd[..., 1] - fwd_dis[..., 1])
            agree_scale = np.maximum(1.5, 0.45 * mag_f + 1.0)
            agree_conf = np.exp(-agree_err / agree_scale)

        confidence = (
            fb_conf
            * (0.28 + 0.72 * tex_conf)
            * (0.45 + 0.55 * photo_conf)
            * (0.55 + 0.45 * agree_conf)
        ).astype(np.float32)
    else:
        confidence = (fb_conf * (0.25 + 0.75 * tex_conf)).astype(np.float32)

    confidence[out_of_bounds] = 0.0
    confidence[occluded] *= 0.25

    res = FlowResult(
        fwd=fwd,
        bwd=bwd,
        fb_error=fb_err,
        occluded=occluded,
        confidence=confidence,
    )
    with _FLOW_CACHE_LOCK:
        _FLOW_CACHE[cache_key] = res
        while len(_FLOW_CACHE) > _FLOW_CACHE_MAX:
            _FLOW_CACHE.popitem(last=False)
    return res


def compute_flow_capped(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    *,
    max_width: int = 384,
    method: str = "farneback",
    fb_alpha: float = 0.05,
    fb_beta: float = 0.75,
) -> FlowResult:
    """Compute bidirectional flow at <= `max_width` and smoothly upsample to native pixel units."""
    h, w = frame_a.shape[:2]
    if w <= max_width:
        return compute_bidirectional_flow(frame_a, frame_b, method=method, fb_alpha=fb_alpha, fb_beta=fb_beta)
    scale = max_width / float(w)
    nw = max(32, int(round(w * scale / 2)) * 2)
    nh = max(32, int(round(h * scale / 2)) * 2)
    sa = cv2.resize(frame_a, (nw, nh), interpolation=cv2.INTER_AREA)
    sb = cv2.resize(frame_b, (nw, nh), interpolation=cv2.INTER_AREA)
    fr = compute_bidirectional_flow(sa, sb, method=method, fb_alpha=fb_alpha, fb_beta=fb_beta)
    sx = w / float(nw)
    sy = h / float(nh)
    fwd = np.stack(
        [
            cv2.resize(fr.fwd[..., 0], (w, h), interpolation=cv2.INTER_LINEAR) * sx,
            cv2.resize(fr.fwd[..., 1], (w, h), interpolation=cv2.INTER_LINEAR) * sy,
        ],
        axis=-1,
    ).astype(np.float32)
    bwd = np.stack(
        [
            cv2.resize(fr.bwd[..., 0], (w, h), interpolation=cv2.INTER_LINEAR) * sx,
            cv2.resize(fr.bwd[..., 1], (w, h), interpolation=cv2.INTER_LINEAR) * sy,
        ],
        axis=-1,
    ).astype(np.float32)
    fb_err = cv2.resize(fr.fb_error, (w, h), interpolation=cv2.INTER_LINEAR) * float(0.5 * (sx + sy))
    conf = np.clip(cv2.resize(fr.confidence, (w, h), interpolation=cv2.INTER_LINEAR), 0.0, 1.0).astype(np.float32)
    occ = cv2.resize(fr.occluded.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
    return FlowResult(fwd=fwd, bwd=bwd, fb_error=fb_err.astype(np.float32), occluded=occ, confidence=conf)


def estimate_subpixel_shift(frame_a: np.ndarray, frame_b: np.ndarray) -> tuple[float, float, float]:
    """Estimate global subpixel translation (dx, dy) and response using Hann-windowed phase correlation."""
    ga = to_gray_u8(frame_a).astype(np.float32)
    gb = to_gray_u8(frame_b).astype(np.float32)
    h, w = ga.shape
    hann = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(ga, gb, hann)
    # phaseCorrelate returns shift needed to align a to b; verify sign convention:
    # if b is shifted right by +dx relative to a, cv2.phaseCorrelate(a, b) returns (+dx, +dy).
    return float(dx), float(dy), float(response)


def estimate_camera_motion(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    flow: np.ndarray | None = None,
    confidence: np.ndarray | None = None,
) -> dict:
    """Estimate dominant global camera affine transform via RANSAC on reliable flow or LK points."""
    ga = to_gray_u8(frame_a)
    gb = to_gray_u8(frame_b)
    h, w = ga.shape

    pts_a = cv2.goodFeaturesToTrack(ga, maxCorners=200, qualityLevel=0.02, minDistance=6, blockSize=5)
    inlier_ratio = 0.0
    matrix = None
    inlier_quadrants = 0
    if pts_a is not None and len(pts_a) >= 12:
        pts_b, st, _ = cv2.calcOpticalFlowPyrLK(ga, gb, pts_a, None, winSize=(15, 15), maxLevel=3)
        good = (st.ravel() == 1) if st is not None else np.zeros(len(pts_a), bool)
        if good.sum() >= 8:
            p0 = pts_a[good].reshape(-1, 2)
            p1 = pts_b[good].reshape(-1, 2)
            # Forward-backward LK check
            pts_back, st_back, _ = cv2.calcOpticalFlowPyrLK(gb, ga, p1.reshape(-1, 1, 2), None, winSize=(15, 15), maxLevel=3)
            if st_back is not None:
                fb = np.linalg.norm(p0 - pts_back.reshape(-1, 2), axis=1)
                keep = (st_back.ravel() == 1) & (fb < 1.2)
                p0, p1 = p0[keep], p1[keep]
            if len(p0) >= 6:
                matrix, inliers = cv2.estimateAffinePartial2D(
                    p0, p1, method=cv2.RANSAC, ransacReprojThreshold=1.5, maxIters=500
                )
                if matrix is not None and inliers is not None:
                    inlier_ratio = float(inliers.mean())
                    inl_pts = p0[inliers.ravel().astype(bool)]
                    if len(inl_pts):
                        q_set = {(int(pt[0] >= w * 0.5), int(pt[1] >= h * 0.5)) for pt in inl_pts}
                        inlier_quadrants = len(q_set)

    # Fallback to flow-grid RANSAC when sparse corners are scarce (e.g., smooth fog/water)
    if matrix is None and flow is not None:
        step = max(4, min(h, w) // 16)
        ys = np.arange(step // 2, h, step)
        xs = np.arange(step // 2, w, step)
        gx, gy = np.meshgrid(xs, ys)
        p0 = np.stack([gx.ravel(), gy.ravel()], axis=-1).astype(np.float32)
        fv = flow[gy.ravel(), gx.ravel()]
        if confidence is not None:
            cf = confidence[gy.ravel(), gx.ravel()]
            keep = cf > 0.35
            p0, fv = p0[keep], fv[keep]
        if len(p0) >= 6:
            p1 = p0 + fv
            matrix, inliers = cv2.estimateAffinePartial2D(
                p0, p1, method=cv2.RANSAC, ransacReprojThreshold=1.5, maxIters=400
            )
            if matrix is not None and inliers is not None:
                inlier_ratio = float(inliers.mean())
                inl_pts = p0[inliers.ravel().astype(bool)]
                if len(inl_pts):
                    q_set = {(int(pt[0] >= w * 0.5), int(pt[1] >= h * 0.5)) for pt in inl_pts}
                    inlier_quadrants = len(q_set)

    if matrix is None:
        return {
            "tx": 0.0,
            "ty": 0.0,
            "rotationDeg": 0.0,
            "scale": 1.0,
            "inlierRatio": 0.0,
            "inlierQuadrants": 0,
            "cameraMoving": False,
            "matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        }

    a00, a01, tx = float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[0, 2])
    a10, a11, ty = float(matrix[1, 0]), float(matrix[1, 1]), float(matrix[1, 2])
    scale = float(np.hypot(a00, a10))
    rot_deg = float(np.degrees(np.arctan2(a10, a00)))
    # Evaluate displacement at frame center so rotation around origin doesn't inflate tx/ty
    cx, cy = w * 0.5, h * 0.5
    center_dx = (a00 * cx + a01 * cy + tx) - cx
    center_dy = (a10 * cx + a11 * cy + ty) - cy
    disp = float(np.hypot(center_dx, center_dy))
    # Support parallax-rich travelling cameras (inliers >= 0.42 across >= 3 quadrants with clear displacement)
    moving = bool(
        (inlier_ratio >= 0.55 and (disp > 0.35 or abs(rot_deg) > 0.15 or abs(scale - 1.0) > 0.004))
        or (inlier_ratio >= 0.42 and inlier_quadrants >= 3 and (disp > 0.65 or abs(rot_deg) > 0.25 or abs(scale - 1.0) > 0.007))
    )
    return {
        "tx": round(center_dx, 4),
        "ty": round(center_dy, 4),
        "rotationDeg": round(rot_deg, 4),
        "scale": round(scale, 5),
        "inlierRatio": round(inlier_ratio, 3),
        "inlierQuadrants": int(inlier_quadrants),
        "cameraMoving": moving,
        "matrix": [[round(a00, 6), round(a01, 6), round(tx, 4)], [round(a10, 6), round(a11, 6), round(ty, 4)]],
    }


def flow_kinematics(flow: np.ndarray, mask: np.ndarray | None = None) -> dict:
    """Compute directional, speed, divergence, and curl statistics of a dense flow field."""
    u = flow[..., 0].astype(np.float32, copy=False)
    v = flow[..., 1].astype(np.float32, copy=False)
    mag = np.hypot(u, v)

    du_dx = cv2.Sobel(u, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    dv_dy = cv2.Sobel(v, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    dv_dx = cv2.Sobel(v, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    du_dy = cv2.Sobel(u, cv2.CV_32F, 0, 1, ksize=3) / 8.0

    divergence = du_dx + dv_dy
    curl = dv_dx - du_dy
    # Jacobian determinant of warp (1 + du/dx)(1 + dv/dy) - (du/dy)(dv/dx): < 0 indicates foldover
    jacobian = (1.0 + du_dx) * (1.0 + dv_dy) - du_dy * dv_dx

    if mask is not None and np.any(mask):
        sel = mask.astype(bool)
        u_s, v_s, m_s = u[sel], v[sel], mag[sel]
        div_s, curl_s, jac_s = divergence[sel], curl[sel], jacobian[sel]
    else:
        u_s, v_s, m_s = u.ravel(), v.ravel(), mag.ravel()
        div_s, curl_s, jac_s = divergence.ravel(), curl.ravel(), jacobian.ravel()

    sum_u = float(u_s.sum())
    sum_v = float(v_s.sum())
    sum_m = float(m_s.sum())
    coherence = float(np.hypot(sum_u, sum_v) / max(sum_m, 1e-6))
    mean_speed = float(m_s.mean()) if m_s.size else 0.0
    p90_speed = float(np.percentile(m_s, 90)) if m_s.size else 0.0
    angle_deg = float(np.degrees(np.arctan2(sum_v, sum_u))) if sum_m > 1e-5 else 0.0

    return {
        "meanSpeed": round(mean_speed, 4),
        "p90Speed": round(p90_speed, 4),
        "meanVec": [round(float(u_s.mean()) if u_s.size else 0.0, 4), round(float(v_s.mean()) if v_s.size else 0.0, 4)],
        "angleDeg": round(angle_deg, 2),
        "coherence": round(coherence, 4),
        "meanDivergence": round(float(np.abs(div_s).mean()) if div_s.size else 0.0, 5),
        "meanCurl": round(float(np.abs(curl_s).mean()) if curl_s.size else 0.0, 5),
        "foldoverFraction": round(float((jac_s < 0.0).mean()) if jac_s.size else 0.0, 5),
    }


def warp_frame(frame: np.ndarray, flow: np.ndarray, *, border_mode: int = cv2.BORDER_REPLICATE) -> np.ndarray:
    """Backward-warp `frame` by displacement field `flow` (sample at x - dx, y - dy)."""
    h, w = frame.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    map_x = xx - flow[..., 0].astype(np.float32, copy=False)
    map_y = yy - flow[..., 1].astype(np.float32, copy=False)
    return cv2.remap(frame, map_x, map_y, interpolation=cv2.INTER_CUBIC, borderMode=border_mode)

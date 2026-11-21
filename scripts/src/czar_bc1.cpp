#include <cstdint>
#include <cstring>
#include <cmath>
#include <algorithm>

#define RGBCX_IMPLEMENTATION
#include "rgbcx.h"

#define ICBC_IMPLEMENTATION
#define ICBC_SIMD 2
#include "icbc.h"

static bool g_inited = false;
static void ensure_init() {
    if (!g_inited) {
        rgbcx::init(rgbcx::bc1_approx_mode::cBC1Ideal);
        icbc::init(icbc::Decoder_D3D10);
        g_inited = true;
    }
}

static inline bool has_dxt1a_transparent_index(const uint8_t* blk) {
    uint16_t c0, c1;
    uint32_t bits;
    std::memcpy(&c0, blk, 2);
    std::memcpy(&c1, blk + 2, 2);
    if (c0 > c1) return false;
    std::memcpy(&bits, blk + 4, 4);
    for (int i = 0; i < 16; ++i) {
        if (((bits >> (2 * i)) & 3u) == 3u) return true;
    }
    return false;
}

static inline void decode_bc1_block_rgb(const uint8_t* blk, uint8_t out_rgb[16 * 3]) {
    uint16_t c0, c1;
    uint32_t bits;
    std::memcpy(&c0, blk, 2);
    std::memcpy(&c1, blk + 2, 2);
    std::memcpy(&bits, blk + 4, 4);
    auto un565 = [](uint16_t c, int& r, int& g, int& b) {
        int r5 = (c >> 11) & 31, g6 = (c >> 5) & 63, b5 = c & 31;
        r = (r5 << 3) | (r5 >> 2);
        g = (g6 << 2) | (g6 >> 4);
        b = (b5 << 3) | (b5 >> 2);
    };
    int p[4][3];
    un565(c0, p[0][0], p[0][1], p[0][2]);
    un565(c1, p[1][0], p[1][1], p[1][2]);
    if (c0 > c1) {
        for (int ch = 0; ch < 3; ++ch) {
            p[2][ch] = (2 * p[0][ch] + p[1][ch] + 1) / 3;
            p[3][ch] = (p[0][ch] + 2 * p[1][ch] + 1) / 3;
        }
    } else {
        for (int ch = 0; ch < 3; ++ch) {
            p[2][ch] = (p[0][ch] + p[1][ch]) / 2;
            p[3][ch] = 0;
        }
    }
    for (int i = 0; i < 16; ++i) {
        int idx = (bits >> (2 * i)) & 3;
        out_rgb[i * 3 + 0] = uint8_t(p[idx][0]);
        out_rgb[i * 3 + 1] = uint8_t(p[idx][1]);
        out_rgb[i * 3 + 2] = uint8_t(p[idx][2]);
    }
}

static inline float block_rgb_sse(const uint8_t* rgba, const uint8_t* dec_rgb, bool perceptual) {
    const float wr = perceptual ? 0.30f : 1.0f;
    const float wg = perceptual ? 0.59f : 1.0f;
    const float wb = perceptual ? 0.11f : 1.0f;
    float s = 0.0f;
    for (int i = 0; i < 16; ++i) {
        float dr = float(rgba[i * 4 + 0]) - float(dec_rgb[i * 3 + 0]);
        float dg = float(rgba[i * 4 + 1]) - float(dec_rgb[i * 3 + 1]);
        float db = float(rgba[i * 4 + 2]) - float(dec_rgb[i * 3 + 2]);
        s += wr * dr * dr + wg * dg * dg + wb * db * db;
    }
    return s;
}

extern "C" {

// Encode RGBA frame into BC1/DXT1a blocks.
// backend: 0 = rgbcx, 1 = icbc
// temporal_prev_blocks: optional previous-frame BC1 blocks (same size) for Mode A/B temporal block optimization
// temporal_mode: 0 = off, 1 = Mode A (safe static reuse), 2 = Mode A + Mode B (endpoint/block cost J = alpha*err + beta*flicker)
void encode_frame_bc1_ex(
    const uint8_t* rgba,
    int width,
    int height,
    int backend,
    int quality,
    int perceptual_weights,
    const uint8_t* prev_rgba,
    const uint8_t* prev_blocks,
    int temporal_mode,
    float static_mae_thresh,
    float beta_flicker,
    uint8_t* out_blocks,
    int* reused_blocks_out
) {
    ensure_init();
    const int bx_count = width / 4;
    const int by_count = height / 4;
    const float w_uniform[3] = {1.0f, 1.0f, 1.0f};
    const float w_percept[3] = {0.55f, 0.77f, 0.34f};
    const float* cw = perceptual_weights ? w_percept : w_uniform;
    int reused = 0;

    for (int by = 0; by < by_count; ++by) {
        for (int bx = 0; bx < bx_count; ++bx) {
            uint8_t block_rgba[64];
            bool has_alpha = false;
            int opaque_count = 0;
            for (int py = 0; py < 4; ++py) {
                const uint8_t* src_row = rgba + ((by * 4 + py) * width + bx * 4) * 4;
                uint8_t* dst_row = block_rgba + py * 16;
                std::memcpy(dst_row, src_row, 16);
                for (int px = 0; px < 4; ++px) {
                    if (dst_row[px * 4 + 3] < 128) has_alpha = true;
                    else opaque_count++;
                }
            }
            const int bidx = by * bx_count + bx;
            uint8_t* dst = out_blocks + bidx * 8;

            // Mode A: Safe temporal reuse of static BC1 blocks (never freezes real micro-motion!)
            if (!has_alpha && temporal_mode >= 1 && prev_rgba != nullptr && prev_blocks != nullptr) {
                float sum_abs = 0.0f;
                int max_abs = 0;
                for (int py = 0; py < 4; ++py) {
                    const uint8_t* prow = prev_rgba + ((by * 4 + py) * width + bx * 4) * 4;
                    const uint8_t* crow = block_rgba + py * 16;
                    for (int px = 0; px < 4; ++px) {
                        for (int ch = 0; ch < 3; ++ch) {
                            int d = std::abs(int(crow[px * 4 + ch]) - int(prow[px * 4 + ch]));
                            sum_abs += float(d);
                            if (d > max_abs) max_abs = d;
                        }
                    }
                }
                float mae = sum_abs / 48.0f;
                if (mae <= static_mae_thresh && max_abs <= 3) {
                    const uint8_t* pblk = prev_blocks + bidx * 8;
                    if (!has_dxt1a_transparent_index(pblk)) {
                        std::memcpy(dst, pblk, 8);
                        reused++;
                        continue;
                    }
                }
            }

            if (has_alpha) {
                if (opaque_count == 0) {
                    uint16_t c0 = 0, c1 = 0;
                    uint32_t idx = 0xFFFFFFFFu;
                    std::memcpy(dst, &c0, 2);
                    std::memcpy(dst + 2, &c1, 2);
                    std::memcpy(dst + 4, &idx, 4);
                    continue;
                }
                uint8_t minr = 255, ming = 255, minb = 255, maxr = 0, maxg = 0, maxb = 0;
                for (int i = 0; i < 16; ++i) {
                    if (block_rgba[i * 4 + 3] >= 128) {
                        minr = std::min(minr, block_rgba[i * 4 + 0]);
                        ming = std::min(ming, block_rgba[i * 4 + 1]);
                        minb = std::min(minb, block_rgba[i * 4 + 2]);
                        maxr = std::max(maxr, block_rgba[i * 4 + 0]);
                        maxg = std::max(maxg, block_rgba[i * 4 + 1]);
                        maxb = std::max(maxb, block_rgba[i * 4 + 2]);
                    }
                }
                auto to565 = [](uint8_t r, uint8_t g, uint8_t b) -> uint16_t {
                    return uint16_t(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                };
                uint16_t c0 = to565(minr, ming, minb);
                uint16_t c1 = to565(maxr, maxg, maxb);
                if (c0 > c1) std::swap(c0, c1);
                auto un565 = [](uint16_t c, int& r, int& g, int& b) {
                    int r5 = (c >> 11) & 31, g6 = (c >> 5) & 63, b5 = c & 31;
                    r = (r5 << 3) | (r5 >> 2);
                    g = (g6 << 2) | (g6 >> 4);
                    b = (b5 << 3) | (b5 >> 2);
                };
                int pal[3][3];
                un565(c0, pal[0][0], pal[0][1], pal[0][2]);
                un565(c1, pal[1][0], pal[1][1], pal[1][2]);
                for (int ch = 0; ch < 3; ++ch) pal[2][ch] = (pal[0][ch] + pal[1][ch]) / 2;
                uint32_t bits = 0;
                for (int i = 0; i < 16; ++i) {
                    uint32_t best = 3;
                    if (block_rgba[i * 4 + 3] >= 128) {
                        int best_d = 1 << 30;
                        for (int p = 0; p < 3; ++p) {
                            int dr = int(block_rgba[i * 4 + 0]) - pal[p][0];
                            int dg = int(block_rgba[i * 4 + 1]) - pal[p][1];
                            int db = int(block_rgba[i * 4 + 2]) - pal[p][2];
                            int d = dr * dr + dg * dg + db * db;
                            if (d < best_d) { best_d = d; best = uint32_t(p); }
                        }
                    }
                    bits |= (best << (2 * i));
                }
                std::memcpy(dst, &c0, 2);
                std::memcpy(dst + 2, &c1, 2);
                std::memcpy(dst + 4, &bits, 4);
                continue;
            }

            if (backend == 0) {
                int lvl = std::max(0, std::min(int(rgbcx::MAX_LEVEL), quality));
                rgbcx::encode_bc1(uint32_t(lvl), dst, block_rgba, true, false);
                if (has_dxt1a_transparent_index(dst)) {
                    rgbcx::encode_bc1(uint32_t(lvl), dst, block_rgba, false, false);
                }
            } else {
                float colors[64];
                float weights[16];
                for (int i = 0; i < 16; ++i) {
                    colors[i * 4 + 0] = block_rgba[i * 4 + 0] / 255.0f;
                    colors[i * 4 + 1] = block_rgba[i * 4 + 1] / 255.0f;
                    colors[i * 4 + 2] = block_rgba[i * 4 + 2] / 255.0f;
                    colors[i * 4 + 3] = 1.0f;
                    weights[i] = 1.0f;
                }
                int q = std::max(0, std::min(9, quality));
                icbc::compress_bc1(static_cast<icbc::Quality>(q), colors, weights, cw, true, false, dst);
                if (has_dxt1a_transparent_index(dst)) {
                    icbc::compress_bc1(static_cast<icbc::Quality>(q), colors, weights, cw, false, false, dst);
                }
            }

            // Mode B: Temporal endpoint / block stability J = alpha*visual_err + beta*temporal_flicker
            // Only consider keeping previous endpoints/block when source motion in this block is small (< 2.5 MAE)
            if (temporal_mode >= 2 && prev_rgba != nullptr && prev_blocks != nullptr) {
                float src_abs = 0.0f;
                for (int py = 0; py < 4; ++py) {
                    const uint8_t* prow = prev_rgba + ((by * 4 + py) * width + bx * 4) * 4;
                    const uint8_t* crow = block_rgba + py * 16;
                    for (int px = 0; px < 4; ++px) {
                        for (int ch = 0; ch < 3; ++ch) {
                            src_abs += float(std::abs(int(crow[px * 4 + ch]) - int(prow[px * 4 + ch])));
                        }
                    }
                }
                if (src_abs / 48.0f < 2.2f) {
                    const uint8_t* pblk = prev_blocks + bidx * 8;
                    if (!has_dxt1a_transparent_index(pblk)) {
                        uint8_t dec_new[48], dec_prev[48];
                        decode_bc1_block_rgb(dst, dec_new);
                        decode_bc1_block_rgb(pblk, dec_prev);
                        float err_new = block_rgb_sse(block_rgba, dec_new, perceptual_weights != 0);
                        float err_prev_on_cur = block_rgb_sse(block_rgba, dec_prev, perceptual_weights != 0);
                        float flick_new = 0.0f;
                        for (int i = 0; i < 48; ++i) {
                            float d = float(dec_new[i]) - float(dec_prev[i]);
                            flick_new += d * d;
                        }
                        // Also try keeping previous endpoints (c0, c1) with freshly re-indexed selectors!
                        uint8_t cand_reidx[8];
                        std::memcpy(cand_reidx, pblk, 4);
                        uint16_t pc0, pc1;
                        std::memcpy(&pc0, pblk, 2);
                        std::memcpy(&pc1, pblk + 2, 2);
                        auto un565 = [](uint16_t c, int& r, int& g, int& b) {
                            int r5 = (c >> 11) & 31, g6 = (c >> 5) & 63, b5 = c & 31;
                            r = (r5 << 3) | (r5 >> 2);
                            g = (g6 << 2) | (g6 >> 4);
                            b = (b5 << 3) | (b5 >> 2);
                        };
                        int pal[4][3];
                        un565(pc0, pal[0][0], pal[0][1], pal[0][2]);
                        un565(pc1, pal[1][0], pal[1][1], pal[1][2]);
                        int n_pal = (pc0 > pc1) ? 4 : 3;
                        if (pc0 > pc1) {
                            for (int ch = 0; ch < 3; ++ch) {
                                pal[2][ch] = (2 * pal[0][ch] + pal[1][ch] + 1) / 3;
                                pal[3][ch] = (pal[0][ch] + 2 * pal[1][ch] + 1) / 3;
                            }
                        } else {
                            for (int ch = 0; ch < 3; ++ch) pal[2][ch] = (pal[0][ch] + pal[1][ch]) / 2;
                        }
                        uint32_t rbits = 0;
                        for (int i = 0; i < 16; ++i) {
                            int best_p = 0, best_d = 1 << 30;
                            for (int p = 0; p < n_pal; ++p) {
                                int dr = int(block_rgba[i * 4 + 0]) - pal[p][0];
                                int dg = int(block_rgba[i * 4 + 1]) - pal[p][1];
                                int db = int(block_rgba[i * 4 + 2]) - pal[p][2];
                                int d = dr * dr + dg * dg + db * db;
                                if (d < best_d) { best_d = d; best_p = p; }
                            }
                            rbits |= (uint32_t(best_p) << (2 * i));
                        }
                        std::memcpy(cand_reidx + 4, &rbits, 4);
                        uint8_t dec_reidx[48];
                        decode_bc1_block_rgb(cand_reidx, dec_reidx);
                        float err_reidx = block_rgb_sse(block_rgba, dec_reidx, perceptual_weights != 0);
                        float flick_reidx = 0.0f;
                        for (int i = 0; i < 48; ++i) {
                            float d = float(dec_reidx[i]) - float(dec_prev[i]);
                            flick_reidx += d * d;
                        }
                        float j_new = err_new + beta_flicker * flick_new;
                        float j_reidx = err_reidx + beta_flicker * flick_reidx;
                        float j_prev = err_prev_on_cur; // zero flicker
                        if (j_prev < j_new && j_prev <= j_reidx && err_prev_on_cur <= err_new * 1.35f + 40.0f) {
                            std::memcpy(dst, pblk, 8);
                            reused++;
                        } else if (j_reidx < j_new && err_reidx <= err_new * 1.25f + 30.0f) {
                            std::memcpy(dst, cand_reidx, 8);
                        }
                    }
                }
            }
        }
    }
    if (reused_blocks_out) *reused_blocks_out = reused;
}

void encode_frame_bc1(
    const uint8_t* rgba,
    int width,
    int height,
    int backend,
    int quality,
    int perceptual_weights,
    uint8_t* out_blocks
) {
    encode_frame_bc1_ex(rgba, width, height, backend, quality, perceptual_weights, nullptr, nullptr, 0, 0.0f, 0.0f, out_blocks, nullptr);
}

} // extern "C"

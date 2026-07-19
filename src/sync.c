/*
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#include "config.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "defines.h"
#include "input.h"
#include "private.h"
#include "sync.h"

#define MAX_PARTITIONS 14
#define MIDDLE_REF_SC 30 // midpoint of Table 11-3 in 1011s.pdf

// Table 6-4 in 1011s.pdf
static const int compatibility_mode[64] = {
    0, 1, 2, 3, 1, 5, 6, 5, 6, 1, 2, 11, 1, 5, 6, 5,
    6, 1, 2, 3, 1, 5, 6, 5, 6, 1, 2, 11, 1, 5, 6, 5,
    6, 1, 2, 3, 1, 5, 6, 5, 6, 1, 2, 11, 1, 5, 6, 5,
    6, 1, 2, 3, 1, 5, 6, 5, 6, 1, 2, 11, 1, 5, 6, 5
};

// albacore: small-array median for robust tracking estimators
static float medianf(const float *v, int n)
{
    float s[32];
    memcpy(s, v, n * sizeof(float));
    for (int a = 1; a < n; a++)
    {
        float t = s[a];
        int b = a - 1;
        while (b >= 0 && s[b] > t)
        {
            s[b + 1] = s[b];
            b--;
        }
        s[b + 1] = t;
    }
    return (n & 1) ? s[n / 2] : 0.5f * (s[n / 2 - 1] + s[n / 2]);
}

static uint8_t gray4(float f)
{
    if (f < -1)
        return 0;
    else if (f < 0)
        return 2;
    else if (f < 1)
        return 3;
    else
        return 1;
}

static uint8_t gray8(float f)
{
    if (f < -3)
        return 0;
    else if (f < -2)
        return 4;
    else if (f < -1)
        return 6;
    else if (f < 0)
        return 2;
    else if (f < 1)
        return 3;
    else if (f < 2)
        return 7;
    else if (f < 3)
        return 5;
    else
        return 1;
}

static inline int8_t demod(float x, float mult)
{
    float clamped = fmaxf(fminf(x, 1), -1);
    return lroundf(clamped * mult);
}

static uint8_t qpsk(complex float cf)
{
    return (crealf(cf) < 0 ? 0 : 1) | (cimagf(cf) < 0 ? 0 : 2);
}

static uint8_t qam16(complex float cf)
{
    return gray4(crealf(cf)) | (gray4(cimagf(cf)) << 2);
}

static uint8_t qam64(complex float cf)
{
    return gray8(crealf(cf)) | (gray8(cimagf(cf)) << 3);
}

static void adjust_ref(sync_t *st, unsigned int ref, int cfo)
{
    unsigned int n;
    float cfo_freq = 2 * M_PI * cfo * CP_FM / FFT_FM;

    // differentially-encoded sync & parity bits
    static const signed char sync[] = {
        -1, 1, -1, -1, -1, 1, 1, 0, 1, -1, 0, 0, 0, -1, -1, 0,
        0, 0, 0, 0, -1, 1, -1, 0, 0, 0, 0, 0, 0, 0, 0, -1
    };

    for (n = 0; n < BLKSZ; n++)
    {
        float error = cargf(st->buffer[ref][n] * st->buffer[ref][n] * cexpf(-I * 2 * st->costas_phase[ref])) * 0.5;

        st->phases[ref][n] = st->costas_phase[ref];
        st->buffer[ref][n] *= cexpf(-I * st->costas_phase[ref]);

        st->costas_freq[ref] += st->beta * error;
        if (st->costas_freq[ref] > 0.5) st->costas_freq[ref] = 0.5;
        if (st->costas_freq[ref] < -0.5) st->costas_freq[ref] = -0.5;
        st->costas_phase[ref] += st->costas_freq[ref] + cfo_freq + (st->alpha * error);
        if (st->costas_phase[ref] > M_PI) st->costas_phase[ref] -= 2 * M_PI;
        if (st->costas_phase[ref] < -M_PI) st->costas_phase[ref] += 2 * M_PI;
    }

    // compare to sync & parity bits
    float x = 0;
    for (n = 0; n < BLKSZ; n++)
        x += crealf(st->buffer[ref][n]) * sync[n];
    if (x < 0)
    {
        // adjust phase by pi to compensate
        for (n = 0; n < BLKSZ; n++)
        {
            st->phases[ref][n] += M_PI;
            st->buffer[ref][n] *= -1;
        }
        st->costas_phase[ref] += M_PI;
    }
}

static void reset_ref(sync_t *st, unsigned int ref)
{
    for (unsigned int n = 0; n < BLKSZ; n++)
        st->buffer[ref][n] *= cexpf(I * st->phases[ref][n]);
}

static void decode_dbpsk(const float complex *buf, unsigned char *data, int size)
{
    unsigned char prev = 0;

    for (int n = 0; n < size; n++)
    {
        unsigned char bit = crealf(buf[n]) <= 0 ? 0 : 1;
        data[n] = bit ^ prev;
        prev = bit;
    }
}

static int fuzzy_match(const signed char *needle, unsigned int needle_size, const unsigned char *data, int size)
{
    for (int n = 0; n < size; n++)
    {
        unsigned int i;
        for (i = 0; i < needle_size; i++)
        {
            // ignore don't care bits
            if (needle[i] < 0) continue;
            // test if bit is correct
            if (needle[i] != data[(n + i) % size])
                break;
        }
        if (i == needle_size)
            return n;
    }
    return -1;
}

static int decode_ref_fm(sync_t *st, unsigned int ref, unsigned int rsid, unsigned int *bc, unsigned int *psmi)
{
    signed char needle[] = {
        0, 1, 0, 0, 0, 1, 1, -1, 1, 0, rsid >> 1, (rsid >> 1) ^ (rsid & 1), -1, 0, 0, -1,
        -1, -1, -1, -1, 0, 1, 0, -1, -1, -1, -1, -1, -1, -1, -1, 0
    };
    unsigned char data[BLKSZ];

    for (int n = 0; n < BLKSZ; n++)
        if (needle[n] >= 0)
            if (needle[n] != (crealf(st->buffer[ref][n]) > 0))
                return -1;

    decode_dbpsk(st->buffer[ref], data, BLKSZ);
    *bc = (data[16] << 3) | (data[17] << 2) | (data[18] << 1) | data[19];
    *psmi = (data[25] << 5) | (data[26] << 4) | (data[27] << 3) | (data[28] << 2) | (data[29] << 1) | data[30];
    return 0;
}

static int find_ref_fm(sync_t *st, unsigned int ref, unsigned int rsid)
{
    signed char needle[] = {
        0, 1, 0, 0, 0, 1, 1, -1, 1, 0, rsid >> 1, (rsid >> 1) ^ (rsid & 1), -1, 0, 0, -1,
        -1, -1, -1, -1, 0, 1, 0, -1, -1, -1, -1, -1, -1, -1, -1, 0
    };
    unsigned char data[BLKSZ];

    for (int n = 0; n < BLKSZ; n++)
        data[n] = crealf(st->buffer[ref][n]) <= 0 ? 0 : 1;

    int match = fuzzy_match(needle, sizeof(needle), data, BLKSZ);
    if (match >= 0)
        return match;

    for (int n = 0; n < BLKSZ; n++)
        data[n] ^= 1;

    return fuzzy_match(needle, sizeof(needle), data, BLKSZ);
}

static int find_block_am(sync_t *st, unsigned int ref)
{
    signed char needle[] = {
        0, 1, 1, 0, 0, 1, 0, -1, -1, 1, -1, -1, -1, -1, 0, -1, -1, -1, -1, -1, -1, 1, 1, -1, -1, -1, -1, -1, -1, -1, -1, -1
    };
    unsigned char data[BLKSZ];
    int bc;

    for (int n = 0; n < BLKSZ; n++)
    {
        data[n] = cimagf(st->buffer[ref][n]) <= 0 ? 0 : 1;
        if ((needle[n] >= 0) && (data[n] != needle[n])) return -1;
    }

    // parity checks
    if (data[7] ^ data[8]) return -1;
    if (data[10] ^ data[11] ^ data[12] ^ data[13]) return -1;
    if (data[15] ^ data[16] ^ data[17] ^ data[18] ^ data[19] ^ data[20]) return -1;
    if (data[23] ^ data[24] ^ data[25] ^ data[26] ^ data[27] ^ data[28] ^ data[29] ^ data[30] ^ data[31]) return -1;

    bc = (data[17] << 2) | (data[18] << 1) | data[19];
    if (bc == 0)
    {
        st->psmi = (data[26] << 4) | (data[27] << 3) | (data[28] << 2) | (data[29] << 1) | data[30];
        st->pli = data[7];
        st->hppi = data[11];
        st->aabi = data[12];
        st->rdbi = data[15];
    }
    return bc;
}

static int find_ref_am(sync_t *st, unsigned int ref)
{
    signed char needle[] = {
        0, 1, 1, 0, 0, 1, 0, -1, -1, 1, -1, -1, -1, -1, 0, -1, -1, -1, -1, -1, -1, 1, 1
    };
    unsigned char data[BLKSZ];

    for (int n = 0; n < BLKSZ; n++)
        data[n] = cimagf(st->buffer[ref][n]) <= 0 ? 0 : 1;

    return fuzzy_match(needle, sizeof(needle), data, BLKSZ);
}

static float calc_smag(sync_t *st, unsigned int ref)
{
    float sum = 0;
    // phase was already corrected, so imaginary component is zero
    for (int n = 0; n < BLKSZ; n++)
        sum += fabsf(crealf(st->buffer[ref][n]));
    return sum / BLKSZ;
}

// albacore: |interpolated channel gain|^2 per (carrier, block symbol),
// captured during equalization for CSI-weighted branch metrics
// (ALBACORE_CSI=1). Zero-forcing equalization divides by the gain and
// AMPLIFIES noise on faded carriers; CSI weighting re-weights each
// carrier's soft bits by its post-equalization reliability (|H|^2),
// per the reference-receiver architecture (US7724850 family).
static float alb_g2[FFT_FM][BLKSZ];

static void adjust_data(sync_t *st, unsigned int lower, unsigned int upper)
{
    float smag0, smag19;
    smag0 = calc_smag(st, lower);
    smag19 = calc_smag(st, upper);

    for (int n = 0; n < BLKSZ; n++)
    {
        float complex upper_phase = cexpf(st->phases[upper][n] * I);
        float complex lower_phase = cexpf(st->phases[lower][n] * I);

        for (int k = 1; k < PARTITION_WIDTH_FM; k++)
        {
            float complex gain = k * smag19 * upper_phase + (PARTITION_WIDTH_FM - k) * smag0 * lower_phase;
            // average phase difference
            float complex C = CMPLXF(PARTITION_WIDTH_FM, PARTITION_WIDTH_FM) / gain;
            // adjust sample
            st->buffer[lower + k][n] *= C;
            alb_g2[lower + k][n] = normf(gain);
        }
    }
}

float phase_diff(float a, float b)
{
    float diff = a - b;
    while (diff > M_PI / 2) diff -= M_PI;
    while (diff < -M_PI / 2) diff += M_PI;
    return diff;
}

void detect_cfo(sync_t *st)
{
    for (int cfo = -2 * PARTITION_WIDTH_FM; cfo < 2 * PARTITION_WIDTH_FM; cfo++)
    {
        int offset;
        int best_offset = -1;
        unsigned int best_count = 0;
        unsigned int offset_count[BLKSZ];

        memset(offset_count, 0, BLKSZ * sizeof(unsigned int));

        for (int i = 0; i <= PM_PARTITIONS; i++)
        {
            adjust_ref(st, cfo + LB_START + i * PARTITION_WIDTH_FM, cfo);
            offset = find_ref_fm(st, cfo + LB_START + i * PARTITION_WIDTH_FM, (MIDDLE_REF_SC-i) & 0x3);
            reset_ref(st, cfo + LB_START + i * PARTITION_WIDTH_FM);
            if (offset >= 0)
                offset_count[offset]++;

            adjust_ref(st, cfo + UB_END - i * PARTITION_WIDTH_FM, cfo);
            offset = find_ref_fm(st, cfo + UB_END - i * PARTITION_WIDTH_FM, (MIDDLE_REF_SC-i) & 0x3);
            reset_ref(st, cfo + UB_END - i * PARTITION_WIDTH_FM);
            if (offset >= 0)
                offset_count[offset]++;
        }

        for (offset = 0; offset < BLKSZ; offset++)
        {
            if (offset_count[offset] > best_count) {
                best_offset = offset;
                best_count = offset_count[offset];
            }
        }

        if (best_offset >= 0 && best_count >= 3)
        {
            // At least three offsets matched, so this is likely the correct CFO.
            acquire_keep_extra(&st->input->acq, ((BLKSZ - best_offset) % BLKSZ) * FFTCP_FM);
            acquire_cfo_adjust(&st->input->acq, cfo);

            // Wait until the buffers have cleared before measuring again.
            st->cfo_wait = 8;
            break;
        }
    }
}

void sync_process_fm(sync_t *st)
{
    int i, partitions_per_band;

    switch (compatibility_mode[st->psmi]) {
        case 2:
            partitions_per_band = 11;
            break;
        case 3:
            partitions_per_band = 12;
            break;
        case 5:
        case 6:
        case 11:
            partitions_per_band = 14;
            break;
        default:
            partitions_per_band = 10;
    }

    for (i = 0; i < partitions_per_band * PARTITION_WIDTH_FM + 1; i += PARTITION_WIDTH_FM)
    {
        adjust_ref(st, LB_START + i, 0);
        adjust_ref(st, UB_END - i, 0);
    }

    // check if we now have synchronization
    if (st->input->sync_state == SYNC_STATE_COARSE)
    {
        unsigned int good_refs = 0;
        unsigned int seen_bc[16] = {0};
        unsigned int seen_psmi[64] = {0};
        for (i = 0; i <= partitions_per_band; i++)
        {
            unsigned int bc, psmi;
            if (decode_ref_fm(st, LB_START + i * PARTITION_WIDTH_FM, (MIDDLE_REF_SC-i) & 0x3, &bc, &psmi) == 0)
            {
                good_refs++;
                seen_bc[bc]++;
                seen_psmi[psmi]++;
            }
            if (decode_ref_fm(st, UB_END - i * PARTITION_WIDTH_FM, (MIDDLE_REF_SC-i) & 0x3, &bc, &psmi) == 0)
            {
                good_refs++;
                seen_bc[bc]++;
                seen_psmi[psmi]++;
            }
        }

        if (good_refs >= 4)
        {
            int majority_bc = -1;
            for (unsigned int bc = 0; bc < 16; bc++)
                if (seen_bc[bc] > good_refs / 2)
                    majority_bc = bc;

            int majority_psmi = -1;
            for (unsigned int psmi = 0; psmi < 16; psmi++)
                if (seen_psmi[psmi] > good_refs / 2)
                    majority_psmi = psmi;

            if ((majority_bc >= 0) && (majority_psmi >= 0))
            {
                st->bc = majority_bc;
                st->psmi = majority_psmi;

                input_set_sync_state(st->input, SYNC_STATE_FINE);

                decode_reset(&st->input->decode);

                frame_reset(&st->input->frame);
            }
        }
        else if (st->cfo_wait == 0)
        {
            detect_cfo(st);
        }
        else
        {
            // Decrease wait counter.
            st->cfo_wait--;
        }
    }

    // if we are still synchronized
    if (st->input->sync_state == SYNC_STATE_FINE)
    {
        // albacore: ALBACORE_ROBUST_TRACK=1 swaps the mean/least-squares
        // tracking estimators for median/MAD-trimmed ones. The stock global
        // `angle` feeds back into EVERY ref's Costas loop, so a few refs
        // wrecked by narrowband interference poison the whole receiver
        // (observed: sync-thrash collapse, MER -22 dB both sidebands).
        static int robust = -1;
        if (robust < 0)
        {
            const char *rt = getenv("ALBACORE_ROBUST_TRACK");
            const char *master = getenv("ALBACORE");
            robust = rt ? (atoi(rt) != 0)
                        : (master && atoi(master) != 0) ? 1 : 0;
        }

        float samperr = 0, angle = 0;
        float sum_xy = 0, sum_x2 = 0;
        float sd_vals[32];
        int sd_n = 0;
        for (i = 0; i < partitions_per_band * PARTITION_WIDTH_FM; i += PARTITION_WIDTH_FM)
        {
            adjust_data(st, LB_START + i, LB_START + i + PARTITION_WIDTH_FM);
            adjust_data(st, UB_END - i - PARTITION_WIDTH_FM, UB_END - i);

            sd_vals[sd_n] = phase_diff(st->phases[LB_START + i][0], st->phases[LB_START + i + PARTITION_WIDTH_FM][0]);
            samperr += sd_vals[sd_n++];
            sd_vals[sd_n] = phase_diff(st->phases[UB_END - i - PARTITION_WIDTH_FM][0], st->phases[UB_END - i][0]);
            samperr += sd_vals[sd_n++];
        }
        if (robust)
            samperr = medianf(sd_vals, sd_n) * FFT_FM / PARTITION_WIDTH_FM / (2 * M_PI);
        else
            samperr = samperr / (partitions_per_band * 2) * FFT_FM / PARTITION_WIDTH_FM / (2 * M_PI);

        float ref_x[32], ref_y[32];
        int ref_n = 0;
        for (i = 0; i < partitions_per_band * PARTITION_WIDTH_FM + 1; i += PARTITION_WIDTH_FM)
        {
            ref_x[ref_n] = LB_START + i - (FFT_FM / 2);
            ref_y[ref_n] = st->costas_freq[LB_START + i];
            ref_n++;
            ref_x[ref_n] = UB_END - i - (FFT_FM / 2);
            ref_y[ref_n] = st->costas_freq[UB_END - i];
            ref_n++;
        }
        if (robust)
        {
            // median angle + MAD-trimmed least-squares slope
            float med = medianf(ref_y, ref_n);
            float dev[32];
            for (i = 0; (int)i < ref_n; i++)
                dev[i] = fabsf(ref_y[i] - med);
            float mad = medianf(dev, ref_n);
            float thr = 3.0f * mad + 1e-6f;
            int kept = 0;
            for (i = 0; (int)i < ref_n; i++)
            {
                if (fabsf(ref_y[i] - med) <= thr)
                {
                    angle += ref_y[i];
                    sum_xy += ref_x[i] * ref_y[i];
                    sum_x2 += ref_x[i] * ref_x[i];
                    kept++;
                }
            }
            // pre-scale so the shared `angle /= (partitions_per_band+1)*2`
            // below lands on mean-of-kept
            angle = (angle / kept) * ((partitions_per_band + 1) * 2);
        }
        else
        {
            for (i = 0; (int)i < ref_n; i++)
            {
                angle += ref_y[i];
                sum_xy += ref_x[i] * ref_y[i];
                sum_x2 += ref_x[i] * ref_x[i];
            }
        }
        samperr -= (sum_xy / sum_x2) * FFT_FM / (2 * M_PI) * ACQUIRE_SYMBOLS;
        st->samperr = roundf(samperr);

        angle /= (partitions_per_band + 1) * 2;
        st->angle = angle;
        for (i = 0; i < partitions_per_band * PARTITION_WIDTH_FM + 1; i += PARTITION_WIDTH_FM)
        {
            st->costas_freq[LB_START + i] -= angle;
            st->costas_freq[UB_END - i] -= angle;
        }

        // Calculate modulation error (per partition, for albacore's
        // optional per-partition confidence weighting) + per-partition
        // mean |gain|^2 (for ALBACORE_CSI per-carrier weighting)
        float error_lb = 0, error_ub = 0;
        float error_lb_part[16] = {0}, error_ub_part[16] = {0};
        float g2_lb_part[16] = {0}, g2_ub_part[16] = {0};
        for (int n = 0; n < BLKSZ; n++)
        {
            float complex c, ideal;
            for (i = 0; i < partitions_per_band * PARTITION_WIDTH_FM; i += PARTITION_WIDTH_FM)
            {
                unsigned int j;
                const int p = i / PARTITION_WIDTH_FM;
                for (j = 1; j < PARTITION_WIDTH_FM; j++)
                {
                    c = st->buffer[LB_START + i + j][n];
                    ideal = CMPLXF(crealf(c) >= 0 ? 1 : -1, cimagf(c) >= 0 ? 1 : -1);
                    error_lb_part[p] += normf(ideal - c);
                    g2_lb_part[p] += alb_g2[LB_START + i + j][n];

                    c = st->buffer[UB_END - i - PARTITION_WIDTH_FM + j][n];
                    ideal = CMPLXF(crealf(c) >= 0 ? 1 : -1, cimagf(c) >= 0 ? 1 : -1);
                    error_ub_part[p] += normf(ideal - c);
                    g2_ub_part[p] += alb_g2[UB_END - i - PARTITION_WIDTH_FM + j][n];
                }
            }
        }
        for (i = 0; (int)i < partitions_per_band; i++)
        {
            g2_lb_part[i] /= (PARTITION_DATA_CARRIERS * BLKSZ);
            g2_ub_part[i] /= (PARTITION_DATA_CARRIERS * BLKSZ);
        }
        for (i = 0; i < partitions_per_band; i++)
        {
            error_lb += error_lb_part[i];
            error_ub += error_ub_part[i];
        }

        st->error_lb += error_lb;
        st->error_ub += error_ub;

        // Display average MER for each sideband
        if (++st->mer_cnt == 16)
        {
            float signal = 2 * BLKSZ * (partitions_per_band * PARTITION_DATA_CARRIERS) * st->mer_cnt;
            float mer_db_lb = 10 * log10f(signal / st->error_lb);
            float mer_db_ub = 10 * log10f(signal / st->error_ub);

            nrsc5_report_mer(st->input->radio, mer_db_lb, mer_db_ub);

            st->mer_cnt = 0;
            st->error_lb = 0;
            st->error_ub = 0;
        }

        // Soft demod based on MER for each sideband
        const float mer_lb = 2.0f * BLKSZ * (float)(partitions_per_band * PARTITION_DATA_CARRIERS) / error_lb;
        const float mer_ub = 2.0f * BLKSZ * (float)(partitions_per_band * PARTITION_DATA_CARRIERS) / error_ub;
        const float mult_lb = fmaxf(fminf(mer_lb * 10, 127), 1);
        const float mult_ub = fmaxf(fminf(mer_ub * 10, 127), 1);

        // albacore: optional per-partition confidence weighting
        // (ALBACORE_PART_WEIGHT=1). Same mult formula, computed per 19-carrier
        // partition instead of per sideband: frequency-selective damage
        // (one faded/jammed partition) stops diluting the clean partitions'
        // confidence. Partition index counts outer->inner on both sidebands.
        // ALBACORE=1 is the master switch: enables the validated stack
        // (robust tracking + partition weighting + erase floor 2); the
        // individual ALBACORE_* vars still override.
        static int part_weight = -1;
        if (part_weight < 0)
        {
            const char *pw = getenv("ALBACORE_PART_WEIGHT");
            const char *master = getenv("ALBACORE");
            part_weight = pw ? (atoi(pw) != 0)
                             : (master && atoi(master) != 0) ? 1 : 0;
        }
        float mult_lb_part[16], mult_ub_part[16];
        for (i = 0; i < 16; i++)
        {
            mult_lb_part[i] = mult_lb;
            mult_ub_part[i] = mult_ub;
        }
        if (part_weight)
        {
            // ALBACORE_ERASE=<floor>: partitions whose weight lands at or
            // below <floor> are output as EXACT soft-zeros (true Viterbi
            // erasure) instead of weak +/-1s. 0 disables (default).
            // NOTE: deliberately NOT enabled by the ALBACORE master switch —
            // certification found regressions on a strong extended-mode
            // station and one jam config. Opt-in experimental only.
            static float erase_floor = -1;
            if (erase_floor < 0)
            {
                const char *ef = getenv("ALBACORE_ERASE");
                erase_floor = ef ? (float)atof(ef) : 0;
                if (erase_floor < 0)
                    erase_floor = 0;
            }
            const float sig_p = 2.0f * BLKSZ * (float)PARTITION_DATA_CARRIERS;
            for (i = 0; (int)i < partitions_per_band; i++)
            {
                mult_lb_part[i] = fmaxf(fminf(sig_p / error_lb_part[i] * 10, 127), 1);
                mult_ub_part[i] = fmaxf(fminf(sig_p / error_ub_part[i] * 10, 127), 1);
                if (erase_floor > 0)
                {
                    if (mult_lb_part[i] <= erase_floor)
                        mult_lb_part[i] = 0;
                    if (mult_ub_part[i] <= erase_floor)
                        mult_ub_part[i] = 0;
                }
            }
            static int dbg = -1;
            if (dbg < 0)
            {
                const char *d = getenv("ALBACORE_DEBUG_PART");
                dbg = (d && atoi(d) != 0) ? 1 : 0;
            }
            static unsigned int dbg_cnt = 0;
            if (dbg && (dbg_cnt++ % 32) == 0)
            {
                fprintf(stderr, "[alb] sb lb=%.0f ub=%.0f | lb parts:", mult_lb, mult_ub);
                for (i = 0; (int)i < partitions_per_band; i++)
                    fprintf(stderr, " %.0f", mult_lb_part[i]);
                fprintf(stderr, " | ub parts:");
                for (i = 0; (int)i < partitions_per_band; i++)
                    fprintf(stderr, " %.0f", mult_ub_part[i]);
                fprintf(stderr, "\n");
            }
        }
#define ALB_PLB(chunk) mult_lb_part[((chunk) - LB_START) / PARTITION_WIDTH_FM]
#define ALB_PUB(chunk) mult_ub_part[(UB_END - (chunk)) / PARTITION_WIDTH_FM - 1]

        // albacore: ALBACORE_CSI=1 — per-SUBCARRIER CSI weighting on top of
        // the partition/sideband mult: scale each carrier's soft bits by
        // |G_k|^2 relative to its partition mean (post-ZF reliability).
        // Scale clamped to [1/8, 4] so a single bad estimate can't erase
        // or rail a carrier on its own.
        static int csi_on = -1;
        if (csi_on < 0)
        {
            const char *cs = getenv("ALBACORE_CSI");
            csi_on = (cs && atoi(cs) != 0) ? 1 : 0;
        }
#define ALB_CSI(mult, car, sym, g2mean) \
        (csi_on ? fminf((mult) * fmaxf(fminf(alb_g2[(car)][(sym)] / ((g2mean) + 1e-9f), 4.0f), 0.125f), 127.0f) : (mult))
#define ALB_WLB(chunk, car, sym) ALB_CSI(ALB_PLB(chunk), (car), (sym), g2_lb_part[((chunk) - LB_START) / PARTITION_WIDTH_FM])
#define ALB_WUB(chunk, car, sym) ALB_CSI(ALB_PUB(chunk), (car), (sym), g2_ub_part[(UB_END - (chunk)) / PARTITION_WIDTH_FM - 1])

        int8_t buffer_pm[PM_BLOCK_SIZE];
        int8_t buffer_px1[P3_FRAME_LEN_MP3_MP11];
        int8_t buffer_px2[P3_FRAME_LEN_MP3_MP11];
        int out_pm = 0, out_px1 = 0, out_px2 = 0;

        for (int n = 0; n < BLKSZ; n++)
        {
            float complex c;
            for (i = LB_START; i < LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM); i += PARTITION_WIDTH_FM)
            {
                unsigned int j;
                for (j = 1; j < PARTITION_WIDTH_FM; j++)
                {
                    c = st->buffer[i + j][n];
                    buffer_pm[out_pm++] = demod(crealf(c), ALB_WLB(i, i + j, n));
                    buffer_pm[out_pm++] = demod(cimagf(c), ALB_WLB(i, i + j, n));
                }
            }
            for (i = UB_END - (PM_PARTITIONS * PARTITION_WIDTH_FM); i < UB_END; i += PARTITION_WIDTH_FM)
            {
                unsigned int j;
                for (j = 1; j < PARTITION_WIDTH_FM; j++)
                {
                    c = st->buffer[i + j][n];
                    buffer_pm[out_pm++] = demod(crealf(c), ALB_WUB(i, i + j, n));
                    buffer_pm[out_pm++] = demod(cimagf(c), ALB_WUB(i, i + j, n));
                }
            }
            if (compatibility_mode[st->psmi] == 2) {
                unsigned int j;
                for (j = 1; j < PARTITION_WIDTH_FM; j++)
                {
                    c = st->buffer[LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM) + j][n];
                    buffer_px1[out_px1++] = demod(crealf(c), ALB_WLB(LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM), LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM) + j, n));
                    buffer_px1[out_px1++] = demod(cimagf(c), ALB_WLB(LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM), LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM) + j, n));
                }
                for (j = 1; j < PARTITION_WIDTH_FM; j++)
                {
                    c = st->buffer[UB_END - (PM_PARTITIONS + 1) * PARTITION_WIDTH_FM + j][n];
                    buffer_px1[out_px1++] = demod(crealf(c), ALB_WUB(UB_END - (PM_PARTITIONS + 1) * PARTITION_WIDTH_FM, UB_END - (PM_PARTITIONS + 1) * PARTITION_WIDTH_FM + j, n));
                    buffer_px1[out_px1++] = demod(cimagf(c), ALB_WUB(UB_END - (PM_PARTITIONS + 1) * PARTITION_WIDTH_FM, UB_END - (PM_PARTITIONS + 1) * PARTITION_WIDTH_FM + j, n));
                }
            }
            if ((compatibility_mode[st->psmi] == 3) || (compatibility_mode[st->psmi] == 11)) {
                for (i = LB_START + (PM_PARTITIONS * PARTITION_WIDTH_FM); i < LB_START + (PM_PARTITIONS + 2) * PARTITION_WIDTH_FM; i += PARTITION_WIDTH_FM)
                {
                    unsigned int j;
                    for (j = 1; j < PARTITION_WIDTH_FM; j++)
                    {
                        c = st->buffer[i + j][n];
                        buffer_px1[out_px1++] = demod(crealf(c), ALB_WLB(i, i + j, n));
                        buffer_px1[out_px1++] = demod(cimagf(c), ALB_WLB(i, i + j, n));
                    }
                }
                for (i = UB_END - (PM_PARTITIONS + 2) * PARTITION_WIDTH_FM; i < UB_END - (PM_PARTITIONS * PARTITION_WIDTH_FM); i += PARTITION_WIDTH_FM)
                {
                    unsigned int j;
                    for (j = 1; j < PARTITION_WIDTH_FM; j++)
                    {
                        c = st->buffer[i + j][n];
                        buffer_px1[out_px1++] = demod(crealf(c), ALB_WUB(i, i + j, n));
                        buffer_px1[out_px1++] = demod(cimagf(c), ALB_WUB(i, i + j, n));
                    }
                }
            }
            if (compatibility_mode[st->psmi] == 11) {
                for (i = LB_START + (PM_PARTITIONS + 2) * PARTITION_WIDTH_FM; i < LB_START + (PM_PARTITIONS + 4) * PARTITION_WIDTH_FM; i += PARTITION_WIDTH_FM)
                {
                    unsigned int j;
                    for (j = 1; j < PARTITION_WIDTH_FM; j++)
                    {
                        c = st->buffer[i + j][n];
                        buffer_px2[out_px2++] = demod(crealf(c), ALB_WLB(i, i + j, n));
                        buffer_px2[out_px2++] = demod(cimagf(c), ALB_WLB(i, i + j, n));
                    }
                }
                for (i = UB_END - (PM_PARTITIONS + 4) * PARTITION_WIDTH_FM; i < UB_END - (PM_PARTITIONS + 2) * PARTITION_WIDTH_FM; i += PARTITION_WIDTH_FM)
                {
                    unsigned int j;
                    for (j = 1; j < PARTITION_WIDTH_FM; j++)
                    {
                        c = st->buffer[i + j][n];
                        // stock uses mult_lb here (upstream quirk); keep that
                        // when the gate is off, fix attribution when on
                        buffer_px2[out_px2++] = demod(crealf(c), part_weight ? ALB_WUB(i, i + j, n) : mult_lb);
                        buffer_px2[out_px2++] = demod(cimagf(c), part_weight ? ALB_WUB(i, i + j, n) : mult_lb);
                    }
                }
            }
        }

        decode_push_pm(&st->input->decode, buffer_pm, st->bc);
        if (out_px1 > 0)
        {
            decode_push_px1(&st->input->decode, buffer_px1, out_px1, st->bc);
        }
        if (out_px2 > 0)
        {
            decode_push_px2(&st->input->decode, buffer_px2, out_px2, st->bc);
        }

        st->bc = (st->bc + 1) % 16;
    }
}

void sync_process_am(sync_t *st)
{
    int offset;

    for (int i = REF_INDEX_AM; i <= MAX_INDEX_AM; i++)
    {
        for (int n = 0; n < BLKSZ; n++)
        {
            st->buffer[CENTER_AM - i][n] = -conjf(st->buffer[CENTER_AM - i][n]);
        }
    }

    if (st->psmi != SERVICE_MODE_MA3)
    {
        for (int i = REF_INDEX_AM; i <= PIDS_OUTER_INDEX_AM; i++)
        {
            for (int n = 0; n < BLKSZ; n++)
            {
                st->buffer[CENTER_AM + i][n] += st->buffer[CENTER_AM - i][n];
            }
        }
    }

    if (st->input->sync_state == SYNC_STATE_COARSE && st->cfo_wait == 0)
    {
        offset = find_ref_am(st, CENTER_AM + REF_INDEX_AM);
        if (offset > 0)
        {
            acquire_keep_extra(&st->input->acq, ((BLKSZ - offset) % BLKSZ) * FFTCP_AM);
            st->cfo_wait = 8;
        }
    }
    else
    {
        st->cfo_wait--;
    }

    if (st->input->sync_state == SYNC_STATE_COARSE)
    {
        int bc = find_block_am(st, CENTER_AM + REF_INDEX_AM);

        if (bc == -1)
            st->offset_history = 0;
        else
            st->offset_history = (st->offset_history << 4) | bc;

        if ((st->offset_history & 0xffff) == 0x5670)
        {
            st->bc = 0;
            input_set_sync_state(st->input, SYNC_STATE_FINE);
            decode_reset(&st->input->decode);
            frame_reset(&st->input->frame);
            st->offset_history = 0;
        }
    }

    if (st->input->sync_state == SYNC_STATE_FINE)
    {
        const int pids1_index = (st->psmi != SERVICE_MODE_MA3) ? PIDS_INNER_INDEX_AM : -PIDS_INNER_INDEX_AM;
        const int pids2_index = (st->psmi != SERVICE_MODE_MA3) ? PIDS_OUTER_INDEX_AM : PIDS_INNER_INDEX_AM;

        const float complex pids1_mult = 2 * CMPLXF(1.5, -0.5) / (st->buffer[CENTER_AM + pids1_index][8] + st->buffer[CENTER_AM + pids1_index][24]);
        const float complex pids2_mult = 2 * CMPLXF(1.5, -0.5) / (st->buffer[CENTER_AM + pids2_index][8] + st->buffer[CENTER_AM + pids2_index][24]);
        uint8_t pids[2 * BLKSZ];
        int pids_out = 0;

        for (int n = 0; n < BLKSZ; n++)
        {
            st->buffer[CENTER_AM + pids1_index][n] *= pids1_mult;
            pids[pids_out++] = qam16(st->buffer[CENTER_AM + pids1_index][n]);

            st->buffer[CENTER_AM + pids2_index][n] *= pids2_mult;
            pids[pids_out++] = qam16(st->buffer[CENTER_AM + pids2_index][n]);
        }

        decode_process_pids_am(&st->input->decode, pids);

        float complex pl_mult[PARTITION_WIDTH_AM];
        float complex pu_mult[PARTITION_WIDTH_AM];
        float complex s_mult[PARTITION_WIDTH_AM];
        float complex t_mult[PARTITION_WIDTH_AM];

        const int primary_index = (st->psmi != SERVICE_MODE_MA3) ? OUTER_PARTITION_START_AM : INNER_PARTITION_START_AM;
        const int secondary_index = MIDDLE_PARTITION_START_AM;
        const int tertiary_index = (st->psmi != SERVICE_MODE_MA3) ? INNER_PARTITION_START_AM : MIDDLE_PARTITION_START_AM;

        float samperr = 0;
        for (int col = 0; col < PARTITION_WIDTH_AM; col++)
        {
            int train1 = (5 + 11*col) % 32;
            int train2 = (21 + 11*col) % 32;

            pl_mult[col] = 2 * CMPLXF(2.5, -2.5) / (st->buffer[CENTER_AM - primary_index - col][train1] + st->buffer[CENTER_AM - primary_index - col][train2]);
            pu_mult[col] = 2 * CMPLXF(2.5, -2.5) / (st->buffer[CENTER_AM + primary_index + col][train1] + st->buffer[CENTER_AM + primary_index + col][train2]);
            if (st->psmi != SERVICE_MODE_MA3)
            {
                s_mult[col] = 2 * CMPLXF(1.5, -0.5) / (st->buffer[CENTER_AM + secondary_index + col][train1] + st->buffer[CENTER_AM + secondary_index + col][train2]);
                t_mult[col] = 2 * CMPLXF(-0.5, 0.5) / (st->buffer[CENTER_AM + tertiary_index + col][train1] + st->buffer[CENTER_AM + tertiary_index + col][train2]);
            }
            else
            {
                s_mult[col] = 2 * CMPLXF(2.5, -2.5) / (st->buffer[CENTER_AM + secondary_index + col][train1] + st->buffer[CENTER_AM + secondary_index + col][train2]);
                t_mult[col] = 2 * CMPLXF(2.5, -2.5) / (st->buffer[CENTER_AM - tertiary_index - col][train1] + st->buffer[CENTER_AM - tertiary_index - col][train2]);
            }

            if (col > 0)
            {
                samperr += phase_diff(cargf(pl_mult[col]), cargf(pl_mult[col-1]));
                samperr += phase_diff(cargf(pu_mult[col]), cargf(pu_mult[col-1]));
            }
        }
        samperr = samperr / (2 * (PARTITION_WIDTH_AM-1)) * FFT_AM / (2 * M_PI);
        st->samperr = roundf(samperr);

        uint8_t pl[BLKSZ * PARTITION_WIDTH_AM];
        uint8_t pu[BLKSZ * PARTITION_WIDTH_AM];
        uint8_t s[BLKSZ * PARTITION_WIDTH_AM];
        uint8_t t[BLKSZ * PARTITION_WIDTH_AM];

        for (int n = 0; n < BLKSZ; n++)
        {
            for (int col = 0; col < PARTITION_WIDTH_AM; col++)
            {
                st->buffer[CENTER_AM - primary_index - col][n] *= pl_mult[col];
                st->buffer[CENTER_AM + primary_index + col][n] *= pu_mult[col];
                st->buffer[CENTER_AM + secondary_index + col][n] *= s_mult[col];
                if (st->psmi != SERVICE_MODE_MA3)
                    st->buffer[CENTER_AM + tertiary_index + col][n] *= t_mult[col];
                else
                    st->buffer[CENTER_AM - tertiary_index - col][n] *= t_mult[col];

                if (st->psmi != SERVICE_MODE_MA3)
                {
                    pl[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM - primary_index - col][n]);
                    pu[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM + primary_index + col][n]);
                    s[n * PARTITION_WIDTH_AM + col] = qam16(st->buffer[CENTER_AM + secondary_index + col][n]);
                    t[n * PARTITION_WIDTH_AM + col] = qpsk(st->buffer[CENTER_AM + tertiary_index + col][n]);
                }
                else
                {
                    pl[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM - primary_index - col][n]);
                    pu[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM + primary_index + col][n]);
                    s[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM + secondary_index + col][n]);
                    t[n * PARTITION_WIDTH_AM + col] = qam64(st->buffer[CENTER_AM - tertiary_index - col][n]);
                }
            }
        }

        decode_push_pl_pu_s_t(
            &st->input->decode,
            pl, pu, s, t, st->bc
        );

        st->bc = (st->bc + 1) % 8;
    }
}

void sync_adjust(sync_t *st, int sample_adj)
{
    int i;
    for (i = 0; i < MAX_PARTITIONS * PARTITION_WIDTH_FM + 1; i++)
    {
        st->costas_phase[LB_START + i] -= sample_adj * (LB_START + i - (FFT_FM / 2)) * 2 * M_PI / FFT_FM;
        st->costas_phase[UB_END - i] -= sample_adj * (UB_END - i - (FFT_FM / 2)) * 2 * M_PI / FFT_FM;
    }
}

void sync_push(sync_t *st, float complex *fftout)
{
    unsigned int i;

    if (st->input->radio->mode == NRSC5_MODE_FM)
    {
        for (i = 0; i < MAX_PARTITIONS * PARTITION_WIDTH_FM + 1; i++)
        {
            st->buffer[LB_START + i][st->idx] = fftout[LB_START + i];
            st->buffer[UB_END - i][st->idx] = fftout[UB_END - i];
        }
    }
    else
    {
        for (i = CENTER_AM - MAX_INDEX_AM; i <= CENTER_AM + MAX_INDEX_AM; i++)
        {
            st->buffer[i][st->idx] = fftout[i];
        }
    }

    if (++st->idx == BLKSZ)
    {
        st->idx = 0;

        if (st->input->radio->mode == NRSC5_MODE_FM)
            sync_process_fm(st);
        else
            sync_process_am(st);
    }
}

void sync_reset(sync_t *st)
{
    unsigned int i;
    for (i = 0; i < FFT_FM; i++)
    {
        st->costas_freq[i] = 0;
        st->costas_phase[i] = 0;
    }

    st->idx = 0;
    st->psmi = 1;
    st->pli = -1;
    st->hppi = -1;
    st->aabi = -1;
    st->rdbi = -1;
    st->cfo_wait = 0;
    st->offset_history = 0;
    st->mer_cnt = 0;
    st->error_lb = 0;
    st->error_ub = 0;
}

void sync_init(sync_t *st, input_t *input)
{
    float loop_bw = 0.05, damping = 0.70710678;
    float denom = 1 + (2 * damping * loop_bw) + (loop_bw * loop_bw);
    st->alpha = (4 * damping * loop_bw) / denom;
    st->beta = (4 * loop_bw * loop_bw) / denom;

    st->input = input;
    sync_reset(st);
}

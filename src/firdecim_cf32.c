#include "config.h"

#include <assert.h>
#include <stdint.h>

#ifdef HAVE_NEON
#include <arm_neon.h>
#endif

#include "firdecim_cf32.h"

#define WINDOW_SIZE 2048

struct firdecim_cf32 {
    float * taps;
    unsigned int ntaps;
    float complex * window;
    unsigned int idx;
};

firdecim_cf32 firdecim_cf32_create(const float * taps, unsigned int ntaps)
{
    firdecim_cf32 q;

    q = malloc(sizeof(*q));
    q->ntaps = (ntaps == 32) ? 32 : 15;
    q->taps = malloc(sizeof(float) * ntaps * 2);
    q->window = calloc(WINDOW_SIZE, sizeof(float complex));
    firdecim_cf32_reset(q);

    // reverse order so we can push into the window
    // duplicate for neon
    for (unsigned int i = 0; i < ntaps; ++i)
    {
        q->taps[i*2] = taps[ntaps - 1 - i];
        q->taps[i*2+1] = taps[ntaps - 1 - i];
    }

    return q;
}

void firdecim_cf32_free(firdecim_cf32 q)
{
    free(q->taps);
    free(q->window);
    free(q);
}

void firdecim_cf32_reset(firdecim_cf32 q)
{
    q->idx = q->ntaps - 1;
}

static void push(firdecim_cf32 q, complex float x)
{
    if (q->idx == WINDOW_SIZE)
    {
        for (unsigned int i = 0; i < q->ntaps - 1; i++)
            q->window[i] = q->window[q->idx - q->ntaps + 1 + i];
        q->idx = q->ntaps - 1;
    }
    q->window[q->idx++] = x;
}

#ifdef HAVE_NEON
static float complex dotprod_32(const float complex *a, const float *b)
{
    float32x4_t s1;
    float32x4_t s2;
    float32x4_t sum = vdupq_n_f32(0.0f);

    for (int i = 0; i < 32; i += 4)
    {
        s1 = vmulq_f32(vld1q_f32((const float *)&a[i]), vld1q_f32(&b[i*2]));
        s2 = vmulq_f32(vld1q_f32((const float *)&a[i + 2]), vld1q_f32(&b[(i + 2)*2]));
        sum = vaddq_f32(sum, vaddq_f32(s1, s2));
    }

    float32x2x2_t sum2 = vuzp_f32(vget_low_f32(sum), vget_high_f32(sum));
    float32x2_t out = vpadd_f32(sum2.val[0], sum2.val[1]);

    float complex result[2];
    vst1_f32((float*)&result, out);

    return result[0];
}
#else
static float complex dotprod_32(const float complex *a, const float *b)
{
    float complex sum = { 0 };
    int i;

    for (i = 1; i < 16; i++)
    {
        sum += (a[i] + a[32-i]) * b[i * 2];
    }
    sum += a[i] * b[i * 2];

    return sum;
}
#endif

#ifdef HAVE_NEON
static float complex dotprod_halfband_4(const float complex *a, const float *b)
{
    float complex pairs[4];
    int i;

    for (i = 0; i < 7; i += 2)
    {
        pairs[i/2] = a[i] + a[14-i];
    }

    float32x4_t prod_lo =
        vmulq_f32(vld1q_f32((float*)pairs), vld1q_f32((float*)b));
    float32x4_t prod_hi =
        vmulq_f32(vld1q_f32((float*)pairs + 4), vld1q_f32((float*)b + 4));

    float32x4x2_t prod2 = vuzpq_f32(prod_lo, prod_hi);
    float32x2_t real = vadd_f32(vget_low_f32(prod2.val[0]), vget_high_f32(prod2.val[0]));
    float32x2_t imag = vadd_f32(vget_low_f32(prod2.val[1]), vget_high_f32(prod2.val[1]));

    float32x2_t sum = vpadd_f32(real, imag);

    float complex result[2];
    vst1_f32((float*)&result, sum);

    result[0] += a[7];
    return result[0];
}
#else
static float complex dotprod_halfband_4(const float complex *a, const float *b)
{
    float complex sum = { 0 };
    int i;

    for (i = 0; i < 7; i += 2)
    {
        sum += (a[i] + a[14-i]) * b[i];
    }
    sum += a[7];

    return sum;
}
#endif

void fir_cf32_execute(firdecim_cf32 q, const float complex *x, float complex *y)
{
    push(q, x[0]);
    *y = dotprod_32(&q->window[q->idx - q->ntaps], q->taps);
}

void halfband_cf32_execute(firdecim_cf32 q, const float complex *x, float complex *y)
{
    push(q, x[0]);
    *y = dotprod_halfband_4(&q->window[q->idx - q->ntaps], q->taps);
    push(q, x[1]);
}

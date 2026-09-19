/* 10-band cascaded peaking biquad EQ, stereo, float32 in/out.
 * Direct Form I biquad per band per channel, RBJ peaking coefficients
 * computed on the C side so Python only ever passes gains in dB.
 *
 * Built as a DLL and called from Python via ctypes.
 */
#include <math.h>
#include <string.h>
#include <stdlib.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

#define N_BANDS 10
#define MAX_CHANNELS 2
#define PI 3.14159265358979323846

typedef struct {
    double b0, b1, b2, a1, a2;
} BiquadCoeffs;

typedef struct {
    /* per band per channel: z1, z2 (Direct Form II Transposed state) */
    double z1[N_BANDS][MAX_CHANNELS];
    double z2[N_BANDS][MAX_CHANNELS];
    BiquadCoeffs coeffs[N_BANDS];
    int sample_rate;
    int channels;
} EqState;

static const double BAND_FREQS[N_BANDS] = {
    31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0
};

EXPORT EqState *eq_create(int sample_rate, int channels) {
    /* sample_rate が 0 以下だと eq_set_band_db の w0 計算がゼロ除算になり、
     * 係数が NaN になって音声経路へ NaN が流れ込む（実測で確認済み）。
     * 呼び出し側（Python）でも弾いているが、ここでも必ず止める。
     * NULL を返せば以降の関数はすべて何もせず戻る。 */
    if (sample_rate <= 0) return NULL;
    EqState *st = (EqState *)calloc(1, sizeof(EqState));
    if (!st) return NULL;
    st->sample_rate = sample_rate;
    st->channels = channels < 1 ? 1 : (channels > MAX_CHANNELS ? MAX_CHANNELS : channels);
    for (int i = 0; i < N_BANDS; i++) {
        st->coeffs[i].b0 = 1.0;
        st->coeffs[i].b1 = 0.0;
        st->coeffs[i].b2 = 0.0;
        st->coeffs[i].a1 = 0.0;
        st->coeffs[i].a2 = 0.0;
    }
    return st;
}

EXPORT void eq_destroy(EqState *st) {
    free(st);
}

/* RBJ Audio EQ Cookbook peaking filter, Q fixed at 1.0 (one octave-ish bandwidth). */
EXPORT void eq_set_band_db(EqState *st, int index, double gain_db) {
    if (!st || index < 0 || index >= N_BANDS) return;
    /* NaN / ±Inf が来ると係数が NaN になり、以後の音声がすべて NaN になる。
     * Python 側でも弾いているが、ここでは更新を拒否して今の係数を保つ。
     * 係数更新は GUI 操作時だけなので、音声処理の速度には影響しない。 */
    if (!isfinite(gain_db)) return;
    if (gain_db > 24.0) gain_db = 24.0;
    if (gain_db < -24.0) gain_db = -24.0;
    double f0 = BAND_FREQS[index];
    double sr = (double)st->sample_rate;
    double Q = 1.0;
    double A = pow(10.0, gain_db / 40.0);
    double w0 = 2.0 * PI * f0 / sr;
    double alpha = sin(w0) / (2.0 * Q);
    double cosw0 = cos(w0);

    double b0 = 1.0 + alpha * A;
    double b1 = -2.0 * cosw0;
    double b2 = 1.0 - alpha * A;
    double a0 = 1.0 + alpha / A;
    double a1 = -2.0 * cosw0;
    double a2 = 1.0 - alpha / A;

    st->coeffs[index].b0 = b0 / a0;
    st->coeffs[index].b1 = b1 / a0;
    st->coeffs[index].b2 = b2 / a0;
    st->coeffs[index].a1 = a1 / a0;
    st->coeffs[index].a2 = a2 / a0;
}

EXPORT void eq_reset(EqState *st) {
    if (!st) return;
    memset(st->z1, 0, sizeof(st->z1));
    memset(st->z2, 0, sizeof(st->z2));
}

/* block: interleaved float32, shape (frames, channels), processed in place.
 * Direct Form II Transposed, cascaded through N_BANDS in series per channel. */
EXPORT void eq_process(EqState *st, float *block, int frames) {
    if (!st || !block) return;
    int channels = st->channels;
    for (int band = 0; band < N_BANDS; band++) {
        double b0 = st->coeffs[band].b0;
        double b1 = st->coeffs[band].b1;
        double b2 = st->coeffs[band].b2;
        double a1 = st->coeffs[band].a1;
        double a2 = st->coeffs[band].a2;
        for (int ch = 0; ch < channels; ch++) {
            double z1 = st->z1[band][ch];
            double z2 = st->z2[band][ch];
            for (int n = 0; n < frames; n++) {
                double x = (double)block[n * channels + ch];
                double y = b0 * x + z1;
                z1 = b1 * x - a1 * y + z2;
                z2 = b2 * x - a2 * y;
                block[n * channels + ch] = (float)y;
            }
            st->z1[band][ch] = z1;
            st->z2[band][ch] = z2;
        }
    }
}

/* 現在の係数における、指定周波数でのカスケード全体の実際の効き[dB]を返す。
 * GUI側の周波数特性グラフ用。RBJ式をPython側で二重管理しないよう、
 * 実際に音声処理で使っている係数から直接計算する。
 * 直列（カスケード）接続なので、各バンドの dB は単純に足し合わせればよい
 * （|H1*H2| = |H1|*|H2| なので、dB（=20log10）は加算になる）。 */
EXPORT double eq_get_response_db(EqState *st, double freq_hz) {
    if (!st || st->sample_rate <= 0) return 0.0;
    double w = 2.0 * PI * freq_hz / (double)st->sample_rate;
    double cos1 = cos(w), sin1 = sin(w);
    double cos2 = cos(2.0 * w), sin2 = sin(2.0 * w);
    double total_db = 0.0;

    for (int band = 0; band < N_BANDS; band++) {
        double b0 = st->coeffs[band].b0;
        double b1 = st->coeffs[band].b1;
        double b2 = st->coeffs[band].b2;
        double a1 = st->coeffs[band].a1;
        double a2 = st->coeffs[band].a2;

        /* 分子 = b0 + b1*e^-jw + b2*e^-2jw、分母 = 1 + a1*e^-jw + a2*e^-2jw */
        double num_re = b0 + b1 * cos1 + b2 * cos2;
        double num_im = -b1 * sin1 - b2 * sin2;
        double den_re = 1.0 + a1 * cos1 + a2 * cos2;
        double den_im = -a1 * sin1 - a2 * sin2;

        double num_mag2 = num_re * num_re + num_im * num_im;
        double den_mag2 = den_re * den_re + den_im * den_im;
        if (den_mag2 < 1e-30) den_mag2 = 1e-30;
        double mag2 = num_mag2 / den_mag2;
        if (mag2 < 1e-30) mag2 = 1e-30;

        total_db += 10.0 * log10(mag2); /* 10*log10(mag^2) == 20*log10(mag) */
    }
    return total_db;
}

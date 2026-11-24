"""Generate, demodulate, and inspect three signals in GNU Radio."""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile
from gnuradio import analog, blocks, filter, gr
from gnuradio.fft import window


AUDIO_RATE = 48_000
AM_RATE = 4_800_000
AM_CARRIER = 2_000_000
AM_DEPTH = 0.7
FM_IQ_RATE = 480_000
FM_RF_RATE = 240_000_000
FM_CARRIER = 100_000_000
FM_DEVIATION = 50_000
TONES = (1_000, 4_000, 10_000)


class PreviewSink(gr.sync_block):
    def __init__(self, dtype, count):
        super().__init__('RF preview', in_sig=[dtype], out_sig=[])
        self.samples = np.empty(count, dtype=dtype)
        self.count = 0

    def work(self, input_items, output_items):
        samples = input_items[0]
        keep = min(len(samples), len(self.samples) - self.count)
        self.samples[self.count:self.count + keep] = samples[:keep]
        self.count += keep
        return len(samples)


def reference_audio(seconds):
    t = np.arange(round(seconds * AUDIO_RATE)) / AUDIO_RATE
    return sum(gain * np.sin(2 * np.pi * freq * t) for gain, freq in
               zip((0.48, 0.30, 0.22), TONES)).astype(np.float32)


def audio_decimator(rate):
    taps = filter.firdes.low_pass(1, rate, 13_500, 6_000, window.WIN_HAMMING)
    return filter.fir_filter_fff(rate // AUDIO_RATE, taps)


def run_graph(mode, message):
    graph = gr.top_block(f'{mode} modulation and demodulation')
    source = blocks.vector_source_f(message.tolist(), False)
    recovered = blocks.vector_sink_f()
    rate = FM_IQ_RATE if mode == 'fm' else AM_RATE
    upsample = filter.rational_resampler_fff(interpolation=rate // AUDIO_RATE,
                                             decimation=1)
    graph.connect(source, upsample)

    if mode == 'fm':
        sensitivity = 2 * np.pi * FM_DEVIATION / rate
        modulated = analog.frequency_modulator_fc(sensitivity)
        demodulated = analog.quadrature_demod_cf(1 / sensitivity)
        preview = PreviewSink(np.complex64, rate // 50)
        graph.connect(upsample, modulated, demodulated, audio_decimator(rate), recovered)
    else:
        carrier = analog.sig_source_f(rate, analog.GR_COS_WAVE, AM_CARRIER, 1, 0)
        modulated = blocks.multiply_ff()
        graph.connect(carrier, (modulated, 1))
        preview = PreviewSink(np.float32, rate // 50)
        if mode == 'dsb-sc':
            mixer = blocks.multiply_ff()
            graph.connect(upsample, modulated, mixer, audio_decimator(rate),
                          blocks.multiply_const_ff(2), recovered)
            graph.connect(carrier, (mixer, 1))
        else:
            graph.connect(upsample, blocks.multiply_const_ff(AM_DEPTH),
                          blocks.add_const_ff(1), modulated, filter.hilbert_fc(129),
                          blocks.complex_to_mag(), audio_decimator(rate),
                          blocks.add_const_ff(-1), blocks.multiply_const_ff(1 / AM_DEPTH),
                          recovered)
    graph.connect(modulated, preview)
    graph.run()
    audio = np.asarray(recovered.data(), dtype=np.float32)
    if mode != 'fm':
        return audio, preview.samples[:preview.count], rate, AM_CARRIER

    # Interpolate the complex envelope first so the 100 MHz carrier is below Nyquist.
    iq = preview.samples[:preview.count]
    ratio = FM_RF_RATE // FM_IQ_RATE
    iq_rf = signal.resample_poly(iq[FM_IQ_RATE // 500: FM_IQ_RATE * 14 // 1000], ratio, 1)
    carrier_phase = 2 * np.pi * (FM_CARRIER / FM_RF_RATE) * np.arange(iq_rf.size)
    rf = (iq_rf.real * np.cos(carrier_phase) - iq_rf.imag * np.sin(carrier_phase))
    return audio, rf.astype(np.float32), FM_RF_RATE, FM_CARRIER


def align_audio(reference, recovered):
    # The input repeats every millisecond, so the selected lag only aligns the plots.
    trim = AUDIO_RATE // 20
    search = AUDIO_RATE // 100
    n = min(reference.size, recovered.size) - 2 * trim - search
    if n < AUDIO_RATE // 10:
        raise RuntimeError('The flowgraph returned too little audio to validate.')
    x = reference[trim:trim + n]
    best = None
    for lag in range(search + 1):
        y = recovered[trim + lag:trim + lag + n]
        score = np.linalg.norm(x - y)
        if best is None or score < best[0]:
            best = (score, lag, y)
    _, coarse_lag, _ = best
    # The RF envelope filter can introduce a fraction of one 48 kHz sample.
    # Interpolate before comparison so rounding that delay does not penalize 10 kHz.
    factor = 16
    oversampled = signal.resample_poly(recovered, factor, 1)
    best = None
    for fine_lag in range(max(0, coarse_lag * factor - factor), (coarse_lag + 1) * factor + 1):
        offset = trim * factor + fine_lag
        y = oversampled[offset:offset + n * factor:factor]
        score = np.linalg.norm(x - y)
        if best is None or score < best[0]:
            best = (score, fine_lag / factor, y)
    error, lag, y = best
    return x, y, {
        'alignment_samples': lag,
        'correlation': float(np.corrcoef(x, y)[0, 1]),
        'normalized_rmse': float(error / np.linalg.norm(x)),
        'tone_gain_db': {str(f): float(20 * np.log10(tone_amplitude(y, f) /
                                                   tone_amplitude(x, f))) for f in TONES},
    }


def tone_amplitude(waveform, frequency):
    n = waveform.size // 48 * 48  # an integer number of periods of every tone
    t = np.arange(n) / AUDIO_RATE
    return 2 * abs(np.dot(waveform[:n], np.exp(-2j * np.pi * frequency * t))) / n


def spectrum(rf, rate):
    # Discard preview boundary transients. Use complete milliseconds for coherent bins.
    data = rf[rate // 1000: rate * 11 // 1000]
    weights = signal.windows.hann(data.size, sym=False)
    amplitudes = 2 * np.abs(np.fft.rfft(data * weights)) / weights.sum()
    return np.fft.rfftfreq(data.size, 1 / rate), amplitudes


def save_plot(path, mode, x, y, rf, rate, carrier, frequencies, amplitudes, show):
    import matplotlib
    if not show:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), layout='constrained')
    fig.suptitle(f'{mode.upper()} modulation and demodulation')
    n = AUDIO_RATE * 3 // 1000
    t = np.arange(n) * 1000 / AUDIO_RATE
    axes[0].plot(t, x[:n], label='Input')
    axes[0].plot(t, y[:n], '--', label='Recovered')
    axes[0].set(xlabel='Time (ms)', ylabel='Audio amplitude')
    axes[0].legend()
    n = round(10 * rate / carrier)
    start = rate // 250
    t = np.arange(n) * 1e9 / rate
    axes[1].plot(t, rf[start:start + n], '.-', markersize=2)
    axes[1].set(xlabel='Time (ns)', ylabel='RF amplitude')
    width = 100_000 if mode == 'fm' else 22_000
    select = np.abs(frequencies - carrier) <= width
    axes[2].plot(frequencies[select] / 1e6,
                 20 * np.log10(np.maximum(amplitudes[select], 1e-6)))
    axes[2].ticklabel_format(axis='x', style='plain', useOffset=False)
    axes[2].set(xlabel='RF frequency (MHz)', ylabel='Amplitude (dBFS)', ylim=(-100, 5))
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.savefig(path, dpi=150)
    if not show:
        plt.close(fig)


def write_wav(path, audio):
    wavfile.write(path, AUDIO_RATE, np.rint(np.clip(audio, -1, 1) * 32767).astype(np.int16))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('all', 'dsb-sc', 'am', 'fm'), default='all')
    parser.add_argument('--seconds', type=float, default=2)
    parser.add_argument('--output', type=Path, default=Path('output/modulation'))
    parser.add_argument('--show', action='store_true')
    args = parser.parse_args()
    if not 0.25 <= args.seconds <= 15:
        parser.error('--seconds must be between 0.25 and 15')
    args.output.mkdir(parents=True, exist_ok=True)
    audio = reference_audio(args.seconds)
    write_wav(args.output / 'baseband.wav', audio)
    passed = True
    for mode in ('dsb-sc', 'am', 'fm') if args.mode == 'all' else (args.mode,):
        recovered, rf, rate, carrier = run_graph(mode, audio)
        x, y, result = align_audio(audio, recovered)
        f, a = spectrum(rf, rate)
        result['rf_sample_rate_hz'] = rate
        result['carrier_hz'] = carrier
        if mode == 'fm':
            band = abs(f - carrier) <= 120_000
            result['spectral_centroid_hz'] = float(np.average(f[band], weights=a[band] ** 2))
            rf_passed = abs(result['spectral_centroid_hz'] - carrier) <= 500
        else:
            carrier_amp = a[np.argmin(abs(f - carrier))]
            sideband_amp = min(a[np.argmin(abs(f - carrier - sign * tone))]
                               for tone in TONES for sign in (-1, 1))
            result['carrier_dbfs'] = float(20 * np.log10(max(carrier_amp, 1e-12)))
            result['weakest_tone_sideband_dbfs'] = float(20 * np.log10(sideband_amp))
            rf_passed = sideband_amp > 0.02 and (carrier_amp < sideband_amp / 30 if mode == 'dsb-sc'
                                                else 0.95 <= carrier_amp <= 1.05)
        result['passed'] = bool(result['correlation'] >= 0.99 and result['normalized_rmse'] < 0.08
                                and all(abs(gain) < 1 for gain in result['tone_gain_db'].values())
                                and rf_passed)
        write_wav(args.output / f'{mode}_recovered.wav', recovered)
        save_plot(args.output / f'{mode}.png', mode, x, y, rf, rate, carrier, f, a, args.show)
        (args.output / f'{mode}.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(mode, json.dumps(result))
        passed &= result['passed']
    if args.show:
        import matplotlib.pyplot as plt
        plt.show()
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())

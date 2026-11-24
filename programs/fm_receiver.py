"""Receive wideband FM with a USB RTL-SDR and GNU Radio."""

import argparse
from pathlib import Path
import signal
import sys

from gnuradio import analog, audio, blocks, filter, gr
from gnuradio.fft import window


RF_RATE = 2_400_000
CHANNEL_RATE = 480_000
AUDIO_RATE = 48_000
TUNING_OFFSET = 250_000
MAX_DEVIATION = 75_000
DEEMPHASIS = 50e-6


def channelize(graph, source):
    from math import pi

    channel_taps = filter.firdes.low_pass(1, RF_RATE, 105_000, 40_000, window.WIN_HAMMING)
    channel = filter.freq_xlating_fir_filter_ccf(RF_RATE // CHANNEL_RATE, channel_taps,
                                                   -TUNING_OFFSET, RF_RATE)
    discriminator = analog.quadrature_demod_cf(CHANNEL_RATE / (2 * pi * MAX_DEVIATION))
    audio_taps = filter.firdes.low_pass(1, CHANNEL_RATE, 15_500, 6_000, window.WIN_HAMMING)
    low_pass = filter.fir_filter_fff(CHANNEL_RATE // AUDIO_RATE, audio_taps)
    dc_block = filter.dc_blocker_ff(128, True)
    deemphasis = analog.fm_deemph(AUDIO_RATE, DEEMPHASIS)
    graph.connect(source, channel, discriminator, low_pass, dc_block, deemphasis)
    return deemphasis


class BroadcastReceiver(gr.top_block):
    def __init__(self, args, extra_sink=None):
        super().__init__('SDR6 V4 FM receiver')
        import osmosdr

        lo = args.frequency * 1e6 + TUNING_OFFSET
        self.radio = osmosdr.source(args=f'numchan=1 rtl={args.device}')
        self.radio.set_sample_rate(RF_RATE)
        self.radio.set_center_freq(lo, 0)
        self.radio.set_freq_corr(args.ppm, 0)
        self.radio.set_gain_mode(False, 0)
        self.radio.set_gain(args.gain, 0)
        print(f'Station {args.frequency:.3f} MHz | LO {self.radio.get_center_freq(0) / 1e6:.3f} MHz '
              f'| gain {self.radio.get_gain(0):g} dB | RF {self.radio.get_sample_rate():g} S/s',
              flush=True)

        source = self.radio
        if args.seconds and args.no_gui:
            source = blocks.head(gr.sizeof_gr_complex, round(RF_RATE * args.seconds))
            self.connect(self.radio, source)
        baseband = channelize(self, source)
        if not args.no_audio:
            volume = blocks.multiply_const_ff(args.volume)
            self.connect(baseband, volume, audio.sink(AUDIO_RATE, '', True))
        if args.wav:
            args.wav.parent.mkdir(parents=True, exist_ok=True)
            self.connect(baseband, blocks.wavfile_sink(str(args.wav), 1, AUDIO_RATE,
                                                       blocks.FORMAT_WAV, blocks.FORMAT_PCM_16,
                                                       False))
        if extra_sink is not None:
            self.extra_sink = extra_sink
            self.connect(baseband, self.extra_sink)
        if args.no_audio and not args.wav and extra_sink is None:
            self.connect(baseband, blocks.null_sink(gr.sizeof_float))

        if not args.no_gui:
            from gnuradio import qtgui
            from PyQt5 import QtWidgets, sip

            screen = qtgui.freq_sink_c(2048, window.WIN_BLACKMAN_HARRIS, lo, RF_RATE,
                                       'RF Spectrum', 1)
            screen.set_update_time(0.12)
            screen.set_y_axis(-90, 0)
            self.connect(source, screen)
            self.spectrum = screen
            self.display = sip.wrapinstance(screen.qwidget(), QtWidgets.QWidget)


def self_test():
    import numpy as np

    t = np.arange(RF_RATE // 3) / RF_RATE
    tone_hz = 1000
    # Place a stronger adjacent carrier above the desired channel and include a pilot.
    composite = 0.6 * np.sin(2 * np.pi * tone_hz * t) + 0.08 * np.sin(2 * np.pi * 19_000 * t)
    phase = 2 * np.pi * (-TUNING_OFFSET * t + MAX_DEVIATION * np.cumsum(composite) / RF_RATE)
    iq = np.exp(1j * phase) + 1.2 * np.exp(2j * np.pi * (-TUNING_OFFSET + 220_000) * t)
    graph = gr.top_block('Test off-center FM signal')
    source = blocks.vector_source_c(iq.astype(np.complex64).tolist(), False)
    sink = blocks.vector_sink_f()
    graph.connect(channelize(graph, source), sink)
    graph.run()
    recovered = np.asarray(sink.data())[AUDIO_RATE // 20:]
    n = min(recovered.size, AUDIO_RATE // 4)
    x = recovered[:n]
    spectrum = 2 * abs(np.fft.rfft(x)) / n
    frequencies = np.fft.rfftfreq(n, 1 / AUDIO_RATE)
    tone = spectrum[np.argmin(abs(frequencies - tone_hz))]
    pilot = spectrum[np.argmin(abs(frequencies - 19_000))]
    peak = frequencies[1 + np.argmax(spectrum[1:])]
    relative_pilot_db = 20 * np.log10(max(pilot / max(tone, 1e-12), 1e-12))
    print(f'Recovered {peak:.0f} Hz, amplitude {tone:.3f}, pilot {relative_pilot_db:.1f} dBc')
    return 0 if peak == tone_hz and 0.48 <= tone <= 0.66 and relative_pilot_db < -40 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frequency', type=float, default=106.1, help='station frequency in MHz')
    parser.add_argument('--device', type=int, default=0, help='RTL-SDR device index')
    parser.add_argument('--gain', type=float, default=28, help='manual tuner gain in dB')
    parser.add_argument('--ppm', type=int, default=0, help='frequency correction in ppm')
    parser.add_argument('--volume', type=float, default=0.8, help='audio output gain')
    parser.add_argument('--seconds', type=float, default=0, help='finite run length, 0 runs until stopped')
    parser.add_argument('--wav', type=Path, help='optional path for a 48 kHz mono WAV file')
    parser.add_argument('--no-audio', action='store_true')
    parser.add_argument('--no-gui', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not 87.5 <= args.frequency <= 108 or not 0 <= args.volume <= 4 or not 0 <= args.seconds:
        parser.error('expected FM band frequency, volume 0–4, and nonnegative duration')

    if args.no_gui:
        graph = BroadcastReceiver(args)
        graph.start()
        try:
            graph.wait()
        except KeyboardInterrupt:
            graph.stop()
            graph.wait()
        return 0

    from PyQt5 import QtCore, QtWidgets

    application = QtWidgets.QApplication(sys.argv[:1])
    graph = BroadcastReceiver(args)
    widget = QtWidgets.QWidget()
    widget.setWindowTitle(f'GNU Radio FM {args.frequency:.3f} MHz')
    layout = QtWidgets.QVBoxLayout(widget)
    layout.addWidget(graph.display)
    widget.resize(980, 450)
    signal.signal(signal.SIGINT, lambda *_: application.quit())
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(250)
    if args.seconds:
        QtCore.QTimer.singleShot(round(args.seconds * 1000), application.quit)
    graph.start()
    widget.show()
    try:
        return application.exec_()
    finally:
        graph.stop()
        graph.wait()


if __name__ == '__main__':
    raise SystemExit(main())

"""Plot the RF spectrum of the 2.4 MS/s interleaved unsigned RTL-SDR I/Q capture."""

import argparse
from pathlib import Path

import matplotlib
import numpy as np
from scipy import signal

from fm_receiver import RF_RATE, TUNING_OFFSET

matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path, help='raw interleaved unsigned 8-bit I/Q file')
    parser.add_argument('--frequency', type=float, default=106.1, help='station frequency in MHz')
    parser.add_argument('--output', type=Path, default=Path('output/fm/rf-1061.png'))
    args = parser.parse_args()
    fft_size = 8192
    raw = np.fromfile(args.recording, dtype=np.uint8)
    if raw.size % 2 or raw.size < 2 * fft_size:
        parser.error(f'expected at least {fft_size} complete I/Q pairs')

    i, q = (raw.reshape(-1, 2).astype(np.float32).T - 127.5) / 127.5
    offsets, psd = signal.welch(i + 1j * q, fs=RF_RATE, nperseg=fft_size,
                                return_onesided=False, detrend=False)
    lo = args.frequency * 1e6 + TUNING_OFFSET
    frequencies = (np.fft.fftshift(offsets) + lo) / 1e6
    power = 10 * np.log10(np.maximum(np.fft.fftshift(psd), np.finfo(psd.dtype).tiny))

    figure, axis = plt.subplots(figsize=(9, 4), layout='constrained')
    axis.plot(frequencies, power, linewidth=0.9)
    axis.axvline(args.frequency, color='tab:red', linestyle='--',
                 label=f'Station {args.frequency:.3f} MHz')
    axis.axvline(lo / 1e6, color='tab:gray', linestyle=':', label=f'LO {lo / 1e6:.3f} MHz')
    axis.set(title='RTL-SDR RF capture (Welch PSD)', xlabel='RF frequency (MHz)',
             ylabel='PSD (dB/Hz, normalized I/Q)')
    axis.grid(alpha=0.25)
    axis.legend()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=160)
    plt.close(figure)
    print(f'{raw.size // 2:,} I/Q pairs at {RF_RATE:,} S/s, saved {args.output}')


if __name__ == '__main__':
    main()

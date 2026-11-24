"""Measure relative playback delay between two audio tracks of a common recording."""

import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
from scipy import signal
from scipy.io import wavfile


RATE = 8_000
WINDOW_SECONDS = 8
STEP_SECONDS = 10
MIN_CORRELATION = 0.32
MIN_PEAK_MARGIN = 0.05
CLUSTER_SECONDS = 0.15


def ffmpeg(args, directory):
    log = directory / 'ffmpeg.log'
    with log.open('wb') as output:
        run = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                              *map(str, args)], stdout=output, stderr=subprocess.STDOUT)
    if run.returncode:
        with log.open('rb') as output:
            output.seek(max(0, log.stat().st_size - 4000))
            raise RuntimeError(output.read().decode(errors='replace'))


def load_tracks(recording, sdr_track, web_track, stereo=False):
    with TemporaryDirectory(prefix='radio-delay-') as temporary:
        directory = Path(temporary)
        paths = (directory / 'sdr.wav', directory / 'web.wav')
        command = ['-copyts', '-start_at_zero', '-i', recording]
        for index, path, channel in zip((sdr_track, web_track), paths, ('FL', 'FR')):
            select = f'pan=mono|c0={channel},' if stereo else ''
            command.extend(['-map', f'0:a:{index}', '-vn', '-ac', '1', '-af',
                            f'{select}aresample={RATE}:async=1000:first_pts=0', '-c:a', 'pcm_f32le', path])
        ffmpeg(command, directory)
        return tuple(wavfile.read(path)[1] for path in paths)


def preprocess(samples):
    bandpass = signal.butter(4, (300, 3300), 'bandpass', fs=RATE, output='sos')
    return signal.sosfiltfilt(bandpass, np.asarray(samples, dtype=np.float64))


def best_match(template, search):
    n = len(template)
    template = template - template.mean()
    norm = np.linalg.norm(template)
    if norm < 0.001 * np.sqrt(n):
        return None
    numerator = signal.correlate(search, template, mode='valid', method='fft')
    cumulative = np.r_[0, np.cumsum(search)]
    cumulative_sq = np.r_[0, np.cumsum(search ** 2)]
    energy = cumulative_sq[n:] - cumulative_sq[:-n]
    energy -= (cumulative[n:] - cumulative[:-n]) ** 2 / n
    useful = energy > n * 1e-6
    correlations = np.divide(numerator, norm * np.sqrt(np.maximum(energy, 0)),
                             out=np.zeros_like(numerator), where=useful)
    strength = abs(correlations)
    peak = int(np.argmax(strength))
    exclusion = RATE // 10
    second = max(strength[:max(0, peak - exclusion)].max(initial=0),
                 strength[peak + exclusion + 1:].max(initial=0))
    return peak, float(correlations[peak]), float(strength[peak] - second)


def estimate(sdr, web, max_lag_seconds):
    n = WINDOW_SECONDS * RATE
    max_lag = round(max_lag_seconds * RATE)
    windows = []
    for start in range(RATE, len(sdr) - n, STEP_SECONDS * RATE):
        first = max(0, start - max_lag)
        last = min(len(web), start + max_lag + n)
        if last - first < n:
            continue
        match = best_match(sdr[start:start + n], web[first:last])
        if match is None:
            continue
        peak, r, margin = match
        lag = (first + peak - start) / RATE
        windows.append({'sdr_start_seconds': start / RATE, 'delay_seconds': lag,
                        'correlation': r, 'peak_margin': margin,
                        'accepted': abs(r) >= MIN_CORRELATION and margin >= MIN_PEAK_MARGIN})

    candidates = [row for row in windows if row['accepted']]
    clusters = [[row for row in candidates
                 if abs(row['delay_seconds'] - anchor['delay_seconds']) <= CLUSTER_SECONDS]
                for anchor in candidates]
    winning = max(clusters, key=len, default=[])
    for row in windows:
        row['in_consensus'] = row in winning
    result = {'sign_convention': 'positive = web playback later than SDR playback',
              'window_seconds': WINDOW_SECONDS, 'search_limit_seconds': max_lag_seconds,
              'sdr_duration_seconds': len(sdr) / RATE, 'web_duration_seconds': len(web) / RATE,
              'matched_windows': len(winning), 'windows': windows}
    if len(winning) < 3 or len(winning) * 2 <= len(candidates):
        result['status'] = 'no_consensus'
        return result
    delays = [row['delay_seconds'] for row in winning]
    result.update(status='matched', median_delay_seconds=float(np.median(delays)),
                  min_delay_seconds=min(delays), max_delay_seconds=max(delays),
                  median_abs_correlation=float(np.median([abs(row['correlation']) for row in winning])))
    return result


def plot_alignment(destination, sdr, web, result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    matching = [row for row in result['windows'] if row['in_consensus']]
    exemplar = max(matching, key=lambda row: abs(row['correlation']))
    start = round(exemplar['sdr_start_seconds'] * RATE)
    delay = round(exemplar['delay_seconds'] * RATE)
    segment = sdr[start:start + WINDOW_SECONDS * RATE].reshape(-1, RATE // 8)
    index = int(np.argmax(np.mean(segment ** 2, axis=1))) * segment.shape[1]
    first = start + index
    n = RATE // 8
    x = sdr[first:first + n]
    y = web[first + delay:first + delay + n] * np.sign(exemplar['correlation'])
    t = np.arange(n) * 1000 / RATE
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6), layout='constrained')
    top.plot(t, x / np.max(abs(x)), label='SDR')
    top.plot(t, y / np.max(abs(y)), '--', label='Web (shifted)')
    top.set(xlabel='Time from aligned excerpt (ms)', ylabel='Normalized amplitude')
    top.legend()
    bottom.scatter([row['sdr_start_seconds'] for row in matching],
                   [row['delay_seconds'] for row in matching], label='Matched windows')
    bottom.axhline(result['median_delay_seconds'], linestyle='--', label='Median')
    bottom.set(xlabel='SDR recording time (s)', ylabel='Web minus SDR delay (s)')
    bottom.ticklabel_format(axis='y', style='plain', useOffset=False)
    bottom.legend()
    fig.savefig(destination, dpi=150)
    plt.close(fig)


def self_test():
    rng = np.random.default_rng(1061)
    envelope = np.interp(np.arange(RATE * 44), np.arange(441) * (RATE // 10), rng.uniform(0.08, 1, 441))
    x = rng.normal(0, 0.2, envelope.size) * envelope
    x_filtered = preprocess(x)
    for delay in (int(2.375 * RATE), -int(1.125 * RATE)):
        y = np.zeros_like(x)
        if delay > 0:
            y[delay:] = x[:-delay]
        else:
            y[:delay] = x[-delay:]
        y = np.tanh(y * 1.7) + rng.normal(0, 0.015, len(y))
        result = estimate(x_filtered, preprocess(y), 5)
        actual = result.get('median_delay_seconds', np.inf)
        print(f'Synthetic delay {delay / RATE:+.3f} s, measured {actual:+.3f} s')
        if abs(actual - delay / RATE) > 2 / RATE:
            return 1
    false_match = estimate(x_filtered, preprocess(rng.normal(0, 0.2, len(x))), 5)
    print(f'Unrelated audio: {false_match["status"]}')
    if false_match['status'] != 'no_consensus':
        return 1

    with TemporaryDirectory(prefix='radio-delay-test-') as temporary:
        directory = Path(temporary)
        source = directory / 'source.wav'
        recording = directory / 'tracks.mkv'
        wavfile.write(source, RATE, x.astype(np.float32))
        ffmpeg(['-i', source, '-itsoffset', '1.5', '-i', source, '-map', '0:a:0',
                '-map', '1:a:0', '-c:a', 'pcm_s16le', recording], directory)
        sdr, web = map(preprocess, load_tracks(recording, 0, 1))
        result = estimate(sdr, web, 5)
        actual = result.get('median_delay_seconds', np.inf)
        print(f'MKV track offset +1.500 s, measured {actual:+.3f} s')
        return 0 if abs(actual - 1.5) <= 0.001 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', nargs='?', type=Path)
    parser.add_argument('--sdr-track', type=int, default=0, help='zero-based audio track index')
    parser.add_argument('--web-track', type=int, default=1, help='zero-based audio track index')
    parser.add_argument('--stereo', action='store_true', help='use left and right channels of audio track 0')
    parser.add_argument('--max-lag', type=float, default=60, help='maximum absolute delay in seconds')
    parser.add_argument('--output', type=Path, help='write JSON and a matching-interval PNG')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.recording:
        parser.error('specify a recording')
    if not args.stereo and (min(args.sdr_track, args.web_track) < 0 or args.sdr_track == args.web_track):
        parser.error('specify two different nonnegative audio track indexes')
    if not 0 < args.max_lag <= 300:
        parser.error('--max-lag must be between 0 and 300 seconds')
    if args.output and args.output.suffix.lower() != '.json':
        parser.error('--output must be a JSON path')
    if args.stereo:
        args.sdr_track = args.web_track = 0
    try:
        tracks = load_tracks(args.recording, args.sdr_track, args.web_track, args.stereo)
    except (RuntimeError, OSError) as error:
        parser.exit(2, f'Cannot decode recording: {error}\n')
    if min(map(len, tracks)) < RATE * WINDOW_SECONDS:
        parser.exit(2, 'The audio tracks must each be at least eight seconds long.\n')
    sdr, web = map(preprocess, tracks)
    result = estimate(sdr, web, args.max_lag)
    result['recording'] = str(args.recording)
    result['sdr_track'] = args.sdr_track
    result['web_track'] = args.web_track
    result['stereo_left_sdr_right_web'] = args.stereo
    content = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding='utf-8')
        if result['status'] == 'matched':
            plot_alignment(args.output.with_suffix('.png'), sdr, web, result)
    print(content, end='')
    return 0 if result['status'] == 'matched' else 1


if __name__ == '__main__':
    raise SystemExit(main())

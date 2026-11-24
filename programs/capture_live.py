"""Record FM and the CNR live feed on one stereo playback clock."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread
import time
from urllib.request import urlopen

from gnuradio import gr
import numpy as np
from scipy.io import wavfile

from fm_receiver import AUDIO_RATE, BroadcastReceiver


CATALOG = 'https://apppc.cnr.cn/national'
CNR_CHANNEL = 'https://apicnrapp.cnr.cn/html/play.html?channelId=1'
BLOCK = AUDIO_RATE // 50
PREROLL = AUDIO_RATE // 5


class PcmBuffer:
    def __init__(self):
        self.samples = np.empty(AUDIO_RATE, dtype=np.float32)
        self.read = self.written = self.dropped = self.missing = 0
        self.lock = Lock()

    def available(self):
        with self.lock:
            return self.written - self.read

    def trim_preroll(self):
        with self.lock:
            self.read = max(self.read, self.written - PREROLL)
            self.dropped = self.missing = 0

    def push(self, samples):
        if len(samples) > len(self.samples):
            samples = samples[-len(self.samples):]
        with self.lock:
            start = self.written % len(self.samples)
            split = min(len(samples), len(self.samples) - start)
            self.samples[start:start + split] = samples[:split]
            self.samples[:len(samples) - split] = samples[split:]
            self.written += len(samples)
            overflow = max(0, self.written - self.read - len(self.samples))
            self.read += overflow
            self.dropped += overflow

    def pop_into(self, destination):
        with self.lock:
            count = min(len(destination), self.written - self.read)
            start = self.read % len(self.samples)
            split = min(count, len(self.samples) - start)
            destination[:split] = self.samples[start:start + split]
            destination[split:count] = self.samples[:count - split]
            destination[count:] = 0
            self.read += count
            self.missing += len(destination) - count


class CaptureSink(gr.sync_block):
    def __init__(self, buffer):
        super().__init__('FM PCM capture', in_sig=[np.float32], out_sig=[])
        self.buffer = buffer

    def work(self, input_items, output_items):
        samples = input_items[0]
        self.buffer.push(samples)
        return len(samples)


def live_url():
    with urlopen(CATALOG, timeout=20) as response:
        channels = json.load(response)['data']['categories']
    for category in channels:
        for entry in category['detail']:
            if entry.get('other_info8') == CNR_CHANNEL:
                return entry['other_info11'][0]['url']
    raise RuntimeError('CNR channel 1 was not found in the official catalog.')


def pump_decoder(pipe, buffer):
    while data := pipe.read(BLOCK * np.dtype('<f4').itemsize):
        size = len(data) // 4 * 4
        buffer.push(np.frombuffer(data[:size], dtype='<f4'))


def capture(args, output):
    sdr, web = PcmBuffer(), PcmBuffer()
    args.no_audio = args.no_gui = True
    args.seconds = 0
    args.wav = None
    receiver = BroadcastReceiver(args, CaptureSink(sdr))
    pcm = np.empty((round(args.duration * AUDIO_RATE), 2), dtype=np.float32)
    position = 0
    output_events = 0
    finished = Event()

    def fill(out, frames):
        nonlocal position
        count = min(frames, len(pcm) - position)
        segment = pcm[position:position + count]
        sdr.pop_into(segment[:, 0])
        web.pop_into(segment[:, 1])
        out[:count] = 0.6 * segment
        out[count:] = 0
        position += count

    def play(out, frames, time_info, status):
        nonlocal output_events
        output_events += bool(status)
        fill(out, frames)
        if position == len(pcm):
            raise sounddevice.CallbackStop

    if not args.no_playback:
        import sounddevice

    decoder_log = output.with_suffix('.ffmpeg.log')
    with decoder_log.open('wb') as diagnostics:
        decoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                                    '-rw_timeout', '10000000', '-live_start_index', '-3', '-re',
                                    '-i', live_url(), '-vn', '-ac', '1', '-ar', str(AUDIO_RATE),
                                    '-f', 'f32le', 'pipe:1'], stdout=subprocess.PIPE,
                                   stderr=diagnostics)
        reader = Thread(target=pump_decoder, args=(decoder.stdout, web), daemon=True)
        reader.start()
        receiver.start()
        try:
            deadline = time.monotonic() + 35
            while min(sdr.available(), web.available()) < PREROLL:
                if decoder.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f'Could not preroll both sources. See {decoder_log}')
                time.sleep(0.02)
            sdr.trim_preroll()
            web.trim_preroll()
            started = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
            print(f'Capturing two sources for {args.duration:g} s from {started}', flush=True)
            if args.no_playback:
                out = np.empty((BLOCK, 2), dtype=np.float32)
                clock_start = time.monotonic()
                while position < len(pcm):
                    time.sleep(max(0, clock_start + position / AUDIO_RATE - time.monotonic()))
                    fill(out, BLOCK)
            else:
                with sounddevice.OutputStream(samplerate=AUDIO_RATE, channels=2, blocksize=BLOCK,
                                              callback=play, finished_callback=finished.set):
                    if not finished.wait(args.duration + 10) or position != len(pcm):
                        raise RuntimeError('Audio output stopped before completing the capture.')
            issues = {name: {'missing_frames': buf.missing, 'dropped_frames': buf.dropped}
                      for name, buf in (('sdr_left', sdr), ('cnr_right', web))}
        finally:
            receiver.stop()
            receiver.wait()
            decoder.terminate()
            decoder.wait(timeout=10)
            reader.join(timeout=2)
            decoder.stdout.close()
    encoded = np.rint(np.clip(pcm, -1, 1) * 32767).astype(np.int16)
    wavfile.write(output, AUDIO_RATE, encoded)
    details = {'started_at': started, 'frequency_mhz': args.frequency, 'duration_seconds': len(pcm) / AUDIO_RATE,
               'web_channel': CNR_CHANNEL, 'player': 'FFmpeg HLS', 'hls_live_start_index': -3,
               'clock': 'monotonic software clock' if args.no_playback else 'stereo audio output clock',
               'per_input_preroll_seconds': PREROLL / AUDIO_RATE, 'audio_output_events': output_events,
               'input_buffer_events': issues}
    output.with_suffix('.capture.json').write_text(json.dumps(details, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(details, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/fm/cnr-1061.wav'))
    parser.add_argument('--duration', type=float, default=180)
    parser.add_argument('--frequency', type=float, default=106.1, help='FM broadcast frequency in MHz')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--gain', type=float, default=28)
    parser.add_argument('--ppm', type=int, default=0)
    parser.add_argument('--no-playback', action='store_true', help='capture to a monotonic clock without audio output')
    args = parser.parse_args()
    if not 87.5 <= args.frequency <= 108 or not 30 <= args.duration <= 600:
        parser.error('expected an FM broadcast frequency and a duration of 30–600 seconds')
    if args.output.suffix.lower() != '.wav':
        parser.error('--output must be a WAV path')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    capture(args, args.output)


if __name__ == '__main__':
    main()

"""语音样本的客观分析 —— 让"音质好不好"变成可比较的数字。

为什么要有它（src/media/STATUS.md §5 的教训之一）：
自研音频那一轮发现过"纯重建增益 -5.5 dB"这种**不崩、不报错、日志全正常**的缺陷，
只靠耳朵听很可能归因成"算法就这样"。所以凡是音量/降噪/音质结论，都要有数字兜底。

用法：
    python build\\analyze-voice.py <a.wav> [b.wav ...]
    python build\\analyze-voice.py --compare <处理前.wav> <处理后.wav>

指标（都是客观量，不看频谱图也能比较）：
    时长 / 采样率 / 声道 / 峰值 dBFS / RMS dBFS / 直流偏移
    削波比例（贴顶样本占比）/ 静音帧比例
    语音带能量占比（300–3000 Hz 占总能量）
    噪声底估计（最安静 10% 帧的 RMS 中位数）
    谱质心（Hz，偏高说明高频成分多 = 更"亮"或噪声多）

判定只给"有没有语音成分"这种**能站得住**的结论，不给主观音质评分。
"""
import argparse
import os
import sys
import wave

import numpy as np

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SPEECH_LO, SPEECH_HI = 300.0, 3000.0


def load_wav(path):
    with wave.open(path, 'rb') as w:
        n, sr, ch, sw = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype='<i4').astype(np.float64) / 2147483648.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        raise SystemExit('不支持的位深: %s' % sw)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr, ch, sw


def db(x):
    return float('-inf') if x <= 0 else 20.0 * np.log10(x)


def analyze(path):
    data, sr, ch, sw = load_wav(path)
    if data.size == 0:
        return None
    dur = data.size / float(sr)
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data ** 2)))
    dc = float(np.mean(data))
    clip = float(np.mean(np.abs(data) >= 0.99))

    # 按 20ms 一帧算能量，用于静音比例与噪声底
    fl = max(1, int(sr * 0.02))
    frames = data[: data.size // fl * fl].reshape(-1, fl)
    frms = np.sqrt(np.mean(frames ** 2, axis=1)) if frames.size else np.array([0.0])
    silence = float(np.mean(frms < 10 ** (-50 / 20)))
    noise_floor = float(np.median(np.sort(frms)[: max(1, frms.size // 10)]))

    # 频谱（整段平均），用于语音带占比与谱质心
    n = min(data.size, sr * 10)          # 最多取 10 秒，够用且快
    seg = data[:n] * np.hanning(n)
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    total = float(np.sum(spec ** 2)) or 1.0
    band = float(np.sum(spec[(freqs >= SPEECH_LO) & (freqs <= SPEECH_HI)] ** 2))
    centroid = float(np.sum(freqs * spec) / (np.sum(spec) or 1.0))

    return {
        'file': os.path.basename(path), 'seconds': round(dur, 2), 'rate': sr, 'ch': ch,
        'peak_dbfs': round(db(peak), 1), 'rms_dbfs': round(db(rms), 1),
        'dc': round(dc, 5), 'clip_pct': round(clip * 100, 3),
        'silence_pct': round(silence * 100, 1),
        'speech_band_pct': round(band / total * 100, 1),
        'noise_floor_dbfs': round(db(noise_floor), 1),
        'centroid_hz': round(centroid, 0),
    }


def verdict(m):
    """只判定"有没有语音成分"，不给主观评分。"""
    # 先看总能量：能量接近于零时，"语音带占比"没有意义
    # （实测：一条 RMS -82.7 dBFS 的纯静音录音，语音带占比却是 90.8%，
    #  因为分母几乎为零，比值不可用）—— 这种情况必须先判成静音，别让数字误导。
    if m['rms_dbfs'] < -70:
        return '基本静音（这段里没有人说话）'
    if m['rms_dbfs'] < -55 and m['silence_pct'] > 60:
        return '基本静音（这段里没有人说话）'
    if m['silence_pct'] > 70:
        return '大部分是静音，只有少量声音'
    if m['speech_band_pct'] >= 60:
        return '有语音成分（能量主要在 300–3000 Hz）'
    return '有声音，但能量不在语音带（可能是噪声或音乐）'


def show(m):
    print('  %s' % m['file'])
    print('    %(seconds)s s / %(rate)s Hz / %(ch)s ch' % m)
    print('    峰值 %(peak_dbfs)s dBFS    RMS %(rms_dbfs)s dBFS    直流 %(dc)s' % m)
    print('    削波 %(clip_pct)s%%    静音帧 %(silence_pct)s%%    噪声底 %(noise_floor_dbfs)s dBFS' % m)
    print('    语音带(300–3000Hz)能量占比 %(speech_band_pct)s%%    谱质心 %(centroid_hz)s Hz' % m)
    print('    判定：%s' % verdict(m))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--compare', action='store_true', help='两个文件：前=处理前，后=处理后')
    a = ap.parse_args()

    metrics = []
    for f in a.files:
        if not os.path.exists(f):
            print('  找不到 %s' % f)
            continue
        m = analyze(f)
        if m:
            metrics.append(m)
            show(m)

    if a.compare and len(metrics) >= 2:
        x, y = metrics[0], metrics[-1]
        print('\n  == 对比（%s → %s）==' % (x['file'], y['file']))
        print('    RMS 变化: %+.1f dB' % (y['rms_dbfs'] - x['rms_dbfs']))
        print('    噪声底变化: %+.1f dB' % (y['noise_floor_dbfs'] - x['noise_floor_dbfs']))
        print('    语音带占比变化: %+.1f 个百分点' % (y['speech_band_pct'] - x['speech_band_pct']))
        print('    谱质心变化: %+.0f Hz' % (y['centroid_hz'] - x['centroid_hz']))
        print('    提醒：**没有真人说话时**，这些数字只反映"底噪被怎么处理了"，不能当音质结论。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
